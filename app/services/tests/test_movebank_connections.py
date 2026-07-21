from unittest.mock import AsyncMock, MagicMock

import pytest

from app.services import movebank_connections
from app.services.movebank_connections import (
    NoConnectionSlot,
    connection_key,
    movebank_slot,
)


def test_connection_key_hashes_username():
    key = connection_key("gundi_user")
    assert key.startswith("movebank:connections:")
    # Plaintext username must not appear in the key.
    assert "gundi_user" not in key
    # Stable and same-length for any username.
    assert key == connection_key("gundi_user")
    assert len(connection_key("a")) == len(connection_key("a-much-longer-name"))


@pytest.fixture
def mock_redis(mocker):
    # Reset singleton before each test so the mock can intercept client creation
    movebank_connections._shared_client = None

    client = MagicMock()
    client.eval = AsyncMock(return_value=1)      # acquired by default
    client.zrem = AsyncMock(return_value=1)
    redis_module = MagicMock()
    redis_module.Redis.return_value = client
    mocker.patch("app.services.movebank_connections.redis", redis_module)

    # Clean up after test
    yield client
    movebank_connections._shared_client = None


@pytest.mark.asyncio
async def test_movebank_slot_acquires_and_releases(mock_redis):
    async with movebank_slot("gundi_user"):
        pass
    assert mock_redis.eval.await_count == 1      # acquire
    assert mock_redis.zrem.await_count == 1      # release


@pytest.mark.asyncio
async def test_movebank_slot_raises_when_full(mock_redis):
    mock_redis.eval = AsyncMock(return_value=0)  # at capacity
    with pytest.raises(NoConnectionSlot):
        async with movebank_slot("gundi_user"):
            pass
    # Nothing acquired, so nothing released.
    assert mock_redis.zrem.await_count == 0


@pytest.mark.asyncio
async def test_movebank_slot_releases_on_exception(mock_redis):
    with pytest.raises(ValueError):
        async with movebank_slot("gundi_user"):
            raise ValueError("boom")
    assert mock_redis.zrem.await_count == 1      # released despite the error
