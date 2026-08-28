import hashlib
import json
import logging

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


def cache_key(base_url: str, study_id, sensor_type_id) -> str:
    """Key a study's attribute list by the Movebank server it came from, not by
    integration: the list is a property of the study, so every integration
    reading that study — and every individual within it — shares one entry.

    The base_url is normalised before hashing so a portal-configured trailing
    slash or stray whitespace doesn't split one study's entry across several
    keys. It is hashed only to keep the key a fixed, punctuation-free length;
    there is nothing secret about it.
    """
    normalized_url = (base_url or "").strip().rstrip("/")
    digest = hashlib.sha256(normalized_url.encode("utf-8")).hexdigest()[:16]
    return f"movebank:study_attributes:{digest}:{study_id}:{sensor_type_id}"


async def get_cached_study_attributes(base_url: str, study_id, sensor_type_id):
    """Return the cached attribute list, or None if absent.

    None means "ask Movebank" — an empty list is a real answer (a study that
    advertises no attributes) and is returned as such. Every failure path also
    returns None, so a Redis outage or a corrupt entry degrades to a live fetch
    rather than breaking the pull.
    """
    key = cache_key(base_url, study_id, sensor_type_id)
    try:
        raw = await _client().get(key)
    except Exception as exc:
        logger.warning(f"Study attributes cache read failed for {key} (fetching live): {exc}")
        return None
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError) as exc:
        logger.warning(f"Discarding corrupt study attributes cache entry {key}: {exc}")
        return None
    if not isinstance(data, list):
        # Callers iterate the result and call item.get(...), so anything but a
        # list would raise AttributeError mid-pull. Same class of corruption as
        # unparseable JSON — discard it and fetch live.
        logger.warning(
            f"Discarding non-list study attributes cache entry {key}: got {type(data).__name__}"
        )
        return None
    return data


async def set_cached_study_attributes(base_url: str, study_id, sensor_type_id, attributes) -> None:
    """Cache a study's attribute list. Best-effort: a write failure just means
    the next caller fetches live."""
    key = cache_key(base_url, study_id, sensor_type_id)
    try:
        await _client().set(
            key,
            json.dumps(attributes),
            ex=settings.MOVEBANK_STUDY_ATTRIBUTES_TTL_SECONDS,
        )
    except Exception as exc:
        logger.warning(f"Study attributes cache write failed for {key}: {exc}")
