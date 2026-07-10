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

