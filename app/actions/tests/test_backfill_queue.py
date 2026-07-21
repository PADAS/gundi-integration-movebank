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


@pytest.mark.asyncio
async def test_pending_remaining_in_snapshot(job):
    await job.seed(["a", "b", "c"], total=3, range_repr="r")
    snap = await job.snapshot()
    assert snap["pending_remaining"] == 3
    await job.next_individual()
    snap = await job.snapshot()
    assert snap["pending_remaining"] == 2
