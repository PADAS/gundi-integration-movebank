# Backfill Step Budget & Wedge Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the backfill cascade sub-action from timing out on high-frequency individuals (and permanently wedging the job), by bounding the work per step, surfacing any hard-timeout losslessly, and giving operators a `restart` button.

**Architecture:** In `action_backfill_events_for_individual`, keep the window atomic but cap the events sent per window — a fully-fetched over-cap window is discarded, the window shrunk proportionally and persisted (`window_seconds`), then retried; so a step always finishes inside the 432s budget and cleanly cascades. Add an `asyncio.CancelledError` backstop that logs + re-raises (scan floor is already persisted → no loss). Add a `restart` flag on `BackfillConfig` that clears a job's Redis state + watermarks and re-seeds from `start`.

**Tech Stack:** Python 3.10, pydantic v1, redis.asyncio, pytest + pytest-asyncio + pytest-mock + respx. Tests run in the Docker `mb-runner-test` image.

## Global Constraints

- Python 3.10, pydantic v1 syntax (`Field`, `validator`, `parse_obj`, `.json()`, `.dict()`).
- Branch: `movebank-backfill-step-budget` (off `main`, post-PR #16). Commit on top; do not create another branch.
- Test command: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q`. Focused: append the test path / `-k`. Never run tests outside Docker (local venv is broken).
- **The window is the atomic unit.** A window is either sent in full (watermarks advance over exactly what was sent) or discarded in full (nothing sent, watermarks and `scan_from` untouched). Never a partial send — `get_individual_events_by_time` returns rows unordered by `event_id` and `loaded_at` defeats Gundi dedup, so a partial send would gap or duplicate.
- **No PubSub redelivery** (`POST /` always acks): a killed step is never redelivered; recovery is either the step's own `trigger_action` cascade or the operator `restart` flag.
- Existing constants: `MAX_RECORDS_PER_BACKFILL_STEP = 10000` (per-step cascade cap, unchanged); `BACKFILL_WATERMARK_ACTION_ID = "backfill_watermark"`; `MAX_ACTION_EXECUTION_TIME = 540`; deadline factor `0.8`.
- New constant defaults: `MAX_RECORDS_PER_BACKFILL_WINDOW = 5000`, `MIN_BACKFILL_WINDOW_SECONDS = 300`, `BACKFILL_WINDOW_SHRINK_SAFETY = 0.8`.
- `restart` semantics = **full restart from `start`** (clear job + job watermarks + progress key, then re-seed). Does not touch steady-state pull cursors (the merged `coverage_start` logic handles those on re-seed).

## File Structure

- `app/settings/integration.py` — three new budget constants.
- `app/actions/handlers.py` — `import asyncio`; `action_backfill_events_for_individual` (persisted `window_seconds`, discard/shrink loop, `CancelledError` backstop); `action_backfill` (`restart` handling).
- `app/actions/configurations.py` — `BackfillConfig.restart`.
- `app/actions/backfill_queue.py` — `BackfillJob.clear()`.
- `app/actions/tests/test_handlers.py` — coverage for all three parts.

---

### Task 1: Part A — bound per-step work (per-window cap, discard + proportional shrink, persisted window)

**Files:**
- Modify: `app/settings/integration.py` (append constants)
- Modify: `app/actions/handlers.py` (`action_backfill_events_for_individual`: window init + discard/shrink loop + persist `window_seconds`)
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `settings.MAX_RECORDS_PER_BACKFILL_WINDOW`, `settings.MIN_BACKFILL_WINDOW_SECONDS`, `settings.BACKFILL_WINDOW_SHRINK_SAFETY`; the backfill-watermark blob (already carries `scan_from`).
- Produces: the watermark blob now also carries `window_seconds`; an over-cap fully-fetched window is discarded (not sent) and the window shrinks; on the next step the persisted `window_seconds` is honoured.

- [ ] **Step 1: Add settings constants**

Append to `app/settings/integration.py`:

```python
# Backfill step budgeting. A cascade step sends one window at a time and never a
# window larger than this many events, bounding the send loop so a step always
# finishes inside MAX_ACTION_EXECUTION_TIME. A fully-fetched window over the cap
# is discarded and the window shrunk (see below), then retried.
MAX_RECORDS_PER_BACKFILL_WINDOW = env.int("MAX_RECORDS_PER_BACKFILL_WINDOW", 5000)
# Floor for the adaptive backfill window (seconds): it shrinks on overflow but
# never below this. At the floor an over-cap window is sent anyway (a burst
# denser than the floor window can hold).
MIN_BACKFILL_WINDOW_SECONDS = env.int("MIN_BACKFILL_WINDOW_SECONDS", 300)
# Safety factor when proportionally shrinking the window on overflow, so the
# resized window aims comfortably below the cap rather than exactly at it.
BACKFILL_WINDOW_SHRINK_SAFETY = env.float("BACKFILL_WINDOW_SHRINK_SAFETY", 0.8)
```

- [ ] **Step 2: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`. These mirror the existing `test_backfill_individual_density_window_applies` / `..._stops_at_per_step_record_backstop` patterns (same fixtures and the `make_counting_events_generator` helper, which yields a fixed number of events **per fetch call regardless of the query range** and exposes `calls["count"]` = number of fetches; `BackfillEventsForIndividualConfig`, `INDIVIDUAL_ROW`, `IndividualState` are already imported).

Constants used below (verify against `handlers.py`): `DEFAULT_BATCH_WINDOW = timedelta(days=5)`; a low-frequency individual (`INDIVIDUAL_ROW.number_of_events == "100"`) gets the 5-day default window. With `MAX_RECORDS_PER_BACKFILL_WINDOW = 2`, `BACKFILL_WINDOW_SHRINK_SAFETY = 0.8`, a 3-event fetch shrinks a 5-day (432000s) window to `432000 * 2/3 * 0.8 = 230400s`, so a `MIN_BACKFILL_WINDOW_SECONDS = 259200` (3-day) floor is reached in exactly one shrink.

```python
@pytest.mark.asyncio
async def test_backfill_window_over_cap_is_discarded_then_sent_after_shrink(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A fully-fetched over-cap window is DISCARDED (not sent); the window shrinks
    # and persists, and only a later (floor-sized) window is actually sent. With
    # the fixed-count generator every fetch returns 3 (> cap 2), so the window
    # shrinks to the floor in one step, then the floor window is sent anyway.
    mocker.patch("app.actions.handlers.settings.MAX_RECORDS_PER_BACKFILL_WINDOW", 2)
    mocker.patch("app.actions.handlers.settings.MIN_BACKFILL_WINDOW_SECONDS", 259200)  # 3 days
    mocker.patch("app.actions.handlers.settings.BACKFILL_WINDOW_SHRINK_SAFETY", 0.8)
    gen, calls = make_counting_events_generator(3)  # 3 > cap 2 on every fetch
    mock_movebank_client.get_individual_events_by_time = gen
    send = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-x",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 2, tzinfo=timezone.utc),  # 1-day range
        ),
    )

    # Two fetches (one discarded at 5d, one sent at the 3d floor) but only ONE
    # send — proving the over-cap fetch was discarded, not sent.
    assert calls["count"] == 2
    assert send.call_count == 1
    saved = mock_state_store[(str(integration.id), "backfill_watermark", "job-x.111")]
    assert float(saved["window_seconds"]) == 259200            # shrunk to the floor


@pytest.mark.asyncio
async def test_backfill_honours_persisted_window_seconds(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A persisted window_seconds from a prior step is used instead of the density
    # estimate: the sub-action queries with end_at = current + persisted window.
    st = IndividualState(individual_id="111", study_id="12345")
    mock_state_store[(str(integration.id), "backfill_watermark", "job-x.111")] = {
        **st.dict(), "scan_from": "2024-01-01T00:00:00+00:00", "window_seconds": 60.0,
    }
    ends = []
    def gen(**kwargs):
        ends.append(kwargs.get("timestamp_end"))
        async def _agen():
            for _ in ():
                yield None
        return _agen()
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-x",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),  # 2-minute range
        ),
    )

    # First window ends 60s after the persisted scan_from, not 5 days later.
    assert ends[0] == datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "over_cap_is_discarded or honours_persisted_window" -q`
Expected: FAIL — `window_seconds` is never written; the persisted window is ignored (first window spans to a computed density window, not +60s).

- [ ] **Step 4: Implement**

In `app/actions/handlers.py`, `action_backfill_events_for_individual`.

(4a) After the window is computed (currently `window = _compute_batch_window(...)` at ~690), honour a persisted `window_seconds`:

```python
    window = _compute_batch_window(ind.number_of_events, span_seconds)
    if saved and saved.get("window_seconds"):
        try:
            window = timedelta(seconds=float(saved["window_seconds"]))
        except (TypeError, ValueError):
            pass
    min_window = timedelta(seconds=settings.MIN_BACKFILL_WINDOW_SECONDS)
```

(4b) Inside the `while` loop, AFTER the per-sensor fetch loop and BEFORE `device_name = _display_name(ind)`, insert the overflow-discard guard:

```python
                # Overflow guard: only SEND a window we can finish within the
                # budget. A FULLY-fetched window (not deadline-interrupted) with
                # more than the per-window cap is discarded — nothing sent,
                # watermarks and scan floor untouched — and the window is shrunk
                # proportionally, persisted, and retried at the same `current`.
                # Never shrink below the floor; at the floor, fall through and
                # send even if over cap (a burst denser than the floor holds).
                if (not window_interrupted
                        and len(events) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW
                        and window > min_window):
                    shrunk = (window.total_seconds()
                              * settings.MAX_RECORDS_PER_BACKFILL_WINDOW / len(events)
                              * settings.BACKFILL_WINDOW_SHRINK_SAFETY)
                    window = max(min_window, timedelta(seconds=shrunk))
                    blob = json.loads(state.json())
                    blob["scan_from"] = current.isoformat()           # unchanged
                    blob["window_seconds"] = window.total_seconds()
                    await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                                  blob, source_id=watermark_source)
                    logger.info(
                        f"Backfill {log_reference}: window over cap "
                        f"({len(events)} > {settings.MAX_RECORDS_PER_BACKFILL_WINDOW}); "
                        f"shrunk to {window.total_seconds():.0f}s, retrying"
                    )
                    continue
                if (not window_interrupted
                        and len(events) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW):
                    logger.warning(
                        f"Backfill {log_reference}: window at floor "
                        f"({settings.MIN_BACKFILL_WINDOW_SECONDS}s) still over cap "
                        f"({len(events)} events); sending anyway"
                    )
```

(4c) In the normal per-window persist block (currently sets only `scan_from`), also persist `window_seconds` so convergence survives across steps:

```python
                blob = json.loads(state.json())
                blob["scan_from"] = current.isoformat()
                blob["window_seconds"] = window.total_seconds()
                await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                              blob, source_id=watermark_source)
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "over_cap_is_discarded or honours_persisted_window" -q`
Expected: PASS.

- [ ] **Step 6: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/settings/integration.py app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "fix: bound backfill per-window volume with discard-and-shrink"
```

---

### Task 2: Part B — CancelledError backstop

**Files:**
- Modify: `app/actions/handlers.py` (`import asyncio`; add `except asyncio.CancelledError` to `action_backfill_events_for_individual`)
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: nothing new.
- Produces: on cancellation the sub-action logs a clear line and re-raises `asyncio.CancelledError`; the persisted `scan_from` reflects the last completed window (no loss, no double-send).

- [ ] **Step 1: Write the failing test**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_cancelled_step_reraises_and_preserves_scan_from(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A hard cancellation mid-send must propagate (CancelledError is BaseException,
    # not caught by the retry/backoff handlers) and must not have advanced the
    # persisted scan_from past what was durably completed.
    gen, _calls = make_counting_events_generator(1)  # 1 event/fetch, under the cap
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi",
                 AsyncMock(side_effect=asyncio.CancelledError()))

    with pytest.raises(asyncio.CancelledError):
        await action_backfill_events_for_individual(
            integration=integration,
            action_config=BackfillEventsForIndividualConfig(
                study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-x",
                start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                end=datetime(2024, 1, 2, tzinfo=timezone.utc),
            ),
        )

    # The send was cancelled before the per-window persist, so no watermark blob
    # was written for this individual (scan_from never advanced).
    assert (str(integration.id), "backfill_watermark", "job-x.111") not in mock_state_store
```

(Add `import asyncio` at the top of `test_handlers.py` if it is not already imported.)

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k cancelled_step_reraises -q`
Expected: FAIL — without `import asyncio` in handlers the module referencing `asyncio.CancelledError` errors, or (if only the test lacks the import) an Import/NameError; the point is the handler doesn't yet name the exception.

- [ ] **Step 3: Implement**

In `app/actions/handlers.py`, add `import asyncio` to the imports (after `import hashlib`). Then add a `CancelledError` handler as the FIRST `except` on the fetch/send `try` in `action_backfill_events_for_individual` (before `except NoConnectionSlot`):

```python
    except asyncio.CancelledError:
        # Hard execution-timeout: asyncio.wait_for in the action runner cancels
        # this task, raising CancelledError (a BaseException — the handlers below
        # do NOT catch it). scan_from + window_seconds are persisted every
        # window, so no data is lost; a later trigger resumes from the last
        # completed window. We do not attempt async recovery here (re-trigger /
        # in_flight unwind): awaits during cancellation are themselves cancelled
        # and unreliable. Recover a job wedged by a timeout with restart=true.
        logger.warning(
            f"Backfill {log_reference}: step hard-cancelled at scan_from={current.isoformat()}; "
            "resume on next trigger or re-run with restart=true"
        )
        raise
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k cancelled_step_reraises -q`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "fix: backfill logs and re-raises on hard cancellation (loss-free)"
```

---

### Task 3: Part C — `restart` flag to recover a wedged job

**Files:**
- Modify: `app/actions/configurations.py` (`BackfillConfig.restart` + ui order)
- Modify: `app/actions/backfill_queue.py` (`BackfillJob.clear()`)
- Modify: `app/actions/handlers.py` (`action_backfill` restart handling)
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `BackfillJob.clear()`, `state_manager.delete_state`, `BACKFILL_WATERMARK_ACTION_ID`.
- Produces: `BackfillConfig.restart: bool = False`; when true, `action_backfill` clears the job's `meta`/`pending`/`configs`, deletes each candidate individual's `backfill_watermark` state (`source="{job_id}.{ind.id}"`) and the `backfill_progress.{job_id}` key, then seeds fresh. `restart=False` is unchanged behaviour (including `already_active`).

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_restart_clears_job_and_reseeds(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # With restart=True, an existing (wedged) job is cleared and re-seeded rather
    # than returning already_active.
    from app.actions.configurations import BackfillConfig
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    # Stateful exists/clear: the job exists until restart clears it. If restart
    # did NOT run, exists()==True would short-circuit to already_active.
    jobstate = {"exists": True, "cleared": False}
    async def fake_exists(self): return jobstate["exists"]
    async def fake_clear(self): jobstate["exists"] = False; jobstate["cleared"] = True
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", fake_exists)
    mocker.patch("app.actions.backfill_queue.BackfillJob.clear", fake_clear)
    seeded = {}
    async def fake_seed(self, ids, *, total, range_repr): seeded["ids"] = list(ids)
    async def fake_next(self): return (seeded.get("ids") or []).pop(0) if seeded.get("ids") else None
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config", AsyncMock(return_value="{}"))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    delete = mocker.patch("app.actions.handlers.state_manager.delete_state", AsyncMock())

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all", restart=True),
    )

    assert jobstate["cleared"] is True
    assert result.get("already_active") is not True
    # Watermark for the candidate individual was cleared (job_id is deterministic).
    assert any(call.kwargs.get("source_id", "").endswith(".111")
               or (len(call.args) >= 3 and str(call.args[2]).endswith(".111"))
               for call in delete.call_args_list)


@pytest.mark.asyncio
async def test_backfill_default_still_bails_when_job_active(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # restart defaults False: an existing active job still returns already_active.
    from app.actions.configurations import BackfillConfig
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 0, "observations_sent": 0,
                                          "in_flight": 1, "pending_remaining": 0, "range": "r"}))
    clear = mocker.patch("app.actions.backfill_queue.BackfillJob.clear", AsyncMock())

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result.get("already_active") is True
    assert not clear.called
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "restart_clears or default_still_bails" -q`
Expected: FAIL — `BackfillConfig` has no `restart` field (validation error) / `BackfillJob` has no `clear`.

- [ ] **Step 3: Implement the config field**

In `app/actions/configurations.py`, `BackfillConfig`, add the field after `backfill_max_concurrency` and include it in `ui_global_options.order`:

```python
    restart: bool = Field(
        False,
        title="Restart",
        description="Clear any existing job for these parameters and start over from the "
                    "beginning. Use to recover a stuck backfill.",
    )
    ui_global_options = GlobalUISchemaOptions(
        order=["study_id", "individual_ids", "start", "backfill_max_concurrency", "restart"],
    )
```

(Replace the existing `ui_global_options = GlobalUISchemaOptions(order=[...])` block — do not add a second one.)

- [ ] **Step 4: Implement `BackfillJob.clear()`**

In `app/actions/backfill_queue.py`, add to `BackfillJob` (e.g. after `exists`):

```python
    async def clear(self) -> None:
        """Delete every Redis key for this job (meta hash, pending queue, and the
        per-individual configs hash). Used by a restart to wipe a stuck or
        finished job before re-seeding."""
        await self.db.delete(self._meta, self._pending, f"{self._meta}.configs")
```

- [ ] **Step 5: Implement the restart handling in `action_backfill`**

In `app/actions/handlers.py`, `action_backfill`, immediately after `job = BackfillJob(integration_id, job_id)` (currently line ~441) and BEFORE the `if await job.exists():` block, insert:

```python
    if action_config.restart:
        # Operator recovery: wipe the deterministic job's state and its
        # per-individual backfill watermarks so it re-seeds and re-runs from
        # `start`, instead of returning already_active forever on a wedged job.
        await job.clear()
        for i in individuals:
            await state_manager.delete_state(
                integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=f"{job_id}.{i.id}"
            )
        await state_manager.delete_state(integration_id, f"backfill_progress.{job_id}")
        logger.warning(
            f"Backfill {job_id}: restart requested — cleared prior job state and watermarks"
        )
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "restart_clears or default_still_bails" -q`
Expected: PASS.

- [ ] **Step 7: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/configurations.py app/actions/backfill_queue.py app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: add backfill restart flag to recover a wedged job"
```

---

### Task 4: Verify, docs, deliver

**Files:**
- Modify: `README.md` (backfill section — note `restart` + step budgeting)
- Modify: `docs/superpowers/specs/2026-07-23-backfill-step-budget-design.md` (status)
- Add (if not already committed): `scripts/purge_integration_state.py`

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
from app.actions.configurations import BackfillConfig
print('has restart:', 'restart' in BackfillConfig.__fields__)
from app.actions.backfill_queue import BackfillJob
print('has clear:', hasattr(BackfillJob, 'clear'))
"
```
Expected: five actions listed; `has restart: True`; `has clear: True`.

- [ ] **Step 3: Update README**

In `README.md`, update the backfill bullet to note the step budgeting and the restart flag:

```markdown
- **backfill** (executable) — operator-triggered historical load for a study.
  Each cascade step processes bounded windows (`MAX_RECORDS_PER_BACKFILL_WINDOW`,
  adaptively shrunk down to `MIN_BACKFILL_WINDOW_SECONDS` on dense data) so a
  step always finishes inside the execution budget and cleanly cascades. Set
  `restart: true` to clear a stuck/previous job for the same parameters and
  start over. Config: `study_id`, optional `individual_ids`, `start`
  (a date or `"all"`), optional `backfill_max_concurrency`, `restart`.
```

(If the current README backfill bullet differs, adapt to fit while conveying the same content; note the adaptation in the report.)

- [ ] **Step 4: Commit the purge helper if untracked**

```bash
git status --porcelain scripts/purge_integration_state.py
# If untracked, stage it:
git add scripts/purge_integration_state.py
```

- [ ] **Step 5: Update spec status + commit + push + PR**

```bash
sed -i '' 's/^\*\*Status:\*\* approved design, pre-implementation/**Status:** implemented/' docs/superpowers/specs/2026-07-23-backfill-step-budget-design.md
git add README.md docs/superpowers/specs/2026-07-23-backfill-step-budget-design.md
git commit -m "docs: document backfill step budgeting and restart"
git push -u origin movebank-backfill-step-budget
gh pr create --base main --head movebank-backfill-step-budget \
  --title "Bound backfill step work + wedge recovery (restart)" \
  --body "Fixes the backfill sub-action timing out on dense individuals and permanently wedging the job. See docs/superpowers/specs/2026-07-23-backfill-step-budget-design.md."
```

- [ ] **Step 6: Confirm CI green**

Watch the CI run for the pushed commit; expected: success.

---

## Notes for the implementer

- Tasks 1 and 2 both edit `action_backfill_events_for_individual`; apply in order and re-run the full suite each task. Task 3 edits a different function (`action_backfill`) plus two other files.
- `mock_state_store` keys are `(integration_id, action_id, source_id)`; the backfill-watermark blob is stored under `("...","backfill_watermark","{job_id}.{ind.id}")`. `make_events_generator`, `_gps_event`, `INDIVIDUAL_ROW`, `BackfillEventsForIndividualConfig` are already defined in `test_handlers.py`.
- Do NOT change `MAX_RECORDS_PER_BACKFILL_STEP`, `_advance_watermarks`, `_query_start_for_sensor`, `_compute_batch_window`, or the steady-state pull. The per-window cap is a distinct, smaller bound layered on top.
- The overflow-discard applies ONLY to a fully-fetched window (`not window_interrupted`); the existing deadline-interrupted partial-send path is unchanged.
