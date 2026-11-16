from typing import List, Optional

import redis.asyncio as redis

from app import settings


STEP_ACQUIRED = "acquired"
STEP_STALE = "stale"
STEP_BUSY = "busy"

# Step claim scripts. KEYS[1] = job meta hash, KEYS[2] = the individual's lease
# key. The meta field `seq.<id>` is the step the chain is on (the lowest step
# number not yet finished). A missing field means a fresh dispatch or a chain
# started before step numbering: the first delivery is accepted and counting
# starts at its number. A missing meta hash (job cleared) is never re-created.
CLAIM_STEP_LUA = """
local current = redis.call('HGET', KEYS[1], ARGV[1])
local seq = tonumber(ARGV[2])
if current and seq < tonumber(current) then
  return 0
end
if not redis.call('SET', KEYS[2], ARGV[3], 'NX', 'PX', ARGV[4]) then
  return -1
end
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], ARGV[1], seq)
end
return 1
"""

RENEW_STEP_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('PEXPIRE', KEYS[1], ARGV[2])
end
return 0
"""

FINISH_STEP_LUA = """
if redis.call('GET', KEYS[2]) ~= ARGV[3] then
  return 0
end
redis.call('DEL', KEYS[2])
if redis.call('EXISTS', KEYS[1]) == 1 then
  local done = tonumber(ARGV[2]) + 1
  local current = tonumber(redis.call('HGET', KEYS[1], ARGV[1]) or '-1')
  if current < done then
    redis.call('HSET', KEYS[1], ARGV[1], done)
  end
end
return 1
"""

RELEASE_STEP_LUA = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""


# Lazily-created module-level singleton (mirrors app.services.movebank_connections'
# _client pattern): one Redis connection shared across every BackfillJob instance
# instead of a new one per instantiation.
_shared_client = None


def _client() -> redis.Redis:
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB
        )
    return _shared_client


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

    async def requeue(self, individual_id: str) -> None:
        """Return a popped individual to the pending queue (e.g. when its
        dispatch failed to publish), so it isn't lost."""
        await self.db.rpush(self._pending, individual_id)

    async def incr_in_flight(self, n: int = 1) -> int:
        return await self.db.hincrby(self._meta, "in_flight", n)

    async def decr_in_flight(self) -> int:
        value = await self.db.hincrby(self._meta, "in_flight", -1)
        if value < 0:
            await self.db.hset(self._meta, mapping={"in_flight": 0})
            return 0
        return value

    async def record_completion(self, observations: int) -> None:
        await self.db.hincrby(self._meta, "completed", 1)
        await self.db.hincrby(self._meta, "observations_sent", observations)

    async def incr_attempts(self, individual_id: str) -> int:
        return await self.db.hincrby(self._meta, f"attempts.{individual_id}", 1)

    async def reset_attempts(self, individual_id: str) -> None:
        await self.db.hdel(self._meta, f"attempts.{individual_id}")

    def _lease(self, individual_id: str) -> str:
        return f"{self._meta}.lease.{individual_id}"

    async def claim_step(self, individual_id: str, seq: int, token: str, *, ttl_seconds: int) -> str:
        """Try to take step `seq` of this individual's chain. Returns
        STEP_ACQUIRED (run it), STEP_STALE (an older step: a duplicate delivery
        of work already handed off; ack and drop) or STEP_BUSY (another worker
        holds the lease on this or the previous step; wait and ask again).

        One Lua script, so the compare and the advance are atomic: two
        deliveries can never both see themselves as next, and a rejected
        delivery never moves the counter. The lease is a separate key with a
        TTL that the holder renews (renew_step_lease); if the holder dies
        without finishing, the lease lapses and a redelivery of the same step
        takes it over instead of being dropped as a duplicate.
        """
        result = await self.db.eval(
            CLAIM_STEP_LUA, 2, self._meta, self._lease(individual_id),
            f"seq.{individual_id}", seq, token, int(ttl_seconds * 1000),
        )
        return {1: STEP_ACQUIRED, 0: STEP_STALE}.get(int(result), STEP_BUSY)

    async def renew_step_lease(self, individual_id: str, token: str, *, ttl_seconds: int) -> bool:
        """Extend our lease. False when it lapsed and someone else may hold it."""
        result = await self.db.eval(
            RENEW_STEP_LUA, 1, self._lease(individual_id), token, int(ttl_seconds * 1000),
        )
        return int(result) == 1

    async def finish_step(self, individual_id: str, seq: int, token: str) -> bool:
        """Hand off: mark step `seq` done (every later delivery of it is stale)
        and drop the lease. Called after the step published its successor or
        finalized, so the successor waits on the lease, never the reverse.
        False when the lease was no longer ours."""
        result = await self.db.eval(
            FINISH_STEP_LUA, 2, self._meta, self._lease(individual_id),
            f"seq.{individual_id}", seq, token,
        )
        return int(result) == 1

    async def release_step(self, individual_id: str, token: str) -> None:
        """Drop our lease without advancing, so a redelivery of this unfinished
        step can run it again."""
        await self.db.eval(RELEASE_STEP_LUA, 1, self._lease(individual_id), token)

    async def reset_steps(self, individual_id: str) -> None:
        """Forget the chain position before a fresh dispatch, so the new chain's
        step 0 is not mistaken for a duplicate of the old one."""
        await self.db.hdel(self._meta, f"seq.{individual_id}")
        await self.db.delete(self._lease(individual_id))

    async def exists(self) -> bool:
        """Whether this job has already been seeded (i.e. is active). Used to
        make action_backfill's seed step idempotent against a redelivered or
        double-clicked command hashing to the same job_id."""
        return bool(await self.db.exists(self._meta))

    async def cancel(self) -> None:
        """Mark the job cancelled and drop its pending queue + configs so
        nothing new can dispatch. The meta hash (with the flag) survives so
        in-flight steps observe it at their next trigger and unwind; the last
        one to drain calls retire_cancelled(), which keeps the flag as a TTL'd
        tombstone (see action_backfill_events_for_individual)."""
        await self.db.hset(self._meta, mapping={"cancelled": 1})
        await self.db.delete(self._pending, f"{self._meta}.configs")

    async def retire_cancelled(self, *, ttl_seconds: int) -> None:
        """Retire a cancelled job whose in-flight work has drained: drop the
        working state but leave the meta hash — carrying `cancelled=1` — as a
        TTL'd tombstone.

        The tombstone is what stops a late or duplicate PubSub delivery (the
        commands topic is at-least-once, and main.py always acks) from resuming
        a cancelled backfill: without it `is_cancelled()` would read a missing
        key as False and the step would fetch from its retained watermark and
        cascade onward. Bounded by the TTL so it can't leak, and dropped up
        front by an intentional re-run or restart."""
        await self.db.delete(self._pending, f"{self._meta}.configs")
        await self.db.expire(self._meta, ttl_seconds)

    async def is_cancelled(self) -> bool:
        value = await self.db.hget(self._meta, "cancelled")
        if isinstance(value, bytes):
            value = value.decode()
        return value in ("1", 1)

    async def clear(self) -> None:
        """Delete every Redis key for this job (meta hash, pending queue, and the
        per-individual configs hash). Used by a restart to wipe a stuck or
        finished job before re-seeding."""
        await self.db.delete(self._meta, self._pending, f"{self._meta}.configs")

    async def put_individual_config(self, individual_id: str, config_json: str) -> None:
        await self.db.hset(f"{self._meta}.configs", mapping={individual_id: config_json})

    async def get_individual_config(self, individual_id: str) -> Optional[str]:
        raw = await self.db.hget(f"{self._meta}.configs", individual_id)
        return (raw.decode() if isinstance(raw, bytes) else raw) if raw else None

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
            "pending_remaining": await self.db.llen(self._pending),
            "range": data.get("range", ""),
        }
