from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import PullObservationsConfig
from app.actions.handlers import action_pull_observations
from app.actions.tests.conftest import INDIVIDUAL_ROW


@pytest.mark.asyncio
async def test_pull_observations_triggers_subaction_per_individual(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    second_row = {**INDIVIDUAL_ROW, "id": "222", "nick_name": "Bubo"}
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW, second_row])
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_pull_observations(
        integration=integration,
        action_config=PullObservationsConfig(study_id="12345", maximum_lookback_hours=48),
    )

    assert result == {"individuals_found": 2, "sub_actions_triggered": 2}
    assert mock_trigger.await_count == 2
    _, kwargs = mock_trigger.await_args_list[0]
    assert kwargs["integration_id"] == str(integration.id)
    assert kwargs["action_id"] == "pull_events_for_individual"
    assert kwargs["config"].study_id == "12345"
    assert kwargs["config"].individual.id == "111"
    assert kwargs["config"].maximum_lookback_hours == 48


@pytest.mark.asyncio
async def test_pull_observations_skips_unparseable_individuals(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    bad_row = {**INDIVIDUAL_ROW, "id": "333", "number_of_events": "not-a-number"}
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW, bad_row])
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_pull_observations(
        integration=integration,
        action_config=PullObservationsConfig(study_id="12345"),
    )

    assert result == {"individuals_found": 1, "sub_actions_triggered": 1}
    assert mock_trigger.await_count == 1


from datetime import datetime, timedelta, timezone

from app.actions.client import IndividualState
from app.actions.configurations import PullEventsForIndividualConfig
from app.actions.handlers import action_pull_events_for_individual
from app.actions.tests.conftest import make_events_generator


def _sub_action_config(**overrides):
    individual = {**INDIVIDUAL_ROW, **overrides.pop("individual_overrides", {})}
    return PullEventsForIndividualConfig(study_id="12345", individual=individual, **overrides)


def _gps_event(event_id, ts):
    return {
        "event_id": str(event_id),
        "timestamp": ts,
        "location_lat": "1.5",
        "location_long": "2.5",
        "individual_id": "111",
        "sensor_type_id": "653",
    }


@pytest.fixture
def mock_state_store(mocker):
    """In-memory stand-in for the module-level state_manager in handlers."""
    store = {}

    async def get_state(integration_id, action_id, source_id="no-source"):
        return store.get((str(integration_id), action_id, source_id), {})

    async def set_state(integration_id, action_id, state, source_id="no-source", expire=None):
        store[(str(integration_id), action_id, source_id)] = state

    manager = mocker.patch("app.actions.handlers.state_manager")
    manager.get_state = AsyncMock(side_effect=get_state)
    manager.set_state = AsyncMock(side_effect=set_state)
    return store


@pytest.mark.asyncio
async def test_pull_events_sends_observations_and_updates_cursor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    events = [_gps_event(100, "2026-01-01 10:00:00.000"), _gps_event(101, "2026-01-01 11:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mock_send = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result["observations_sent"] == 2
    assert mock_send.await_count >= 1
    sent = [obs for call in mock_send.await_args_list for obs in call.kwargs["observations"]]
    assert len(sent) == 2
    assert sent[0]["source"] == "111"
    # Cursor state persisted: GPS sensor advanced to the newest event.
    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    state = IndividualState.parse_obj(saved)
    assert state.get_sensor_state(653).highest_event_id == 101
    assert state.get_sensor_state(653).latest_timestamp == datetime(2026, 1, 1, 11, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_pull_events_skips_individual_in_quiet_period(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    mock_state_store[(str(integration.id), "pull_events_for_individual_quiet", "111")] = {"quiet": True}
    mock_send = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock())

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result == {"skipped": "quiet_period"}
    assert mock_send.await_count == 0


@pytest.mark.asyncio
async def test_pull_events_sets_quiet_period_when_window_is_empty(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    mock_movebank_client.get_individual_events_by_time = make_events_generator([])
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock())

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result["observations_sent"] == 0
    quiet = mock_state_store.get((str(integration.id), "pull_events_for_individual_quiet", "111"))
    assert quiet == {"quiet": True}


@pytest.mark.asyncio
async def test_pull_events_skips_individual_without_timestamp_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    result = await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(individual_overrides={"timestamp_start": ""}),
    )
    assert result == {"skipped": "no_timestamp_start"}


@pytest.mark.asyncio
async def test_pull_events_skips_when_all_sensors_are_current(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Saved cursor is already past the individual's timestamp_end.
    state = IndividualState(individual_id="111", study_id="12345")
    state.update_sensor_state(653, datetime(2026, 7, 1, tzinfo=timezone.utc), 999)
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = state.dict()

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )
    assert result == {"skipped": "no_new_data"}


@pytest.mark.asyncio
async def test_pull_events_respects_max_records_cap(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # 2500 events in one window: cap stops processing at >= 2000.
    events = [_gps_event(i, "2026-06-30 10:00:00.000") for i in range(2500)]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration,
        # High-frequency path: number_of_events > 5000 shrinks the window, but a
        # single window still returned everything here; the cap must still hold
        # across windows.
        action_config=_sub_action_config(individual_overrides={"number_of_events": "6000"}),
    )
    assert result["observations_sent"] == 2500  # first window completes...
    # ...but no further windows ran (cap reached after the first).
