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
