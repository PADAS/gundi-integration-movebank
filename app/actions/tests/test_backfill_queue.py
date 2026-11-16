import asyncio

import pytest
import pytest_asyncio

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


# ---- step lease -------------------------------------------------------------
# These run against the in-memory fake and, when one is reachable, against a
# real Redis (BACKFILL_TEST_REDIS_URL, default redis://localhost:6379/15), which
# is what exercises the Lua scripts themselves. CI has no Redis and skips those.

TTL = 30


@pytest_asyncio.fixture(params=["fake", "redis"])
async def step_job(request, mocker, fake_backfill_redis):
    if request.param == "fake":
        mocker.patch("app.actions.backfill_queue._client", return_value=fake_backfill_redis)
        job = BackfillJob("int-1", "job-1")
        await job.seed([], total=1, range_repr="r")
        yield job
        return
    import os
    import redis.asyncio as redis
    db = redis.Redis.from_url(os.environ.get("BACKFILL_TEST_REDIS_URL", "redis://localhost:6379/15"))
    try:
        await db.ping()
    except Exception:
        await db.aclose()
        pytest.skip("no Redis reachable for the Lua tests")
    mocker.patch("app.actions.backfill_queue._client", return_value=db)
    job = BackfillJob("int-test-lease", f"job-{os.getpid()}-{id(request)}")
    await job.seed([], total=1, range_repr="r")
    try:
        yield job
    finally:
        await db.delete(job._meta, job._lease("ind-1"), job._lease("ind-2"))
        await db.aclose()


async def _run(job, ind, seq, token="t"):
    """A whole uneventful step: claim, then hand off when acquired."""
    claim = await job.claim_step(ind, seq, token, ttl_seconds=TTL)
    if claim == "acquired":
        assert await job.finish_step(ind, seq, token) is True
    return claim


@pytest.mark.asyncio
async def test_claim_step_accepts_each_step_once_in_order(step_job):
    job = step_job
    assert await _run(job, "ind-1", 0) == "acquired"
    assert await _run(job, "ind-1", 0) == "stale"      # duplicate delivery of step 0
    assert await _run(job, "ind-1", 1) == "acquired"   # the re-trigger
    assert await _run(job, "ind-1", 0) == "stale"      # late duplicate of step 0
    assert await _run(job, "ind-1", 1) == "stale"      # duplicate of step 1
    assert await _run(job, "ind-1", 2) == "acquired"


@pytest.mark.asyncio
async def test_late_duplicate_cannot_make_the_real_next_step_look_like_a_duplicate(step_job):
    # Review on PR 38: with the chain on step 5, a late duplicate of step 4 and
    # the real step 5 arrive together. Under the old increment-then-check they
    # could both bump the counter and both be rejected, losing the chain.
    # The scripted claim never mutates on rejection, in either order.
    job = step_job
    for seq in range(5):
        assert await _run(job, "ind-1", seq) == "acquired"
    stale, real = await asyncio.gather(
        job.claim_step("ind-1", 4, "dup", ttl_seconds=TTL),
        job.claim_step("ind-1", 5, "real", ttl_seconds=TTL),
    )
    assert (stale, real) == ("stale", "acquired")
    assert await job.claim_step("ind-1", 4, "dup2", ttl_seconds=TTL) == "stale"
    assert await job.finish_step("ind-1", 5, "real") is True
    assert await _run(job, "ind-1", 6) == "acquired"


@pytest.mark.asyncio
async def test_concurrent_deliveries_of_one_step_admit_exactly_one(step_job):
    job = step_job
    results = await asyncio.gather(*[
        job.claim_step("ind-1", 0, f"w{i}", ttl_seconds=TTL) for i in range(5)
    ])
    assert sorted(results) == ["acquired"] + ["busy"] * 4


@pytest.mark.asyncio
async def test_successor_waits_until_its_predecessor_hands_off(step_job):
    # The step publishes its successor before handing off; the successor sees
    # the predecessor's lease and must wait, not run alongside it.
    job = step_job
    assert await job.claim_step("ind-1", 3, "pred", ttl_seconds=TTL) == "acquired"
    assert await job.claim_step("ind-1", 4, "succ", ttl_seconds=TTL) == "busy"
    assert await job.finish_step("ind-1", 3, "pred") is True
    assert await job.claim_step("ind-1", 4, "succ", ttl_seconds=TTL) == "acquired"


@pytest.mark.asyncio
async def test_released_step_is_claimable_again_by_its_redelivery(step_job):
    # Review on PR 38: a step that fails after claiming must stay recoverable.
    job = step_job
    assert await job.claim_step("ind-1", 2, "first", ttl_seconds=TTL) == "acquired"
    await job.release_step("ind-1", "first")
    assert await job.claim_step("ind-1", 2, "redelivery", ttl_seconds=TTL) == "acquired"


@pytest.mark.asyncio
async def test_lapsed_lease_lets_a_redelivery_take_over_and_shuts_out_the_old_holder(step_job):
    job = step_job
    assert await job.claim_step("ind-1", 2, "dead", ttl_seconds=TTL) == "acquired"
    assert await job.claim_step("ind-1", 2, "redelivery", ttl_seconds=TTL) == "busy"
    await job.db.delete(job._lease("ind-1"))          # the TTL runs out
    assert await job.claim_step("ind-1", 2, "redelivery", ttl_seconds=TTL) == "acquired"
    # The old holder, if it was only stalled, can no longer renew, finish or release.
    assert await job.renew_step_lease("ind-1", "dead", ttl_seconds=TTL) is False
    assert await job.finish_step("ind-1", 2, "dead") is False
    await job.release_step("ind-1", "dead")
    assert await job.claim_step("ind-1", 2, "third", ttl_seconds=TTL) == "busy"
    assert await job.renew_step_lease("ind-1", "redelivery", ttl_seconds=TTL) is True


@pytest.mark.asyncio
async def test_claim_step_without_a_counter_accepts_a_legacy_message(step_job):
    # A chain started before step numbering existed carries no seq; whatever
    # it sends first is accepted and counting starts there.
    job = step_job
    assert await _run(job, "ind-1", 7) == "acquired"
    assert await _run(job, "ind-1", 7) == "stale"
    assert await _run(job, "ind-1", 8) == "acquired"


@pytest.mark.asyncio
async def test_reset_steps_lets_a_fresh_dispatch_start_at_zero(step_job):
    job = step_job
    assert await _run(job, "ind-1", 0) == "acquired"
    assert await _run(job, "ind-1", 1) == "acquired"
    await job.reset_steps("ind-1")
    assert await _run(job, "ind-1", 0) == "acquired"


@pytest.mark.asyncio
async def test_claim_step_counters_are_per_individual(step_job):
    job = step_job
    assert await job.claim_step("ind-1", 0, "a", ttl_seconds=TTL) == "acquired"
    assert await job.claim_step("ind-2", 0, "b", ttl_seconds=TTL) == "acquired"


@pytest.mark.asyncio
async def test_claim_does_not_recreate_a_cleared_job(step_job):
    job = step_job
    await job.clear()
    assert await job.claim_step("ind-1", 0, "a", ttl_seconds=TTL) == "acquired"
    assert await job.exists() is False


@pytest.mark.asyncio
async def test_claim_step_rejects_a_delivery_from_an_earlier_run(step_job):
    from app.actions.backfill_queue import STEP_ACQUIRED, STEP_STALE_RUN
    current = await step_job.run_id()
    assert current
    assert await step_job.claim_step("ind-1", 0, "old", ttl_seconds=30, run_id="run-before-cancel") == STEP_STALE_RUN
    # A message from before run ids existed is not checked.
    assert await step_job.claim_step("ind-1", 0, "legacy", ttl_seconds=30, run_id="") == STEP_ACQUIRED
    assert await step_job.finish_step("ind-1", 0, "legacy")
    assert await step_job.claim_step("ind-1", 1, "t", ttl_seconds=30, run_id=current) == STEP_ACQUIRED
