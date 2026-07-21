# Movebank Backfill Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an operator-triggered backfill that loads a Movebank study's full history (hundreds of thousands of records per high-frequency individual) via self-cascading per-individual sub-actions, bounded by a shared connection semaphore, meeting the steady-state pull at a clean boundary, with throttled Activity Log progress.

**Architecture:** A new executable `backfill` action seeds a Redis work-queue and triggers up to K internal `backfill_individual` sub-actions (rolling queue). Each sub-action self-cascades through one individual's history in time-budgeted steps, acquiring a username-keyed Redis connection semaphore (shared with the steady-state pull and all integrations on that username) around every Movebank call, and sends to Gundi inline. Backfill covers `[start, end)`; the pull covers `[end, ∞)`; the accessory sensor gets a configurable settling margin so multi-hour-late records aren't missed.

**Tech Stack:** Python 3.10, pydantic v1, redis.asyncio, movebank-client ~=1.3.1, pytest + pytest-asyncio + pytest-mock + respx. Tests run in the Docker `mb-runner-test` image.

## Global Constraints

- Python 3.10 only; run all tests in Docker: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q`. (Rebuild the image only if requirements change: `docker build -t mb-runner-test --target devimage -f docker/Dockerfile .`)
- pydantic **v1** syntax everywhere (`validator`, `parse_obj`, `.dict()`, `Field`).
- Branch from `sync-20260721` (or `main` if the sync PR #12 has merged). Never work on `main`.
- New integration settings go in `app/settings/integration.py`, NOT `app/settings/base.py` (base.py syncs from the upstream template).
- Production values, copied verbatim: `MOVEBANK_MAX_CONNECTIONS` default **25** (headroom under Movebank's 31/username); `ACCESSORY_SETTLING_HOURS` default **12**; `BACKFILL_MAX_CONCURRENCY` default **8**; cascade step time budget = **0.8 × MAX_ACTION_EXECUTION_TIME**.
- The transform's `loaded_at` field defeats Gundi dedup, so backfill `[start, end)` and pull `[end, ∞)` MUST NOT overlap — a window fetched twice becomes duplicate observations.
- GPS records arrive promptly (no settling margin); only accessory-measurements (sensor id 7842954) gets the settling margin.
- Movebank event ids are ~monotonic with ingest order, so a late-arriving record has a high `event_id`; the `event_id >= minimum_event_id` filter is what makes a widened re-read safe (drops already-sent events).
- Commit messages end with the `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>` trailer.

## File Structure

- `app/settings/integration.py` (MODIFY) — new settings.
- `app/services/movebank_connections.py` (CREATE) — the username-keyed connection semaphore + `movebank_slot` async context manager.
- `app/actions/handlers.py` (MODIFY) — adopt the semaphore in `action_pull_events_for_individual`; apply the accessory settling margin; add `action_backfill` and `action_backfill_events_for_individual`.
- `app/actions/configurations.py` (MODIFY) — `BackfillConfig`, `BackfillEventsForIndividualConfig`.
- `app/actions/backfill_queue.py` (CREATE) — Redis job/queue state helpers.
- `app/actions/tests/` (CREATE/MODIFY) — tests per component.
- `README.md` (MODIFY) — document the actions and settings.

---

### Task 1: Settings + connection semaphore

**Files:**
- Modify: `app/settings/integration.py`
- Create: `app/services/movebank_connections.py`
- Test: `app/services/tests/test_movebank_connections.py`

**Interfaces:**
- Consumes: `settings.MOVEBANK_MAX_CONNECTIONS`, `settings.REDIS_HOST/REDIS_PORT/REDIS_STATE_DB`.
- Produces:
  - `connection_key(username: str) -> str` — `"movebank:connections:" + sha256(username)[:16]`.
  - `async movebank_slot(username: str, *, ttl_seconds: int = 600)` — async context manager; acquires a slot (raises `NoConnectionSlot` if the username is at capacity) and releases on exit.
  - `class NoConnectionSlot(Exception)`.

- [ ] **Step 1: Add settings**

Append to `app/settings/integration.py`:

```python
# Movebank connection budget, shared across all integrations using the same
# Movebank username (Movebank documents ~31 simultaneous connections per user).
MOVEBANK_MAX_CONNECTIONS = env.int("MOVEBANK_MAX_CONNECTIONS", 25)
# Accessory-measurements records can arrive at Movebank hours after their
# timestamp; the accessory query re-reads this many hours so late arrivals are
# caught (the event-id filter drops already-sent events, so no duplicates).
ACCESSORY_SETTLING_HOURS = env.int("ACCESSORY_SETTLING_HOURS", 12)
# Rolling-queue width for a backfill job: how many individuals are in flight at
# once. Deliberately below MOVEBANK_MAX_CONNECTIONS so backfill never starves
# the steady-state pull or other integrations on the same username.
BACKFILL_MAX_CONCURRENCY = env.int("BACKFILL_MAX_CONCURRENCY", 8)
```

(If `app/settings/integration.py` does not already `from environs import Env` / define `env`, mirror the pattern already in `app/settings/base.py`; import `env` from there if it is exported, otherwise create a local `env = Env()`.)

- [ ] **Step 2: Write the failing tests**

Create `app/services/tests/test_movebank_connections.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services.movebank_connections import (
    NoConnectionSlot,
    connection_key,
    movebank_slot,
)


def test_connection_key_hashes_username():
    key = connection_key("gundi_user")
    assert key.startswith("movebank:connections:")
    # Plaintext username must not appear in the key.
    assert "gundi_user" not in key
    # Stable and same-length for any username.
    assert key == connection_key("gundi_user")
    assert len(connection_key("a")) == len(connection_key("a-much-longer-name"))


@pytest.fixture
def mock_redis(mocker):
    client = MagicMock()
    client.eval = AsyncMock(return_value=1)      # acquired by default
    client.zrem = AsyncMock(return_value=1)
    redis_module = MagicMock()
    redis_module.Redis.return_value = client
    mocker.patch("app.services.movebank_connections.redis", redis_module)
    return client


@pytest.mark.asyncio
async def test_movebank_slot_acquires_and_releases(mock_redis):
    async with movebank_slot("gundi_user"):
        pass
    assert mock_redis.eval.await_count == 1      # acquire
    assert mock_redis.zrem.await_count == 1      # release


@pytest.mark.asyncio
async def test_movebank_slot_raises_when_full(mock_redis):
    mock_redis.eval = AsyncMock(return_value=0)  # at capacity
    with pytest.raises(NoConnectionSlot):
        async with movebank_slot("gundi_user"):
            pass
    # Nothing acquired, so nothing released.
    assert mock_redis.zrem.await_count == 0


@pytest.mark.asyncio
async def test_movebank_slot_releases_on_exception(mock_redis):
    with pytest.raises(ValueError):
        async with movebank_slot("gundi_user"):
            raise ValueError("boom")
    assert mock_redis.zrem.await_count == 1      # released despite the error
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/services/tests/test_movebank_connections.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'app.services.movebank_connections'`.

- [ ] **Step 4: Implement**

Create `app/services/movebank_connections.py`:

```python
import hashlib
import time
import uuid
from contextlib import asynccontextmanager

import redis.asyncio as redis

from app import settings


class NoConnectionSlot(Exception):
    """Raised when the Movebank connection budget for a username is exhausted."""


# Atomic acquire: purge expired slots, then add a new slot only if under the
# ceiling. KEYS[1]=zset key. ARGV: now, expiry, ceiling, token.
# Returns 1 if acquired, 0 if at capacity.
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], 0, ARGV[1])
if redis.call('ZCARD', KEYS[1]) < tonumber(ARGV[3]) then
    redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
    return 1
end
return 0
"""


def connection_key(username: str) -> str:
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:16]
    return f"movebank:connections:{digest}"


def _client() -> redis.Redis:
    return redis.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB
    )


@asynccontextmanager
async def movebank_slot(username: str, *, ttl_seconds: int = 600):
    """Acquire one Movebank connection slot for `username`, shared across every
    integration on the same Redis. Raises NoConnectionSlot if at capacity.

    Slots are members of a per-username sorted set scored by expiry time, so a
    crashed holder's slot self-expires (purged on the next acquire) rather than
    leaking the budget permanently.
    """
    client = _client()
    key = connection_key(username)
    token = str(uuid.uuid4())
    now = time.time()
    acquired = await client.eval(
        _ACQUIRE_LUA, 1, key, now, now + ttl_seconds, settings.MOVEBANK_MAX_CONNECTIONS, token
    )
    if not acquired:
        raise NoConnectionSlot(f"No Movebank connection slot available (limit {settings.MOVEBANK_MAX_CONNECTIONS}).")
    try:
        yield
    finally:
        await client.zrem(key, token)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/services/tests/test_movebank_connections.py -q`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add app/settings/integration.py app/services/movebank_connections.py app/services/tests/test_movebank_connections.py
git commit -m "feat: add username-keyed Movebank connection semaphore + settings"
```

---

### Task 2: Adopt the semaphore + accessory settling margin in the steady-state pull

**Files:**
- Modify: `app/actions/handlers.py` (`action_pull_events_for_individual`, and the imports/constants block)
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `movebank_slot` (Task 1), `settings.ACCESSORY_SETTLING_HOURS`.
- Produces: no new public symbol; the pull now (a) wraps each `get_individual_events_by_time` call in `movebank_slot(auth_config.username)`, and (b) for the accessory sensor, queries from `sensor_start - ACCESSORY_SETTLING_HOURS`.

This is a change to shipped steady-state code. GPS behavior is unchanged (exact cursor); only accessory widens its re-read.

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_pull_accessory_query_applies_settling_margin(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # An accessory-only individual with a saved cursor: the query start must be
    # pushed back by ACCESSORY_SETTLING_HOURS so multi-hour-late records are caught.
    from app.actions.client import IndividualState
    from datetime import datetime, timezone
    cursor_ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    state = IndividualState(individual_id="111", study_id="12345")
    state.update_sensor_state(7842954, cursor_ts, 500)
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = state.dict()

    captured = {}

    async def _gen(**kwargs):
        captured.update(kwargs)
        if False:
            yield {}
    mock_movebank_client.get_individual_events_by_time = _gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(individual_overrides={"sensor_type_ids": "accessory-measurements"}),
    )

    # First fetch for the accessory sensor starts >= 12h before the cursor.
    assert captured["sensor_type_ids"] == [7842954]
    assert captured["timestamp_start"] <= cursor_ts - timedelta(hours=12)


@pytest.mark.asyncio
async def test_pull_acquires_connection_slot(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    events = [_gps_event(100, "2026-01-01 10:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    slot = mocker.patch("app.actions.handlers.movebank_slot")

    await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    # The pull opened at least one connection under the shared semaphore.
    assert slot.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "settling_margin or connection_slot" -q`
Expected: FAIL — `settling_margin` on the assertion (start not pushed back), `connection_slot` with `AttributeError`/`ImportError` on `movebank_slot`.

- [ ] **Step 3: Implement**

In `app/actions/handlers.py` add to imports:

```python
from app.services.movebank_connections import movebank_slot, NoConnectionSlot
```

Add a helper near the constants block:

```python
def _query_start_for_sensor(sensor_type_id: int, sensor_start: datetime) -> datetime:
    """Accessory-measurements can arrive hours late, so its query re-reads back
    ACCESSORY_SETTLING_HOURS; GPS is prompt and uses its exact cursor."""
    if sensor_type_id == MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID["accessory-measurements"]:
        return sensor_start - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS)
    return sensor_start
```

Ensure `from app import settings` is imported in the module (add if missing). In the per-sensor fetch loop of `action_pull_events_for_individual`, wrap the fetch and apply the margin. Replace the existing per-sensor fetch block:

```python
            for sensor_type_id in sensor_type_ids:
                sensor_start = sensor_type_timestamps[sensor_type_id]
                if sensor_start > end_at:
                    continue
                query_start = _query_start_for_sensor(sensor_type_id, sensor_start)
                async with movebank_slot(auth_config.username):
                    async for event in mb.get_individual_events_by_time(
                        study_id=action_config.study_id,
                        individual_id=ind.id,
                        timestamp_start=query_start,
                        timestamp_end=end_at,
                        sensor_type_ids=[sensor_type_id],
                        minimum_event_id=minimum_event_ids[sensor_type_id],
                    ):
                        events.append(event)
```

(`auth_config` is already resolved in this handler via `client.get_auth_config(integration)`; if it is currently resolved after this loop, move that resolution above the loop.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -q`
Expected: PASS (all existing handler tests plus the 2 new).

- [ ] **Step 5: Run the full suite, then commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: apply connection semaphore and accessory settling margin to steady-state pull"
```

---

### Task 3: Backfill configuration models

**Files:**
- Modify: `app/actions/configurations.py`
- Test: `app/actions/tests/test_configurations.py` (append)

**Interfaces:**
- Consumes: `GenericActionConfiguration`, `ExecutableActionMixin`, `InternalActionConfiguration` from `app.actions.core`; `Individual` from `app.actions.client`.
- Produces:
  - `BackfillConfig(GenericActionConfiguration, ExecutableActionMixin)`: `study_id: str`, `individual_ids: Optional[List[str]] = None`, `start: Union[datetime, str]` (a datetime, or the literal `"all"`), `backfill_max_concurrency: Optional[int] = None`.
  - `BackfillEventsForIndividualConfig(InternalActionConfiguration)`: `study_id: str`, `individual: Individual`, `job_id: str`, `start: datetime`, `end: datetime`.

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_configurations.py`:

```python
def test_backfill_config_whole_study_all_data():
    from app.actions.configurations import BackfillConfig
    from app.actions.core import ExecutableActionMixin
    config = BackfillConfig(study_id="12345", start="all")
    assert config.individual_ids is None
    assert config.start == "all"
    assert issubclass(BackfillConfig, ExecutableActionMixin)


def test_backfill_config_dated_and_filtered():
    from datetime import datetime, timezone
    from app.actions.configurations import BackfillConfig
    config = BackfillConfig(
        study_id="12345",
        individual_ids=["111", "222"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        backfill_max_concurrency=4,
    )
    assert config.individual_ids == ["111", "222"]
    assert config.start.year == 2024
    assert config.backfill_max_concurrency == 4


def test_backfill_individual_config_is_internal_and_roundtrips():
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from app.actions.core import InternalActionConfiguration
    from datetime import datetime, timezone
    cfg = BackfillEventsForIndividualConfig(
        study_id="12345", individual=INDIVIDUAL, job_id="job-1",
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert isinstance(cfg, InternalActionConfiguration)
    restored = BackfillEventsForIndividualConfig.parse_obj(cfg.dict())
    assert restored.individual.id == "111"
    assert restored.job_id == "job-1"
```

(`INDIVIDUAL` is the fixture dict already defined at the top of `test_configurations.py`.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_configurations.py -k backfill -q`
Expected: FAIL with `ImportError: cannot import name 'BackfillConfig'`.

- [ ] **Step 3: Implement**

Add to `app/actions/configurations.py` (extend the imports to include `List`, `Union`, `datetime`, and `GenericActionConfiguration`):

```python
from datetime import datetime
from typing import List, Optional, Union

from app.actions.core import GenericActionConfiguration
```

```python
class BackfillConfig(GenericActionConfiguration, ExecutableActionMixin):
    """Operator-triggered study backfill. Executable, NOT scheduled — it must
    not subclass PullActionConfiguration."""
    study_id: str = Field(..., title="Movebank Study ID")
    individual_ids: Optional[List[str]] = Field(
        None,
        title="Individual IDs",
        description="Leave empty to backfill the whole study, or list specific individual IDs.",
    )
    start: Union[datetime, str] = Field(
        "all",
        title="Start",
        description="Earliest datetime to backfill from, or 'all' to fetch from each individual's earliest record.",
    )
    backfill_max_concurrency: Optional[int] = Field(
        None,
        title="Max Concurrency",
        description="Individuals processed in parallel. Defaults to the service's BACKFILL_MAX_CONCURRENCY.",
    )


class BackfillEventsForIndividualConfig(InternalActionConfiguration):
    study_id: str
    individual: Individual
    job_id: str
    start: datetime
    end: datetime
```

- [ ] **Step 4: Run tests + full suite, then commit**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q`
Expected: PASS.

```bash
git add app/actions/configurations.py app/actions/tests/test_configurations.py
git commit -m "feat: add backfill action configuration models"
```

---

### Task 4: Backfill job/queue state

**Files:**
- Create: `app/actions/backfill_queue.py`
- Test: `app/actions/tests/test_backfill_queue.py`

**Interfaces:**
- Consumes: `IntegrationStateManager` (`app.services.state`) for its Redis client, or a dedicated `redis.asyncio` client mirroring its construction.
- Produces a `BackfillJob` class bound to `(integration_id, job_id)`:
  - `async seed(individual_ids: List[str], *, total: int, range_repr: str) -> None`
  - `async next_individual() -> Optional[str]` — atomic pop from the pending list; returns None when empty.
  - `async mark_started() -> None` / reads via `async snapshot() -> dict` (keys: `total`, `completed`, `observations_sent`, `pending_remaining`, `in_flight`, `range`).
  - `async incr_in_flight(n: int = 1) -> int` / `async decr_in_flight() -> int`
  - `async record_completion(observations: int) -> None` — increments `completed` and `observations_sent`.
  - `async incr_attempts(individual_id: str) -> int` / `async is_done() -> bool` (pending empty and in_flight == 0).

Use one Redis hash `backfill.{integration_id}.{job_id}.meta` for counters and a list `backfill.{integration_id}.{job_id}.pending` for the queue; `LPOP` for atomic dequeue, `HINCRBY` for counters.

- [ ] **Step 1: Write the failing tests**

Create `app/actions/tests/test_backfill_queue.py`:

```python
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.actions.backfill_queue import BackfillJob


@pytest.fixture
def fake_redis():
    # Minimal in-memory stand-in for the subset of redis.asyncio used here.
    store = {"hash": {}, "list": []}

    client = MagicMock()

    async def hincrby(key, field, n):
        store["hash"][field] = int(store["hash"].get(field, 0)) + n
        return store["hash"][field]

    async def hset(key, mapping=None, **kw):
        store["hash"].update(mapping or kw)

    async def hgetall(key):
        return {k: str(v) for k, v in store["hash"].items()}

    async def rpush(key, *vals):
        store["list"].extend(vals)
        return len(store["list"])

    async def lpop(key):
        return store["list"].pop(0) if store["list"] else None

    async def llen(key):
        return len(store["list"])

    client.hincrby = AsyncMock(side_effect=hincrby)
    client.hset = AsyncMock(side_effect=hset)
    client.hgetall = AsyncMock(side_effect=hgetall)
    client.rpush = AsyncMock(side_effect=rpush)
    client.lpop = AsyncMock(side_effect=lpop)
    client.llen = AsyncMock(side_effect=llen)
    return client


@pytest.fixture
def job(mocker, fake_redis):
    mocker.patch("app.actions.backfill_queue._client", return_value=fake_redis)
    return BackfillJob("int-1", "job-1")


@pytest.mark.asyncio
async def test_seed_and_pop_order(job):
    await job.seed(["a", "b", "c"], total=3, range_repr="[2024..2026)")
    assert await job.next_individual() == "a"
    assert await job.next_individual() == "b"
    assert await job.next_individual() == "c"
    assert await job.next_individual() is None


@pytest.mark.asyncio
async def test_in_flight_and_completion(job):
    await job.seed(["a"], total=1, range_repr="r")
    await job.incr_in_flight()
    assert not await job.is_done()          # in_flight == 1
    await job.next_individual()             # drain the queue
    await job.record_completion(1500)
    await job.decr_in_flight()
    assert await job.is_done()              # pending empty AND in_flight == 0
    snap = await job.snapshot()
    assert snap["completed"] == 1
    assert snap["observations_sent"] == 1500


@pytest.mark.asyncio
async def test_attempts_counter(job):
    await job.seed(["a"], total=1, range_repr="r")
    assert await job.incr_attempts("a") == 1
    assert await job.incr_attempts("a") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_backfill_queue.py -q`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Implement**

Create `app/actions/backfill_queue.py`:

```python
from typing import List, Optional

import redis.asyncio as redis

from app import settings


def _client() -> redis.Redis:
    return redis.Redis(
        host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB
    )


class BackfillJob:
    """Redis-backed state for one backfill job: a pending work-queue of
    individual IDs plus aggregate counters."""

    def __init__(self, integration_id: str, job_id: str):
        self.integration_id = str(integration_id)
        self.job_id = job_id
        self.db = _client()

    @property
    def _meta(self) -> str:
        return f"backfill.{self.integration_id}.{self.job_id}.meta"

    @property
    def _pending(self) -> str:
        return f"backfill.{self.integration_id}.{self.job_id}.pending"

    async def seed(self, individual_ids: List[str], *, total: int, range_repr: str) -> None:
        await self.db.hset(self._meta, mapping={
            "total": total, "completed": 0, "observations_sent": 0,
            "in_flight": 0, "range": range_repr,
        })
        if individual_ids:
            await self.db.rpush(self._pending, *individual_ids)

    async def next_individual(self) -> Optional[str]:
        value = await self.db.lpop(self._pending)
        if value is None:
            return None
        return value.decode() if isinstance(value, bytes) else value

    async def incr_in_flight(self, n: int = 1) -> int:
        return await self.db.hincrby(self._meta, "in_flight", n)

    async def decr_in_flight(self) -> int:
        return await self.db.hincrby(self._meta, "in_flight", -1)

    async def record_completion(self, observations: int) -> None:
        await self.db.hincrby(self._meta, "completed", 1)
        await self.db.hincrby(self._meta, "observations_sent", observations)

    async def incr_attempts(self, individual_id: str) -> int:
        return await self.db.hincrby(self._meta, f"attempts.{individual_id}", 1)

    async def is_done(self) -> bool:
        remaining = await self.db.llen(self._pending)
        snap = await self.snapshot()
        return remaining == 0 and snap["in_flight"] == 0

    async def snapshot(self) -> dict:
        raw = await self.db.hgetall(self._meta)
        data = {(k.decode() if isinstance(k, bytes) else k):
                (v.decode() if isinstance(v, bytes) else v) for k, v in raw.items()}
        return {
            "total": int(data.get("total", 0)),
            "completed": int(data.get("completed", 0)),
            "observations_sent": int(data.get("observations_sent", 0)),
            "in_flight": int(data.get("in_flight", 0)),
            "range": data.get("range", ""),
        }
```

- [ ] **Step 4: Run tests to verify they pass, then commit**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_backfill_queue.py -q`
Expected: 3 passed.

```bash
git add app/actions/backfill_queue.py app/actions/tests/test_backfill_queue.py
git commit -m "feat: add backfill job/queue Redis state"
```

---

### Task 5: `backfill` action — resolve targets, seed queue, dispatch first wave

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `BackfillConfig`, `BackfillEventsForIndividualConfig` (Task 3), `BackfillJob` (Task 4), `trigger_action`, `client.get_auth_config`, `client.MovebankClient`, `generate_individuals`, `settings.BACKFILL_MAX_CONCURRENCY`.
- Produces: `action_backfill(integration, action_config: BackfillConfig) -> dict` returning `{"job_id": str, "individuals": int, "dispatched": int}`. Registered as action id `backfill` (executable). Uses a fixed `job_id` derived from inputs (see below) so a redelivered command doesn't start a second job.

`end` resolution and `start="all"` resolution live here (Task 6 covers the per-sensor detail inside the sub-action; this task resolves the per-individual `[start, end)` pair and freezes it into each `BackfillEventsForIndividualConfig`).

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_seeds_queue_and_dispatches_up_to_k(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    from app.actions.configurations import BackfillConfig
    rows = [{**INDIVIDUAL_ROW, "id": str(i)} for i in range(5)]
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=rows)
    seen = {}

    async def fake_seed(self, individual_ids, *, total, range_repr):
        seen["seeded"] = list(individual_ids); seen["total"] = total
    async def fake_next(self):
        return seen["seeded"].pop(0) if seen.get("seeded") else None
    async def fake_incr(self, n=1):
        return n
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", fake_incr)
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    mocker.patch("app.actions.handlers.settings.BACKFILL_MAX_CONCURRENCY", 3)

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 5
    assert result["dispatched"] == 3            # K, not all 5
    assert mock_trigger.await_count == 3
    _, kwargs = mock_trigger.await_args_list[0]
    assert kwargs["action_id"] == "backfill_events_for_individual"
    assert kwargs["config"].job_id == result["job_id"]


@pytest.mark.asyncio
async def test_backfill_respects_individual_filter(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    from app.actions.configurations import BackfillConfig
    rows = [{**INDIVIDUAL_ROW, "id": str(i)} for i in range(5)]
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=rows)
    captured = {}
    async def fake_seed(self, individual_ids, *, total, range_repr):
        captured["ids"] = list(individual_ids)
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all", individual_ids=["2", "4"]),
    )
    assert set(captured["ids"]) == {"2", "4"}
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k backfill_seeds -q`
Expected: FAIL with `ImportError: cannot import name 'action_backfill'`.

- [ ] **Step 3: Implement**

In `app/actions/handlers.py` add imports:

```python
import hashlib
from typing import Optional
from app.actions.backfill_queue import BackfillJob
from app.actions.configurations import BackfillConfig, BackfillEventsForIndividualConfig
```

Add constants near the existing block:

```python
BACKFILL_ACTION_ID = "backfill_events_for_individual"
BACKFILL_WATERMARK_ACTION_ID = "backfill_watermark"
BACKFILL_PROGRESS_THROTTLE_SECONDS = 300
```

Add the handler:

```python
def _resolve_start(start, ind) -> datetime:
    """A concrete datetime for this individual: the operator's date, or for
    'all' the individual's earliest record (falling back to the lookback floor)."""
    if isinstance(start, datetime):
        return start
    # start == "all"
    if ind.timestamp_start:
        return ind.timestamp_start
    return datetime.now(tz=timezone.utc) - timedelta(days=3650)  # ~10y floor


async def _resolve_end(integration_id: str, ind, now: datetime) -> datetime:
    """Freeze the boundary where backfill meets the steady-state pull: the
    individual's existing pull cursor if any, else now (and the pull is seeded
    to now by the individual sub-action on completion)."""
    saved = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
    if saved:
        state = IndividualState.parse_obj(saved)
        stamps = [s.latest_timestamp for s in state.sensor_states.values() if s.latest_timestamp]
        if stamps:
            return min(stamps)
    return now


@activity_logger()
async def action_backfill(integration, action_config: BackfillConfig):
    integration_id = str(integration.id)
    now = datetime.now(tz=timezone.utc)
    auth_config = client.get_auth_config(integration)
    mb_client = client.MovebankClient(
        base_url=integration.base_url,
        username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        rows = await mb.get_individuals_by_study(study_id=action_config.study_id)
    individuals = list(generate_individuals(rows))
    if action_config.individual_ids:
        wanted = set(action_config.individual_ids)
        individuals = [i for i in individuals if i.id in wanted]

    # Deterministic job id: a redelivered backfill command resumes the same job.
    job_seed = f"{action_config.study_id}:{sorted(i.id for i in individuals)}:{action_config.start}"
    job_id = "job-" + hashlib.sha256(job_seed.encode()).hexdigest()[:12]
    job = BackfillJob(integration_id, job_id)

    ranges = {i.id: (_resolve_start(action_config.start, i), await _resolve_end(integration_id, i, now)) for i in individuals}
    # Only individuals with a non-empty range are worth queuing.
    queued = [i for i in individuals if ranges[i.id][0] < ranges[i.id][1]]
    range_repr = f"[{action_config.start} .. {now.isoformat()})"
    await job.seed([i.id for i in queued], total=len(queued), range_repr=range_repr)

    logger.info(f"Backfill {job_id} for study {action_config.study_id}: {len(queued)} individuals, range {range_repr}")

    k = action_config.backfill_max_concurrency or settings.BACKFILL_MAX_CONCURRENCY
    by_id = {i.id: i for i in queued}
    dispatched = 0
    while dispatched < k:
        next_id = await job.next_individual()
        if next_id is None:
            break
        ind = by_id[next_id]
        start_dt, end_dt = ranges[next_id]
        await job.incr_in_flight()
        await trigger_action(
            integration_id=integration_id,
            action_id=BACKFILL_ACTION_ID,
            config=BackfillEventsForIndividualConfig(
                study_id=action_config.study_id, individual=ind,
                job_id=job_id, start=start_dt, end=end_dt,
            ),
        )
        dispatched += 1

    return {"job_id": job_id, "individuals": len(queued), "dispatched": dispatched}
```

- [ ] **Step 4: Run tests + full suite, then commit**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q`
Expected: PASS.

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: add backfill action that seeds the queue and dispatches the first wave"
```

---

### Task 6: `backfill_events_for_individual` sub-action — cascade, hand-off, dispatch-next

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: everything from Tasks 1–5 plus `build_observation`, `chunks`, `send_observations_to_gundi`, `_ensure_utc`, `_query_start_for_sensor`, `movebank_slot`, `MAX_ACTION_EXECUTION_TIME`.
- Produces: `action_backfill_events_for_individual(integration, action_config: BackfillEventsForIndividualConfig) -> dict`. Registered internal action `backfill_events_for_individual`. On each invocation it does one time-bounded cascade step over `[watermark, end)`; if the watermark reaches `end` it finalizes (writes the steady-state pull cursor from the backfill watermark, records completion, dispatches the next queued individual); otherwise it re-triggers itself.

The backfill watermark is stored under action id `BACKFILL_WATERMARK_ACTION_ID` with `source_id = f"{job_id}.{individual_id}"`, reusing the `IndividualState` model. The step uses a time deadline `time.monotonic() + 0.8 * MAX_ACTION_EXECUTION_TIME`.

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_individual_finalizes_and_dispatches_next(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from app.actions.client import IndividualState
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    # One small window of GPS events fully inside [start, end): the step reaches
    # `end` in a single pass and finalizes.
    events = [_gps_event(10, "2024-01-02 00:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 1, "in_flight": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "completed"
    # The steady-state pull cursor was written from the backfill watermark (ts + event id).
    handed = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    st = IndividualState.parse_obj(handed)
    assert st.get_sensor_state(653).highest_event_id == 10
    # Queue empty -> no next dispatch.
    assert not mock_trigger.called


@pytest.mark.asyncio
async def test_backfill_individual_retriggers_self_when_budget_exhausted(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)   # huge range
    events = [_gps_event(i, "2024-01-02 00:00:00.000") for i in range(3)]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    # Force the time budget to zero so the step stops after the first window.
    mocker.patch("app.actions.handlers.settings.MAX_ACTION_EXECUTION_TIME", 0)
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "continued"
    # It re-triggered ITSELF (same individual), not the next one.
    assert mock_trigger.await_count == 1
    _, kwargs = mock_trigger.await_args_list[0]
    assert kwargs["action_id"] == "backfill_events_for_individual"
    assert kwargs["config"].individual.id == "111"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k backfill_individual -q`
Expected: FAIL with `ImportError: cannot import name 'action_backfill_events_for_individual'`.

- [ ] **Step 3: Implement**

Add `import time` to `app/actions/handlers.py` if not present, then add the handler:

```python
@activity_logger()
async def action_backfill_events_for_individual(integration, action_config: BackfillEventsForIndividualConfig):
    ind = action_config.individual
    integration_id = str(integration.id)
    job = BackfillJob(integration_id, action_config.job_id)
    watermark_source = f"{action_config.job_id}.{ind.id}"
    log_reference = f"job:{action_config.job_id},individual:{ind.id}"

    sensor_type_labels = [label.strip().lower() for label in ind.sensor_type_ids.split(",")]
    sensor_type_ids = [
        MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID[label]
        for label in sensor_type_labels
        if label in MovebankClient.MOVEBANK_SENSOR_TYPE_LABEL_TO_ID
    ]
    if not sensor_type_ids:
        return await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=0)

    saved = await state_manager.get_state(integration_id, BACKFILL_WATERMARK_ACTION_ID, source_id=watermark_source)
    state = IndividualState.parse_obj(saved) if saved else IndividualState(
        individual_id=ind.id, study_id=action_config.study_id, local_identifier=ind.local_identifier
    )
    sensor_type_timestamps, minimum_event_ids = {}, {}
    for stid in sensor_type_ids:
        ss = state.get_sensor_state(stid)
        sensor_type_timestamps[stid] = ss.latest_timestamp or action_config.start
        minimum_event_ids[stid] = (ss.highest_event_id or 0) + 1

    auth_config = client.get_auth_config(integration)
    deadline = time.monotonic() + 0.8 * settings.MAX_ACTION_EXECUTION_TIME
    window = DEFAULT_BATCH_WINDOW
    observations_sent = 0
    current = min(sensor_type_timestamps.values())

    mb_client = client.MovebankClient(
        base_url=integration.base_url, username=auth_config.username,
        password=auth_config.password.get_secret_value(),
    )
    async with mb_client as mb:
        while current < action_config.end and time.monotonic() < deadline:
            end_at = min(action_config.end, current + window)
            events = []
            for stid in sensor_type_ids:
                sensor_start = sensor_type_timestamps[stid]
                if sensor_start > end_at:
                    continue
                query_start = _query_start_for_sensor(stid, sensor_start)
                async with movebank_slot(auth_config.username):
                    async for event in mb.get_individual_events_by_time(
                        study_id=action_config.study_id, individual_id=ind.id,
                        timestamp_start=query_start, timestamp_end=end_at,
                        sensor_type_ids=[stid], minimum_event_id=minimum_event_ids[stid],
                    ):
                        events.append(event)

            device_name = ind.nick_name or ind.local_identifier or ind.ring_id
            observations = [o for e in events if (o := build_observation(event=e, device_name=device_name)) is not None]
            for batch in chunks(observations, OBSERVATIONS_BATCH_SIZE):
                await send_observations_to_gundi(observations=batch, integration_id=integration_id)

            _advance_watermarks(state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids)
            observations_sent += len(observations)
            current = current + window
            await state_manager.set_state(integration_id, BACKFILL_WATERMARK_ACTION_ID,
                                          json.loads(state.json()), source_id=watermark_source)

    if current < action_config.end:
        # Budget exhausted mid-range: continue THIS individual on the next step.
        await trigger_action(
            integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=action_config,
        )
        logger.info(f"Backfill {log_reference} continued at {current.isoformat()} ({observations_sent} obs this step)")
        return {"status": "continued", "observations_sent": observations_sent}

    return await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=observations_sent, state=state)
```

Add the two helpers. `_advance_watermarks` is the per-sensor cursor logic factored out of the steady-state handler (extract it there too so both call one function — DRY):

```python
def _advance_watermarks(state, events, sensor_type_ids, sensor_type_timestamps, minimum_event_ids):
    events_by_sensor = {}
    for e in events:
        try:
            events_by_sensor.setdefault(int(e.get("sensor_type_id")), []).append(e)
        except (TypeError, ValueError):
            continue
    for stid in sensor_type_ids:
        se = events_by_sensor.get(stid, [])
        stamps, ids = [], []
        for e in se:
            try:
                stamps.append(_ensure_utc(parse_date(e.get("timestamp"))))
            except Exception:
                pass
            try:
                ids.append(int(e.get("event_id")))
            except (TypeError, ValueError):
                pass
        if stamps or ids:
            new_latest = max(stamps) if stamps else sensor_type_timestamps[stid]
            new_max = max(ids) if ids else minimum_event_ids[stid] - 1
            state.update_sensor_state(stid, new_latest, new_max)
            sensor_type_timestamps[stid] = new_latest
            minimum_event_ids[stid] = new_max + 1


async def _finalize_backfill_individual(integration_id, job, ind, action_config, *, observations, state=None):
    # Hand off the FULL cursor (timestamp + highest event id per sensor) to the
    # steady-state pull, so the pull's event-id filter absorbs the accessory
    # settling re-read at the boundary without re-emitting backfilled events.
    if state is not None:
        existing = await state_manager.get_state(integration_id, CURSOR_STATE_ACTION_ID, source_id=ind.id)
        if not existing:
            await state_manager.set_state(integration_id, CURSOR_STATE_ACTION_ID,
                                          json.loads(state.json()), source_id=ind.id)
    await job.record_completion(observations)
    await job.decr_in_flight()

    if await job.is_done():
        snap = await job.snapshot()
        logger.info(f"Backfill {action_config.job_id} finished: {snap['completed']}/{snap['total']} "
                    f"individuals, {snap['observations_sent']} observations")
    else:
        if await state_manager.set_if_absent(
            integration_id, f"backfill_progress.{action_config.job_id}", ttl_seconds=BACKFILL_PROGRESS_THROTTLE_SECONDS
        ):
            snap = await job.snapshot()
            logger.info(f"Backfill {action_config.job_id} progress: {snap['completed']}/{snap['total']} "
                        f"individuals, ~{snap['observations_sent']} observations")
        next_id = await job.next_individual()
        if next_id is not None:
            await _dispatch_backfill_individual(integration_id, job, action_config, next_id)

    return {"status": "completed", "observations_sent": observations}
```

`_dispatch_backfill_individual` re-fetches the next individual's `Individual` and range. Because the sub-action does not carry the whole study roster, store each queued individual's `(row, start, end)` in job state at seed time is heavier than needed; instead the next individual's config is reconstructed from the study lookup is too slow here. Simplest correct approach: the `backfill` action (Task 5) writes a per-individual config blob to Redis at seed time, and dispatch reads it. Add to Task 4's `BackfillJob`:

```python
    async def put_individual_config(self, individual_id: str, config_json: str) -> None:
        await self.db.hset(f"{self._meta}.configs", mapping={individual_id: config_json})

    async def get_individual_config(self, individual_id: str) -> Optional[str]:
        raw = await self.db.hget(f"{self._meta}.configs", individual_id)
        return (raw.decode() if isinstance(raw, bytes) else raw) if raw else None
```

Then in Task 5, after computing each queued individual's range, also call `await job.put_individual_config(i.id, BackfillEventsForIndividualConfig(...).json())`, and `_dispatch_backfill_individual` becomes:

```python
async def _dispatch_backfill_individual(integration_id, job, action_config, individual_id):
    blob = await job.get_individual_config(individual_id)
    if blob is None:
        logger.warning(f"Backfill {action_config.job_id}: no stored config for individual {individual_id}; skipping")
        return
    cfg = BackfillEventsForIndividualConfig.parse_raw(blob)
    await job.incr_in_flight()
    await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=cfg)
```

(Task 5's first-wave dispatch loop should use `_dispatch_backfill_individual` too, so config storage/read is the single dispatch path. Update Task 5's loop accordingly when implementing this task, and add a `put_individual_config` call for every queued individual before the dispatch loop.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k backfill -q`
Expected: PASS.

- [ ] **Step 5: Full suite + commit**

```bash
docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q
git add app/actions/handlers.py app/actions/backfill_queue.py app/actions/tests/
git commit -m "feat: add self-cascading backfill sub-action with boundary hand-off and rolling dispatch"
```

---

### Task 7: Error handling — abandon-after-retries, connection backoff, one-job guard

**Files:**
- Modify: `app/actions/handlers.py`
- Test: `app/actions/tests/test_handlers.py` (append)

**Interfaces:**
- Consumes: `BackfillJob.incr_attempts`, `NoConnectionSlot`.
- Produces: `BACKFILL_MAX_ATTEMPTS = 5` constant; the sub-action catches `NoConnectionSlot` → re-triggers itself (backoff, no work lost); catches other exceptions → increments attempts, and once attempts exceed the max, logs a warning, records the individual done-with-error (decrement in-flight, dispatch next) so the job still drains.

- [ ] **Step 1: Write the failing tests**

Append to `app/actions/tests/test_handlers.py`:

```python
@pytest.mark.asyncio
async def test_backfill_individual_backs_off_when_no_slot(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from app.services.movebank_connections import NoConnectionSlot
    from datetime import datetime, timezone
    def _raise(*a, **k):
        raise NoConnectionSlot("full")
    mocker.patch("app.actions.handlers.movebank_slot", _raise)
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    assert result["status"] == "backoff"
    # Re-triggered self to retry later; did not finalize.
    assert mock_trigger.await_count == 1
    assert mock_trigger.await_args_list[0].kwargs["config"].individual.id == "111"


@pytest.mark.asyncio
async def test_backfill_individual_abandoned_after_max_attempts(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    mock_movebank_client.get_individual_events_by_time = mocker.MagicMock(side_effect=RuntimeError("mb down"))
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_attempts", AsyncMock(return_value=6))  # over the max
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0, "in_flight": 0, "range": "r"}))
    warn = mocker.patch("app.actions.handlers.logger.warning")

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    assert result["status"] == "abandoned"
    assert warn.called
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app/actions/tests/test_handlers.py -k "no_slot or abandoned" -q`
Expected: FAIL (statuses not implemented).

- [ ] **Step 3: Implement**

Add `BACKFILL_MAX_ATTEMPTS = 5` to the constants block. Wrap the sub-action body from Task 6 so the fetch/send loop is inside a `try`. Structure:

```python
async def action_backfill_events_for_individual(integration, action_config: BackfillEventsForIndividualConfig):
    ind = action_config.individual
    integration_id = str(integration.id)
    job = BackfillJob(integration_id, action_config.job_id)
    try:
        # ... the Task 6 body: build watermark, cascade loop, then either
        # re-trigger self ("continued") or _finalize_backfill_individual ("completed") ...
        ...
    except NoConnectionSlot:
        await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=action_config)
        logger.info(f"Backfill {action_config.job_id}/{ind.id}: no connection slot, backing off")
        return {"status": "backoff"}
    except Exception as exc:
        attempts = await job.incr_attempts(ind.id)
        if attempts <= BACKFILL_MAX_ATTEMPTS:
            await trigger_action(integration_id=integration_id, action_id=BACKFILL_ACTION_ID, config=action_config)
            logger.info(f"Backfill {action_config.job_id}/{ind.id}: attempt {attempts} failed ({exc}); retrying")
            return {"status": "retry", "attempts": attempts}
        logger.warning(f"Backfill {action_config.job_id}/{ind.id}: abandoned after {attempts} attempts: {exc}")
        result = await _finalize_backfill_individual(integration_id, job, ind, action_config, observations=0)
        return {"status": "abandoned", **{k: v for k, v in result.items() if k != "status"}}
```

Note: the `NoConnectionSlot` re-trigger must NOT double-count in-flight (the individual is still the same in-flight unit), and the abandon path calls `_finalize_backfill_individual` which decrements in-flight and dispatches next — consistent with normal completion.

- [ ] **Step 4: Run tests + full suite, then commit**

Run: `docker run --rm -v "$PWD/app":/code/app -w /code mb-runner-test python -m pytest app -q`
Expected: PASS.

```bash
git add app/actions/handlers.py app/actions/tests/test_handlers.py
git commit -m "feat: backfill error handling — connection backoff, abandon-after-retries"
```

---

### Task 8: Final verification, docs, PR

**Files:**
- Modify: `README.md`
- Test: clean-build full suite + discovery check

**Interfaces:** none — verification and delivery.

- [ ] **Step 1: Clean build + full suite**

```bash
docker build -t mb-runner-test --target devimage -f docker/Dockerfile .
docker run --rm mb-runner-test python -m pytest app -q
```
Expected: PASS (no volume mount — verifies the committed tree).

- [ ] **Step 2: Verify action discovery**

```bash
docker run --rm mb-runner-test python -c "
from app.actions import action_handlers
from app.actions.core import InternalActionConfiguration, ExecutableActionMixin
print('actions:', sorted(action_handlers.keys()))
_, backfill_cfg, _ = action_handlers['backfill']
_, sub_cfg, _ = action_handlers['backfill_events_for_individual']
print('backfill executable:', issubclass(backfill_cfg, ExecutableActionMixin))
print('sub-action internal:', issubclass(sub_cfg, InternalActionConfiguration))
"
```
Expected: actions include `auth`, `pull_observations`, `pull_events_for_individual`, `backfill`, `backfill_events_for_individual`; backfill executable True; sub-action internal True.

- [ ] **Step 3: Update README**

Add a "Backfill" subsection under the existing Actions section:

```markdown
- **backfill** (executable) — operator-triggered historical load for a study.
  Config: `study_id`, optional `individual_ids` (whole study if empty), `start`
  (a datetime or `"all"`), optional `backfill_max_concurrency`. Seeds a rolling
  work-queue and dispatches per-individual sub-actions.
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
```

- [ ] **Step 4: Commit, push, open PR**

```bash
git add README.md
git commit -m "docs: document backfill actions and settings"
git push -u origin <branch>
gh pr create --repo PADAS/gundi-integration-movebank --base main \
  --title "Add Movebank study backfill" \
  --body "Operator-triggered backfill for full study history. Cascading per-individual sub-actions bounded by a shared per-username connection semaphore (25, under Movebank's 31); rolling queue of 8 in flight; time-budgeted cascade steps; accessory-measurements settling margin (12h) so multi-hour-late records aren't missed; full-cursor hand-off to the steady-state pull at a no-overlap boundary; throttled Activity Log progress. See docs/superpowers/specs/2026-07-21-movebank-backfill-design.md.

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

---

## Notes for the implementer

- **Task 6 is the big one** and depends on a small addition to Task 4's `BackfillJob` (`put_individual_config`/`get_individual_config`) and a rework of Task 5's dispatch loop to go through `_dispatch_backfill_individual`. When executing Task 6, make those two edits as part of it and re-run Tasks 4 and 5's tests to confirm no regression.
- **DRY the watermark logic**: `_advance_watermarks` should be extracted from the existing `action_pull_events_for_individual` (Task 6 step 3) and called by both handlers, not duplicated.
- The `NoConnectionSlot` backoff currently re-triggers immediately; if churn is observed in the stage smoke test, add a jittered delay marker to the command (out of scope here, noted for follow-up).
