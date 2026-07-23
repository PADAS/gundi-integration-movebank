# Recent-First (Reverse-Chronological) Backfill — Design

**Status:** implemented
**Date:** 2026-07-23
**Branch:** movebank-backfill-recent-first (off main, post-PR #17)

## Problem / goal

A study backfill can span years. Today `action_backfill_events_for_individual`
marches a scan position **up** from `start` toward `coverage_start`, so the
oldest data lands in EarthRanger first and the most recent (operationally most
valuable) history only appears once the whole multi-day job finishes.

Goal: deliver **recent data first** — process the range `[start, coverage_start)`
newest-window-first — so ER gains progressively older history from the present
backward, and a partial run is immediately useful.

## Chosen approach (from brainstorming)

**Full reverse chronological** (not a recent-slice-then-forward hybrid, and not a
configurable direction): reverse **replaces** forward as the only backfill
ordering. Recent-first is strictly better operationally once de-duplication is
handled, and a direction switch would double the surface of a fragile path
(YAGNI).

## How it works

### Traversal — walk the window down

Today the loop anchors on an ascending scan floor (`current`, advanced
`current += window`, persisted as `scan_from`). Reverse it:

- Start the position at `end` (= `action_config.end`, i.e. `coverage_start`).
- Each step processes the window `[max(start, pos − window), pos)`, then sets
  `pos` to that window's **lower** edge.
- Continue until `pos <= action_config.start`.

The persisted position (the backfill watermark's `scan_from`, reinterpreted as
"we have covered `[scan_from, end)`; still owe `[start, scan_from)`") is written
**every window**, exactly as today — so recent-first progress is durable and a
re-trigger of the same `job_id` resumes from where it stopped with **zero
redo**. The step budget, discard/shrink, cascade (`continue` → `trigger_action`),
and `CancelledError` backstop all carry over unchanged; only the direction of
`pos` movement and the window bounds flip.

### De-duplication — timestamp-range ownership (replaces the event-id floor)

The forward model dedups via a per-sensor `minimum_event_id` floor that only
ever marches **up**; reverse breaks it (older windows carry lower ids and would
be filtered out wholesale). That floor's only real job was cleaning up two
sources of query over-fetch. In reverse we replace it with **exact
timestamp-range ownership**:

- Every window fetches its sensors, then the handler **keeps only events whose
  parsed timestamp is in `[window_lower, window_upper)`** (half-open), discarding
  the rest. Each event therefore belongs to exactly one window — no cross-window
  event-id bookkeeping needed.
- This absorbs both over-fetch sources without special-casing: the
  movebank-client always prepends 60 min to accessory queries
  (`movebank_client/client.py:259`) and truncates the query `timestamp_end` to
  whole seconds (`:254`). Both would otherwise re-deliver rows into adjacent
  windows; the timestamp filter makes ownership exact at full millisecond
  precision and **does not depend on Movebank's endpoint inclusivity**.
- Backfill only ever touches **settled** historical data (`coverage_start ≈
  now − maximum_lookback_hours`, and every backfill window is below that, older
  than the 12 h `ACCESSORY_SETTLING_HOURS` margin), so dropping the accessory
  settling re-read loses nothing: there is no late-arriving accessory data in
  the historical range. Late arrivals at the *newest* edge remain the
  steady-state pull's job, unchanged.
- Consequence: the sub-action passes `minimum_event_id = 0` (no client-side id
  filter) and no longer calls `_query_start_for_sensor` for the accessory
  widening. Per-sensor `highest_event_id` / `latest_timestamp` are still tracked
  from the **kept** events, but only to seed the finalize forward-merge (below),
  not for dedup. `_query_start_for_sensor` stays in the tree for the
  steady-state pull, which still needs it.

The per-window overflow cap and the per-step backstop (from the step-budget
fix) now count **kept** (in-range) events, not raw fetched rows — the cap bounds
the *send* loop, and the discarded over-fetch (accessory pre-roll, boundary
second) is never sent. The discard/shrink still triggers on the kept count
exceeding `MAX_RECORDS_PER_BACKFILL_WINDOW`.

The half-open `[lower, upper)` convention keeps the seam with the pull exact:
backfill's top window is `[coverage_start − window, coverage_start)`, the pull
owns `[coverage_start, ∞)`, and an event exactly at `coverage_start` belongs to
the pull — identical to today's boundary.

### `coverage_start` — progressive progress, race-safe reconciliation

Two distinct records, deliberately kept separate to avoid a concurrency hazard:

1. **Durable progress (per window):** the backfill watermark's descending
   position, persisted every window (as today). This is the authoritative
   record that makes recent-first resumable with no redo. It is backfill-owned
   state (`backfill_watermark` key) — the `*/10` pull never touches it, so there
   is **no race**.

2. **Pull-facing `coverage_start` (per individual boundary):** the
   `coverage_start` field lives inside the pull cursor `IndividualState`, which
   the steady-state pull also writes. Backfill reconciles it at
   **finalize**, not per window:
   - **Success** (individual fully backfilled): `coverage_start = start`
     (unchanged from today).
   - **Abandon** (max attempts): **new** — set `coverage_start` to the reached
     floor (the watermark position), via the same forward-merge write finalize
     already uses for success, so an abandoned reverse job records how far back
     it actually got. Today's abandon path leaves the pull cursor untouched;
     this adds a single write on that path.

   **Why not write `coverage_start` every window?** It shares the pull cursor
   object with the `*/10` pull. Every backfill write to that key is a
   get-merge-set that can clobber a concurrent pull cursor advance (a known,
   accepted TOCTOU that finalize incurs **once** per individual today). Writing
   it per window over a days-long single-individual backfill would multiply that
   into thousands of chances to revert a pull advance → duplicate observations
   (`loaded_at` defeats Gundi dedup). The user-facing goal — recent data fast,
   and a re-run that never redoes completed windows — is fully delivered by
   reverse traversal + the per-window watermark; `coverage_start` is the
   coarser pull-facing floor and only needs to be right at individual
   completion/abandonment.

   **Deferred alternative (out of scope):** if a continuously-moving pull-facing
   floor is later wanted (e.g. a live progress readout), the race-free way is to
   store `coverage_start` in its own Redis key decoupled from the sensor cursor,
   so backfill and the pull never write the same object. Noted, not built —
   it churns the just-merged `coverage_start` feature for marginal benefit.

### Pull interaction & finalize

Unchanged in substance (see the reverse-backfill/pull analysis): pull and
backfill run on separate state during the run; the pull owns
`[coverage_start, ∞)` and never reads `coverage_start` to decide what to fetch;
`BACKFILL_MAX_CONCURRENCY` (8) keeps backfill below the 25-connection budget so
the pull isn't starved. Finalize's forward-merge is effectively **inert** in
reverse (backfill's max timestamp/event-id, captured in the first/recent window,
sits below `coverage_start`, so `max(pull, backfill)` = the pull's value) — its
real jobs in reverse are setting `coverage_start` and cleaning up the watermark.
The `_seed_pull_cursor_at_end` boundary claim at job start is unchanged.

## What changes / what doesn't

- **Changes:** `action_backfill_events_for_individual` window loop (descending
  traversal; timestamp-range keep-filter; `minimum_event_id=0`; no accessory
  widening; per-window descending position persist); `_finalize_backfill_individual`
  abandon path (set `coverage_start` to reached floor).
- **Unchanged:** step budget + discard/shrink + `window_seconds`; `CancelledError`
  backstop; `restart` flag; `_seed_pull_cursor_at_end`; the steady-state pull
  (including its use of `_query_start_for_sensor` and `minimum_event_id`);
  `_compute_batch_window`; `coverage_start`'s storage (stays an `IndividualState`
  field); `BackfillConfig` (no new fields).

## Testing

Docker `mb-runner-test`, `respx`/mocked Redis, existing `app/actions/tests`
patterns.

- Reverse traversal covers `[start, coverage_start)` newest-window-first and
  reaches `start`; the descending position is persisted every window and a
  re-trigger resumes from it with no redo.
- Timestamp-range ownership: an event is emitted by exactly the window whose
  `[lower, upper)` contains its timestamp; the accessory 60-min client pre-roll
  and the whole-second `timestamp_end` truncation do **not** cause duplicates or
  gaps at window seams (assert with events straddling a boundary second and
  within the accessory pre-roll).
- The most-recent window is processed first (recent data reaches Gundi before
  older data).
- `coverage_start`: success finalize sets it to `start`; abandon finalize sets
  it to the reached floor; it is **not** written per window (assert the pull
  cursor is untouched mid-run).
- Boundary with the pull stays exact (no double-emit at `coverage_start`).
- Regression: step budget / discard-shrink, `restart`, `CancelledError`, and the
  steady-state pull tests stay green.

## Files

- `app/actions/handlers.py` — `action_backfill_events_for_individual` (reverse
  loop + timestamp-range keep-filter + no accessory widening + descending
  position); `_finalize_backfill_individual` (abandon-path `coverage_start`).
- `app/actions/tests/*` — coverage per above.

## Out of scope

- Configurable forward/reverse direction (reverse replaces forward).
- Decoupling `coverage_start` into its own Redis key for per-window pull-facing
  updates (deferred; the watermark already provides per-window durability).
- Any movebank-client change (the handler-side timestamp filter absorbs the
  client's accessory pre-roll and second-truncation).
- Changing the PubSub ack/redelivery model.
