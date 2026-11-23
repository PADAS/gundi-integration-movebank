# Searchable Study and Individual Pickers Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a portal user search and pick the Movebank study and individuals by name instead of typing IDs.

**Architecture:** Two stateless reference actions, `list_studies` and `list_individuals`, answer the portal's typeahead (`q`) and label-saved-values (`ids`) requests. Movebank has no name search, so the runner fetches each list once, caches a trimmed copy in Redis per account, and filters it in pure functions. Config models opt in through a `gundi:reference` entry in `ui_schema()`.

**Tech Stack:** Python 3.10, FastAPI, Pydantic v1, `redis.asyncio`, movebank-client 1.3.2, pytest + pytest-asyncio + pytest-mock.

**Spec:** `docs/superpowers/specs/2026-11-23-searchable-study-individual-reference-actions-design.md`

## Global Constraints

- Branch: `feat/searchable-study-individual-reference-actions` (already created, stacked on `chore/sync-upstream-142`).
- Run tests with `.venv/bin/python -m pytest app -q`. Full suite before every commit.
- Pydantic v1 idioms only (`.dict()`, `parse_obj`, `__fields__`).
- Stored values stay Movebank ID strings. `BackfillConfig`'s validators and job-id hashing are untouched.
- Annotation key is the literal `"gundi:reference"`; never set `ui:widget` beside it; `allow_free_text` is `True`; `target` is `"self"`.
- Search annotation is exactly `{"param": "q", "min_chars": 2}`; resolve is exactly `{"param": "ids"}`.
- Result cap: 50 options. Cache TTL setting: `REFERENCE_CACHE_TTL_SECONDS`, default 600.
- Reference handlers are not wrapped in `@activity_logger()`.
- No edits to template files under `app/services/` or `app/actions/core.py`; new code goes in new fork-owned modules.
- Third-party client code takes plain values, never a Gundi config model.
- Usernames and passwords never appear in cache keys in clear, in cache values, in option fields, or in error messages.
- Every commit message ends with `Co-Authored-By: Claude Fable 5.2 <noreply@anthropic.com>`.

## Review Focus

1. **Query with surrounding whitespace or mixed case** (`"  VULTURE "`): matches as `vulture`; whitespace-only counts as empty.
2. **`ids` or `q` arriving as a scalar, a comma-separated string, or with repeats** (`"12,13"`, `12`, `["12","12"]`): coerced to a deduped list of ID strings, order kept; never a 422.
3. **Rows with empty names** (an individual with no local identifier, nick name or ring ID; a study with an empty name): still offered, labelled by ID alone, and never raise.
4. **A saved study ID the cached list does not contain**: resolved with one live `get_study` call; still unknown means left out, not an error.
5. **Redis down or a corrupt cache entry**: the lookup falls back to a live Movebank fetch and still answers.

## File Structure

| File | Responsibility |
|---|---|
| `app/services/reference_cache.py` (new) | Redis get/set of trimmed study and individual lists, per account. Fail-open. |
| `app/actions/reference_data.py` (new) | Pure functions: trim rows, match, order, cap, build options. No I/O. |
| `app/actions/configurations.py` | `_reference()` helper, `ListStudiesQuery`, `ListIndividualsQuery`, `ui_schema()` overrides. |
| `app/actions/handlers.py` | `action_list_studies`, `action_list_individuals`: auth, cache, Movebank fetch under a slot. |
| `app/settings/integration.py` | `REFERENCE_CACHE_TTL_SECONDS`. |
| `README.md` | Actions and Settings entries. |

---

### Task 1: Settle the server-side search unknown

**Files:** none changed unless the answer is yes.

- [ ] **Step 1:** Read Movebank's direct-read API documentation (`https://github.com/movebank/movebank-api-doc/blob/master/movebank-api.md`) for `entity_type=study` and `entity_type=individual` request parameters. Record in the PR description whether a name filter exists.
- [ ] **Step 2:** If no name filter exists (expected), continue with Task 2 unchanged. If one exists, stop and report back before building: the spec says to prefer it for `q`, which changes Tasks 3 and 5.

---

### Task 2: Reference cache

**Files:**
- Create: `app/services/reference_cache.py`
- Modify: `app/settings/integration.py` (add `REFERENCE_CACHE_TTL_SECONDS = env.int("REFERENCE_CACHE_TTL_SECONDS", 600)`)
- Test: `app/services/tests/test_reference_cache.py`

**Interfaces:**
- Produces:
  - `studies_key(base_url: str, username: str) -> str` → `movebank:reference:studies:<url digest>:<username digest>`
  - `individuals_key(base_url: str, username: str, study_id: str) -> str` → `movebank:reference:individuals:<url digest>:<username digest>:<study_id>`
  - `async get_cached_list(key: str) -> Optional[list]` (None means "fetch live")
  - `async set_cached_list(key: str, rows: list) -> None`

Model the module on `app/services/study_attributes_cache.py`: same Redis DB (`REDIS_STATE_DB`), same URL normalisation (strip, drop trailing slash), 16-hex SHA-256 digests, JSON values, and the same fail-open rules. The username is lower-cased and stripped before hashing.

- [ ] **Step 1: Write the failing tests** (mirror the fixtures in `app/services/tests/test_study_attributes_cache.py`):

```python
def test_keys_separate_accounts_and_hide_the_username():
    a = studies_key("https://www.movebank.org", "alice")
    b = studies_key("https://www.movebank.org/", " Alice ")
    c = studies_key("https://www.movebank.org", "bob")
    assert a == b and a != c
    assert "alice" not in a.lower()

def test_individuals_key_includes_the_study():
    assert individuals_key("u", "alice", "1").endswith(":1")
    assert individuals_key("u", "alice", "1") != individuals_key("u", "alice", "2")

async def test_roundtrip_uses_the_ttl_setting(fake_redis): ...
    # set_cached_list(key, [{"id": "1"}]) then get_cached_list(key) == [{"id": "1"}]
    # and the write carried ex=settings.REFERENCE_CACHE_TTL_SECONDS

async def test_miss_returns_none(fake_redis): ...
async def test_redis_read_error_returns_none(fake_redis): ...
async def test_redis_write_error_is_swallowed(fake_redis): ...
async def test_corrupt_or_non_list_entry_returns_none(fake_redis): ...   # b"not json", b'{"a": 1}'
async def test_empty_list_is_a_real_answer(fake_redis): ...              # [] round-trips as [], not None
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest app/services/tests/test_reference_cache.py -q`. Expected: FAIL, module not found.
- [ ] **Step 3:** Implement the four functions and the setting.
- [ ] **Step 4:** Run the same command. Expected: PASS. Then the full suite.
- [ ] **Step 5:** Commit: `Reference cache: per-account Redis lists for study and individual lookups`.

---

### Task 3: Matching and option building (pure)

**Files:**
- Create: `app/actions/reference_data.py`
- Test: `app/actions/tests/test_reference_data.py`

**Interfaces:**
- Consumes: `ReferenceOption`, `ReferenceDataResponse` from `app.actions.core`.
- Produces:
  - `MAX_OPTIONS = 50`
  - `trim_study(row: dict) -> dict` → `{"id", "name"}` (strings; missing → `""`)
  - `trim_individual(row: dict) -> dict` → `{"id", "local_identifier", "nick_name", "ring_id", "taxon"}` (taxon from `taxon_canonical_name`)
  - `study_option(study: dict) -> ReferenceOption`
  - `individual_option(ind: dict) -> ReferenceOption`
  - `search_studies(studies: list, q: Optional[str]) -> dict`
  - `search_individuals(individuals: list, q: Optional[str]) -> dict`
  - `resolve(rows: list, ids: list, to_option) -> dict`

  The three search/resolve functions return `ReferenceDataResponse(...).dict()`.

Rules the tests pin:
- Labels: study `"<name> (<id>)"`, or `"<id>"` when the name is empty. Individual `"<display> (<id>)"` where display is `local_identifier or nick_name or ring_id` (the rule in `handlers._display_name`), or `"<id>"` when all are empty; `description` is the taxon or `None`.
- Query is stripped and compared case-insensitively as a substring.
- Study search fields: name, id. Individual search fields: local_identifier, nick_name, ring_id, id.
- Order: exact ID match first, then rows whose label starts with the query, then the rest; ties by label, case-insensitive.
- Cap at `MAX_OPTIONS`; `truncated` is true only when more matched than were returned.
- Empty query: studies → no options, `truncated=True`; individuals → first 50 by label, `truncated` per the cap.
- `resolve` keeps the order of `ids`, skips unknown IDs, `truncated=False`.

- [ ] **Step 1: Write the failing tests:**

```python
STUDIES = [{"id": "10", "name": "Mara Vultures"}, {"id": "2", "name": "vulture tracking"},
           {"id": "33", "name": "Elephants"}, {"id": "44", "name": ""}]

def test_study_label_and_value():
    assert study_option(STUDIES[0]).dict(exclude_none=True) == {"value": "10", "label": "Mara Vultures (10)"}

def test_study_without_a_name_is_labelled_by_id():
    assert study_option(STUDIES[3]).label == "44"

def test_search_is_case_insensitive_and_trims_whitespace():
    assert [o["value"] for o in search_studies(STUDIES, "  VULTURE ")["options"]] == ["2", "10"]
    # "vulture tracking" starts with the query, so it sorts ahead of "Mara Vultures"

def test_exact_id_match_sorts_first():
    studies = STUDIES + [{"id": "210", "name": "Aardvarks"}]
    assert [o["value"] for o in search_studies(studies, "2")["options"]][0] == "2"

def test_empty_or_blank_study_query_returns_nothing_and_says_truncated():
    for q in (None, "", "   "):
        assert search_studies(STUDIES, q) == {"options": [], "cache_ttl_seconds": 300, "truncated": True}

def test_study_search_caps_at_50_and_flags_truncation():
    many = [{"id": str(i), "name": f"Study {i:03d}"} for i in range(60)]
    out = search_studies(many, "study")
    assert len(out["options"]) == 50 and out["truncated"] is True
    assert search_studies(many[:50], "study")["truncated"] is False

def test_individual_label_falls_back_through_names_then_id():
    assert individual_option({"id": "7", "local_identifier": "", "nick_name": "Kito", "ring_id": "R1", "taxon": "Gyps africanus"}).dict() == \
        {"value": "7", "label": "Kito (7)", "description": "Gyps africanus", "group": None}
    assert individual_option({"id": "8", "local_identifier": "", "nick_name": "", "ring_id": "", "taxon": ""}).label == "8"

def test_individual_search_matches_ring_id_and_id(): ...
def test_empty_individual_query_returns_the_first_50_by_label(): ...   # 60 rows → 50, truncated True

def test_resolve_keeps_request_order_and_skips_unknown_ids():
    out = resolve(STUDIES, ["33", "999", "10"], study_option)
    assert [o["value"] for o in out["options"]] == ["33", "10"] and out["truncated"] is False

def test_trim_helpers_tolerate_missing_columns():
    assert trim_study({"id": 5}) == {"id": "5", "name": ""}
    assert trim_individual({"id": "9"})["local_identifier"] == ""
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest app/actions/tests/test_reference_data.py -q`. Expected: FAIL, module not found.
- [ ] **Step 3:** Implement. One private `_search(rows, q, fields, to_option, *, empty_query_lists_all: bool)` behind both search functions.
- [ ] **Step 4:** Run the file, then the full suite. Expected: PASS.
- [ ] **Step 5:** Commit: `Reference data: match, order and label studies and individuals`.

---

### Task 4: Query models and form annotations

**Files:**
- Modify: `app/actions/configurations.py`
- Test: `app/actions/tests/test_configurations.py` (append)

**Interfaces:**
- Produces:
  - `_reference(action: str, params: Optional[dict] = None, search: Optional[dict] = None, resolve: Optional[dict] = None) -> dict` (same output shape as `gundi-integration-inaturalist/app/actions/configurations.py::_reference`, target fixed to `"self"`)
  - `ListStudiesQuery(ReferenceActionConfiguration)`: `q: Optional[str] = None`, `ids: Optional[List[str]] = None`
  - `ListIndividualsQuery(ReferenceActionConfiguration)`: `study_id: Optional[str] = None`, `q`, `ids` as above
  - `PullObservationsConfig.ui_schema()` and `BackfillConfig.ui_schema()` classmethod overrides

`ids` uses a `pre=True` validator shared by both models that accepts a list, a scalar or a comma-separated string and returns a deduped, order-preserving list of stripped ID strings (`None`/empty stays `None`). `study_id` is coerced to a stripped string by the same style of validator. Overrides merge into `super().ui_schema()` output; they do not replace it.

- [ ] **Step 1: Write the failing tests:**

```python
STUDY_REF = {"action": "list_studies", "target": "self", "params": {}, "allow_free_text": True,
             "search": {"param": "q", "min_chars": 2}, "resolve": {"param": "ids"}}

def test_pull_config_study_id_is_a_searchable_reference():
    ui = PullObservationsConfig.ui_schema()
    assert ui["study_id"]["gundi:reference"] == STUDY_REF
    assert "ui:widget" not in ui["study_id"]

def test_backfill_config_references_and_keeps_its_field_order():
    ui = BackfillConfig.ui_schema()
    assert ui["study_id"]["gundi:reference"] == STUDY_REF
    assert ui["individual_ids"]["items"]["gundi:reference"] == {
        "action": "list_individuals", "target": "self",
        "params": {"study_id": {"$data": "../study_id"}}, "allow_free_text": True,
        "search": {"param": "q", "min_chars": 2}, "resolve": {"param": "ids"},
    }
    assert ui["ui:order"] == ["study_id", "individual_ids", "start", "backfill_max_concurrency", "dry_run", "restart", "cancel"]

@pytest.mark.parametrize("raw,expected", [
    ("12,13", ["12", "13"]), (12, ["12"]), (["12", "12", " 13 "], ["12", "13"]), (None, None), ([], None), ("", None),
])
def test_query_ids_are_coerced(raw, expected):
    assert ListStudiesQuery(ids=raw).ids == expected
    assert ListIndividualsQuery(study_id=1, ids=raw).ids == expected

def test_queries_accept_no_params_at_all():
    assert ListStudiesQuery().q is None
    assert ListIndividualsQuery().study_id is None

def test_every_annotated_action_is_a_registered_reference_action():
    # drift guard: walk both ui schemas for "gundi:reference" nodes, and assert each
    # node's "action" is a key of discover_actions("app.actions.handlers", "action_")
    # whose config model subclasses ReferenceActionConfiguration.
    ...
```

Today `BackfillConfig.ui_schema()` returns only `{"ui:order": [...]}` and `PullObservationsConfig.ui_schema()` returns `{}`.

- [ ] **Step 2:** Run `.venv/bin/python -m pytest app/actions/tests/test_configurations.py -q`. Expected: the new tests FAIL (the drift guard fails until Task 5; mark it `xfail(strict=True)` here and remove the mark in Task 5).
- [ ] **Step 3:** Implement the helper, the two models and the two overrides.
- [ ] **Step 4:** Run the file, then the full suite. Expected: PASS (drift guard xfail).
- [ ] **Step 5:** Commit: `Config forms: annotate study and individual fields as searchable references`.

---

### Task 5: Reference handlers

**Files:**
- Modify: `app/actions/handlers.py` (append after `action_auth`; import the two query models, `reference_data`, `reference_cache`, `IntegrationConfigurationError`)
- Modify: `app/actions/tests/test_configurations.py` (remove the `xfail` from the drift guard)
- Test: `app/actions/tests/test_reference_actions.py` (new)

**Interfaces:**
- Consumes: Task 2's four cache functions; Task 3's `trim_*`, `search_*`, `resolve`, `*_option`; Task 4's query models; `client.get_auth_config`, `client.MovebankClient`, `movebank_slot`, `NoConnectionSlot`.
- Produces:
  - `@action_title("Search Movebank Studies") async def action_list_studies(integration, action_config: ListStudiesQuery) -> dict`
  - `@action_title("Search Individuals in a Study") async def action_list_individuals(integration, action_config: ListIndividualsQuery) -> dict`
  - `BUSY_MESSAGE = "Movebank is busy right now. Try again shortly."`

Behaviour:
- Both load the trimmed list through one private helper: cache hit returns it; on a miss, fetch from Movebank inside `movebank_slot(username)`, trim, cache, return. `NoConnectionSlot` raises `IntegrationConfigurationError(BUSY_MESSAGE)` and caches nothing.
- `action_list_studies`: with `ids`, resolve from the list; for each requested ID not in the list, call `get_study(study_id=...)` once (same slot rule), and include it if Movebank returns a row. A live-resolved study is not written back to the cached list. Without `ids`, `search_studies(list, q)`.
- `action_list_individuals`: no `study_id` → `ReferenceDataResponse(options=[]).dict()` without touching Movebank or Redis. Otherwise `ids` → `resolve`, else `search_individuals`.
- Log the integration ID and study ID only; never the query's auth values.

- [ ] **Step 1: Write the failing tests** (reuse `app/actions/tests/conftest.py` fixtures; patch `app.actions.handlers.client.MovebankClient` and the cache functions as the existing handler tests patch their collaborators):

```python
async def test_list_studies_searches_the_fetched_list_and_caches_it(...):
    # cache miss; get_studies returns [{"id": "10", "name": "Mara Vultures", ...extra columns}]
    # result options == [{"value": "10", "label": "Mara Vultures (10)", "description": None, "group": None}]
    # set_cached_list called once with [{"id": "10", "name": "Mara Vultures"}]

async def test_list_studies_cache_hit_makes_no_movebank_request(...):
    # get_cached_list returns a list; MovebankClient never constructed; movebank_slot not entered

async def test_list_studies_empty_query_returns_nothing_truncated(...)

async def test_list_studies_resolves_saved_ids_in_order(...)

async def test_list_studies_resolves_an_id_missing_from_the_cache_with_one_live_lookup(...):
    # cached list lacks "77"; get_study returns {"id": "77", "name": "New Study"}
    # options carry "77"; get_study awaited once with study_id="77"; set_cached_list not called

async def test_list_studies_unknown_id_is_left_out_not_an_error(...):   # get_study returns None

async def test_list_studies_when_movebank_is_busy_raises_the_busy_message(...):
    # movebank_slot raises NoConnectionSlot on a cache miss
    # pytest.raises(IntegrationConfigurationError, match="Movebank is busy"); set_cached_list not called

async def test_list_studies_falls_back_to_live_fetch_when_the_cache_is_unavailable(...):
    # get_cached_list returns None and set_cached_list is a no-op; the answer still comes back

async def test_list_individuals_without_a_study_returns_empty_and_touches_nothing(...)

async def test_list_individuals_is_keyed_by_study_and_searches(...):
    # individuals_key called with the study id; get_individuals_by_study(study_id="5")
    # q="kit" returns the "Kito" row with description "Gyps africanus"

async def test_list_individuals_resolves_saved_ids(...)

async def test_reference_responses_and_cache_writes_carry_no_credentials(...):
    # json.dumps of the result and of every set_cached_list / key argument
    # contains neither the username nor the password from the auth config

def test_reference_handlers_are_not_activity_logged():
    # neither handler is wrapped: mock_activity_publish is not awaited during a successful call
```

- [ ] **Step 2:** Run `.venv/bin/python -m pytest app/actions/tests/test_reference_actions.py -q`. Expected: FAIL, handlers not defined.
- [ ] **Step 3:** Implement the two handlers and the shared loader; remove the drift-guard `xfail`.
- [ ] **Step 4:** Run the file, `test_configurations.py`, then the full suite. Expected: PASS.
- [ ] **Step 5:** Check registration: add (or extend) a test in `app/actions/tests/test_reference_actions.py` that `discover_actions("app.actions.handlers", "action_")` contains `list_studies` and `list_individuals` with the two query models. Run. Expected: PASS.
- [ ] **Step 6:** Commit: `Reference actions: searchable study and individual lookups`.

---

### Task 6: Docs and rollout notes

**Files:**
- Modify: `README.md`

- [ ] **Step 1:** Under `## Actions`, add `list_studies` and `list_individuals` (reference, portal-invoked): what each searches, the 50-option cap, that an empty study query returns nothing, and that typing an ID still works. Under `### Settings`, add `REFERENCE_CACHE_TTL_SECONDS` (default 600) with one line on why the cache exists (no Movebank request per keystroke; lookups do not compete with backfills for connection slots).
- [ ] **Step 2:** Add a short "Rollout" note to the PR description, not the README: deploy first, then re-register the integration type in dev, stage and production; on dev, check whether the backfill run form shows the pickers and record the answer; note the cached study-list size for one real account (compress if it is several MB, per the spec).
- [ ] **Step 3:** Full suite, then commit: `Docs: reference actions and the reference cache setting`.
