# Backfill `coverage_start` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let backfill run on integrations that already have pull cursors by recording a per-individual `coverage_start` floor and filling `[start, coverage_start)` instead of blanket-skipping cursored individuals.

**Architecture:** Add `coverage_start` to the pull cursor (`IndividualState`). The steady-state pull stamps it on first-run cursor creation (`= default_start`); backfill's seed stamps it (`= end`) and finalize stamps it (`= start`). `action_backfill` resolves each individual's backfill `end` from `coverage_start` (with legacy fallbacks) rather than skipping, so aborted-seed cursors no longer poison future runs and real cursors can be filled behind.

**Tech Stack:** Python 3.10, pydantic v1, redis.asyncio, pytest + pytest-asyncio + pytest-mock. Tests run in the Docker `mb-runner-test` image.

## Global Constraints

- Python 3.10, pydantic v1 syntax (`Optional`, `parse_obj`, `.json()`, `.dict()`).
- Branch: `movebank-backfill` (extends PR #14). Commit on top; do not create a new branch.
- Test command: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q`. Focused: append the test path. Never run tests outside Docker (local venv is broken).
- `coverage_start` is the oldest timestamp an individual's steady-state coverage begins at. Set once; never advanced forward by the pull. Immutable for the cursor's lifetime except backfill finalize (sets it to the backfill `start`).
- Backfill fills `[start, coverage_start)`; the pull owns `[coverage_start-ish, ∞)`. They must not overlap (the transform's `loaded_at` defeats Gundi dedup → overlap = duplicate observations).
- Legacy cursor rules: `coverage_start` present → use it; absent AND all sensors `highest_event_id == 0` (seed placeholder) → `end = min(sensor latest_timestamp)`; absent AND some `highest_event_id > 0` (real coverage) → skip with warning.
- `maximum_lookback_hours ≥ ACCESSORY_SETTLING_HOURS` is the documented expectation; when a cursor's span is below the settling margin, log a warning (no per-sensor clamp — YAGNI).
- `CURSOR_STATE_ACTION_ID = "pull_events_for_individual"`; `settings.ACCESSORY_SETTLING_HOURS` default 12; `maximum_lookback_hours` default 24.

## File Structure

- `app/actions/client.py` — `IndividualState` gains `coverage_start`.
- `app/actions/handlers.py` — pull first-run stamp; `_seed_pull_cursor_at_end` stamp; `_finalize_backfill_individual` stamp; `action_backfill` `end`-resolution replacing the skip filter.
- `app/actions/tests/test_models.py`, `test_handlers.py` — coverage.

---

### Task 1: Add `coverage_start` to `IndividualState`

**Files:**
- Modify: `app/actions/client.py` (`IndividualState`)
- Test: `app/actions/tests/test_models.py` (append)

**Interfaces:**
- Produces: `IndividualState.coverage_start: Optional[datetime] = None`, round-trips through `.dict()`/`.parse_obj()`/`.json()`.

- [ ] **Step 1: Write the failing test**

Append to `app/actions/tests/test_models.py`:

```python
def test_individual_state_coverage_start_roundtrips():
    from datetime import datetime, timezone
    state = IndividualState(individual_id="111", study_id="12345")
    assert state.coverage_start is None
    state.coverage_start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    restored = IndividualState.parse_obj(state.dict())
    assert restored.coverage_start == datetime(2024, 1, 1, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_models.py -k coverage_start -q`
Expected: FAIL — `AttributeError`/validation: `coverage_start` not a field.

- [ ] **Step 3: Implement**

In `app/actions/client.py`, add the field to `IndividualState` (after `sensor_states`):

```python
class IndividualState(pydantic.BaseModel):
    individual_id: str
    study_id: str
    local_identifier: Optional[str] = None
    # Per-sensor-type cursors (key = sensor_type_id as string, because JSON keys are strings)
    sensor_states: Dict[str, SensorState] = pydantic.Field(default_factory=dict)
    # Oldest timestamp this individual's steady-state coverage begins at. Set once
    # (pull first run, or backfill seed/finalize); never advanced forward. Backfill
    # fills [start, coverage_start) for an already-cursored individual.
    coverage_start: Optional[datetime] = None
```

(`datetime` and `Optional` are already imported in this module.)

- [ ] **Step 4: Run test to verify it passes**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_models.py -k coverage_start -q`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/client.py app/actions/tests/test_models.py
git commit -m "feat: add coverage_start to IndividualState"
```

---

### Task 2: Pull stamps `coverage_start` on first-run cursor creation

**Files:**
- Modify: `app/actions/handlers.py` (`action_pull_events_for_individual`)
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `IndividualState.coverage_start` (Task 1).
- Produces: after the pull's first run for a fresh individual, the persisted cursor has `coverage_start == default_start` (`resolved_end − maximum_lookback_hours`). A subsequent run with a saved cursor does not change it.

- [ ] **Step 1: Write the failing test**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_pull_first_run_stamps_coverage_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Fresh individual (no saved cursor): after the first run that persists a
    # cursor, coverage_start must equal default_start = timestamp_end - lookback.
    events = [_gps_event(100, "2026-06-30 10:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(
            maximum_lookback_hours=24,
            individual_overrides={"timestamp_end": "2026-07-01 00:00:00.000"},
        ),
    )

    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    state = IndividualState.parse_obj(saved)
    assert state.coverage_start == datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc)  # end - 24h


@pytest.mark.asyncio
async def test_pull_second_run_preserves_coverage_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A saved cursor with an existing coverage_start must not be overwritten.
    prior = IndividualState(individual_id="111", study_id="12345")
    prior.update_sensor_state(653, datetime(2026, 6, 1, tzinfo=timezone.utc), 50)
    prior.coverage_start = datetime(2020, 1, 1, tzinfo=timezone.utc)  # far-back floor
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = prior.dict()
    events = [_gps_event(100, "2026-06-30 10:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    assert IndividualState.parse_obj(saved).coverage_start == datetime(2020, 1, 1, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "stamps_coverage_start or preserves_coverage_start" -q`
Expected: FAIL — `test_pull_first_run_stamps_coverage_start` asserts a `coverage_start` that is currently `None`.

- [ ] **Step 3: Implement**

In `action_pull_events_for_individual`, capture freshness and stamp after `default_start` is computed. Change the state-load block:

```python
    saved_state = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    try:
        individual_state = IndividualState.parse_obj(saved_state) if saved_state else None
    except pydantic.ValidationError:
        logger.exception(f"Failed parsing saved state for {log_reference}; starting fresh.")
        individual_state = None
    was_fresh = individual_state is None
    if individual_state is None:
        individual_state = IndividualState(
            individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
        )

    # Build per-sensor cursors from state; new sensors start at the lookback window.
    default_start = resolved_individual_timestamp_end - timedelta(hours=action_config.maximum_lookback_hours)
    if was_fresh:
        # First-ever cursor for this individual: record the oldest point the pull
        # will cover, so a later backfill knows where to stop (fills [start, this)).
        individual_state.coverage_start = default_start
```

(Only stamp when `was_fresh` — a loaded cursor without `coverage_start` is a legacy cursor whose real floor is unknown; leave it `None` so backfill's legacy logic handles it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "stamps_coverage_start or preserves_coverage_start" -q`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: pull stamps coverage_start on first-run cursor creation"
```

---

### Task 3: Backfill seed and finalize stamp `coverage_start`

**Files:**
- Modify: `app/actions/handlers.py` (`_seed_pull_cursor_at_end`, `_finalize_backfill_individual`)
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `IndividualState.coverage_start` (Task 1).
- Produces: `_seed_pull_cursor_at_end` writes a cursor with `coverage_start = end`; `_finalize_backfill_individual` sets the merged cursor's `coverage_start = action_config.start`.

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_seed_pull_cursor_stamps_coverage_start(mocker, integration, mock_state_store):
    from app.actions.handlers import _seed_pull_cursor_at_end
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    await _seed_pull_cursor_at_end(str(integration.id), "12345", _make_individual(), end)
    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    assert IndividualState.parse_obj(saved).coverage_start == end


@pytest.mark.asyncio
async def test_finalize_sets_coverage_start_to_backfill_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    events = [_gps_event(10, "2024-01-02 00:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 1,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    assert IndividualState.parse_obj(saved).coverage_start == start
```

Add this helper near the top of `test_handlers.py` (after `_gps_event`), if not already present:

```python
def _make_individual(**overrides):
    from app.actions.client import Individual
    return Individual.parse_obj({**INDIVIDUAL_ROW, **overrides})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "seed_pull_cursor_stamps or finalize_sets_coverage_start" -q`
Expected: FAIL — `coverage_start` is `None` in both saved cursors.

- [ ] **Step 3: Implement**

In `_seed_pull_cursor_at_end`, set `coverage_start` on the seeded state:

```python
async def _seed_pull_cursor_at_end(integration_id: str, study_id: str, ind, end: datetime) -> None:
    """Claim [end, +inf) for the steady-state pull cursor before backfill starts.

    Without this, an individual with no existing pull cursor lets the */10
    pull compute its own lookback-based start and reach back into the range
    backfill is about to cover — shipping duplicate observations, since
    loaded_at doesn't dedup them.
    """
    state = IndividualState(individual_id=ind.id, study_id=study_id, local_identifier=ind.local_identifier)
    state.coverage_start = end  # pull owns [end, +inf); everything before is backfill's to fill
    for stid in _supported_sensor_type_ids(ind):
        state.update_sensor_state(stid, end, 0)
    await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID, json.loads(state.json()), source_id=ind.id)
```

In `_finalize_backfill_individual`, inside the `if state is not None:` block, set `coverage_start = action_config.start` on the merged `existing_state` before writing it. Change the merge write:

```python
        for stid_str, backfill_ss in state.sensor_states.items():
            stid = int(stid_str)
            existing_ss = existing_state.get_sensor_state(stid)
            candidate_stamps = [t for t in (existing_ss.latest_timestamp, backfill_ss.latest_timestamp) if t]
            if candidate_stamps:
                new_ts = max(candidate_stamps)
                new_event_id = max(existing_ss.highest_event_id or 0, backfill_ss.highest_event_id or 0)
                existing_state.update_sensor_state(stid, new_ts, new_event_id)
        # Backfill has now covered down to action_config.start, so the combined
        # coverage floor moves there — a later backfill with the same/later start
        # will see an empty range for this individual.
        existing_state.coverage_start = action_config.start
        await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID,
                                      json.loads(existing_state.json()), source_id=ind.id)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "seed_pull_cursor_stamps or finalize_sets_coverage_start" -q`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: backfill seed/finalize stamp coverage_start"
```

---

### Task 4: `action_backfill` resolves `end` from `coverage_start` (replaces skip filter)

**Files:**
- Modify: `app/actions/handlers.py` (`action_backfill`)
- Test: `app/actions/tests/test_handlers.py` (append + update the existing skip test)

**Interfaces:**
- Consumes: `IndividualState.coverage_start` (Task 1), `_seed_pull_cursor_at_end`, `_resolve_start`, `settings.ACCESSORY_SETTLING_HOURS`.
- Produces: `action_backfill` no longer blanket-skips cursored individuals. Per individual: no cursor → `end = now` + seed; `coverage_start` present → `end = coverage_start`; legacy seed placeholder (all `event_id == 0`) → `end = min(latest_timestamp)`; legacy real cursor → skip. Queues `[start, end)` where `start < end`. Return dict keys unchanged: `job_id`, `individuals`, `dispatched`, `skipped_existing` (now = legacy-unknown-floor skips).

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_fills_behind_existing_coverage_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # An individual WITH a real cursor (coverage_start recorded) must be queued,
    # with end == coverage_start (not skipped).
    from app.actions.configurations import BackfillConfig
    cov = datetime(2026, 1, 1, tzinfo=timezone.utc)
    st = IndividualState(individual_id="111", study_id="12345")
    st.update_sensor_state(653, datetime(2026, 6, 1, tzinfo=timezone.utc), 500)
    st.coverage_start = cov
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    seeded = {}
    async def fake_seed(self, ids, *, total, range_repr): seeded["ids"] = list(ids)
    async def fake_put(self, iid, blob): seeded.setdefault("configs", {})[iid] = blob
    async def fake_next(self): return (seeded.get("ids") or []).pop(0) if seeded.get("ids") else None
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", fake_put)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config",
                 AsyncMock(side_effect=lambda iid: seeded["configs"][iid]))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 1          # queued, not skipped
    cfg = BackfillEventsForIndividualConfig.parse_raw(seeded["configs"]["111"])
    assert cfg.end == cov                       # end == coverage_start


@pytest.mark.asyncio
async def test_backfill_legacy_seed_placeholder_full_backfill(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Legacy cursor: all sensors event_id == 0, no coverage_start -> treat its
    # timestamp as the floor (full backfill), do NOT skip.
    seed_ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    st = IndividualState(individual_id="111", study_id="12345")
    st.update_sensor_state(653, seed_ts, 0)     # event_id 0 => placeholder
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    seeded = {}
    async def fake_seed(self, ids, *, total, range_repr): seeded["ids"] = list(ids)
    async def fake_put(self, iid, blob): seeded.setdefault("configs", {})[iid] = blob
    async def fake_next(self): return (seeded.get("ids") or []).pop(0) if seeded.get("ids") else None
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", fake_put)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config",
                 AsyncMock(side_effect=lambda iid: seeded["configs"][iid]))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 1
    cfg = BackfillEventsForIndividualConfig.parse_raw(seeded["configs"]["111"])
    assert cfg.end == seed_ts


@pytest.mark.asyncio
async def test_backfill_skips_legacy_real_cursor_unknown_floor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Legacy real coverage (event_id > 0) with no coverage_start -> skip with warning.
    st = IndividualState(individual_id="111", study_id="12345")
    st.update_sensor_state(653, datetime(2026, 6, 1, tzinfo=timezone.utc), 500)  # real events, no coverage_start
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 0
    assert result["skipped_existing"] == 1
    assert not mock_trigger.called
```

(Ensure `from app.actions.configurations import BackfillEventsForIndividualConfig` is imported at the top of `test_handlers.py`; add it if missing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "fills_behind or legacy_seed_placeholder or skips_legacy_real" -q`
Expected: FAIL — the first two skip (current code skips ALL cursored individuals → `individuals == 0`).

- [ ] **Step 3: Implement**

In `action_backfill`, replace the skip-filter block AND the `ranges`/seed loop. Replace this current block:

```python
    # Backfill is for INITIAL load only: an individual that already has a
    # steady-state pull cursor is skipped entirely (not queued) — filling
    # history behind an existing cursor isn't supported yet, and attempting it
    # would duplicate data the pull has already collected.
    candidates = []
    skipped_existing = []
    for i in individuals:
        saved = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=i.id)
        if saved:
            skipped_existing.append(i.id)
        else:
            candidates.append(i)
    if skipped_existing:
        logger.warning(
            f"Backfill {job_id}: skipped {len(skipped_existing)} individuals that already have "
            "collection history (backfill is for initial load; filling history behind an "
            "existing cursor is not yet supported)"
        )
    individuals = candidates

    # Every candidate reaching here has NO existing pull cursor (the filter
    # above skipped those that did), so each individual's boundary is simply
    # `now`: backfill covers [start, now) and the pull will cover [now, +inf)
    # once its cursor is claimed below.
    ranges = {i.id: (_resolve_start(action_config.start, i), now) for i in individuals}
```

with:

```python
    # Resolve each individual's backfill end from its steady-state coverage
    # floor. Backfill fills [start, end); the pull owns [end, +inf).
    #   - no cursor            -> end = now, and seed the pull cursor at now
    #   - coverage_start set   -> end = coverage_start (fill history behind the pull)
    #   - legacy seed (all event_id 0, no coverage_start) -> end = its timestamp
    #   - legacy real cursor (event_id > 0, no coverage_start) -> skip (unknown floor)
    ranges = {}
    seed_needed = set()
    skipped_existing = []
    for i in individuals:
        saved = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=i.id)
        if not saved:
            end_dt = now
            seed_needed.add(i.id)
        else:
            state = IndividualState.parse_obj(saved)
            if state.coverage_start is not None:
                end_dt = state.coverage_start
            elif all((ss.highest_event_id or 0) == 0 for ss in state.sensor_states.values()):
                stamps = [ss.latest_timestamp for ss in state.sensor_states.values() if ss.latest_timestamp]
                end_dt = min(stamps) if stamps else now
            else:
                skipped_existing.append(i.id)
                continue
            latest = max((ss.latest_timestamp for ss in state.sensor_states.values() if ss.latest_timestamp), default=None)
            if latest and end_dt > latest - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS):
                logger.warning(
                    f"Backfill {job_id}/{i.id}: coverage span is below the accessory settling "
                    "margin; accessory rows near the boundary may duplicate."
                )
        ranges[i.id] = (_resolve_start(action_config.start, i), end_dt)
    if skipped_existing:
        logger.warning(
            f"Backfill {job_id}: skipped {len(skipped_existing)} individuals with existing collection "
            "history but no recorded coverage floor; clear their cursor to force a full backfill."
        )
```

Then update the queued/seed loop so only `seed_needed` individuals are seeded. Replace:

```python
    for i in queued:
        start_dt, end_dt = ranges[i.id]
        # No prior cursor (guaranteed by the skip filter) — claim the boundary
        # so a concurrent */10 pull can't reach back into backfill's range.
        await _seed_pull_cursor_at_end(integration_id, action_config.study_id, i, end_dt)
        await job.put_individual_config(
```

with:

```python
    for i in queued:
        start_dt, end_dt = ranges[i.id]
        # Only fresh individuals need a boundary-claim seed; an already-cursored
        # individual's cursor already owns [coverage_start, +inf).
        if i.id in seed_needed:
            await _seed_pull_cursor_at_end(integration_id, action_config.study_id, i, end_dt)
        await job.put_individual_config(
```

The `queued = [i for i in individuals if ranges[i.id][0] < ranges[i.id][1]]` line must change to guard against individuals dropped by `continue` (not in `ranges`):

```python
    queued = [i for i in individuals if i.id in ranges and ranges[i.id][0] < ranges[i.id][1]]
```

(`timedelta` is already imported in `handlers.py`.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "fills_behind or legacy_seed_placeholder or skips_legacy_real" -q`
Expected: PASS.

- [ ] **Step 5: Reconcile the existing skip test**

The old `test_backfill_respects_individual_filter` / any test asserting all-cursored-individuals are skipped may now behave differently. Run the full backfill test set and fix any that encoded the old blanket-skip:

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k backfill -q`
Expected: PASS. If a pre-existing test fails because it assumed cursored individuals are skipped, update it to the new behavior (queued with `end = coverage_start`) and note the change in the report.

- [ ] **Step 6: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: backfill fills [start, coverage_start) instead of skipping cursored individuals"
```

---

### Task 5: Verify, docs, push

**Files:**
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-22-backfill-coverage-start-design.md` (status)

**Interfaces:** none — verification and delivery.

- [ ] **Step 1: Clean-build full suite**

```bash
docker build -t mb-runner-test --target devimage -f docker/Dockerfile .
docker run --rm mb-runner-test python -m pytest app -q
```
Expected: PASS (no volume mount — verifies the committed tree).

- [ ] **Step 2: Discovery check**

```bash
docker run --rm mb-runner-test python -c "
from app.actions import action_handlers
print('actions:', sorted(action_handlers.keys()))
from app.actions.client import IndividualState
print('has coverage_start:', 'coverage_start' in IndividualState.__fields__)
"
```
Expected: the five actions listed, `has coverage_start: True`.

- [ ] **Step 3: Update README**

In `README.md`, update the backfill bullet to note it now works on already-collecting integrations:

```markdown
- **backfill** (executable) — operator-triggered historical load for a study.
  Works on a fresh integration and on one that already has collection history:
  each individual is back-filled up to its recorded coverage floor
  (`coverage_start`), so backfill and the steady-state pull partition the
  timeline with no gap or overlap. Config: `study_id`, optional `individual_ids`,
  `start` (a date or `"all"`), optional `backfill_max_concurrency`.
```

- [ ] **Step 4: Update spec status + commit + push**

Set the spec's status line to `implemented (PR #14)`:

```bash
sed -i '' 's/^\*\*Status:\*\* approved design, pre-implementation/**Status:** implemented (PR #14)/' docs/superpowers/specs/2026-07-22-backfill-coverage-start-design.md
git add README.md docs/superpowers/specs/2026-07-22-backfill-coverage-start-design.md
git commit -m "docs: document backfill coverage_start behavior"
git push
```

- [ ] **Step 5: Confirm CI green**

Watch the `movebank-backfill` CI run for the latest commit; expected: success.

---

## Notes for the implementer

- Tasks 2 and 4 both touch `action_pull_events_for_individual` / `action_backfill` in `handlers.py`; apply in order and re-run the full suite each task, since the pull-first-run stamp (Task 2) is what makes Task 4's `coverage_start`-present path reachable in an end-to-end run.
- The existing `mock_state_store` fixture already supports `get_state`/`set_state`/`delete_state`; the new tests reuse it. `make_events_generator`, `_gps_event`, `_sub_action_config`, `INDIVIDUAL_ROW` are all already defined in `test_handlers.py`.
- Do NOT add a per-sensor accessory-`end` clamp — the spec deliberately handles `lookback < settling` with a warning only (YAGNI).
