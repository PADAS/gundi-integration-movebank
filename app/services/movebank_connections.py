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


_shared_client = None


def _client() -> redis.Redis:
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB
        )
    return _shared_client


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
