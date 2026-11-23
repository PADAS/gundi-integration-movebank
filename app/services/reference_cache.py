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


# Slow-hash work factor for the credential part of the key, and the memo that
# keeps it off the per-keystroke path (keyed by a fast digest, so the memo
# holds no credentials).
_KDF_ITERATIONS = 100_000
_scope_memo = {}
_SCOPE_MEMO_MAX = 1024


def _account_scope(base_url: str, username: str, password: str) -> str:
    """Scope an entry to the Movebank server and the exact credentials that
    fetched it.

    A cache hit answers without calling Movebank, so nothing re-checks the
    password: an entry keyed by username alone would hand one account's lists
    to anyone naming that username (a reference action runs against a draft
    integration with caller-supplied credentials). Binding the key to the
    password means a caller without it cannot reach the entry.

    The password goes through PBKDF2, salted with the server and username, so
    a Redis key listing is not a cheap way to test password guesses.
    """
    normalized_url = (base_url or "").strip().rstrip("/")
    normalized_username = (username or "").strip()
    salt = f"{normalized_url}|{normalized_username}".encode("utf-8")
    secret = (password or "").encode("utf-8")
    memo_key = hashlib.sha256(salt + b"|" + secret).digest()
    credential_digest = _scope_memo.get(memo_key)
    if credential_digest is None:
        credential_digest = hashlib.pbkdf2_hmac("sha256", secret, salt, _KDF_ITERATIONS).hex()[:32]
        if len(_scope_memo) >= _SCOPE_MEMO_MAX:
            _scope_memo.clear()
        _scope_memo[memo_key] = credential_digest
    return f"{_digest(normalized_url)}:{credential_digest}"


def studies_key(base_url: str, username: str, password: str) -> str:
    return f"movebank:reference:studies:{_account_scope(base_url, username, password)}"


def individuals_key(base_url: str, username: str, password: str, study_id: str) -> str:
    return f"movebank:reference:individuals:{_account_scope(base_url, username, password)}:{study_id}"


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
