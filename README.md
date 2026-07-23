# gundi-integration-movebank
This is Gundi's integration for pulling data from Movebank Studies.

## Actions

- **auth** — validates Movebank credentials.
- **pull_observations** (scheduled, every 10 min) — lists the configured study's
  individuals and triggers one `pull_events_for_individual` sub-action per individual.
  `maximum_lookback_hours` (default 24) controls how far back a new individual's
  first fetch reaches — override it on a manual run to backfill history.
- **pull_events_for_individual** (internal) — fetches events for one individual with
  a separate cursor per sensor type (GPS, accessory-measurements), batches requests in
  adaptive time windows, transforms events into observations, and sends them to Gundi.
  State lives in Redis keyed by integration/action/individual.
- **backfill** (executable) — operator-triggered historical load for a study.
  Works on a fresh integration and on one that already has collection history:
  each individual is back-filled up to its recorded coverage floor
  (`coverage_start`), so backfill and the steady-state pull partition the
  timeline with no gap or overlap. Each cascade step processes bounded windows
  (`MAX_RECORDS_PER_BACKFILL_WINDOW`, adaptively shrunk down to
  `MIN_BACKFILL_WINDOW_SECONDS` on dense data) so a step always finishes inside
  the execution budget and cleanly cascades. Set `restart: true` to clear a
  stuck/previous job for the same parameters and start over. Config:
  `study_id`, optional `individual_ids`, `start` (a date or `"all"`), optional
  `backfill_max_concurrency`, `restart`.
- **backfill_events_for_individual** (internal) — self-cascading worker for one
  individual: fetches `[start, end)` in time-budgeted steps under the shared
  Movebank connection semaphore, sends to Gundi, and on completion hands the
  full `(timestamp, event_id)` cursor to `pull_events_for_individual` so
  steady-state collection continues without a gap or duplicates.

### Settings

- `MOVEBANK_MAX_CONNECTIONS` (default 25) — shared per-username Movebank
  connection ceiling (Movebank allows ~31).
- `ACCESSORY_SETTLING_HOURS` (default 12) — how far back accessory-measurements
  queries re-read, since those records can arrive hours late.
- `BACKFILL_MAX_CONCURRENCY` (default 8) — individuals in flight per backfill job.

