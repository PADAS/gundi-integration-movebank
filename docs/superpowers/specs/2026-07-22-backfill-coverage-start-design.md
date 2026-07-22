# Backfill on Already-Cursored Integrations (`coverage_start`) — Design

**Status:** approved design, pre-implementation
**Date:** 2026-07-22
**Branch:** movebank-backfill (extends PR #14)

## Problem

The backfill capability only works on a brand-new integration. Two coupled defects:

1. **Aborted backfills poison future runs.** `_seed_pull_cursor_at_end` writes a steady-state pull cursor for every queued individual at job start (to claim the boundary against a concurrent pull). If the job then aborts (e.g. the earlier `INTEGRATION_COMMANDS_TOPIC`-unset failure, or a sync-mode timeout), those seeded cursors persist. Every subsequent backfill then sees a cursor and skips the individual. Observed in dev: a study's 67 individuals were all skipped (`skipped 67 individuals that already have collection history`), so nothing was queued.
2. **No way to backfill behind an existing cursor.** The interim rule (chosen when the feature shipped) skips any individual that already has a pull cursor — "filling history behind an existing cursor is not yet supported." So an integration that has ever collected can't be backfilled.

Root cause of both: the pull cursor records only the **newest** covered point (`latest_timestamp`, `highest_event_id` per sensor). There is no record of the **oldest** point coverage begins at, so backfill has no floor to fill behind and can't tell a real cursor from an aborted seed placeholder.

## Key decisions (from brainstorming)

1. **Unifying `coverage_start` floor** (not a minimal #1-only fix): every pull cursor records the oldest timestamp its coverage begins at. Backfill fills `[start, coverage_start)` and never blanket-skips. Solves both defects.
2. **Legacy cursors** (written before `coverage_start` exists): a bare seed placeholder (all sensors `event_id == 0`) is auto-treated as `coverage_start = its timestamp` (full backfill, no manual Redis clearing). A legacy cursor with real coverage (`event_id > 0`) but no `coverage_start` is skipped with a warning (can't safely fill behind an unknown floor).

## Data model

Add one field to `IndividualState` (`app/actions/client.py`):

```python
coverage_start: Optional[datetime] = None
```

Per-individual (the pull starts all of an individual's sensors at the same first-run floor, so one field suffices). It is the oldest timestamp the individual's steady-state coverage begins at. Set once; never advanced forward.

## Producers — who stamps `coverage_start`

1. **Pull first run** — `action_pull_events_for_individual` (shipped steady-state code). When it constructs a brand-new `IndividualState` for a fresh individual (no saved state), it already computes `default_start = resolved_individual_timestamp_end − maximum_lookback_hours`. Stamp `coverage_start = default_start` on that new state before the first `set_state`. On subsequent runs (state loaded from Redis), leave `coverage_start` untouched — it is immutable for the cursor's lifetime.
2. **Backfill seed** — `_seed_pull_cursor_at_end(integration_id, study_id, ind, end)` writes the placeholder cursor with each sensor at `(end, event_id=0)`. Also set `coverage_start = end`. Semantics: the pull owns `[end, ∞)`; everything before `end` is unclaimed and backfill will cover it.
3. **Backfill finalize** — `_finalize_backfill_individual` merges the backfill watermark forward into the pull cursor. Also set `coverage_start = action_config.start`: the combined pull+backfill coverage now reaches down to the backfill's start, so a later backfill with the same/later start correctly sees an empty range.

## Consumer — backfill's per-individual `end`

Replaces the blanket skip filter in `action_backfill`. For each candidate individual, resolve `end`:

```
saved = get_state(CURSOR_STATE_ACTION_ID, source_id=ind.id)
if not saved:
    end = now
    seed_needed = True                      # fresh individual: seed cursor at now
else:
    state = IndividualState.parse_obj(saved)
    if state.coverage_start is not None:
        end = state.coverage_start
    elif all((ss.highest_event_id or 0) == 0 for ss in state.sensor_states.values()):
        # legacy seed placeholder — never really collected
        stamps = [ss.latest_timestamp for ss in state.sensor_states.values() if ss.latest_timestamp]
        end = min(stamps) if stamps else now
    else:
        # legacy real coverage, unknown floor — cannot safely fill behind it
        skip_with_warning; continue
    seed_needed = False                     # cursor already claims the boundary
start_dt = _resolve_start(action_config.start, ind)
if start_dt < end:
    queue individual with range [start_dt, end)
    if seed_needed: _seed_pull_cursor_at_end(..., end=now)   # only for fresh individuals
else:
    skip as an empty range (already covered)
```

- No blanket skip. The only skips are: legacy-real-unknown-floor, and empty range (`start >= end`).
- `job_id` remains deterministic on `(study_id, individual set, start)`.
- Fresh individuals are still seeded at `now` (unchanged boundary-claim behavior); already-cursored individuals are **not** re-seeded (their cursor already claims the boundary at its newest edge).

## Boundary correctness

Backfill fills `[start, end)` where `end = coverage_start`. The steady-state pull operates at its newest edge — for accessory, `[latest_timestamp − ACCESSORY_SETTLING_HOURS, ∞)`. These do not overlap as long as `coverage_start ≤ latest_timestamp − ACCESSORY_SETTLING_HOURS`, i.e. the pull's covered span exceeds the settling margin. With defaults (`maximum_lookback_hours = 24` ≥ `ACCESSORY_SETTLING_HOURS = 12`) this always holds on first run.

**Edge — `maximum_lookback_hours < ACCESSORY_SETTLING_HOURS`:** the pull's accessory settling re-read can dip below `coverage_start` into backfill's range and re-emit accessory rows there (duplicates, since `loaded_at` defeats dedup). This only occurs under that specific misconfiguration; the defaults (`24 ≥ 12`) are safe. Handling: treat `maximum_lookback_hours ≥ ACCESSORY_SETTLING_HOURS` as a documented expectation, and when backfill resolves `end` from an existing cursor whose span is below the settling margin (`coverage_start > latest_timestamp − ACCESSORY_SETTLING_HOURS`), log a one-time warning for that individual noting the possible ≤`(settling − lookback)`h accessory overlap. No per-sensor `end` clamp — that would require threading the pull's per-sensor `latest` into the sub-action, unwarranted for a misconfiguration-only edge (YAGNI). GPS is unaffected (no settling margin), and the finalize event-id hand-off (unchanged) dedups the finalized boundary regardless.

## What is removed / simplified

- The blanket "skip existing-cursor individuals" filter is deleted — replaced by the `end`-from-`coverage_start` resolution above.
- **No explicit seed-rollback** (the original standalone #1 fix, and the whole-branch review's Minor #5) is needed. A seed placeholder carries `coverage_start = now`, so an aborted job's leftover cursors make a re-backfill fill full history rather than poisoning it. This removes a class of rollback bookkeeping instead of adding one.

## Error handling / edge cases

- **Legacy real cursor, no `coverage_start`:** skipped with a warning naming the individual; operator can clear its cursor to force a full backfill. (New cursors always carry `coverage_start`, so this is a one-time migration concern only.)
- **Empty range** (`start >= end`): individual already fully covered; skipped, counted separately from unknown-floor skips.
- **Idempotent seed / resume / rollback-on-dispatch-failure:** unchanged from current behavior.
- **Redelivery:** `coverage_start` writes are idempotent (same value re-written).

## Testing

Docker `mb-runner-test`, `respx`/mocked Redis, existing `app/actions/tests` patterns.

- `IndividualState` round-trips `coverage_start` (including `None`).
- Pull first run stamps `coverage_start = default_start`; a second run with saved state does **not** change it.
- `_seed_pull_cursor_at_end` writes `coverage_start = end`; `_finalize_backfill_individual` sets `coverage_start = start`.
- Backfill `end` resolution: fresh → now (+ seeded); `coverage_start` present → that value; legacy all-event_id-0 → its timestamp; legacy real → skip-with-warning; `start >= end` → empty-range skip.
- `[start, coverage_start)` partition emits no boundary duplicates (event-id hand-off) under default settings; the `lookback < settling` case logs the overlap warning.
- Regression: existing pull and backfill tests remain green after the first-run stamp and the skip-filter replacement.

## Files

- `app/actions/client.py` — add `coverage_start` to `IndividualState`.
- `app/actions/handlers.py` — stamp in pull first-run; stamp in `_seed_pull_cursor_at_end`; stamp in `_finalize_backfill_individual`; replace the skip filter in `action_backfill` with the `end`-resolution logic; `lookback < settling` warning.
- `app/actions/tests/*` — coverage per above.

Scope: one implementation plan (~4–5 TDD tasks).

## Out of scope

- Per-sensor `coverage_start` (per-individual suffices; all sensors share the first-run floor).
- A UI/action to reset or inspect an individual's coverage.
- Backfilling to an *earlier* start than a prior completed backfill (would need `coverage_start` to extend backward on finalize; currently set to the latest backfill's start).
