from typing import List, Optional

import redis.asyncio as redis

from app import settings


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

    async def exists(self) -> bool:
        """Whether this job has already been seeded (i.e. is active). Used to
        make action_backfill's seed step idempotent against a redelivered or
        double-clicked command hashing to the same job_id."""
        return bool(await self.db.exists(self._meta))

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
