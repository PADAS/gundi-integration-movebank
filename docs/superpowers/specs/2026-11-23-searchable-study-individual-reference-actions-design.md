# Searchable study and individual pickers (reference actions)

Date: 2026-11-23. Status: approved in conversation.

## Intent

Operators configuring a Movebank integration type numeric IDs into `study_id`
and `individual_ids` today. They should be able to search by name in the portal
form and pick, the way the iNaturalist integration lets them search taxa.

## Goals

- `study_id` (pull config and backfill config) and `individual_ids` (backfill
  config) render as typeahead pickers in portals that support `gundi:reference`.
- Saved values show a readable label ("Name (ID)") when the form is reopened.
- Typing a raw ID keeps working, and portals without reference support keep
  plain text fields.

## Non-goals

- No change to what is stored: values stay Movebank ID strings.
- No change to pull or backfill behaviour, job-id hashing, or the template
  code under `app/services/` outside a new cache module.
- No upstreaming of a `reference_annotation()` helper to the template here.

## Anti-goals

- Reference lookups must not starve pulls or backfills of Movebank connection
  slots, and must not add a Movebank request per keystroke.
- Credentials must never appear in reference responses, errors, or cache keys
  in clear.

## Approaches

1. **Runner-side search over a Redis-cached list (chosen).** Movebank's
   direct-read API returns the whole visible study list or one study by ID; it
   has no name search. The runner fetches the list once, caches it, and filters
   it for each query. One Movebank request per cache TTL instead of one per
   keystroke.
2. **Call Movebank on every keystroke, no cache.** Simplest code, but each
   keystroke re-downloads the full study list and takes a connection slot that
   backfills already saturate. Rejected on the first anti-goal.
3. **Plain dropdown of everything (no search).** The account can see every
   public study, likely thousands. Rejected by the user in favour of search.

## Design

Follows the iNaturalist contract (`gundi-integration-inaturalist`
`app/actions/configurations.py`, `action_list_taxa`): the annotation carries
`search={"param": "q", "min_chars": 2}` and `resolve={"param": "ids"}`; the
portal sends `q` while typing and `ids` to label saved values.

### Reference actions

`list_studies` — `ListStudiesQuery(ReferenceActionConfiguration)`:
`q: Optional[str]`, `ids: Optional[List[str]]`.

- `ids` given: return those studies in the order asked; unknown IDs are left
  out (the portal keeps showing the raw value). An ID missing from the cached
  list is looked up live with `get_study` before being dropped, so a study
  shared with the account after the list was cached still gets its label.
- `q` empty: `options=[]`, `truncated=True`.
- otherwise: case-insensitive substring match on study name and ID; an exact
  ID match sorts first, then name-prefix matches, then the rest by name.
  Return at most 50 with `truncated` set when more matched.
- Option: `value=<study id>`, `label="<name> (<id>)"`.

`list_individuals` — `ListIndividualsQuery(ReferenceActionConfiguration)`:
`study_id: Optional[str]`, `q`, `ids`.

- `study_id` empty: `options=[]` (the portal blocks the fetch anyway and shows
  "Select study_id first").
- `ids` given: those individuals, in the order asked, unknown ones left out.
- `q` empty: the first 50, sorted by label.
- otherwise: substring match on local identifier, nick name, ring ID and ID.
- Option: `value=<individual id>`, `label` from the existing `_display_name`
  rule plus the ID, `description=<taxon_canonical_name>`.

Both return `ReferenceDataResponse(...).dict()` and are not wrapped in
`@activity_logger()` (reference lookups are interactive and must not write
activity-log entries per keystroke).

### Annotations

A module-level `_reference()` helper in `app/actions/configurations.py`,
copied from iNaturalist's (action, params, search, resolve; target fixed to
`"self"`; `allow_free_text=True`; never sets `ui:widget`).

- `PullObservationsConfig.ui_schema()`: `study_id` → `list_studies`.
- `BackfillConfig.ui_schema()`: `study_id` → `list_studies`;
  `individual_ids.items` → `list_individuals` with
  `params={"study_id": {"$data": "../study_id"}}`.

Existing `ui_schema` output (field order) is merged, not replaced.

### Caching

New `app/services/reference_cache.py`, modelled on
`study_attributes_cache.py` (same Redis DB, same fail-open rules: any Redis
error or corrupt entry falls back to a live fetch).

- Study list key: normalised base URL digest + SHA-256 digest of the
  username. The list depends on what the account can see, so it must not be
  shared across accounts; the username is hashed so it is not readable in
  Redis key listings.
- Individual list key: base URL digest + username digest + study ID.
- TTL: `REFERENCE_CACHE_TTL_SECONDS`, default 600. Entries hold only the
  fields the options need (ID, name; for individuals also local identifier,
  nick name, ring ID, taxon), not the full CSV rows.
- Only a real list is cached; a failed fetch is not.

### Movebank access

- Fetches go through `MovebankClient` under `movebank_slot(username)` like
  every other call. `NoConnectionSlot` on a cache miss raises an
  `IntegrationConfigurationError`-style message ("Movebank is busy, try again
  shortly") so the portal shows its "couldn't load options" state; it never
  queues behind a backfill.
- Client purity holds: handlers unpack the auth config and call the client
  with plain values; the cache module takes plain values too.

### Errors

Wrong credentials or a study the account cannot see surface through the
runner's existing reference-action error path (no config data attached).
Matching and labelling are pure functions so they are tested without Movebank.

## Unknowns to settle while building

- **Server-side search.** Confirm against Movebank's direct-read docs that
  there is no name filter for `entity_type=study`. If one exists, prefer it
  for `q` and keep the cache only for `ids`.
- **Backfill run form.** The annotation's effect on an executable action's
  run form in the portal is unverified. It is inert if unsupported; check on
  dev after registering.
- **Study list size.** Measure the cached payload for a real account; if it
  is large (several MB), store it compressed.

## Rollout

Deploy the runner, then re-register the integration type per environment
(dev, stage, production) so the portal receives the two new actions and the
updated ui schemas. Registering before the code is live would point the
portal at actions the runner does not have.

## Testing

- Unit tests for matching, ordering, truncation, resolve order and unknown-ID
  handling, for both actions.
- Cache tests mirroring `test_study_attributes_cache`: hit, miss, Redis error,
  corrupt entry, per-account key separation.
- Configuration tests: `ui_schema()` carries the annotations with the expected
  action, params, search and resolve, and no `ui:widget`; a drift guard that
  every annotated action name is a registered reference action.
- Handler tests: second call within the TTL makes no Movebank request;
  `NoConnectionSlot` on a miss yields the busy error.
