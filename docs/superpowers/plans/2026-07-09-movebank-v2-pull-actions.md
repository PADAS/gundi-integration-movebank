# Movebank v2 Pull Actions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Port the proven Movebank v1 pull logic (per-sensor cursors, adaptive batching, transform quirks) into the v2 action runner using per-individual sub-actions, after fixing event-fetch bugs in `movebank-client` and releasing 1.3.0.

**Architecture:** Part 1 fixes `movebank-client` (repo: `/Users/chrisdo/padas/movebank-client`): the accessory-measurements sensor is silently never queried (request block sits inside a GPS-only `else`), the GPS attributes list is missing common attributes, and 429 backoff can retry forever and crashes on HTTP-date `Retry-After` headers. Part 2 (repo: `/Users/chrisdo/padas/gundi-integration-movebank`) adds two actions: `pull_observations` (portal-visible, scheduled, lists a study's individuals and triggers one sub-action per individual) and `pull_events_for_individual` (internal sub-action that manages per-sensor cursors in Redis, fetches events in adaptive time windows, transforms them, and sends observations to Gundi). Per-sensor cursors live in the integration (one client call per sensor type — same HTTP profile as the old function), so the library keeps its backward-compatible signature.

**Tech Stack:** Python 3.10 (run everything in Docker — local venvs in both repos are broken/stale), pydantic v1, httpx/respx, backoff, pytest + pytest-asyncio + pytest-mock, poetry (movebank-client only).

## Global Constraints

- Python 3.10 only. Both repos' local venvs are unusable. Run tests via the Docker commands given in each task, verbatim.
- pydantic **v1** syntax everywhere (`validator`, `root_validator`, `.parse_obj()`, `.dict()`, `.json()`) — matching both codebases.
- Old-function logic constants are load-bearing production values — copy them exactly: quiet period `3600` s, high-frequency threshold `5000` events, max `2000` records per individual per run, default batch window `5` days, accessory query overlap `60` minutes, non-GPS default handled via lookback config default `24` hours.
- Do not modify anything under `app/services/` in gundi-integration-movebank — that is template code synced from upstream.
- `movebank-client` local checkout is 6 commits behind `origin/main` and has a dirty `movebank_client/cli.py`. Always branch from `origin/main` (v1.2.0), never from local `main`.
- gundi-integration-movebank work branches from `sync-20260709` (PR #10, the template sync). If PR #10 has merged, branch from `main` instead.
- Commit messages: plain `feat:`/`fix:`/`test:` style. If the user provides a GUNDI ticket ID, prefix it (`GUNDI-XXXX: ...`) instead.

---

## Part 1 — movebank-client 1.3.0

All Part 1 tasks run in `/Users/chrisdo/padas/movebank-client`.

**Docker test command** (used by every Part 1 task; first run downloads images, ~1–2 min):

```bash
docker run --rm -v "$PWD":/work -w /work python:3.10-slim bash -c \
  "pip install -q -e . 'marshmallow<4' aiofiles pytest pytest-asyncio respx && python -m pytest tests -q"
```

Expected baseline: all existing tests pass.

### Task 1: Branch and baseline

**Files:** none created; git state only.

**Interfaces:**
- Consumes: `origin/main` at v1.2.0.
- Produces: branch `fix/event-fetch-per-sensor` with a green baseline; later tasks commit onto it.

- [ ] **Step 1: Stash local dirt and branch from origin/main**

```bash
cd /Users/chrisdo/padas/movebank-client
git stash push -m "pre-1.3.0 local cli.py edits" movebank_client/cli.py
git fetch origin
git checkout -b fix/event-fetch-per-sensor origin/main
```

Expected: `Switched to a new branch 'fix/event-fetch-per-sensor'`. `git log -1 --oneline` shows `66c90f9 Merge pull request #6 ...`.

- [ ] **Step 2: Run baseline test suite**

Run the Docker test command from the Part 1 header.
Expected: PASS (all existing tests green). If anything fails at baseline, STOP and report — do not proceed on a red baseline.

### Task 2: Harden 429 handling

**Files:**
- Modify: `movebank_client/client.py` (module-level functions above `class MovebankClient`, and the first decorator on `_call_api`, currently at lines 90–95 of origin/main)
- Test: `tests/client/test_rate_limiting.py` (create)

**Interfaces:**
- Consumes: existing `_call_api` decorator stack.
- Produces: module-level `parse_retry_after(response, default=15) -> int`, `on_backoff_429(details)`, `on_giveup_429(details)` in `movebank_client.client`; `_call_api` retries 429 at most 3 tries / 120 s total.

- [ ] **Step 1: Write the failing tests**

Create `tests/client/test_rate_limiting.py`:

```python
import httpx
import pytest
import respx

from movebank_client.client import parse_retry_after


def test_parse_retry_after_integer_seconds():
    response = httpx.Response(429, headers={"Retry-After": "30"})
    assert parse_retry_after(response) == 30


def test_parse_retry_after_missing_header_uses_default():
    response = httpx.Response(429)
    assert parse_retry_after(response) == 15
    assert parse_retry_after(response, default=5) == 5


def test_parse_retry_after_http_date_falls_back_to_default():
    response = httpx.Response(429, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
    assert parse_retry_after(response) == 15


@pytest.mark.asyncio
async def test_429_is_retried_then_succeeds(movebank_client, mock_movebank_get_individuals_by_study_response):
    async with respx.mock(assert_all_called=False) as movebank_api_mock:
        route = movebank_api_mock.get(movebank_client.direct_read_endpoint).mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "0"}),
                httpx.Response(200, content=mock_movebank_get_individuals_by_study_response),
            ]
        )
        async with movebank_client as client:
            individuals = await client.get_individuals_by_study(study_id=1234567890)
        assert len(individuals) == 2
        assert route.call_count == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run the Docker test command.
Expected: FAIL with `ImportError: cannot import name 'parse_retry_after'`.

- [ ] **Step 3: Implement**

In `movebank_client/client.py`, add above `class MovebankClient` (after the existing `on_licenseterms_response` function):

```python
def parse_retry_after(response, default=15):
    """Parse Retry-After header, handling both integer seconds and HTTP-date formats."""
    retry_after = response.headers.get("Retry-After")
    if retry_after is None:
        return default
    try:
        return int(retry_after)
    except ValueError:
        # Could be HTTP-date format; fall back to the default.
        logger.warning(f"Unexpected Retry-After format: {retry_after}, using default {default}s")
        return default


def on_backoff_429(details):
    wait = details.get('wait', 0)
    tries = details.get('tries', 0)
    logger.warning(f"Rate limited (429). Attempt {tries}, waiting {wait:.1f}s before retry.")


def on_giveup_429(details):
    tries = details.get('tries', 0)
    elapsed = details.get('elapsed', 0)
    logger.error(f"Rate limit retries exhausted after {tries} attempts over {elapsed:.1f}s")
```

Replace the first decorator on `_call_api` — currently:

```python
    @backoff.on_predicate(
        backoff.runtime,
        predicate=lambda r: r.status_code == 429,
        value=lambda r: int(r.headers.get("Retry-After", 15)),
        jitter=None,
    )
```

with:

```python
    @backoff.on_predicate(
        backoff.runtime,
        predicate=lambda r: r.status_code == 429,
        value=parse_retry_after,
        jitter=None,
        max_tries=3,
        max_time=120,
        on_backoff=on_backoff_429,
        on_giveup=on_giveup_429,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run the Docker test command.
Expected: PASS, including all pre-existing tests.

- [ ] **Step 5: Commit**

```bash
git add movebank_client/client.py tests/client/test_rate_limiting.py
git commit -m "fix: bound 429 retries and tolerate HTTP-date Retry-After headers"
```

### Task 3: Fix `get_individual_events_by_time`

**Files:**
- Modify: `movebank_client/client.py:198-244` (the `get_individual_events_by_time` method) and the class constants block
- Modify: `tests/conftest.py` (add fixtures)
- Test: `tests/client/test_get_individual_events.py` (create)

**Interfaces:**
- Consumes: `_call_api`, `get_study_attributes` (unchanged).
- Produces: `get_individual_events_by_time(*, study_id, individual_id, timestamp_start: datetime, timestamp_end: datetime, sensor_type_ids: List[int] = [653], minimum_event_id: int = 0)` — same signature as 1.2.0, but: accessory-measurements sensors are actually queried (with 60-min overlap and `attributes=all`), and GPS queries union study attributes with `MovebankClient.GPS_COMMON_ATTRIBUTES`. Part 2 calls this once per sensor type.

- [ ] **Step 1: Add test fixtures**

Append to `tests/conftest.py`:

```python
@pytest.fixture
def mock_movebank_events_response():
    return (
        b'event_id,timestamp,location_lat,location_long,individual_id,sensor_type_id\r\n'
        b'100,2026-01-01 10:00:00.000,1.5,2.5,111,653\r\n'
        b'101,2026-01-01 11:00:00.000,1.6,2.6,111,653\r\n'
    )


@pytest.fixture
def mock_movebank_accessory_events_response():
    return (
        b'event_id,timestamp,individual_id,sensor_type_id,battery_charge_percent\r\n'
        b'200,2026-01-01 10:30:00.000,111,7842954,88\r\n'
    )


@pytest.fixture
def mock_movebank_study_attributes_response():
    return (
        b'short_name,sensor_type_id\r\n'
        b'location_lat,653\r\n'
        b'location_long,653\r\n'
        b'ground_speed,653\r\n'
    )
```

- [ ] **Step 2: Write the failing tests**

Create `tests/client/test_get_individual_events.py`:

```python
from datetime import datetime, timezone

import httpx
import pytest
import respx


START = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
END = datetime(2026, 1, 2, 0, 0, 0, tzinfo=timezone.utc)


def _mock_api(movebank_api_mock, movebank_client, events_response, study_attributes_response):
    def side_effect(request):
        params = dict(request.url.params)
        if params.get("entity_type") == "study_attribute":
            return httpx.Response(200, content=study_attributes_response)
        return httpx.Response(200, content=events_response)

    return movebank_api_mock.get(movebank_client.direct_read_endpoint).mock(side_effect=side_effect)


@pytest.mark.asyncio
async def test_accessory_sensor_is_queried_with_overlap(
        movebank_client, mock_movebank_accessory_events_response, mock_movebank_study_attributes_response
):
    async with respx.mock(assert_all_called=False) as movebank_api_mock:
        route = _mock_api(movebank_api_mock, movebank_client,
                          mock_movebank_accessory_events_response, mock_movebank_study_attributes_response)
        async with movebank_client as client:
            events = [e async for e in client.get_individual_events_by_time(
                study_id="12345", individual_id="111",
                timestamp_start=START, timestamp_end=END,
                sensor_type_ids=[client.SENSOR_TYPE_ACCESSORY_MEASUREMENTS],
            )]

        assert len(events) == 1
        assert events[0]["event_id"] == "200"
        event_calls = [c for c in route.calls if dict(c.request.url.params).get("entity_type") == "event"]
        assert len(event_calls) == 1
        params = dict(event_calls[0].request.url.params)
        assert params["sensor_type_id"] == "7842954"
        # 60-minute overlap: 2026-01-01T00:00 becomes 2025-12-31T23:00
        assert params["timestamp_start"] == "20251231230000000"
        assert params["attributes"] == "all"


@pytest.mark.asyncio
async def test_gps_attributes_union_study_and_common(
        movebank_client, mock_movebank_events_response, mock_movebank_study_attributes_response
):
    async with respx.mock(assert_all_called=False) as movebank_api_mock:
        route = _mock_api(movebank_api_mock, movebank_client,
                          mock_movebank_events_response, mock_movebank_study_attributes_response)
        async with movebank_client as client:
            events = [e async for e in client.get_individual_events_by_time(
                study_id="12345", individual_id="111",
                timestamp_start=START, timestamp_end=END,
                sensor_type_ids=[client.SENSOR_TYPE_GPS],
            )]

        assert len(events) == 2
        event_calls = [c for c in route.calls if dict(c.request.url.params).get("entity_type") == "event"]
        params = dict(event_calls[0].request.url.params)
        attributes = set(params["attributes"].split(","))
        # Study-advertised attributes are present...
        assert {"location_lat", "location_long", "ground_speed"} <= attributes
        # ...and so is every common attribute the transform relies on.
        assert {"event_id", "individual_id", "deployment_id", "tag_id", "study_id",
                "sensor_type_id", "individual_local_identifier", "tag_local_identifier",
                "individual_taxon_canonical_name"} <= attributes
        assert params["timestamp_start"] == "20260101000000000"


@pytest.mark.asyncio
async def test_gps_attributes_default_to_all_without_study_metadata(
        movebank_client, mock_movebank_events_response
):
    empty_attributes = b'short_name,sensor_type_id\r\n'
    async with respx.mock(assert_all_called=False) as movebank_api_mock:
        route = _mock_api(movebank_api_mock, movebank_client,
                          mock_movebank_events_response, empty_attributes)
        async with movebank_client as client:
            [e async for e in client.get_individual_events_by_time(
                study_id="12345", individual_id="111",
                timestamp_start=START, timestamp_end=END,
                sensor_type_ids=[client.SENSOR_TYPE_GPS],
            )]
        event_calls = [c for c in route.calls if dict(c.request.url.params).get("entity_type") == "event"]
        assert dict(event_calls[0].request.url.params)["attributes"] == "all"


@pytest.mark.asyncio
async def test_minimum_event_id_filters_results(
        movebank_client, mock_movebank_events_response, mock_movebank_study_attributes_response
):
    async with respx.mock(assert_all_called=False) as movebank_api_mock:
        _mock_api(movebank_api_mock, movebank_client,
                  mock_movebank_events_response, mock_movebank_study_attributes_response)
        async with movebank_client as client:
            events = [e async for e in client.get_individual_events_by_time(
                study_id="12345", individual_id="111",
                timestamp_start=START, timestamp_end=END,
                sensor_type_ids=[client.SENSOR_TYPE_GPS],
                minimum_event_id=101,
            )]
        assert [e["event_id"] for e in events] == ["101"]
```

- [ ] **Step 3: Run tests to verify they fail**

Run the Docker test command.
Expected: `test_accessory_sensor_is_queried_with_overlap` FAILS with `assert 0 == 1` (no events — the 1.2.0 bug); `test_gps_attributes_union_study_and_common` FAILS on the missing common attributes.

- [ ] **Step 4: Implement the rewrite**

In `movebank_client/client.py`, add to the class constants block (right after `MOVEBANK_SENSOR_TYPE_LABEL_TO_ID`):

```python
    # Attributes the integrations rely on for cursoring and transforms —
    # always requested for GPS queries, in addition to study-advertised ones.
    GPS_COMMON_ATTRIBUTES = {
        'event_id',
        'individual_id',
        'deployment_id',
        'tag_id',
        'study_id',
        'sensor_type_id',
        'individual_local_identifier',
        'tag_local_identifier',
        'individual_taxon_canonical_name',
    }
```

Replace the entire `get_individual_events_by_time` method with:

```python
    async def get_individual_events_by_time(
            self, *,
            study_id: str = None,
            individual_id: str = None,
            timestamp_start: datetime = None,
            timestamp_end: datetime = None,
            sensor_type_ids: typing.List[int] = [SENSOR_TYPE_GPS],
            minimum_event_id: int = 0
    ):
        # Movebank expects timestamps as yyyyMMddHHmmssSSS
        timestamp_end_str = timestamp_end.strftime("%Y%m%d%H%M%S000") if timestamp_end else None
        url = self.direct_read_endpoint
        for sensor_type in sensor_type_ids:
            if sensor_type == self.SENSOR_TYPE_ACCESSORY_MEASUREMENTS:
                # Accessory data can arrive out of order, so query with a one-hour overlap.
                lower_bound = (timestamp_start - timedelta(minutes=60)).strftime("%Y%m%d%H%M%S000")
            else:
                lower_bound = timestamp_start.strftime("%Y%m%d%H%M%S000")

            attributes = 'all'
            if sensor_type == self.SENSOR_TYPE_GPS:
                # For GPS, narrow the attributes list using study metadata when available.
                study_attributes = await self.get_study_attributes(study_id=study_id, sensor_type_id=str(sensor_type))
                if study_attribute_names := {
                    item.get('short_name') for item in (study_attributes or [])
                    if item.get('sensor_type_id') == str(sensor_type)
                }:
                    attributes = ','.join(sorted(study_attribute_names | self.GPS_COMMON_ATTRIBUTES))

            params = (
                ('entity_type', 'event'),
                ('study_id', study_id),
                ('individual_id', individual_id),
                ('timestamp_start', lower_bound),
                ('timestamp_end', timestamp_end_str),
                ('sensor_type_id', sensor_type),
                ('attributes', attributes),
            )
            events = await self._call_api(url=url, params=params)
            if events:
                events_csv = events.content.decode('utf8')
                for item in csv.DictReader(io.StringIO(events_csv), delimiter=','):
                    if int(item.get('event_id')) >= minimum_event_id:
                        yield item
```

(The giant commented-out `attributes = 'individual_id,deployment_id,...'` line is deleted with the old body.)

- [ ] **Step 5: Run tests to verify they pass**

Run the Docker test command.
Expected: PASS — the 4 new tests plus all pre-existing tests.

- [ ] **Step 6: Commit**

```bash
git add movebank_client/client.py tests/conftest.py tests/client/test_get_individual_events.py
git commit -m "fix: query accessory-measurements sensors and request common GPS attributes"
```

### Task 4: Release 1.3.0

**Files:**
- Modify: `pyproject.toml:3` (`version = "1.2.0"` → `version = "1.3.0"`)
- Create: `.github/workflows/publish.yml` (publish to PyPI on GitHub release)

**Interfaces:**
- Produces: `movebank-client==1.3.0` on PyPI. Part 2 Task 5 depends on this being published.

- [ ] **Step 1: Bump version and constrain marshmallow**

In `pyproject.toml` change `version = "1.2.0"` to `version = "1.3.0"`, and in `[tool.poetry.dependencies]` add:

```toml
marshmallow = "<4"
```

(Fresh installs otherwise resolve marshmallow 4.x, which breaks `environs` 9.5 — `AttributeError: module 'marshmallow' has no attribute '__version_info__'` at import time. Verify with the Docker test command that the suite is still green after editing.)

- [ ] **Step 2: Add a PyPI publish workflow**

Create `.github/workflows/publish.yml`:

```yaml
name: Publish to PyPI

on:
  release:
    types: [published]

jobs:
  publish:
    runs-on: ubuntu-latest
    environment: pypi
    permissions:
      id-token: write  # required for PyPI trusted publishing
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v6
      - name: Build
        run: uv build
      - name: Publish
        run: uv publish --trusted-publishing always
```

(`uv build` uses the project's declared poetry-core build backend; `uv publish --trusted-publishing always` exchanges the workflow's OIDC token for a PyPI token — no long-lived secret.)

This uses PyPI **trusted publishing** (no long-lived token). One-time human setup on pypi.org: project `movebank-client` → Settings → Publishing → add GitHub publisher with owner `PADAS`, repo `movebank-client`, workflow `publish.yml`, environment `pypi` (and create the `pypi` environment in the GitHub repo settings). If the user prefers an API token instead, replace the Publish step's `uses:` with `with: password: ${{ secrets.PYPI_API_TOKEN }}` and skip the `permissions`/`environment` keys.

- [ ] **Step 3: Commit and push**

```bash
git add pyproject.toml .github/workflows/publish.yml
git commit -m "chore: bump version to 1.3.0, add PyPI publish workflow"
git push -u origin fix/event-fetch-per-sensor
```

- [ ] **Step 4: Open PR**

```bash
gh pr create --repo PADAS/movebank-client --base main --head fix/event-fetch-per-sensor \
  --title "Fix accessory-measurements event fetching; harden 429 handling" \
  --body "- Accessory-measurements sensors were never queried: the request block sat inside the GPS-only else branch, so they were silently skipped.
- Accessory queries now use attributes=all (delayed/out-of-order data needs the full set).
- GPS queries union study-advertised attributes with a common set (event_id, individual_id, deployment_id, tag_id, study_id, sensor_type_id, local identifiers, taxon) that integrations rely on for cursoring and transforms.
- 429 handling: bounded to 3 tries / 120s total, tolerates HTTP-date Retry-After headers, and logs backoff/giveup.

Ports fixes proven in production in cdip-integrations/movebank (commits 717532d, 4ca0efb, f04e052).

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 5: Publish (human step)**

After the PR merges and the one-time PyPI trusted-publisher setup is done (see Step 2), publish by creating a GitHub release:

```bash
gh release create v1.3.0 --repo PADAS/movebank-client --target main --title "v1.3.0" \
  --notes "Accessory-measurements fetch fix, GPS common attributes, bounded 429 retries, marshmallow<4 constraint"
```

The `publish.yml` workflow builds and uploads to PyPI. Fallback if trusted publishing isn't set up yet: `git checkout main && git pull && poetry build && poetry publish` with the user's credentials.

STOP: Part 2 Task 5 cannot proceed until `pip index versions movebank-client` shows 1.3.0.

---

## Part 2 — gundi-integration-movebank pull actions

All Part 2 tasks run in `/Users/chrisdo/padas/gundi-integration-movebank`.

**Docker test setup** (Task 5 builds the image once; later tasks reuse it):

```bash
# Build once (rebuild only if requirements.txt changes):
docker build -t mb-runner-test --target devimage -f docker/Dockerfile .
# Run tests (mount app/ so edits are picked up without rebuilding):
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
```

### Task 5: Branch, dependency bump, test image

**Files:**
- Modify: `requirements.in` (`movebank-client~=1.1.1` → `movebank-client~=1.3.0`)
- Modify: `requirements.txt` (regenerated)

**Interfaces:**
- Produces: branch `movebank-pull-actions` with `movebank-client==1.3.0` installed in the `mb-runner-test` image; all later tasks build on it.

- [ ] **Step 1: Branch off the template sync**

```bash
cd /Users/chrisdo/padas/gundi-integration-movebank
git checkout sync-20260709 && git pull
git checkout -b movebank-pull-actions
```

(If PR #10 already merged, use `git checkout main && git pull` as the base instead.)

- [ ] **Step 2: Bump the dependency and recompile**

Edit `requirements.in` to contain exactly:

```
movebank-client~=1.3.1
```

(1.3.1, not 1.3.0: the 1.3.0 release declared `httpx ^0.28`, which cannot co-resolve with `gundi-client-v2 2.4.0`'s `httpx <0.25`. 1.3.1 widens the range to `>=0.24,<1` and moves respx to the dev group.)

Then regenerate (matches how the repo's requirements.txt was produced):

```bash
docker run --rm -v "$PWD":/work -w /work python:3.10-slim bash -c \
  "pip install -q pip-tools && pip-compile --quiet --output-file=requirements.txt requirements-base.in requirements-dev.in requirements.in"
```

Expected: `requirements.txt` shows `movebank-client==1.3.0`. If pip-compile errors with "no matching distribution", 1.3.0 is not on PyPI yet — STOP and finish Part 1 Task 4.

- [ ] **Step 3: Build the test image and verify the suite is green**

```bash
docker build -t mb-runner-test --target devimage -f docker/Dockerfile .
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
```

Expected: 77 passed (the post-sync baseline).

- [ ] **Step 4: Commit**

```bash
git add requirements.in requirements.txt
git commit -m "chore: bump movebank-client to 1.3.0"
```

### Task 6: Movebank domain models

**Files:**
- Modify: `app/actions/client.py` (currently 20 lines: imports + `get_auth_config`)
- Create: `app/actions/tests/__init__.py` (empty file)
- Test: `app/actions/tests/test_models.py`

**Interfaces:**
- Consumes: `MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID` (`{'gps': 653, 'accessory-measurements': 7842954}`).
- Produces, all in `app.actions.client`:
  - `Individual` (pydantic): fields `id: str`, `local_identifier: str`, `nick_name: str`, `ring_id: str`, `sex: str`, `taxon_canonical_name: str`, `timestamp_start: Optional[Union[str, datetime]]`, `timestamp_end: Optional[Union[datetime, str]]`, `number_of_events: Optional[int] = 0`, `number_of_deployments: Optional[int] = 0`, `sensor_type_ids: str`, `taxon_detail: str`
  - `generate_individuals(items: Iterable[dict]) -> Iterator[Individual]` — skips (and logs) unparseable rows
  - `SensorState` (pydantic): `highest_event_id: Optional[int] = 0`, `latest_timestamp: Optional[datetime] = None`
  - `IndividualState` (pydantic): `individual_id: str`, `study_id: str`, `local_identifier: Optional[str] = None`, `sensor_states: Dict[str, SensorState]`, methods `get_sensor_state(sensor_type_id: int) -> SensorState` and `update_sensor_state(sensor_type_id: int, latest_timestamp: datetime, highest_event_id: int)`

Design note: the old function's legacy-state migration validator is deliberately **not** ported — the v2 integration starts with a fresh Redis state store, so there is no legacy portal `device_states` payload to migrate. Similarly, `SensorState.latest_timestamp` defaults to `None` (handler falls back to the lookback window) instead of the old hardcoded now-minus-7-days factory.

- [ ] **Step 1: Write the failing tests**

Create empty `app/actions/tests/__init__.py`, then `app/actions/tests/test_models.py`:

```python
from datetime import datetime, timezone

from app.actions.client import Individual, IndividualState, SensorState, generate_individuals


INDIVIDUAL_ROW = {
    "id": "111",
    "local_identifier": "tag-1",
    "nick_name": "Aquila",
    "ring_id": "R1",
    "sex": "f",
    "taxon_canonical_name": "Aquila chrysaetos",
    "timestamp_start": "2025-01-01 00:00:00.000",
    "timestamp_end": "2026-07-01 00:00:00.000",
    "number_of_events": "100",
    "number_of_deployments": "1",
    "sensor_type_ids": "gps,accessory-measurements",
    "taxon_detail": "",
}


def test_individual_coerces_naive_timestamps_to_utc():
    ind = Individual.parse_obj(INDIVIDUAL_ROW)
    assert ind.timestamp_start == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert ind.timestamp_end == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert ind.number_of_events == 100


def test_individual_tolerates_empty_timestamps():
    row = {**INDIVIDUAL_ROW, "timestamp_start": "", "timestamp_end": ""}
    ind = Individual.parse_obj(row)
    assert ind.timestamp_start is None
    assert ind.timestamp_end is None


def test_generate_individuals_skips_bad_rows():
    bad_row = {**INDIVIDUAL_ROW, "number_of_events": "not-a-number"}
    result = list(generate_individuals([INDIVIDUAL_ROW, bad_row]))
    assert len(result) == 1
    assert result[0].id == "111"


def test_individual_state_creates_default_sensor_state():
    state = IndividualState(individual_id="111", study_id="12345")
    sensor_state = state.get_sensor_state(653)
    assert sensor_state.highest_event_id == 0
    assert sensor_state.latest_timestamp is None


def test_individual_state_update_and_roundtrip():
    state = IndividualState(individual_id="111", study_id="12345")
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state.update_sensor_state(653, ts, 42)

    restored = IndividualState.parse_obj(state.dict())
    assert restored.get_sensor_state(653).highest_event_id == 42
    assert restored.get_sensor_state(653).latest_timestamp == ts
    # An unrelated sensor still gets a fresh default state.
    assert restored.get_sensor_state(7842954).highest_event_id == 0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_models.py -q
```

Expected: FAIL with `ImportError: cannot import name 'Individual'`.

- [ ] **Step 3: Implement**

Replace the full contents of `app/actions/client.py` with:

```python
import logging
from datetime import datetime, timezone
from typing import Dict, Optional, Union

import pydantic
from dateutil.parser import parse as parse_date
from movebank_client import MovebankClient
from movebank_client.errors import MBClientError, MBForbiddenError

from app.actions.configurations import AuthenticateConfig
from app.services.errors import ConfigurationNotFound
from app.services.utils import find_config_for_action

logger = logging.getLogger(__name__)


def get_auth_config(integration):
    # Look for the login credentials, needed for any action
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth"
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)


class Individual(pydantic.BaseModel):
    id: str
    local_identifier: str
    nick_name: str
    ring_id: str
    sex: str
    taxon_canonical_name: str

    # Tolerate timestamps with empty-string. Let validator coerce to datetime.
    timestamp_start: Optional[Union[str, datetime]]
    timestamp_end: Optional[Union[datetime, str]]
    number_of_events: Optional[int] = 0
    number_of_deployments: Optional[int] = 0
    sensor_type_ids: str
    taxon_detail: str

    @pydantic.validator('timestamp_start', 'timestamp_end')
    def clean_timestamp(cls, val):
        if isinstance(val, str):
            try:
                val = parse_date(val)
            except Exception:
                return None
        return val if val.tzinfo else val.replace(tzinfo=timezone.utc)


def generate_individuals(items):
    for item in items:
        try:
            val = Individual.parse_obj(item)
        except Exception:
            logger.exception('Failed parsing Individual %s', item)
        else:
            yield val


class SensorState(pydantic.BaseModel):
    """Cursor for a single sensor type of one individual."""
    highest_event_id: Optional[int] = 0
    latest_timestamp: Optional[datetime] = None


class IndividualState(pydantic.BaseModel):
    individual_id: str
    study_id: str
    local_identifier: Optional[str] = None
    # Per-sensor-type cursors (key = sensor_type_id as string, because JSON keys are strings)
    sensor_states: Dict[str, SensorState] = pydantic.Field(default_factory=dict)

    def get_sensor_state(self, sensor_type_id: int) -> SensorState:
        key = str(sensor_type_id)
        if key not in self.sensor_states:
            self.sensor_states[key] = SensorState()
        return self.sensor_states[key]

    def update_sensor_state(self, sensor_type_id: int, latest_timestamp: datetime, highest_event_id: int):
        self.sensor_states[str(sensor_type_id)] = SensorState(
            latest_timestamp=latest_timestamp,
            highest_event_id=highest_event_id
        )
```

Note: `MovebankClient`, `MBClientError`, `MBForbiddenError` stay imported here because `app/actions/handlers.py` references them as `client.MovebankClient` / `client.MBForbiddenError`.

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_models.py -q
```

Expected: 5 passed.

- [ ] **Step 5: Run the whole suite (guard against import breakage), then commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/client.py app/actions/tests/
git commit -m "feat: add Movebank individual and per-sensor cursor models"
```

### Task 7: Observation transform

**Files:**
- Create: `app/actions/transform.py`
- Test: `app/actions/tests/test_transform.py`

**Interfaces:**
- Consumes: nothing project-specific (pure functions).
- Produces, in `app.actions.transform`:
  - `build_observation(*, event: dict, device_name: str) -> Optional[dict]` — returns a Gundi v2 observation dict (`source`, `source_name`, `type`, `recorded_at` ISO string, `location: {"lat": float, "lon": float}`, `additional`) or `None` for unusable events
  - `human_friendly_timedelta(td: timedelta) -> str`
  - `chunks(items: list, n: int) -> Iterator[list]`

These port the old function's production-hardened quirks verbatim: events without coordinates become `(0, 0)` with `recorded_at` shifted **+1 ms** (so gps and accessory records for the same instant don't collide in EarthRanger); `subject_name` and `loaded_at` (duplicate-drop fudge) go into `additional`; `update_latency` is derived from `update_ts`; future-dated events are dropped (checked *after* the +1 ms shift, matching the old order).

- [ ] **Step 1: Write the failing tests**

Create `app/actions/tests/test_transform.py`:

```python
from datetime import datetime, timedelta, timezone

from app.actions.transform import build_observation, chunks, human_friendly_timedelta


GPS_EVENT = {
    "event_id": "100",
    "timestamp": "2026-01-01 10:00:00.000",
    "location_lat": "1.5",
    "location_long": "2.5",
    "individual_id": "111",
    "sensor_type_id": "653",
    "ground_speed": "3.2",
}


def test_build_observation_maps_gps_event():
    obs = build_observation(event=GPS_EVENT, device_name="Aquila")
    assert obs["source"] == "111"
    assert obs["source_name"] == "Aquila"
    assert obs["type"] == "tracking-device"
    assert obs["recorded_at"] == "2026-01-01T10:00:00+00:00"
    assert obs["location"] == {"lat": 1.5, "lon": 2.5}
    # Everything except coordinates and timestamp lands in additional.
    assert obs["additional"]["ground_speed"] == "3.2"
    assert obs["additional"]["event_id"] == "100"
    assert "location_lat" not in obs["additional"]
    assert obs["additional"]["subject_name"] == "Aquila"
    assert "loaded_at" in obs["additional"]


def test_build_observation_fudges_missing_coordinates():
    event = {**GPS_EVENT, "location_lat": "", "location_long": "", "sensor_type_id": "7842954"}
    obs = build_observation(event=event, device_name="Aquila")
    assert obs["location"] == {"lat": 0.0, "lon": 0.0}
    # +1ms so it doesn't collide with a GPS record at the same instant.
    assert obs["recorded_at"] == "2026-01-01T10:00:00.001000+00:00"


def test_build_observation_computes_update_latency():
    event = {**GPS_EVENT, "update_ts": "2026-01-01 12:30:00.000"}
    obs = build_observation(event=event, device_name="Aquila")
    assert obs["additional"]["update_latency"] == "2h, 30m"


def test_build_observation_drops_unparseable_timestamp():
    assert build_observation(event={**GPS_EVENT, "timestamp": "garbage"}, device_name="A") is None


def test_build_observation_drops_future_events():
    future = (datetime.now(tz=timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S.000")
    assert build_observation(event={**GPS_EVENT, "timestamp": future}, device_name="A") is None


def test_build_observation_drops_events_without_individual_id():
    assert build_observation(event={**GPS_EVENT, "individual_id": ""}, device_name="A") is None


def test_human_friendly_timedelta():
    assert human_friendly_timedelta(timedelta(days=2, hours=3, minutes=4)) == "2d, 3h, 4m"
    assert human_friendly_timedelta(timedelta(seconds=30)) == "0m"


def test_chunks():
    assert list(chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunks([], 2)) == []
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_transform.py -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.actions.transform'`.

- [ ] **Step 3: Implement**

Create `app/actions/transform.py`:

```python
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from dateutil.parser import parse as parse_date

logger = logging.getLogger(__name__)


def human_friendly_timedelta(td: timedelta) -> str:
    """Format a timedelta as e.g. '2d, 3h, 4m' (sub-minute becomes '0m')."""
    days = td.days
    hours, remainder = divmod(td.seconds, 3600)
    minutes, _ = divmod(remainder, 60)

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0:
        parts.append(f"{minutes}m")
    return ", ".join(parts) if parts else "0m"


def chunks(items: list, n: int):
    for i in range(0, len(items), n):
        yield items[i:i + n]


def build_observation(*, event: dict, device_name: str) -> Optional[dict]:
    """Transform one Movebank event record into a Gundi v2 observation dict.

    Returns None for events that can't be used (bad timestamp, no individual_id,
    or future-dated).
    """
    try:
        recorded_at = parse_date(event.get("timestamp")).replace(tzinfo=timezone.utc)
    except Exception:
        logger.warning(f"unable to parse timestamp: {event.get('timestamp')}")
        return None

    # Everything except coordinates and timestamp goes to additional, so it's
    # available for analysis in the destination.
    additional = {k: v for k, v in event.items() if k not in ("location_long", "location_lat", "timestamp")}

    if update_ts := event.get("update_ts"):
        try:
            updated_at = parse_date(update_ts).replace(tzinfo=timezone.utc)
            additional["update_latency"] = human_friendly_timedelta(updated_at - recorded_at)
        except Exception:
            pass

    individual_id = event.get("individual_id")
    lon, lat = event.get("location_long"), event.get("location_lat")

    # Records without coordinates (e.g. accessory-measurements) are stored at
    # (0, 0) with a +1ms shift, so they don't collide with a GPS observation at
    # the same timestamp. Revisit when observations can be updated in EarthRanger.
    if not lon or not lat:
        x, y = 0.0, 0.0
        recorded_at += timedelta(milliseconds=1)
    else:
        x, y = float(lon), float(lat)

    if not individual_id or recorded_at > datetime.now(tz=timezone.utc):
        return None

    additional["subject_name"] = device_name
    additional["loaded_at"] = datetime.now(tz=timezone.utc).isoformat()  # fudge to avoid duplicate drop

    return {
        "source": individual_id,
        "source_name": device_name,
        "type": "tracking-device",
        "recorded_at": recorded_at.isoformat(),
        "location": {"lat": y, "lon": x},
        "additional": additional,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_transform.py -q
```

Expected: 8 passed.

- [ ] **Step 5: Commit**

```bash
git add app/actions/transform.py app/actions/tests/test_transform.py
git commit -m "feat: port Movebank event-to-observation transform"
```

### Task 8: Action configurations

**Files:**
- Modify: `app/actions/configurations.py` (replace the `FetchIndividualEventsConfig` / `FetchStudyIndividualsConfig` stubs)
- Modify: `app/actions/handlers.py:9` (drop the stale import of the deleted stubs; keep everything else as-is for now)
- Test: `app/actions/tests/test_configurations.py`

**Interfaces:**
- Consumes: `PullActionConfiguration`, `AuthActionConfiguration` from `app.actions`; `InternalActionConfiguration` from `app.actions.core`; `Individual` from `app.actions.client`.
- Produces, in `app.actions.configurations`:
  - `AuthenticateConfig(AuthActionConfiguration)` — unchanged: `username: str`, `password: SecretStr`
  - `PullObservationsConfig(PullActionConfiguration)` — `study_id: str` (required), `maximum_lookback_hours: int = 24`
  - `PullEventsForIndividualConfig(InternalActionConfiguration)` — `study_id: str`, `individual: Individual`, `maximum_lookback_hours: int = 24`

Circular-import note: `configurations.py` needs `Individual` from `client.py`, but `client.py` (as of Task 6) imports `AuthenticateConfig` from `configurations.py` at module level — that would be a cycle. The cycle is broken in Step 3 by moving `client.py`'s `AuthenticateConfig` import inside the `get_auth_config` function body (the only place it's used). After that, `configurations.py` imports `Individual` from `client.py` normally.

- [ ] **Step 1: Write the failing tests**

Create `app/actions/tests/test_configurations.py`:

```python
import pytest
from pydantic import ValidationError

from app.actions.configurations import (
    AuthenticateConfig,
    PullEventsForIndividualConfig,
    PullObservationsConfig,
)
from app.actions.core import InternalActionConfiguration, PullActionConfiguration


INDIVIDUAL = {
    "id": "111",
    "local_identifier": "tag-1",
    "nick_name": "Aquila",
    "ring_id": "R1",
    "sex": "f",
    "taxon_canonical_name": "Aquila chrysaetos",
    "timestamp_start": "2025-01-01 00:00:00.000",
    "timestamp_end": "2026-07-01 00:00:00.000",
    "number_of_events": 100,
    "number_of_deployments": 1,
    "sensor_type_ids": "gps",
    "taxon_detail": "",
}


def test_pull_observations_config_requires_study_id():
    with pytest.raises(ValidationError):
        PullObservationsConfig()
    config = PullObservationsConfig(study_id="12345")
    assert config.maximum_lookback_hours == 24
    assert isinstance(config, PullActionConfiguration)


def test_pull_events_for_individual_config_is_internal():
    config = PullEventsForIndividualConfig(study_id="12345", individual=INDIVIDUAL)
    # Internal actions are not registered in the portal.
    assert isinstance(config, InternalActionConfiguration)
    assert config.individual.id == "111"
    # Round-trips through dict (this is how trigger_action serializes it).
    restored = PullEventsForIndividualConfig.parse_obj(config.dict())
    assert restored.individual.nick_name == "Aquila"


def test_auth_config_unchanged():
    config = AuthenticateConfig(username="u", password="p")
    assert config.password.get_secret_value() == "p"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_configurations.py -q
```

Expected: FAIL with `ImportError: cannot import name 'PullObservationsConfig'`.

- [ ] **Step 3: Implement**

First break the import cycle in `app/actions/client.py`: delete the module-level line `from app.actions.configurations import AuthenticateConfig` and change `get_auth_config` to import locally:

```python
def get_auth_config(integration):
    from app.actions.configurations import AuthenticateConfig

    # Look for the login credentials, needed for any action
    auth_config = find_config_for_action(
        configurations=integration.configurations,
        action_id="auth"
    )
    if not auth_config:
        raise ConfigurationNotFound(
            f"Authentication settings for integration {str(integration.id)} "
            f"are missing. Please fix the integration setup in the portal."
        )
    return AuthenticateConfig.parse_obj(auth_config.data)
```

Then replace the full contents of `app/actions/configurations.py` with:

```python
from pydantic import Field, SecretStr

from app.actions import AuthActionConfiguration, PullActionConfiguration
from app.actions.client import Individual
from app.actions.core import InternalActionConfiguration


class AuthenticateConfig(AuthActionConfiguration):
    username: str
    password: SecretStr = Field(..., format="password")


class PullObservationsConfig(PullActionConfiguration):
    study_id: str = Field(
        ...,
        title="Movebank Study ID",
        description="ID of the Movebank study to pull observations from.",
    )
    maximum_lookback_hours: int = Field(
        24,
        title="Maximum Lookback (hours)",
        description=(
            "How far back to fetch events for an individual that has no saved state yet. "
            "Override this on a manual run to backfill historical data."
        ),
    )


class PullEventsForIndividualConfig(InternalActionConfiguration):
    """Config for the internal sub-action that pulls events for one individual."""
    study_id: str
    individual: Individual
    maximum_lookback_hours: int = 24
```

Finally, in `app/actions/handlers.py` change the import line

```python
from app.actions.configurations import AuthenticateConfig, FetchStudyIndividualsConfig, FetchIndividualEventsConfig
```

to

```python
from app.actions.configurations import AuthenticateConfig
```

- [ ] **Step 4: Run the full suite to verify it passes**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
```

Expected: PASS (baseline + 16 new tests so far). The full suite matters here because config/handler imports affect action discovery in `app/conftest.py`.

- [ ] **Step 5: Commit**

```bash
git add app/actions/configurations.py app/actions/client.py app/actions/handlers.py app/actions/tests/test_configurations.py
git commit -m "feat: add pull action configurations, replace unused stubs"
```

### Task 9: `pull_observations` action

**Files:**
- Modify: `app/actions/handlers.py`
- Create: `app/actions/tests/conftest.py`
- Test: `app/actions/tests/test_handlers.py`

**Interfaces:**
- Consumes: `trigger_action(integration_id: str, action_id: str, config=None)` from `app.services.action_scheduler`; `generate_individuals`, `get_auth_config`, `client.MovebankClient` from `app.actions.client`; `PullObservationsConfig`, `PullEventsForIndividualConfig` from `app.actions.configurations`.
- Produces: `action_pull_observations(integration, action_config: PullObservationsConfig) -> dict` returning `{"individuals_found": int, "sub_actions_triggered": int}`; registered by discovery as action id `pull_observations`, scheduled every 10 minutes (the old cronjob cadence).

- [ ] **Step 1: Create shared test fixtures**

Create `app/actions/tests/conftest.py`:

```python
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.actions.configurations import AuthenticateConfig


@pytest.fixture(autouse=True)
def mock_activity_publish(mocker):
    """Keep @activity_logger from touching PubSub in tests."""
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock(return_value=None))


@pytest.fixture
def integration():
    integration = MagicMock()
    integration.id = uuid4()
    integration.base_url = "https://www.movebank.org"
    integration.name = "Movebank Test"
    return integration


@pytest.fixture
def mock_auth_config(mocker):
    mocker.patch(
        "app.actions.client.get_auth_config",
        return_value=AuthenticateConfig(username="user", password="pass"),
    )


@pytest.fixture
def mock_movebank_client(mocker):
    """A MovebankClient instance mock usable as an async context manager."""
    mb = MagicMock()
    mb.__aenter__ = AsyncMock(return_value=mb)
    mb.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("app.actions.client.MovebankClient", return_value=mb)
    return mb


def make_events_generator(events):
    """Build a stand-in for the async-generator method get_individual_events_by_time."""
    async def _gen(**kwargs):
        for event in events:
            yield event
    return _gen


INDIVIDUAL_ROW = {
    "id": "111",
    "local_identifier": "tag-1",
    "nick_name": "Aquila",
    "ring_id": "R1",
    "sex": "f",
    "taxon_canonical_name": "Aquila chrysaetos",
    "timestamp_start": "2025-01-01 00:00:00.000",
    "timestamp_end": "2026-07-01 00:00:00.000",
    "number_of_events": "100",
    "number_of_deployments": "1",
    "sensor_type_ids": "gps",
    "taxon_detail": "",
}
```

- [ ] **Step 2: Write the failing tests**

Create `app/actions/tests/test_handlers.py`:

```python
from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import PullObservationsConfig
from app.actions.handlers import action_pull_observations
from app.actions.tests.conftest import INDIVIDUAL_ROW


@pytest.mark.asyncio
async def test_pull_observations_triggers_subaction_per_individual(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    second_row = {**INDIVIDUAL_ROW, "id": "222", "nick_name": "Bubo"}
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW, second_row])
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_pull_observations(
        integration=integration,
        action_config=PullObservationsConfig(study_id="12345", maximum_lookback_hours=48),
    )

    assert result == {"individuals_found": 2, "sub_actions_triggered": 2}
    assert mock_trigger.await_count == 2
    _, kwargs = mock_trigger.await_args_list[0]
    assert kwargs["integration_id"] == str(integration.id)
    assert kwargs["action_id"] == "pull_events_for_individual"
    assert kwargs["config"].study_id == "12345"
    assert kwargs["config"].individual.id == "111"
    assert kwargs["config"].maximum_lookback_hours == 48


@pytest.mark.asyncio
async def test_pull_observations_skips_unparseable_individuals(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    bad_row = {**INDIVIDUAL_ROW, "id": "333", "number_of_events": "not-a-number"}
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW, bad_row])
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_pull_observations(
        integration=integration,
        action_config=PullObservationsConfig(study_id="12345"),
    )

    assert result == {"individuals_found": 1, "sub_actions_triggered": 1}
    assert mock_trigger.await_count == 1
```

- [ ] **Step 3: Run tests to verify they fail**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -q
```

Expected: FAIL with `ImportError: cannot import name 'action_pull_observations'`.

- [ ] **Step 4: Implement**

In `app/actions/handlers.py`, extend the imports at the top to:

```python
import logging

import app.actions.client as client
from app.actions.client import generate_individuals
from app.actions.configurations import AuthenticateConfig, PullObservationsConfig, PullEventsForIndividualConfig
from app.services.action_scheduler import crontab_schedule, trigger_action
from app.services.activity_logger import activity_logger
from app.services.state import IntegrationStateManager
```

(Keep `action_auth` and its existing body unchanged; drop any now-unused imports such as `httpx`/`stamina`/`gundi_tools` if the existing file has them and they are no longer referenced — Task 10 re-adds what it needs.)

Add the new handler after `action_auth`:

```python
@activity_logger()
@crontab_schedule("*/10 * * * *")  # same cadence as the v1 cronjob
async def action_pull_observations(integration, action_config: PullObservationsConfig):
    """List the study's individuals and trigger one sub-action per individual."""
    integration_id = str(integration.id)
    logger.info(f"Pulling observations for study {action_config.study_id}, integration {integration_id}...")

    auth_config = client.get_auth_config(integration)
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        individual_rows = await mb.get_individuals_by_study(study_id=action_config.study_id)

    individuals = list(generate_individuals(individual_rows))
    logger.info(f"{len(individuals)} individuals found for study {action_config.study_id}")

    triggered = 0
    for ind in individuals:
        await trigger_action(
            integration_id=integration_id,
            action_id="pull_events_for_individual",
            config=PullEventsForIndividualConfig(
                study_id=action_config.study_id,
                individual=ind,
                maximum_lookback_hours=action_config.maximum_lookback_hours,
            ),
        )
        triggered += 1

    return {"individuals_found": len(individuals), "sub_actions_triggered": triggered}
```

- [ ] **Step 5: Run tests to verify they pass, then the full suite**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -q
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
```

Expected: both PASS.

- [ ] **Step 6: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/conftest.py app/actions/tests/test_handlers.py
git commit -m "feat: add pull_observations action triggering per-individual sub-actions"
```

### Task 10: `pull_events_for_individual` sub-action

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `IntegrationStateManager.get_state/set_state` (`app.services.state`), `send_observations_to_gundi(observations: List[dict], integration_id=...)` (`app.services.gundi`), `build_observation`/`chunks` (`app.actions.transform`), `IndividualState` (`app.actions.client`), `MovebankClient.get_individual_events_by_time` (1.3.0, called once per sensor type), `MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID`.
- Produces: `action_pull_events_for_individual(integration, action_config: PullEventsForIndividualConfig) -> dict` returning `{"observations_sent": int}` or `{"skipped": <reason>}`. State keys: cursors under action id `pull_events_for_individual`, quiet flag under action id `pull_events_for_individual_quiet`, both with `source_id=<individual_id>`.

Behavior ported from the old `process_individual` (v1 `movebank_main.py:243-432`), with one deliberate improvement: observations are **sent before** the cursor state is saved, so a failed send can't advance cursors and drop data (the old code updated portal state without knowing load success).

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
from datetime import datetime, timedelta, timezone

from app.actions.client import IndividualState
from app.actions.configurations import PullEventsForIndividualConfig
from app.actions.handlers import action_pull_events_for_individual
from app.actions.tests.conftest import make_events_generator


def _sub_action_config(**overrides):
    individual = {**INDIVIDUAL_ROW, **overrides.pop("individual_overrides", {})}
    return PullEventsForIndividualConfig(study_id="12345", individual=individual, **overrides)


def _gps_event(event_id, ts):
    return {
        "event_id": str(event_id),
        "timestamp": ts,
        "location_lat": "1.5",
        "location_long": "2.5",
        "individual_id": "111",
        "sensor_type_id": "653",
    }


@pytest.fixture
def mock_state_store(mocker):
    """In-memory stand-in for the module-level state_manager in handlers."""
    store = {}

    async def get_state(integration_id, action_id, source_id="no-source"):
        return store.get((str(integration_id), action_id, source_id), {})

    async def set_state(integration_id, action_id, state, source_id="no-source", expire=None):
        store[(str(integration_id), action_id, source_id)] = state

    manager = mocker.patch("app.actions.handlers.state_manager")
    manager.get_state = AsyncMock(side_effect=get_state)
    manager.set_state = AsyncMock(side_effect=set_state)
    return store


@pytest.mark.asyncio
async def test_pull_events_sends_observations_and_updates_cursor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    events = [_gps_event(100, "2026-01-01 10:00:00.000"), _gps_event(101, "2026-01-01 11:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mock_send = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result["observations_sent"] == 2
    assert mock_send.await_count >= 1
    sent = [obs for call in mock_send.await_args_list for obs in call.kwargs["observations"]]
    assert len(sent) == 2
    assert sent[0]["source"] == "111"
    # Cursor state persisted: GPS sensor advanced to the newest event.
    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    state = IndividualState.parse_obj(saved)
    assert state.get_sensor_state(653).highest_event_id == 101
    assert state.get_sensor_state(653).latest_timestamp == datetime(2026, 1, 1, 11, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_pull_events_skips_individual_in_quiet_period(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    mock_state_store[(str(integration.id), "pull_events_for_individual_quiet", "111")] = {"quiet": True}
    mock_send = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock())

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result == {"skipped": "quiet_period"}
    assert mock_send.await_count == 0


@pytest.mark.asyncio
async def test_pull_events_sets_quiet_period_when_window_is_empty(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    mock_movebank_client.get_individual_events_by_time = make_events_generator([])
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock())

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result["observations_sent"] == 0
    quiet = mock_state_store.get((str(integration.id), "pull_events_for_individual_quiet", "111"))
    assert quiet == {"quiet": True}


@pytest.mark.asyncio
async def test_pull_events_skips_individual_without_timestamp_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    result = await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(individual_overrides={"timestamp_start": ""}),
    )
    assert result == {"skipped": "no_timestamp_start"}


@pytest.mark.asyncio
async def test_pull_events_skips_when_all_sensors_are_current(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Saved cursor is already past the individual's timestamp_end.
    state = IndividualState(individual_id="111", study_id="12345")
    state.update_sensor_state(653, datetime(2026, 7, 1, tzinfo=timezone.utc), 999)
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = state.dict()

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )
    assert result == {"skipped": "no_new_data"}


@pytest.mark.asyncio
async def test_pull_events_respects_max_records_cap(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # 2500 events in one window: cap stops processing at >= 2000.
    events = [_gps_event(i, "2026-06-30 10:00:00.000") for i in range(2500)]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration,
        # High-frequency path: number_of_events > 5000 shrinks the window, but a
        # single window still returned everything here; the cap must still hold
        # across windows.
        action_config=_sub_action_config(individual_overrides={"number_of_events": "6000"}),
    )
    assert result["observations_sent"] == 2500  # first window completes...
    # ...but no further windows ran (cap reached after the first).
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -q
```

Expected: new tests FAIL with `ImportError: cannot import name 'action_pull_events_for_individual'`; Task 9's tests still PASS.

- [ ] **Step 3: Implement**

In `app/actions/handlers.py`, extend imports with:

```python
import json
from datetime import datetime, timedelta, timezone

import pydantic
from dateutil.parser import parse as parse_date
from movebank_client import MovebankClient

from app.actions.client import IndividualState, generate_individuals
from app.actions.transform import build_observation, chunks
from app.services.gundi import send_observations_to_gundi
```

Add module-level constants after `state_manager = IntegrationStateManager()`:

```python
# Ported from the v1 integration — production-proven values.
HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD = 5000  # events; above this, shrink the fetch window
MAXIMUM_RECORDS_PER_INDIVIDUAL = 2000  # hard cap per individual per run
DEFAULT_BATCH_WINDOW = timedelta(days=5)
QUIET_PERIOD_SECONDS = 3600  # skip an individual for this long after an empty window
OBSERVATIONS_BATCH_SIZE = 200

CURSOR_STATE_ACTION_ID = "pull_events_for_individual"
QUIET_STATE_ACTION_ID = "pull_events_for_individual_quiet"
```

Add the handler:

```python
@activity_logger()
async def action_pull_events_for_individual(integration, action_config: PullEventsForIndividualConfig):
    """Fetch events for one individual with per-sensor cursors, transform, and send to Gundi.

    Internal sub-action, triggered by pull_observations once per individual.
    """
    ind = action_config.individual
    integration_id = str(integration.id)
    log_reference = f"study:{action_config.study_id},individual:{ind.id},local_identifier:{ind.local_identifier}"

    if await state_manager.get_state(integration_id, QUIET_STATE_ACTION_ID, source_id=ind.id):
        logger.info(f"Skipping individual {log_reference} for quiet period.")
        return {"skipped": "quiet_period"}

    if ind.timestamp_start is None:
        logger.info(f"Skip Movebank {log_reference} for no timestamp_start.")
        return {"skipped": "no_timestamp_start"}

    now = datetime.now(tz=timezone.utc)
    # Some individuals don't provide timestamp_end, so resolve a reasonable value.
    resolved_individual_timestamp_end = ind.timestamp_end or now
    highest_date = min(now, resolved_individual_timestamp_end)

    sensor_type_labels = [label.lower() for label in ind.sensor_type_ids.split(",")]
    sensor_type_ids = [
        MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID[label]
        for label in sensor_type_labels
        if label in MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID
    ]
    if not sensor_type_ids:
        logger.info(f"Skip Movebank {log_reference} — no supported sensor types in '{ind.sensor_type_ids}'.")
        return {"skipped": "no_supported_sensors"}

    saved_state = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    try:
        individual_state = IndividualState.parse_obj(saved_state) if saved_state else None
    except pydantic.ValidationError:
        logger.exception(f"Failed parsing saved state for {log_reference}; starting fresh.")
        individual_state = None
    if individual_state is None:
        individual_state = IndividualState(
            individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
        )

    # Build per-sensor cursors from state; new sensors start at the lookback window.
    default_start = resolved_individual_timestamp_end - timedelta(hours=action_config.maximum_lookback_hours)
    sensor_type_timestamps = {}
    minimum_event_ids = {}
    for sensor_type_id in sensor_type_ids:
        sensor_state = individual_state.get_sensor_state(sensor_type_id)
        sensor_type_timestamps[sensor_type_id] = sensor_state.latest_timestamp or default_start
        minimum_event_ids[sensor_type_id] = (sensor_state.highest_event_id or 0) + 1

    earliest_start = min(sensor_type_timestamps.values())
    if earliest_start >= resolved_individual_timestamp_end:
        logger.info(f"Skip Movebank {log_reference} for no new data.")
        return {"skipped": "no_new_data"}

    # Size the fetch window by event density: high-frequency individuals get a
    # window that should hold roughly HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD events,
    # assuming an even distribution over the individual's active range.
    if ind.number_of_events > HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD and ind.timestamp_end:
        total_seconds = (ind.timestamp_end - ind.timestamp_start).total_seconds()
        batch_window_size = timedelta(
            seconds=(HIGH_FREQUENCY_INDIVIDUAL_THRESHOLD / ind.number_of_events) * total_seconds
        )
    else:
        batch_window_size = DEFAULT_BATCH_WINDOW

    logger.info(
        f"For individual {ind.id} ({ind.nick_name}), using a window size of {batch_window_size} "
        f"({ind.number_of_events} events on record)."
    )

    device_name = ind.nick_name or ind.local_identifier or ind.ring_id
    auth_config = client.get_auth_config(integration)
    total_observations_sent = 0
    current_window_start = earliest_start

    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        while current_window_start <= highest_date and total_observations_sent < MAXIMUM_RECORDS_PER_INDIVIDUAL:
            end_at = min(highest_date, current_window_start + batch_window_size)

            # One request per sensor type, each from its own cursor.
            events = []
            for sensor_type_id in sensor_type_ids:
                sensor_start = sensor_type_timestamps[sensor_type_id]
                if sensor_start > end_at:
                    continue  # this sensor is already past the window
                async for event in mb.get_individual_events_by_time(
                    study_id=action_config.study_id,
                    individual_id=ind.id,
                    timestamp_start=sensor_start,
                    timestamp_end=end_at,
                    sensor_type_ids=[sensor_type_id],
                    minimum_event_id=minimum_event_ids[sensor_type_id],
                ):
                    events.append(event)

            observations = [
                obs for event in events
                if (obs := build_observation(event=event, device_name=device_name)) is not None
            ]
            for batch in chunks(observations, OBSERVATIONS_BATCH_SIZE):
                await send_observations_to_gundi(observations=batch, integration_id=integration_id)

            # Advance per-sensor cursors from what actually came back.
            events_by_sensor = {}
            for event in events:
                try:
                    events_by_sensor.setdefault(int(event.get("sensor_type_id")), []).append(event)
                except (TypeError, ValueError):
                    continue
            for sensor_type_id in sensor_type_ids:
                sensor_events = events_by_sensor.get(sensor_type_id, [])
                sensor_timestamps = []
                sensor_event_ids = []
                for event in sensor_events:
                    try:
                        sensor_timestamps.append(parse_date(event.get("timestamp")).replace(tzinfo=timezone.utc))
                    except Exception:
                        pass
                    try:
                        sensor_event_ids.append(int(event.get("event_id")))
                    except (TypeError, ValueError):
                        pass
                if sensor_timestamps:
                    new_latest = max(sensor_timestamps)
                    new_max_event_id = max(sensor_event_ids) if sensor_event_ids else minimum_event_ids[sensor_type_id] - 1
                    individual_state.update_sensor_state(sensor_type_id, new_latest, new_max_event_id)
                    sensor_type_timestamps[sensor_type_id] = new_latest
                    minimum_event_ids[sensor_type_id] = new_max_event_id + 1

            logger.info(
                f"Processed Movebank data for {log_reference}: {len(observations)} observations, "
                f"events by sensor: { {k: len(v) for k, v in events_by_sensor.items()} }"
            )

            if observations:
                # Observations were sent above — only now is it safe to advance the saved cursor.
                await state_manager.set_state(
                    integration_id,
                    CURSOR_STATE_ACTION_ID,
                    json.loads(individual_state.json()),
                    source_id=ind.id,
                )
            else:
                # Nothing in this window: back off from this individual for a while.
                await state_manager.set_state(
                    integration_id,
                    QUIET_STATE_ACTION_ID,
                    {"quiet": True},
                    source_id=ind.id,
                    expire=QUIET_PERIOD_SECONDS,
                )

            total_observations_sent += len(observations)
            current_window_start = current_window_start + batch_window_size

    return {"observations_sent": total_observations_sent}
```

- [ ] **Step 4: Run tests to verify they pass, then the full suite**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -q
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
```

Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: add pull_events_for_individual sub-action with per-sensor cursors"
```

### Task 11: Final verification and PR

**Files:**
- Modify: `README.md` (short section describing the actions)

**Interfaces:** none — verification and delivery.

- [ ] **Step 1: Full suite, clean build**

```bash
docker build -t mb-runner-test --target devimage -f docker/Dockerfile .
docker run --rm mb-runner-test python -m pytest app -q
```

(No volume mount this time — verifies the committed tree, not the working dir.)
Expected: PASS.

- [ ] **Step 2: Verify action discovery and registration behavior**

```bash
docker run --rm mb-runner-test python -c "
from app.actions import action_handlers
from app.actions.core import InternalActionConfiguration
print('actions:', sorted(action_handlers.keys()))
_, config_model, _ = action_handlers['pull_events_for_individual']
print('sub-action internal:', issubclass(config_model, InternalActionConfiguration))
"
```

Expected: `actions: ['auth', 'pull_events_for_individual', 'pull_observations']` and `sub-action internal: True` (internal actions are skipped by self-registration, `app/services/self_registration.py:48`).

- [ ] **Step 3: Update README**

Add to `README.md` under the existing intro a section:

```markdown
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
```

- [ ] **Step 4: Commit, push, open PR**

```bash
git add README.md
git commit -m "docs: describe pull actions"
git push -u origin movebank-pull-actions
gh pr create --repo PADAS/gundi-integration-movebank --base main --head movebank-pull-actions \
  --title "Add pull actions with per-individual sub-actions" \
  --body "Ports the v1 Movebank function's pull logic onto the v2 action runner.

- pull_observations (scheduled */10): lists study individuals, triggers one sub-action each
- pull_events_for_individual (internal): per-sensor-type cursors in Redis, adaptive batch
  windows for high-frequency individuals, 2000-records-per-run cap, 1h quiet period after
  empty windows, and the production transform quirks (0,0 +1ms fudge for coordinate-less
  records, subject_name/loaded_at/update_latency in additional)
- Requires movebank-client 1.3.0 (accessory-measurements fetch fix)
- Observations are sent before cursors are saved, so a failed send cannot drop data

Depends on the template sync (PR #10) — rebase onto main once that merges.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 5 (manual, post-merge): stage smoke test**

Human step for the user: configure a test integration in the Gundi stage portal with real Movebank credentials and a small study, set `TRIGGER_ACTIONS_ALWAYS_SYNC=true` in `local/.env.local` (so sub-actions run inline without the commands topic), run `cd local && docker compose up --build`, and trigger `pull_observations` via `POST /v1/actions/execute` from http://localhost:8080/docs. Verify observations arrive in the stage destination.

---

## Deferred (explicitly out of scope, per YAGNI)

- **"All data" / "last N years" study-wide backfill UX** — the plumbing is ready (`maximum_lookback_hours` rides `config_overrides` through `trigger_action` and manual runs), but a dedicated backfill action/UI is a follow-up.
- **v1 → v2 state migration** — the v2 store starts fresh; first run refetches up to `maximum_lookback_hours` per individual. Accept the small overlap (Gundi dedups by `loaded_at` fudge design) rather than porting the legacy-state migration validator.
- **Additional sensor types** beyond GPS and accessory-measurements (the label map bounds what's fetched, matching v1).
