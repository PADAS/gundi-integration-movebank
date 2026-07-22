from unittest.mock import AsyncMock, MagicMock

import pytest

from app.actions.backfill_queue import BackfillJob


@pytest.fixture
def fake_redis():
    # Minimal in-memory stand-in for the subset of redis.asyncio used here.
    # Hashes are keyed by their Redis key so distinct hashes (meta vs. the
    # per-individual configs hash) don't collide on field names.
    store = {"hashes": {}, "list": []}

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

    client.hincrby = AsyncMock(side_effect=hincrby)
    client.hset = AsyncMock(side_effect=hset)
    client.hgetall = AsyncMock(side_effect=hgetall)
    client.hget = AsyncMock(side_effect=hget)
    client.hdel = AsyncMock(side_effect=hdel)
    client.exists = AsyncMock(side_effect=exists)
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


@pytest.mark.asyncio
async def test_put_and_get_individual_config(job):
    await job.put_individual_config("a", '{"foo": "bar"}')
    assert await job.get_individual_config("a") == '{"foo": "bar"}'


@pytest.mark.asyncio
async def test_get_individual_config_missing_returns_none(job):
    assert await job.get_individual_config("missing-id") is None


@pytest.mark.asyncio
async def test_individual_configs_do_not_collide_with_meta_hash(job):
    # Meta hash and configs hash are distinct Redis keys, so an individual id
    # that happens to match a meta field name must not leak/overwrite it.
    await job.seed(["a"], total=1, range_repr="r")
    await job.put_individual_config("total", '{"total-config": true}')
    snap = await job.snapshot()
    assert snap["total"] == 1
    assert await job.get_individual_config("total") == '{"total-config": true}'


@pytest.mark.asyncio
async def test_reset_attempts_clears_the_counter(job):
    await job.seed(["a"], total=1, range_repr="r")
    assert await job.incr_attempts("a") == 1
    assert await job.incr_attempts("a") == 2
    await job.reset_attempts("a")
    # A fresh incr after reset starts back at 1, not 3.
    assert await job.incr_attempts("a") == 1


@pytest.mark.asyncio
async def test_reset_attempts_is_a_noop_when_never_incremented(job):
    await job.seed(["a"], total=1, range_repr="r")
    await job.reset_attempts("a")  # must not raise
    assert await job.incr_attempts("a") == 1


@pytest.mark.asyncio
async def test_exists_false_before_seed_true_after(job):
    assert await job.exists() is False
    await job.seed(["a"], total=1, range_repr="r")
    assert await job.exists() is True


@pytest.mark.asyncio
async def test_requeue_returns_individual_to_pending(job):
    await job.seed(["a", "b"], total=2, range_repr="r")
    assert await job.next_individual() == "a"      # pop a
    await job.requeue("a")                          # put it back
    # a is now at the tail: b comes first, then a.
    assert await job.next_individual() == "b"
    assert await job.next_individual() == "a"
    assert await job.next_individual() is None
