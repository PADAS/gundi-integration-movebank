# Recent-First (Reverse-Chronological) Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make backfill process `[start, coverage_start)` newest-window-first so recent history reaches EarthRanger first, replacing the event-id dedup floor with exact timestamp-range ownership.

**Architecture:** Reverse the sub-action's scan so a `cursor` descends from `end` to `start`; each window `[max(start, cursor − window), cursor)` fetches its sensors, keeps only events whose timestamp lies in that half-open range (dropping the client's accessory pre-roll and second-truncation over-fetch), sends them, and descends. Durable per-window progress stays in the backfill watermark; the pull-facing `coverage_start` reconciles only at finalize (success → `start`; abandon → reached floor).

**Tech Stack:** Python 3.10, pydantic v1, redis.asyncio, pytest + pytest-asyncio + pytest-mock + respx. Tests run in Docker `mb-runner-test`.

## Global Constraints

- Python 3.10, pydantic v1.
- Branch: `movebank-backfill-recent-first` (off `main`, base `f669398`). Commit on top; no new branch.
- Test command: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q`. Focused: append the path / `-k`. Never run pytest outside Docker.
- **Reverse replaces forward** — there is no direction config.
- **The window is the atomic unit**; sent in full or discarded in full. Reverse uses **timestamp-range ownership** for dedup: keep only events with `window_lower <= ts < window_upper` (half-open). No `event_id` floor (`minimum_event_id = 0`); no accessory settling widening in backfill.
- Backfill touches only settled historical data (`coverage_start ≈ now − maximum_lookback_hours`; every window is older than `ACCESSORY_SETTLING_HOURS = 12h`), so dropping the accessory re-read loses nothing.
- The per-window overflow cap / per-step backstop count **kept** (in-range) events, not raw fetched rows.
- `coverage_start` is written to the pull cursor ONLY at finalize (success → `action_config.start`; abandon → reached floor). NEVER per window (avoids multiplying the pull-cursor TOCTOU).
- Unchanged: step budget + discard/shrink + `window_seconds`; `CancelledError` backstop; `restart`; `_seed_pull_cursor_at_end`; the steady-state pull and its `_query_start_for_sensor` / `minimum_event_id` use; `_compute_batch_window`; `_advance_watermarks` (kept as-is — its output is vestigial in reverse, feeding only the inert finalize merge); `coverage_start` storage (an `IndividualState` field); `BackfillConfig`.
- Constants (unchanged): `MAX_RECORDS_PER_BACKFILL_WINDOW=5000`, `MIN_BACKFILL_WINDOW_SECONDS=300`, `BACKFILL_WINDOW_SHRINK_SAFETY=0.8`, `MAX_RECORDS_PER_BACKFILL_STEP=10000`, `OBSERVATIONS_BATCH_SIZE=200`, `BACKFILL_MAX_ATTEMPTS=5`.

## File Structure

- `app/actions/handlers.py` — `action_backfill_events_for_individual` (reverse loop + keep-filter); `_finalize_backfill_individual` + a new `_record_abandoned_coverage` helper (Task 2).
- `app/actions/tests/test_handlers.py` — a new windowed-events generator, new reverse tests, and reconciliation of the existing forward sub-action tests.

---

### Task 1: Reverse the sub-action traversal with timestamp-range ownership

**Files:**
- Modify: `app/actions/handlers.py` (`action_backfill_events_for_individual`, roughly lines 702–860)
- Test: `app/actions/tests/test_handlers.py` (new helper + new tests + reconcile existing)

**Interfaces:**
- Consumes: `_ensure_utc`, `parse_date`, `build_observation`, `send_observations_to_gundi`, `_advance_watermarks`, `settings.*` caps.
- Produces: backfill fills `[start, end)` newest-window-first; the watermark blob's `scan_from` is the descending cursor (we have covered `[scan_from, end)`); each event is emitted by exactly the window whose `[lower, upper)` contains its timestamp.

- [ ] **Step 1: Add a windowed-events generator to the test module**

The existing `make_counting_events_generator` / `make_events_generator` yield events with fixed timestamps that ignore the query range — incompatible with the new keep-filter. Add near the other generators in `app/actions/tests/test_handlers.py`:

```python
def make_windowed_events_generator(per_window, sensor_type_id="653"):
    """Async-generator stand-in that yields `per_window` events with timestamps
    spread INSIDE each requested [timestamp_start, timestamp_end) window, so the
    reverse sub-action's timestamp-range keep-filter retains them. Records each
    call's (timestamp_start, timestamp_end) in `calls['windows']`."""
    calls = {"count": 0, "windows": []}

    async def _gen(**kwargs):
        start = kwargs["timestamp_start"]
        end = kwargs["timestamp_end"]
        calls["windows"].append((start, end))
        base = calls["count"] * 100000
        calls["count"] += 1
        span = (end - start).total_seconds()
        for i in range(per_window):
            # Evenly place events strictly inside [start, end).
            offset = span * (i + 1) / (per_window + 1)
            ts = start + timedelta(seconds=offset)
            yield {
                "event_id": str(base + i),
                "timestamp": ts.strftime("%Y-%m-%d %H:%M:%S.000"),
                "location_lat": "1.5", "location_long": "2.5",
                "individual_id": "111", "sensor_type_id": sensor_type_id,
            }
    return _gen, calls
```

- [ ] **Step 2: Write the failing reverse tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_processes_recent_window_first(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Reverse: the FIRST window queried is the most recent one, [end - window, end).
    from app.actions.configurations import BackfillEventsForIndividualConfig
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)
    gen, calls = make_windowed_events_generator(1)
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    for m in ("record_completion", "decr_in_flight", "reset_attempts"):
        mocker.patch(f"app.actions.backfill_queue.BackfillJob.{m}", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual={**INDIVIDUAL_ROW, "number_of_events": "60000"},
            job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "completed"
    first_lower, first_upper = calls["windows"][0]
    assert first_upper == end                      # started at the most recent edge
    assert first_lower > start                     # first window is a recent slice, not the whole range
    # windows strictly descend and tile down to start with no gap/overlap
    lowers = [w[0] for w in calls["windows"]]
    uppers = [w[1] for w in calls["windows"]]
    assert uppers[1:] == lowers[:-1]               # each window's upper == previous window's lower
    assert min(lowers) == start                     # reached start


@pytest.mark.asyncio
async def test_backfill_timestamp_ownership_no_dup_across_boundary(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # An event exactly at a window boundary, and the accessory 60-min pre-roll,
    # must each be emitted by exactly one window (no dup, no gap). We drive real
    # source-id collection through send and assert uniqueness.
    from app.actions.configurations import BackfillEventsForIndividualConfig
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 11, tzinfo=timezone.utc)

    # Generator returns the SAME fixed set every call (like the real client's
    # range-agnostic over-fetch): three events, one on a window boundary.
    boundary = datetime(2024, 1, 6, tzinfo=timezone.utc)
    fixed = [
        {"event_id": "1", "timestamp": "2024-01-03 12:00:00.000", "location_lat": "1.5",
         "location_long": "2.5", "individual_id": "111", "sensor_type_id": "653"},
        {"event_id": "2", "timestamp": boundary.strftime("%Y-%m-%d %H:%M:%S.000"), "location_lat": "1.5",
         "location_long": "2.5", "individual_id": "111", "sensor_type_id": "653"},
        {"event_id": "3", "timestamp": "2024-01-08 12:00:00.000", "location_lat": "1.5",
         "location_long": "2.5", "individual_id": "111", "sensor_type_id": "653"},
    ]
    async def gen(**kwargs):
        for e in fixed:
            yield e
    mock_movebank_client.get_individual_events_by_time = gen
    sent_ids = []
    async def capture(observations, **kwargs):
        sent_ids.extend(o["additional"]["event_id"] for o in observations)
        return []
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(side_effect=capture))
    mocker.patch("app.actions.handlers.settings.MIN_BACKFILL_WINDOW_SECONDS", 60)
    for m in ("record_completion", "decr_in_flight", "reset_attempts"):
        mocker.patch(f"app.actions.backfill_queue.BackfillJob.{m}", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual={**INDIVIDUAL_ROW, "number_of_events": "1000"},
            job_id="job-1", start=start, end=end,
        ),
    )

    # Each of the three in-range events emitted exactly once (boundary event not doubled).
    assert sorted(sent_ids) == ["1", "2", "3"]


@pytest.mark.asyncio
async def test_backfill_resumes_from_persisted_descending_cursor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A persisted scan_from (descending cursor) resumes the descent: the first
    # window queried is [scan_from - window, scan_from), not [end - window, end).
    from app.actions.configurations import BackfillEventsForIndividualConfig
    st = IndividualState(individual_id="111", study_id="12345")
    mock_state_store[(str(integration.id), "backfill_watermark", "job-1.111")] = {
        **st.dict(), "scan_from": "2024-06-01T00:00:00+00:00", "window_seconds": 86400.0,  # 1-day window
    }
    gen, calls = make_windowed_events_generator(0)
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    for m in ("record_completion", "decr_in_flight", "reset_attempts"):
        mocker.patch(f"app.actions.backfill_queue.BackfillJob.{m}", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 12, 1, tzinfo=timezone.utc),
        ),
    )

    assert calls["windows"][0][1] == datetime(2024, 6, 1, tzinfo=timezone.utc)          # resumed at scan_from
    assert calls["windows"][0][0] == datetime(2024, 5, 31, tzinfo=timezone.utc)         # one 1-day window down
```

- [ ] **Step 3: Run the new tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "recent_window_first or timestamp_ownership or resumes_from_persisted_descending" -q`
Expected: FAIL — the current forward loop queries `[start …]` upward, so the first window's `timestamp_upper` is not `end`, and the boundary/ownership assertions don't hold.

- [ ] **Step 4: Implement the reverse loop**

In `action_backfill_events_for_individual`, replace the window-setup and the whole `while` loop (currently ~702–814) with the reverse version. Keep the surrounding `try` / `except` structure; only the loop body and the initial `cursor` change here (the `except`/completion tail changes in the next steps).

Replace the setup line that computes the initial position (`current = persisted_scan_from if persisted_scan_from is not None else min(sensor_type_timestamps.values())`) with:

```python
    # Reverse (recent-first): `cursor` is the LOWER edge of coverage so far — we
    # have covered [cursor, end) and still owe [start, cursor). It begins at
    # `end` (= coverage_start) and descends toward `start`; a persisted position
    # resumes the descent. (The backfill watermark's scan_from stores it.)
    cursor = persisted_scan_from if persisted_scan_from is not None else action_config.end
```

Replace the loop body:

```python
    try:
        async with mb_client as mb:
            while cursor > action_config.start and time.monotonic() < deadline:
                window_lower = max(action_config.start, cursor - window)
                window_upper = cursor
                events = []
                window_interrupted = False
                for stid in sensor_type_ids:
                    if time.monotonic() >= deadline:
                        window_interrupted = True
                        break
                    async with movebank_slot(auth_config.username):
                        async for event in mb.get_individual_events_by_time(
                            study_id=action_config.study_id, individual_id=ind.id,
                            timestamp_start=window_lower, timestamp_end=window_upper,
                            sensor_type_ids=[stid], minimum_event_id=0,
                        ):
                            events.append(event)

                # Timestamp-range ownership: keep only events whose timestamp is
                # in this window's half-open [lower, upper). Discards the client's
                # accessory 60-min pre-roll and the whole-second timestamp_end
                # truncation, so each event belongs to exactly one window and no
                # cross-window event-id dedup is needed. Unparseable timestamps
                # are dropped (build_observation would drop them anyway).
                kept = []
                for e in events:
                    try:
                        ts = _ensure_utc(parse_date(e.get("timestamp")))
                    except Exception:
                        continue
                    if window_lower <= ts < window_upper:
                        kept.append(e)

                # Overflow guard on KEPT (send) volume — see step-budget design.
                if (not window_interrupted
                        and len(kept) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW
                        and window > min_window):
                    shrunk = (window.total_seconds()
                              * settings.MAX_RECORDS_PER_BACKFILL_WINDOW / len(kept)
                              * settings.BACKFILL_WINDOW_SHRINK_SAFETY)
                    window = max(min_window, timedelta(seconds=shrunk))
                    blob = json.loads(state.json())
                    blob["scan_from"] = cursor.isoformat()           # unchanged (cursor not advanced)
                    blob["window_seconds"] = window.total_seconds()
                    await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                                  blob, source_id=watermark_source)
                    logger.info(
                        f"Backfill {log_reference}: window over cap "
                        f"({len(kept)} > {settings.MAX_RECORDS_PER_BACKFILL_WINDOW}); "
                        f"shrunk to {window.total_seconds():.0f}s, retrying"
                    )
                    continue
                if (not window_interrupted
                        and len(kept) > settings.MAX_RECORDS_PER_BACKFILL_WINDOW):
                    logger.warning(
                        f"Backfill {log_reference}: window at floor "
                        f"({settings.MIN_BACKFILL_WINDOW_SECONDS}s) still over cap "
                        f"({len(kept)} events); sending anyway"
                    )

                device_name = _display_name(ind)
                observations = [o for e in kept if (o := build_observation(event=e, device_name=device_name)) is not None]
                for batch in chunks(observations, OBSERVATIONS_BATCH_SIZE):
                    await send_observations_to_gundi(observations=batch, integration_id=integration_id)

                # Vestigial in reverse: advances per-sensor state from the kept
                # events. Used only by the finalize forward-merge, which is inert
                # here (backfill's max is below coverage_start). Kept for parity
                # and harmless (may reflect the oldest window's values).
                _advance_watermarks(state, kept, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)
                observations_sent += len(observations)
                if not window_interrupted:
                    cursor = window_lower       # descend

                blob = json.loads(state.json())
                blob["scan_from"] = cursor.isoformat()
                blob["window_seconds"] = window.total_seconds()
                await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                              blob, source_id=watermark_source)

                if window_interrupted or observations_sent >= MAX_RECORDS_PER_BACKFILL_STEP:
                    break
```

Update the `except asyncio.CancelledError` log to reference `cursor` instead of `current`:

```python
        logger.warning(
            f"Backfill {log_reference}: step hard-cancelled at scan_from={cursor.isoformat()}; "
            "resume on next trigger or re-run with restart=true"
        )
```

Update the completion check at the tail (currently `if current < action_config.end:`):

```python
    if cursor > action_config.start:
        # Budget exhausted mid-range: continue THIS individual on the next step
        # (descending from the persisted cursor).
        await trigger_action(
            integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=action_config,
        )
        logger.info(f"Backfill {log_reference} continued at {cursor.isoformat()} ({observations_sent} obs this step)")
        return {"status": "continued", "observations_sent": observations_sent}

    return await _finalize_backfill_individual(
        integration_id, job, ind, action_config, observations=observations_sent, state=state
    )
```

Also update the `persisted_scan_from` comment block (~689–694) to describe a descending cursor rather than an ascending floor (semantics: "we have covered `[scan_from, end)`").

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "recent_window_first or timestamp_ownership or resumes_from_persisted_descending" -q`
Expected: PASS.

- [ ] **Step 6: Reconcile the existing forward sub-action tests**

The keep-filter and the reversed traversal break tests that (a) use fixed-timestamp generators whose events fall outside the queried windows, or (b) assert forward-specific window bounds / positions. Run the backfill sub-action set and fix each failure, preserving the test's original intent:

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "backfill_individual or over_cap or honours_persisted or cancelled_step" -q`

Known tests to update (verify against actual results):
- `test_backfill_individual_density_window_applies` — 0-event generator; asserts window *count*. Reverse tiles the same 30-day range at the same window size → same count (12). Likely passes; if the count differs by one due to the descending clamp at `start`, update the expected count to the observed tiling.
- `test_backfill_individual_stops_at_per_step_record_backstop` — uses `make_counting_events_generator(3000)` with a fixed 2026 timestamp far outside the 2024 range → all dropped by the keep-filter. Switch to `make_windowed_events_generator(3000)` so 3000 in-range events are kept per window; keep the assertion that the per-step backstop stops after `ceil(10000/3000)=4` windows (`observations_sent == 12000`, one `trigger_action`).
- `test_backfill_individual_checks_deadline_between_sensor_fetches` — deadline mechanics are direction-agnostic; if it asserts a specific window bound, flip it to the descending first window `[end - window, end)`.
- `test_backfill_individual_resumes_scan_from_persisted_floor` — forward-resume semantics; supersede/rename to the descending-resume behavior already covered by `test_backfill_resumes_from_persisted_descending_cursor` (delete this forward-only test or convert it).
- `test_backfill_individual_sparse_sensor_reaches_end_without_livelock` — forward "reaches end"; convert to reverse "reaches start" (cursor descends to `start`, no livelock) using `make_windowed_events_generator(0)`.
- `test_backfill_window_over_cap_is_discarded_then_sent_after_shrink` — uses `make_counting_events_generator(3)` (fixed 2026 ts). Switch to `make_windowed_events_generator(3)` so 3 in-range events are kept and exceed the patched cap of 2; keep the discard-then-shrink assertions (`calls["count"] == 2`, `send.call_count == 1`, persisted `window_seconds == floor`). Note windows now descend from `end`.
- `test_backfill_honours_persisted_window_seconds` — asserts the first window's `timestamp_end`. In reverse the first window is `[scan_from - 60s, scan_from)`; update the assertion to `timestamp_start == scan_from - 60s` and `timestamp_end == scan_from` (it already patches `MIN_BACKFILL_WINDOW_SECONDS`).
- `test_backfill_individual_retriggers_self_when_budget_exhausted` — "continued when current<end" → "continued when cursor>start"; adjust any position assertion to the descending cursor.
- Tests that are direction-agnostic and should pass unchanged (verify): `..._acquires_connection_slot`, `..._finalizes_and_dispatches_next`, `..._backs_off_when_no_slot`, `..._abandoned_after_max_attempts`, `..._finalize_error_is_not_misclassified...`, `..._resets_attempts_after_successful_step`, `..._seeds_pull_cursor_at_end_and_finalize_merges_forward`, `test_backfill_cancelled_step_reraises_and_preserves_scan_from`.

For each test you change, note the change and why in the report.

- [ ] **Step 7: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: reverse-chronological backfill with timestamp-range ownership"
```

---

### Task 2: Abandon-path `coverage_start` = reached floor

**Files:**
- Modify: `app/actions/handlers.py` (new `_record_abandoned_coverage` helper; call it in the abandon branch of `action_backfill_events_for_individual`)
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `state_manager`, `CURSOR_STATE_ACTION_ID`, `IndividualState`.
- Produces: on abandon (after `BACKFILL_MAX_ATTEMPTS`), the pull cursor's `coverage_start` is lowered to the reached floor (`cursor`), so an abandoned reverse job records how far back it actually covered. Success finalize is unchanged (`= start`).

- [ ] **Step 1: Write the failing test**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_abandon_lowers_coverage_start_to_reached_floor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # One window completes (cursor descends), then fetches fail and the
    # individual is abandoned; coverage_start must be LOWERED to the reached
    # floor (the descended cursor), not left at its original value.
    from app.actions.handlers import BACKFILL_MAX_ATTEMPTS
    from app.actions.configurations import BackfillEventsForIndividualConfig
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 6, 1, tzinfo=timezone.utc)
    # A 30-day window means the first window is [end - 30d, end) = [2024-05-02, 2024-06-01).
    st = IndividualState(individual_id="111", study_id="12345")
    mock_state_store[(str(integration.id), "backfill_watermark", "job-1.111")] = {
        **st.dict(), "window_seconds": 2592000.0,  # 30 days
    }
    # Pre-existing pull cursor with coverage_start at end (the backfill boundary).
    pull = IndividualState(individual_id="111", study_id="12345")
    pull.coverage_start = end
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = pull.dict()

    # Window 1 returns one in-range event (succeeds, cursor descends to 2024-05-02);
    # window 2's fetch raises -> abandon (incr_attempts mocked past the limit).
    fetches = {"n": 0}
    async def gen(**kwargs):
        fetches["n"] += 1
        if fetches["n"] == 1:
            mid = kwargs["timestamp_start"] + (kwargs["timestamp_end"] - kwargs["timestamp_start"]) / 2
            yield {"event_id": "1", "timestamp": mid.strftime("%Y-%m-%d %H:%M:%S.000"),
                   "location_lat": "1.5", "location_long": "2.5",
                   "individual_id": "111", "sensor_type_id": "653"}
        else:
            raise RuntimeError("movebank down")
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_attempts",
                 AsyncMock(return_value=BACKFILL_MAX_ATTEMPTS + 1))  # over the limit -> abandon
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "abandoned"
    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    # Lowered from end (2024-06-01) to the reached floor (2024-05-02).
    assert IndividualState.parse_obj(saved).coverage_start == datetime(2024, 5, 2, tzinfo=timezone.utc)
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k abandon_lowers_coverage_start -q`
Expected: FAIL — today's abandon path leaves the pull cursor untouched, so `coverage_start` keeps whatever it was (here it happens to equal `end`, but nothing writes it; assert-write fails if the cursor object isn't re-written) — confirm the failure is that `coverage_start` isn't reconciled on abandon.

- [ ] **Step 3: Implement**

Add the helper near `_finalize_backfill_individual` in `app/actions/handlers.py`:

```python
async def _record_abandoned_coverage(integration_id, ind, action_config, floor):
    """On abandon, lower the pull cursor's coverage_start to the reached floor so
    the recent portion a reverse backfill DID cover is recorded (a later fresh
    backfill sees a smaller remaining range). Single best-effort write on a rare
    path; preserves the pull's sensor cursors as read."""
    raw = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    existing = IndividualState.parse_obj(raw) if raw else IndividualState(
        individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
    )
    if existing.coverage_start is None or floor < existing.coverage_start:
        existing.coverage_start = floor
    await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID,
                                  json.loads(existing.json()), source_id=ind.id)
```

In `action_backfill_events_for_individual`, the abandon branch (inside `except Exception`, when `attempts > BACKFILL_MAX_ATTEMPTS`) — add the coverage record before finalize:

```python
        logger.warning(f"Backfill {action_config.job_id}/{ind.id}: abandoned after {attempts} attempts: {exc}")
        await _record_abandoned_coverage(integration_id, ind, action_config, floor=cursor)
        result = await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=0)
        return {"status": "abandoned", **{k: v for k, v in result.items() if k != "status"}}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k abandon_lowers_coverage_start -q`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: abandoned reverse backfill records reached coverage floor"
```

---

### Task 3: Verify, docs, deliver

**Files:**
- Modify: `README.md` (backfill bullet — recent-first)
- Modify: `docs/superpowers/specs/2026-07-23-backfill-recent-first-design.md` (status)

**Interfaces:** none — verification and delivery.

- [ ] **Step 1: Clean-build full suite**

```bash
docker build -t mb-runner-test --target devimage -f docker/Dockerfile .
docker run --rm mb-runner-test python -m pytest app -q
```
Expected: PASS (no volume mount).

- [ ] **Step 2: Discovery check**

```bash
docker run --rm mb-runner-test python -c "
from app.actions import action_handlers
print('actions:', sorted(action_handlers.keys()))
"
```
Expected: the five actions listed.

- [ ] **Step 3: Update README**

In `README.md`, update the backfill bullet to note recent-first ordering:

```markdown
- **backfill** (executable) — operator-triggered historical load for a study,
  processed **newest-first**: recent history reaches EarthRanger before older
  data. Each cascade step processes bounded windows descending from
  `coverage_start` toward `start`; progress is durable per window, so an
  interrupted job resumes with no rework. Config: `study_id`, optional
  `individual_ids`, `start` (a date or `"all"`), optional
  `backfill_max_concurrency`, `restart`.
```

(Adapt to fit the current bullet's wording; note any adaptation in the report.)

- [ ] **Step 4: Spec status + commit + push + PR**

```bash
sed -i '' 's/^\*\*Status:\*\* approved design, pre-implementation/**Status:** implemented/' docs/superpowers/specs/2026-07-23-backfill-recent-first-design.md
git add README.md docs/superpowers/specs/2026-07-23-backfill-recent-first-design.md
git commit -m "docs: document recent-first backfill"
git push -u origin movebank-backfill-recent-first
gh pr create --base main --head movebank-backfill-recent-first \
  --title "Recent-first (reverse-chronological) backfill" \
  --body "Processes a study backfill newest-window-first so recent history reaches ER first. Replaces the event-id dedup floor with timestamp-range ownership. See docs/superpowers/specs/2026-07-23-backfill-recent-first-design.md."
```

- [ ] **Step 5: Confirm CI green**

Watch the CI run for the pushed commit; expected: success.

---

## Notes for the implementer

- The single biggest effort is Step 6 of Task 1 (reconciling existing forward tests). Work through the failures one at a time; most need only swapping a fixed-timestamp generator for `make_windowed_events_generator` and/or flipping a forward-position assertion to the descending cursor. Preserve each test's original intent.
- `make_windowed_events_generator`, `INDIVIDUAL_ROW`, `IndividualState`, `BackfillEventsForIndividualConfig` are (or become) available in `test_handlers.py`.
- Do NOT change `_advance_watermarks`, `_query_start_for_sensor`, `_compute_batch_window`, the steady-state pull, or `coverage_start`'s storage. The reverse loop passes `minimum_event_id=0` and does not call `_query_start_for_sensor`.
- Keep whole-window atomicity: a window is fully sent or fully discarded; watermarks/`scan_from` never advance on a discarded window.
