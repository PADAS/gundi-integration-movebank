# Backfill Step Budget & Wedge Recovery — Design

**Status:** implemented
**Date:** 2026-07-23
**Branch:** movebank-backfill-step-budget (off main, post-PR #16)

## Problem

Observed in the dev deployment: the backfill cascade sub-action
`action_backfill_events_for_individual` **times out** on high-frequency
individuals and then **permanently wedges the whole job**.

Two coupled defects:

1. **The step exceeds its execution budget.** The sub-action's internal
   deadline (`0.8 × MAX_ACTION_EXECUTION_TIME = 432s`) is only checked
   *between* windows and *between* per-sensor fetches — never during a single
   window's (atomic) fetch or during the send loop. `get_individual_events_by_time`
   downloads a whole window's CSV in one call and yields rows in Movebank's
   order (not sorted by `event_id`), so one dense window can hold far more
   events than the density-based `_compute_batch_window` estimate. Sending
   that window is hundreds of successful `POST /v2/observations/` batches
   (confirmed all-200 in the logs) that run past the 432s deadline to the
   540s hard kill. `MAX_RECORDS_PER_BACKFILL_STEP` can't help — it is only
   evaluated *after* a window completes.

2. **A timed-out step wedges the job forever.** The `asyncio.wait_for(…, 540s)`
   in `action_runner.execute_action` cancels the task, raising
   `asyncio.CancelledError` — a `BaseException`, **not** an `Exception`. The
   sub-action's `except NoConnectionSlot` / `except Exception` handlers don't
   catch it, so the retry path, the continue-cascade `trigger_action`, and
   `_finalize_backfill_individual` all never run. `in_flight` (incremented at
   dispatch) is never unwound, and with no PubSub redelivery nothing restarts
   the step. The job stays `in_flight > 0` forever; every re-trigger logs
   `job already active` (the stalled-resume path only fires when
   `in_flight == 0`). Observed as `job-1ffe699e0486: job already active`.

## Constraints that shape the fix

- **The window is the atomic unit.** The per-(sensor, window) fetch returns
  one CSV, rows unordered by `event_id`, and the `loaded_at` field
  deliberately defeats Gundi dedup. So a window cannot be *partially* sent:
  stopping mid-send would either skip unsent lower-`event_id` rows (gap) or
  re-send (duplicates). Watermarks may only advance over a *fully* sent window.
- **No PubSub redelivery.** The service always acks (`POST /` returns 200), so
  a killed step is never redelivered — recovery must be either self-scheduled
  by the step (`trigger_action`) or operator-initiated.
- **`coverage_start` is now in `main`** (PR #16). This fix builds on top; the
  restart path relies on the merged cursor semantics (seed/legacy handling).

## Design — Option 1 (chosen from brainstorming)

Keep the window atomic; bound *how large a window we are willing to send*;
converge the window size down in dense regions; make a hard-timeout loud and
loss-free; and give operators a restart button.

### Part A — Bound per-step work (fixes defect 1)

1. **Per-window event cap.** New constant `MAX_RECORDS_PER_BACKFILL_WINDOW`
   (default 5000). After a window's atomic fetch of *all* sensors, **before
   sending**: if the fetched event count exceeds the cap, **discard the fetch,
   shrink the window, persist the new size, and retry the same `current`** —
   no send, no watermark advance, no `scan_from` advance. Only a window at or
   under the cap is ever sent, so the send loop length (and time) is bounded.
   The cost is a wasted fetch on an over-dense window; it converges quickly.
   This overflow-discard applies only to a window that was **fully fetched**
   (all sensors). The existing `window_interrupted` path — the deadline hit
   *between sensors* mid-fetch — is unchanged: it sends the sensors fetched so
   far and retries the window (safe, since per-sensor `minimum_event_id`
   advanced only for the sensors that were fetched).

2. **Proportional shrink.** On overflow, set
   `window_seconds = max(MIN_BACKFILL_WINDOW_SECONDS,
   window_seconds × (cap / count) × SAFETY)` (SAFETY ≈ 0.8) rather than blind
   halving — usually one retry suffices. `MIN_BACKFILL_WINDOW_SECONDS`
   (default 300) is a floor so the window can't shrink to nothing; at the floor
   a window is sent even if over cap (pathological single-instant burst),
   with a warning.

3. **Persist `window_seconds`** in the backfill-watermark blob alongside the
   existing `scan_from` key (an ad-hoc blob key, *not* an `IndividualState`
   model field — same pattern `scan_from` already uses). Initialised from
   `_compute_batch_window`; shrinks on overflow; read back on the next step so
   convergence persists across cascade steps.

4. **Deadline coverage.** Keep the `0.8 × MAX_ACTION_EXECUTION_TIME` budget and
   the existing checks (before each window via the loop condition, between
   sensors). The discard/retry loop also honours the deadline. With a ≤5000-event
   window (~25 send batches, tens of seconds) the ~108s slack to the 540s hard
   kill is never consumed, so a step always ends by cleanly `trigger_action`-
   cascading (or finalising) — the hard timeout becomes unreachable in practice.

5. **Whole-window atomicity preserved.** A sub-cap window is sent in full and
   watermarks advance over exactly what was sent. No mid-window partial send.

### Part B — CancelledError backstop (surfaces defect 2 losslessly)

Add `except asyncio.CancelledError:` to the sub-action that logs a clear line
(`step hard-cancelled at scan_from=<X>; will resume on next trigger`) and
**re-raises**. Because Part A persists `scan_from` and `window_seconds` every
window, no data is lost — a later trigger resumes exactly where it stopped.

This intentionally does **not** attempt async recovery (re-trigger / `in_flight`
decrement) during cancellation: once a task is being cancelled, further awaits
(Redis, PubSub publish) are themselves cancelled and unreliable, and Cloud Run
may throttle the instance after the response returns. Part A prevents the
timeout; Part B guarantees that if one ever occurs it is visible and loss-free;
Part C is the recovery path.

### Part C — `restart` flag (recovers a wedged job)

Add `restart: bool = False` to `BackfillConfig`. When true, `action_backfill`,
before the `job.exists()` check, **clears the deterministic job's state** and
re-seeds from scratch:

- `BackfillJob.clear()` (new) deletes the job's `meta`, `pending`, and
  `meta.configs` keys.
- Delete the per-individual backfill watermarks for this job
  (`backfill_watermark` state, source `"{job_id}.{ind.id}"`) for each candidate
  individual, and the `backfill_progress.{job_id}` throttle key.
- Then proceed through the normal seed path (re-running the backfill from
  `start`).

Semantics: **full restart from `start`** — predictable and idempotent, at the
cost of redoing work already done. This is the operator's recovery button for a
wedged/stalled job, with no Redis access required. It does **not** touch the
steady-state pull cursors: the merged `coverage_start` logic already handles
seed/legacy/real cursors on re-seed.

`job_id` stays deterministic on `(study_id, individual set, start)`, so a
`restart=True` run with the same parameters targets the same job it is clearing.

## What is removed / unchanged

- No change to `IndividualState`, `_advance_watermarks`, `_query_start_for_sensor`,
  `_compute_batch_window`, or the steady-state pull.
- `MAX_RECORDS_PER_BACKFILL_STEP` (per-step cascade cap) is unchanged and still
  triggers a clean cascade; the new per-*window* cap is a distinct, smaller bound.
- The existing `NoConnectionSlot` backoff and `except Exception` retry/abandon
  paths are unchanged.

## Immediate operations (out of band)

The currently-wedged `job-1ffe699e0486` is cleared either by the
`scripts/purge_integration_state.py` helper (integration-scoped, dry-run-first)
or, once this ships, by one `restart=True` backfill.

## Testing

Docker `mb-runner-test`, `respx`/mocked Redis, existing `app/actions/tests`
patterns.

- **A:** a window whose fetch exceeds the cap is discarded (not sent), the
  window shrinks and persists, and the retry sends the smaller window; at the
  `MIN_BACKFILL_WINDOW_SECONDS` floor an over-cap window is sent with a warning;
  `window_seconds` round-trips through the watermark blob and is honoured on the
  next step; watermarks/`scan_from` do **not** advance on a discarded window.
- **B:** when the fetch/send loop is cancelled, the handler logs and re-raises,
  and the persisted `scan_from` is unchanged from the last completed window (no
  loss, no double-send).
- **C:** `restart=True` clears job meta/pending/configs and the job's
  watermarks, then seeds and dispatches fresh; `restart=False` (default)
  preserves today's behaviour (including `already active` on an existing job).
- Regression: existing backfill and pull tests stay green.

## Files

- `app/actions/handlers.py` — `action_backfill_events_for_individual`
  (discard/shrink loop, persisted `window_seconds`, `CancelledError` backstop);
  `action_backfill` (`restart` handling).
- `app/actions/configurations.py` — `BackfillConfig.restart`.
- `app/actions/backfill_queue.py` — `BackfillJob.clear()`.
- `app/settings/integration.py` — `MAX_RECORDS_PER_BACKFILL_WINDOW`,
  `MIN_BACKFILL_WINDOW_SECONDS` (and any shrink SAFETY constant).
- `app/actions/tests/*` — coverage per above.

## Out of scope

- Mid-window partial sends / streaming checkpoints (unsafe given unordered rows
  + `loaded_at`).
- Adaptive window *growth* in sparse regions (YAGNI — the failure is overflow;
  shrink-on-overflow is sufficient).
- Changing the PubSub ack/redelivery model (infra).
- Auto-detection of stale jobs (the `restart` flag is the explicit recovery).
