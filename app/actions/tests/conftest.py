from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.actions.configurations import AuthenticateConfig


@pytest.fixture(autouse=True)
def mock_activity_publish(mocker):
    """Keep @activity_logger from touching PubSub in tests."""
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def mock_connection_slot(mocker):
    """Stub the Redis-backed connection semaphore so handler tests don't need a
    live Redis. Tests that care about slot acquisition re-patch it explicitly."""
    @asynccontextmanager
    async def _noop_slot(username, *, ttl_seconds=600):
        yield

    return mocker.patch("app.actions.handlers.movebank_slot", side_effect=_noop_slot)


@pytest.fixture(autouse=True)
def mock_state_manager_redis(mocker):
    """Stub the state manager's Redis calls so handler tests don't need a live
    Redis. Tests that care about specific saved state re-patch state_manager
    explicitly (e.g. the mock_state_store fixture below)."""
    mocker.patch("app.actions.handlers.state_manager.get_state", AsyncMock(return_value={}))
    mocker.patch("app.actions.handlers.state_manager.set_state", AsyncMock(return_value=None))
    mocker.patch("app.actions.handlers.state_manager.delete_state", AsyncMock(return_value=None))


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


@pytest.fixture
def fake_backfill_redis():
    """Minimal in-memory stand-in for the subset of redis.asyncio that
    BackfillJob uses. Hashes are keyed by their Redis key so distinct hashes
    (meta vs. the per-individual configs hash) don't collide on field names.
    Shared by the queue unit tests and the handler tests that need real
    key-level semantics (e.g. whether a flag survives a cleanup)."""
    store = {"hashes": {}, "list": [], "ttls": {}}

    client = MagicMock()

    async def hincrby(key, field, n):
        h = store["hashes"].setdefault(key, {})
        h[field] = int(h.get(field, 0)) + n
        return h[field]

    async def hset(key, mapping=None, **kw):
        h = store["hashes"].setdefault(key, {})
        h.update(mapping or kw)

    async def hgetall(key):
        return {k: str(v) for k, v in store["hashes"].get(key, {}).items()}

    async def hget(key, field):
        return store["hashes"].get(key, {}).get(field)

    async def hdel(key, *fields):
        h = store["hashes"].get(key, {})
        for f in fields:
            h.pop(f, None)

    async def exists(key):
        return 1 if store["hashes"].get(key) else 0

    async def rpush(key, *vals):
        store["list"].extend(vals)
        return len(store["list"])

    async def lpop(key):
        return store["list"].pop(0) if store["list"] else None

    async def llen(key):
        return len(store["list"])

    async def delete(*keys):
        for key in keys:
            store["hashes"].pop(key, None)
            store["ttls"].pop(key, None)
            if key.endswith(".pending"):
                store["list"].clear()

    async def expire(key, seconds):
        store["ttls"][key] = seconds
        return 1

    client.hincrby = AsyncMock(side_effect=hincrby)
    client.hset = AsyncMock(side_effect=hset)
    client.hgetall = AsyncMock(side_effect=hgetall)
    client.hget = AsyncMock(side_effect=hget)
    client.hdel = AsyncMock(side_effect=hdel)
    client.exists = AsyncMock(side_effect=exists)
    client.rpush = AsyncMock(side_effect=rpush)
    client.lpop = AsyncMock(side_effect=lpop)
    client.llen = AsyncMock(side_effect=llen)
    client.delete = AsyncMock(side_effect=delete)
    client.expire = AsyncMock(side_effect=expire)
    # Expose the backing store so tests can assert TTLs and simulate expiry.
    client.store = store
    return client
