from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from app.actions.configurations import AuthenticateConfig


@pytest.fixture(autouse=True)
def mock_activity_publish(mocker):
    """Keep @activity_logger from touching PubSub in tests."""
    mocker.patch("app.services.activity_logger.publish_event", AsyncMock(return_value=None))


@pytest.fixture(autouse=True)
def mock_connection_slot(mocker):
    """Stub the Redis-backed connection semaphore so handler tests don't need a
    live Redis. Tests that care about slot acquisition re-patch it explicitly."""
    @asynccontextmanager
    async def _noop_slot(username, *, ttl_seconds=600):
        yield

    return mocker.patch("app.actions.handlers.movebank_slot", side_effect=_noop_slot)


@pytest.fixture(autouse=True)
def mock_state_manager_redis(mocker):
    """Stub the state manager's Redis calls so handler tests don't need a live
    Redis. Tests that care about specific saved state re-patch state_manager
    explicitly (e.g. the mock_state_store fixture below)."""
    mocker.patch("app.actions.handlers.state_manager.get_state", AsyncMock(return_value={}))
    mocker.patch("app.actions.handlers.state_manager.set_state", AsyncMock(return_value=None))


@pytest.fixture
def integration():
    integration = MagicMock()
    integration.id = uuid4()
    integration.base_url = "https://www.movebank.org"
    integration.name = "Movebank Test"
    return integration


@pytest.fixture
def mock_auth_config(mocker):
    mocker.patch(
        "app.actions.client.get_auth_config",
        return_value=AuthenticateConfig(username="user", password="pass"),
    )


@pytest.fixture
def mock_movebank_client(mocker):
    """A MovebankClient instance mock usable as an async context manager."""
    mb = MagicMock()
    mb.__aenter__ = AsyncMock(return_value=mb)
    mb.__aexit__ = AsyncMock(return_value=False)
    mocker.patch("app.actions.client.MovebankClient", return_value=mb)
    return mb


def make_events_generator(events):
    """Build a stand-in for the async-generator method get_individual_events_by_time."""
    async def _gen(**kwargs):
        for event in events:
            yield event
    return _gen


INDIVIDUAL_ROW = {
    "id": "111",
    "local_identifier": "tag-1",
    "nick_name": "Aquila",
    "ring_id": "R1",
    "sex": "f",
    "taxon_canonical_name": "Aquila chrysaetos",
    "timestamp_start": "2025-01-01 00:00:00.000",
    "timestamp_end": "2026-07-01 00:00:00.000",
    "number_of_events": "100",
    "number_of_deployments": "1",
    "sensor_type_ids": "gps",
    "taxon_detail": "",
}
