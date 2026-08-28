from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as redis_asyncio

from app.services import study_attributes_cache
from app.services.study_attributes_cache import (
    cache_key,
    get_cached_study_attributes,
    set_cached_study_attributes,
)


@pytest.fixture
def mock_redis(mocker):
    # Reset the singleton so the mock intercepts client creation.
    study_attributes_cache._shared_client = None

    client = MagicMock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock(return_value=True)
    redis_module = MagicMock()
    redis_module.Redis.return_value = client
    redis_module.RedisError = redis_asyncio.RedisError
    mocker.patch("app.services.study_attributes_cache.redis", redis_module)

    yield client
    study_attributes_cache._shared_client = None


def test_cache_key_is_namespaced_and_not_integration_scoped():
    key = cache_key("https://www.movebank.org", "1573471517", 653)
    assert key.startswith("movebank:study_attributes:")
    assert "1573471517" in key
    assert key.endswith(":653")
    # Stable across calls, and int/str sensor ids collapse to one key.
    assert key == cache_key("https://www.movebank.org", "1573471517", "653")


def test_cache_key_separates_movebank_servers():
    assert cache_key("https://www.movebank.org", "1", 653) != cache_key(
        "https://test.movebank.org", "1", 653
    )


@pytest.mark.asyncio
async def test_get_returns_none_on_miss(mock_redis):
    assert await get_cached_study_attributes("https://www.movebank.org", "1", 653) is None


@pytest.mark.asyncio
async def test_get_returns_decoded_attributes_on_hit(mock_redis):
    mock_redis.get = AsyncMock(
        return_value=b'[{"short_name": "gps_dop", "sensor_type_id": "653"}]'
    )
    result = await get_cached_study_attributes("https://www.movebank.org", "1", 653)
    assert result == [{"short_name": "gps_dop", "sensor_type_id": "653"}]


@pytest.mark.asyncio
async def test_get_returns_empty_list_distinctly_from_miss(mock_redis):
    # An empty attribute list is a legitimate cached answer, not a miss.
    mock_redis.get = AsyncMock(return_value=b"[]")
    assert await get_cached_study_attributes("https://www.movebank.org", "1", 653) == []


@pytest.mark.asyncio
async def test_set_writes_json_with_a_ttl(mock_redis):
    await set_cached_study_attributes(
        "https://www.movebank.org", "1", 653, [{"short_name": "gps_dop"}]
    )
    mock_redis.set.assert_awaited_once()
    args, kwargs = mock_redis.set.await_args
    assert args[0] == cache_key("https://www.movebank.org", "1", 653)
    assert args[1] == '[{"short_name": "gps_dop"}]'
    assert kwargs["ex"] > 0


@pytest.mark.asyncio
async def test_get_fails_open_on_redis_error(mock_redis):
    mock_redis.get = AsyncMock(side_effect=redis_asyncio.RedisError("down"))
    # A cache outage must degrade to a live fetch, never break the pull.
    assert await get_cached_study_attributes("https://www.movebank.org", "1", 653) is None


@pytest.mark.asyncio
async def test_get_fails_open_on_corrupt_payload(mock_redis):
    mock_redis.get = AsyncMock(return_value=b"not json")
    assert await get_cached_study_attributes("https://www.movebank.org", "1", 653) is None


@pytest.mark.asyncio
async def test_set_fails_open_on_redis_error(mock_redis):
    mock_redis.set = AsyncMock(side_effect=redis_asyncio.RedisError("down"))
    await set_cached_study_attributes("https://www.movebank.org", "1", 653, [])


def test_cache_key_ignores_trailing_slashes_and_whitespace():
    # A portal-configured base_url may carry a trailing slash or stray whitespace.
    # Those must not split one study's entry across several keys.
    canonical = cache_key("https://www.movebank.org", "1", 653)
    assert cache_key("https://www.movebank.org/", "1", 653) == canonical
    assert cache_key("https://www.movebank.org///", "1", 653) == canonical
    assert cache_key("  https://www.movebank.org  ", "1", 653) == canonical


@pytest.mark.asyncio
async def test_get_rejects_a_non_list_payload(mock_redis):
    # movebank-client iterates the result and calls item.get(...), so a dict or
    # string would raise AttributeError mid-pull. Treat it as corruption.
    mock_redis.get = AsyncMock(return_value=b'{"short_name": "gps_dop"}')
    assert await get_cached_study_attributes("https://www.movebank.org", "1", 653) is None


@pytest.mark.asyncio
async def test_get_rejects_a_scalar_payload(mock_redis):
    mock_redis.get = AsyncMock(return_value=b'"gps_dop"')
    assert await get_cached_study_attributes("https://www.movebank.org", "1", 653) is None
