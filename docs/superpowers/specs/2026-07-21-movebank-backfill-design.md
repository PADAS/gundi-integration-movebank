# Movebank Backfill — Design

**Status:** approved design, pre-implementation
**Date:** 2026-07-21

## Goal

Let an operator backfill the full history of a Movebank study — potentially hundreds of thousands of observations per individual for high-frequency trackers (e.g. vultures) spanning months or years — without exceeding Movebank's connection limits, without stalling steady-state collection, and with visible progress in the Gundi Activity Log.

This repo is not yet in production, so the actions and configuration may be reshaped freely.

## Background: the current pull

- `pull_observations` (scheduled `*/10`) lists a study's individuals and triggers one internal `pull_events_for_individual` sub-action each.
- `pull_events_for_individual` keeps a **per-sensor cursor** (`latest_timestamp`, `highest_event_id`) in Redis per individual, fetches events in adaptive time windows, transforms them, and sends to Gundi in batches. A per-run record cap (`MAXIMUM_RECORDS_PER_INDIVIDUAL = 2000`) stops each run between windows; the next scheduled tick continues from the saved cursor.
- The `movebank-client` applies a **60-minute look-back overlap** on accessory-measurements queries to catch mildly out-of-order data; the `event_id >= minimum_event_id` filter prevents the overlap from re-emitting already-seen events.

Backfilling large history through this path alone is too slow (≈2000 records / individual / 10 min, schedule-paced) and has no progress signal or trigger UX. This design adds a dedicated backfill capability on top of the same primitives.

## Key decisions (from brainstorming)

1. **Inline, no GCS.** Backfill reads from Movebank and sends to Gundi in batches directly, like the current pull. No storage-decoupling layer. Revisit only if Gundi-side throughput becomes the bottleneck.
2. **Shared connection semaphore.** All integrations sharing a Movebank username share one Redis instance, so a username-keyed Redis semaphore enforces the real connection limit across every integration and every action.
3. **Separate backfill action.** A distinct operator-triggered `backfill` action runs its own self-cascading sub-actions; the `*/10` incremental pull is unchanged and independent. Both share the cascade engine and the semaphore.
4. **Time-budget cascade step.** A cascade step runs until ≈80% of `MAX_ACTION_EXECUTION_TIME`, then persists its watermark and re-triggers itself; a record/window backstop guards pathological cases.
5. **Study-wide + optional individual filter.** Default backfills the whole study; an optional individual-ID list narrows it. `start` = a date or an "all data" sentinel; `end` is frozen per individual so backfill meets the pull with no gap or overlap.
6. **Bounded rolling queue (dispatcher B).** The job keeps `K` individuals in flight (`backfill_max_concurrency`, below the semaphore ceiling); each finishing individual dequeues and triggers the next.
7. **Throttled job-level progress** in the Activity Log — start, throttled aggregate, complete, plus a warning when an individual is abandoned.
8. **Full-cursor hand-off + accessory settling margin** at the boundary (see below).

## Architecture

### Components

- **`backfill` action** (executable; `ExecutableActionMixin` so the portal shows a Run button). Resolves target individuals, computes each one's `[start, end)` range, writes the job's Redis work-queue and metadata, logs "started," and triggers the first `K` `backfill_individual` sub-actions.
- **`backfill_individual` sub-action** (internal; `InternalActionConfiguration`). Self-cascading worker for one individual: fetch from its backfill watermark toward `end` in per-sensor windows, send to Gundi, advance watermark, bounded by the time budget. On budget exhaustion → re-trigger self. On reaching `end` → finalize (hand off cursor), then dequeue + trigger the next individual.
- **Connection semaphore** (`app/services/movebank_connections.py`, new). `async with movebank_slot(username):` around every Movebank connection, adopted by **both** backfill and the steady-state pull.

### Data flow

```
operator → backfill action
  ├─ resolve individuals (whole study | filter list)
  ├─ per individual: compute [start, end)
  ├─ Redis: pending-queue = [individuals], in_flight = 0, counters = 0
  ├─ log "backfill started (N individuals, range)"
  └─ trigger K × backfill_individual

backfill_individual (per individual, cascading)
  loop within time budget:
    for each sensor:
      async with movebank_slot(username):   ← hard cap across all integrations,
        fetch window [watermark, min(watermark+window, end))    held around each
                                                                Movebank connection
    transform + send to Gundi (batches of OBSERVATIONS_BATCH_SIZE)
    advance per-sensor backfill watermark (timestamp + highest_event_id)
  if watermark < end:  re-trigger self          (continue this individual)
  else:                finalize + dequeue next   (individual done)
     ├─ write steady-state pull cursor = (watermark_ts, highest_event_id) per sensor
     ├─ Redis: in_flight--, completed++, pop next from pending
     ├─ throttled: log "Y/N complete, ~M observations"
     ├─ if next: trigger backfill_individual(next)
     └─ if queue empty and in_flight == 0: log "backfill finished"
```

## Action & config surfaces

```python
# Executable but NOT a scheduled pull: it must not subclass PullActionConfiguration
# (that would register it for type-wide scheduling). Operator-triggered only.
class BackfillConfig(GenericActionConfiguration, ExecutableActionMixin):
    study_id: str
    individual_ids: Optional[List[str]] = None          # empty/None = whole study
    start: Union[datetime, Literal["all"]]               # date, or "all data"
    backfill_max_concurrency: int = 8                    # rolling-queue width (K)

class BackfillIndividualConfig(InternalActionConfiguration):
    study_id: str
    individual: Individual
    job_id: str
    start: datetime                                      # resolved (no "all" sentinel here)
    end: datetime                                        # frozen boundary
```

- `PullObservationsConfig` / `PullEventsForIndividualConfig` are unchanged. The old `maximum_lookback_hours`-override manual-backfill hack is removed — `backfill` replaces it.
- `end` is resolved per individual at job start (see boundary section); `start="all"` resolves to the individual's Movebank `timestamp_start` (falling back to a floor if absent).

## Concurrency

### Global connection semaphore (correctness guarantee)

- Redis key `movebank:connections:<username_hash>` where `<username_hash>` = truncated SHA-256 of the username (avoids storing the plaintext account name in a key).
- Ceiling: `MOVEBANK_MAX_CONNECTIONS` setting, default **25** (headroom under Movebank's documented 31/username).
- Acquire = atomic `INCR`; if result > ceiling, `DECR` and report "no slot." Release = `DECR`, always in `finally`.
- Each slot key/counter carries a **TTL** (a few × the step time budget) so a crashed worker cannot leak a slot permanently.
- When no slot is available, a worker does **not** spin: it re-triggers itself with a jittered delay marker and returns, freeing its execution slot.
- **The steady-state `pull_events_for_individual` adopts the same `movebank_slot` context manager**, so backfill and ongoing collection draw from one shared budget.

### Rolling queue (good-citizen throttle)

- Per-`job_id` Redis state: `pending` (list of individual IDs), `in_flight` (counter), `completed`, `observations_sent`, `started_at`, `range`, per-individual `attempts`.
- `backfill_max_concurrency` (K, default 8) is intentionally **below** `MOVEBANK_MAX_CONNECTIONS`, so a running backfill never consumes the entire connection budget — the scheduled pull and other integrations keep getting slots.
- Hand-off is atomic ("mark self done, pop next, trigger it") so an individual is never double-dequeued under PubSub at-least-once redelivery.

Two layers, two jobs: the **semaphore** guarantees we never exceed Movebank's hard limit; the **queue** keeps backfill from monopolizing it.

## Watermarks & the backfill/pull boundary

Backfill covers `[start, end)`; the steady-state pull covers `[end, ∞)`. They must partition the timeline with **no overlap and no gap**, because the transform's `loaded_at` field intentionally defeats Gundi's duplicate-drop — so any window fetched by both actions becomes duplicate observations in EarthRanger.

- **`end`**, frozen per individual at job start:
  - If the individual already has a steady-state pull cursor, `end` = that cursor's timestamp (backfill fills history *behind* the pull).
  - If no cursor exists yet (backfill is the initial load), `end` = job start `T`, and the parent seeds the pull cursor to `T` so the pull only ever handles `[T, ∞)`. One-time initialization, not ongoing shared state.
- **Backfill watermark** is separate Redis state (keyed by `job_id` + individual + sensor), so it never collides with the steady-state cursor. It advances forward from `start` toward `end`; the individual is done when it reaches `end`.
- **Full-cursor hand-off.** When backfill finalizes an individual, it writes the steady-state pull cursor with **both** `latest_timestamp` **and** `highest_event_id` per sensor — not just the timestamp. This lets the pull's existing `event_id` filter absorb any time-window re-read at the boundary without re-emitting events backfill already sent.

### Accessory-measurements boundary window — CRITICAL

**Why this matters:** accessory-measurements records can arrive at Movebank **several hours after their event timestamp**. The device buffers them and uploads them late; Movebank stores them under their original (old) timestamp but they only become queryable hours later. The `movebank-client`'s built-in 60-minute accessory look-back was sized for *mild* reordering and is **not** enough for multi-hour settling.

**The failure it prevents:** consider an accessory record timestamped `T` that arrives at Movebank 4 hours after `T`.
- A timestamp-only watermark that has already advanced past `T` will never re-query `T` → the record is **missed** (a silent data gap, not a duplicate).
- At the backfill/pull boundary the risk is sharpest: backfill sweeps `[start, end)` and finalizes; if the late record lands after backfill passed its timestamp but the pull only re-reads the last 60 minutes, neither action fetches it.

**The change:** introduce a configurable **accessory settling margin**, `ACCESSORY_SETTLING_HOURS` (default **12**), applied to the **accessory-measurements sensor only**, in **both** places:

1. **Steady-state pull** (change to existing shipped code): the accessory query re-reads back `ACCESSORY_SETTLING_HOURS` from the watermark instead of the client's 60 minutes, every run. The `event_id >= minimum_event_id` filter drops everything already emitted, so the wider re-read catches late arrivals **without producing duplicates**. GPS is unaffected (it does not arrive late) and keeps its exact-cursor behavior.
2. **Backfill boundary hand-off**: because the pull re-reads `ACCESSORY_SETTLING_HOURS` of accessory history and the hand-off carries the `highest_event_id`, any accessory record that settles into the boundary region after backfill finished is picked up by the pull's next run and de-duplicated by event id.

**Cost & bound:** the steady-state accessory query is wider every run (12 h vs 1 h of re-read), which means more rows scanned and filtered per run — acceptable because accessory volume is far lower than GPS and the event-id filter keeps the emitted set exact. `ACCESSORY_SETTLING_HOURS` is configurable so operators of studies with slower-settling devices can raise it; the guarantee is "no accessory record later than the margin is missed." Records that settle *beyond* the margin remain a documented residual risk, recoverable by re-running backfill for the affected range.

GPS records are assumed to arrive promptly and are **not** subject to the settling margin.

## Progress reporting

Job metadata (Redis, per `job_id`) drives Activity Log records via the existing activity logger:

- **Start**: "Backfill started for study `X`: `N` individuals, range `[start, end)`."
- **Aggregate** (throttled with the existing `set_if_absent` TTL gate, ≈ every few minutes): "Backfill `Y`/`N` individuals complete, ~`M` observations." Emitted opportunistically as individuals finish, so a 500-individual job yields a readable trickle, not a flood.
- **Complete**: "Backfill finished: `N` individuals, `M` observations, elapsed `T`." Emitted by whichever worker empties the queue with `in_flight == 0`.
- **Per-individual**: no routine record; only a **warning** when an individual is abandoned after exhausting retries — so a stuck individual is visible without `2N` routine entries.

(Progress lives in the Activity Log for now; there is no dedicated progress home in the Gundi UI yet.)

## Error handling & edge cases

- **Cascade guard**: a step that fetches zero events and cannot advance its watermark does **not** re-trigger — it finalizes the individual. Reaching `end` is the normal terminal condition. Prevents infinite cascades.
- **Transient Movebank errors** (429/5xx/timeouts): the client already retries with bounded backoff; a step that still fails re-triggers itself up to a bounded `attempts` counter in job state, then logs the abandon warning and removes the individual from `in_flight` (dequeuing the next so the job keeps progressing).
- **Semaphore leak protection**: slot TTL (above) plus `finally`-release.
- **At-least-once redelivery**: watermark advancement is idempotent (a redelivered step re-fetches from the same watermark; the event-id filter dedups), and queue hand-off uses atomic Redis ops so an individual is not double-dequeued or double-triggered.
- **One active job per integration**: re-triggering `backfill` while a job is in flight is guarded (new job refused, or explicitly supersedes) rather than clobbering live job state. A fresh `job_id` per accepted run.
- **`MAX_ACTION_EXECUTION_TIME` retuning**: the step time budget is derived as ≈80% of the setting, so it tracks changes to the ack deadline.

## Testing

All in the Docker `mb-runner-test` image, `respx`-mocked Movebank, mocked Redis/Gundi, following existing `app/actions/tests` patterns.

- **Semaphore**: acquire/release; ceiling enforcement; `DECR`-on-over-limit; TTL; backoff-re-trigger when full; pull and backfill share one counter.
- **Rolling queue**: `K`-in-flight maintained; hand-off dequeues and triggers next; aggregate counters; completion detected when queue drains and `in_flight == 0`; no double-dequeue on redelivery.
- **Watermark / boundary**: backfill covers `[start, end)`; `end` resolves from an existing cursor vs. seeds `T`; hand-off writes `(latest_timestamp, highest_event_id)`; `start="all"` resolves from `timestamp_start`.
- **Accessory settling margin**: steady-state accessory query re-reads `ACCESSORY_SETTLING_HOURS`; a late-arriving accessory record within the margin is emitted exactly once (caught, not duplicated); GPS keeps exact-cursor behavior; a record beyond the margin is missed (documents the residual bound).
- **Cascade**: time-budget stop re-triggers self with advanced watermark; reaching `end` finalizes; zero-yield terminates; abandon-after-retries logs warning and dequeues.
- **Config / registration**: `backfill` registers executable; `backfill_individual` stays internal; discovery lists all actions.

## Out of scope / deferred

- GCS storage decoupling (revisit only if Gundi-side throughput is the bottleneck).
- A dedicated progress surface in the Gundi UI (Activity Log records for now).
- Cross-username coordination (semaphore is per-username; different usernames are independent budgets).
- Sensor types beyond GPS and accessory-measurements.
