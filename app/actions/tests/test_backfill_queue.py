import pytest

from app.actions.backfill_queue import BackfillJob


@pytest.fixture
def job(mocker, fake_backfill_redis):
    mocker.patch("app.actions.backfill_queue._client", return_value=fake_backfill_redis)
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
async def test_is_cancelled_false_by_default(job):
    await job.seed(["a"], total=1, range_repr="r")
    assert await job.is_cancelled() is False


@pytest.mark.asyncio
async def test_cancel_marks_job_and_drops_pending_and_configs(job):
    await job.seed(["a", "b"], total=2, range_repr="r")
    await job.put_individual_config("a", '{"x": 1}')
    await job.cancel()
    assert await job.is_cancelled() is True
    # The meta hash survives so in-flight steps still see the flag ...
    assert await job.exists() is True
    # ... but nothing new can dispatch: pending queue and configs are gone.
    assert await job.next_individual() is None
    assert await job.get_individual_config("a") is None


@pytest.mark.asyncio
async def test_clear_removes_cancelled_flag(job):
    # restart=true recovers a cancelled job: clear() wipes the flag along with
    # the rest of the meta hash.
    await job.seed(["a"], total=1, range_repr="r")
    await job.cancel()
    await job.clear()
    assert await job.is_cancelled() is False


@pytest.mark.asyncio
async def test_requeue_returns_individual_to_pending(job):
    await job.seed(["a", "b"], total=2, range_repr="r")
    assert await job.next_individual() == "a"      # pop a
    await job.requeue("a")                          # put it back
    # a is now at the tail: b comes first, then a.
    assert await job.next_individual() == "b"
    assert await job.next_individual() == "a"
    assert await job.next_individual() is None


@pytest.mark.asyncio
async def test_retire_cancelled_keeps_flag_and_expires_the_meta_hash(job, fake_backfill_redis):
    # The last in-flight step of a cancelled job retires it: working state goes
    # away, but the cancelled flag SURVIVES so a late/duplicate PubSub delivery
    # for the same job still short-circuits instead of resuming the backfill.
    await job.seed(["a", "b"], total=2, range_repr="r")
    await job.put_individual_config("a", '{"x": 1}')
    await job.cancel()

    await job.retire_cancelled(ttl_seconds=3600)

    assert await job.is_cancelled() is True
    assert await job.next_individual() is None
    assert await job.get_individual_config("a") is None
    # The tombstone is bounded — it expires rather than leaking forever.
    assert fake_backfill_redis.store["ttls"][job._meta] == 3600


@pytest.mark.asyncio
async def test_retired_tombstone_stops_reporting_cancelled_once_expired(job, fake_backfill_redis):
    await job.seed(["a"], total=1, range_repr="r")
    await job.cancel()
    await job.retire_cancelled(ttl_seconds=3600)
    # Simulate Redis expiring the tombstone: the job is simply gone again.
    fake_backfill_redis.store["hashes"].pop(job._meta)
    assert await job.is_cancelled() is False
    assert await job.exists() is False
