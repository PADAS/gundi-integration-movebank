from unittest.mock import AsyncMock, MagicMock

import pytest
import redis.asyncio as redis_asyncio

from app import settings
from app.services import reference_cache
from app.services.reference_cache import (
    get_cached_list,
    individuals_key,
    set_cached_list,
    studies_key,
)


@pytest.fixture
def fake_redis(mocker):
    # Reset the singleton so the mock intercepts client creation.
    reference_cache._shared_client = None

    client = MagicMock()
    client.get = AsyncMock(return_value=None)
    client.set = AsyncMock(return_value=True)
    redis_module = MagicMock()
    redis_module.Redis.return_value = client
    mocker.patch("app.services.reference_cache.redis", redis_module)

    yield client
    reference_cache._shared_client = None


URL = "https://www.movebank.org"


def test_keys_separate_accounts_and_hide_the_username():
    a = studies_key(URL, "alice", "pw")
    b = studies_key(URL + "/", " alice ", "pw")
    c = studies_key(URL, "bob", "pw")
    assert a.startswith("movebank:reference:studies:")
    assert a == b and a != c
    assert "alice" not in a.lower()


def test_keys_are_bound_to_the_password():
    # A cache hit answers without calling Movebank, so the entry must only be
    # reachable by a caller holding the same credentials that fetched it: a
    # draft integration naming someone else's username must miss.
    right = studies_key(URL, "alice", "correct horse")
    wrong = studies_key(URL, "alice", "guess")
    assert right != wrong
    assert "correct horse" not in right
    assert individuals_key(URL, "alice", "correct horse", "1") != individuals_key(URL, "alice", "guess", "1")
    # Stable across calls, so the cache is actually shared.
    assert right == studies_key(URL, "alice", "correct horse")


def test_keys_separate_movebank_servers():
    assert studies_key(URL, "alice", "pw") != studies_key("https://test.movebank.org", "alice", "pw")


def test_individuals_key_includes_the_study():
    key = individuals_key("u", "alice", "pw", "1")
    assert key.startswith("movebank:reference:individuals:")
    assert key.endswith(":1")
    assert key != individuals_key("u", "alice", "pw", "2")
    assert key != individuals_key("u", "bob", "pw", "1")
    assert "alice" not in key.lower()


@pytest.mark.asyncio
async def test_roundtrip_uses_the_ttl_setting(fake_redis):
    key = studies_key("u", "alice", "pw")
    await set_cached_list(key, [{"id": "1"}])

    args, kwargs = fake_redis.set.call_args
    assert args[0] == key
    assert kwargs["ex"] == settings.REFERENCE_CACHE_TTL_SECONDS == 600

    fake_redis.get = AsyncMock(return_value=args[1].encode() if isinstance(args[1], str) else args[1])
    assert await get_cached_list(key) == [{"id": "1"}]


@pytest.mark.asyncio
async def test_miss_returns_none(fake_redis):
    assert await get_cached_list("k") is None


@pytest.mark.asyncio
async def test_redis_read_error_returns_none(fake_redis):
    fake_redis.get = AsyncMock(side_effect=redis_asyncio.RedisError("down"))
    assert await get_cached_list("k") is None


@pytest.mark.asyncio
async def test_redis_write_error_is_swallowed(fake_redis):
    fake_redis.set = AsyncMock(side_effect=redis_asyncio.RedisError("down"))
    await set_cached_list("k", [{"id": "1"}])  # must not raise


@pytest.mark.asyncio
@pytest.mark.parametrize("raw", [b"not json", b'{"a": 1}', b'"text"'])
async def test_corrupt_or_non_list_entry_returns_none(fake_redis, raw):
    fake_redis.get = AsyncMock(return_value=raw)
    assert await get_cached_list("k") is None


@pytest.mark.asyncio
async def test_empty_list_is_a_real_answer(fake_redis):
    fake_redis.get = AsyncMock(return_value=b"[]")
    assert await get_cached_list("k") == []
