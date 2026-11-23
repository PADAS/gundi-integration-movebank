"""Redis cache for the lists behind the portal's study and individual pickers.

The portal calls a reference action on every keystroke, and Movebank has no
name search: answering means downloading the whole list. Caching a trimmed
copy keeps that to one Movebank request per TTL, so lookups neither hammer
Movebank nor compete with pulls and backfills for connection slots.
"""
import hashlib
import json
import logging
from typing import Optional

import redis.asyncio as redis

from app import settings

logger = logging.getLogger(__name__)


_shared_client = None


def _client() -> redis.Redis:
    global _shared_client
    if _shared_client is None:
        _shared_client = redis.Redis(
            host=settings.REDIS_HOST, port=settings.REDIS_PORT, db=settings.REDIS_STATE_DB
        )
    return _shared_client


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _account_scope(base_url: str, username: str) -> str:
    """Both lists depend on what the account may see, so entries are scoped to
    the Movebank server and the account. The username is hashed so it cannot
    be read out of a Redis key listing; the URL only to keep the key tidy."""
    normalized_url = (base_url or "").strip().rstrip("/")
    normalized_username = (username or "").strip().lower()
    return f"{_digest(normalized_url)}:{_digest(normalized_username)}"


def studies_key(base_url: str, username: str) -> str:
    return f"movebank:reference:studies:{_account_scope(base_url, username)}"


def individuals_key(base_url: str, username: str, study_id: str) -> str:
    return f"movebank:reference:individuals:{_account_scope(base_url, username)}:{study_id}"


async def get_cached_list(key: str) -> Optional[list]:
    """Return the cached list, or None meaning "fetch live".

    An empty list is a real answer. Every failure (Redis down, corrupt or
    non-list entry) returns None, so the lookup degrades to a live fetch.
    """
    try:
        raw = await _client().get(key)
    except Exception as exc:
        logger.warning(f"Reference cache read failed for {key} (fetching live): {exc}")
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning(f"Discarding corrupt reference cache entry {key}: {exc}")
        return None
    if not isinstance(data, list):
        logger.warning(f"Discarding non-list reference cache entry {key}: got {type(data).__name__}")
        return None
    return data


async def set_cached_list(key: str, rows: list) -> None:
    """Best-effort write: a failure just means the next caller fetches live."""
    try:
        await _client().set(key, json.dumps(rows), ex=settings.REFERENCE_CACHE_TTL_SECONDS)
    except Exception as exc:
        logger.warning(f"Reference cache write failed for {key}: {exc}")
