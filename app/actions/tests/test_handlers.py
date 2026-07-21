from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.actions.client import IndividualState
from app.actions.configurations import PullEventsForIndividualConfig, PullObservationsConfig
from app.actions.handlers import action_pull_events_for_individual, action_pull_observations
from app.actions.tests.conftest import INDIVIDUAL_ROW, make_events_generator


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


def make_counting_events_generator(events_per_call):
    calls = {"count": 0}

    async def _gen(**kwargs):
        base = calls["count"] * 10000
        calls["count"] += 1
        for i in range(events_per_call):
            yield {
                "event_id": str(base + i),
                "timestamp": "2026-06-17 00:00:00.000",
                "location_lat": "1.5",
                "location_long": "2.5",
                "individual_id": "111",
                "sensor_type_id": "653",
            }
    return _gen, calls


@pytest.mark.asyncio
async def test_pull_events_respects_max_records_cap(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # 15-day lookback with the default 5-day window => 3 windows would run,
    # but 1200 events per window crosses the 2000 cap after window 2.
    gen, calls = make_counting_events_generator(1200)
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(maximum_lookback_hours=360),
    )
    assert result["observations_sent"] == 2400
    assert calls["count"] == 2  # third window never ran


@pytest.mark.asyncio
async def test_pull_events_zero_range_high_frequency_individual_terminates(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # timestamp_end == timestamp_start would make the density window zero -> guard must fall back.
    mock_movebank_client.get_individual_events_by_time = make_events_generator([])
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    result = await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(individual_overrides={
            "number_of_events": "6000",
            "timestamp_start": "2026-07-01 00:00:00.000",
            "timestamp_end": "2026-07-01 00:00:00.000",
        }),
    )
    assert result["observations_sent"] == 0


@pytest.mark.asyncio
async def test_pull_events_does_not_advance_cursor_when_send_fails(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    events = [_gps_event(100, "2026-01-01 10:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(side_effect=Exception("gundi down")))

    with pytest.raises(Exception):
        await action_pull_events_for_individual(
            integration=integration, action_config=_sub_action_config()
        )
    assert (str(integration.id), "pull_events_for_individual", "111") not in mock_state_store


@pytest.mark.asyncio
async def test_pull_events_density_window_applies_without_timestamp_end(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # 60k events over ~30 days => ~2.5-day density window. With a 10-day lookback
    # that means 4 fetch windows; the old 5-day fallback would only run 2.
    start = (datetime.now(tz=timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S.000")
    gen, calls = make_counting_events_generator(0)
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(
            maximum_lookback_hours=240,
            individual_overrides={
                "timestamp_start": start,
                "timestamp_end": "",
                "number_of_events": "60000",
            },
        ),
    )

    assert result["observations_sent"] == 0
    assert calls["count"] == 4


@pytest.mark.asyncio
async def test_pull_events_advances_cursor_when_all_events_are_unusable(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Valid timestamps but no individual_id: the transform drops every record.
    events = [{**_gps_event(100, "2026-01-01 10:00:00.000"), "individual_id": ""}]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mock_send = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result["observations_sent"] == 0
    assert mock_send.await_count == 0
    # Cursors advanced past the unusable events so they aren't refetched forever...
    saved = mock_state_store.get((str(integration.id), "pull_events_for_individual", "111"))
    assert saved is not None
    assert IndividualState.parse_obj(saved).get_sensor_state(653).highest_event_id == 100
    # ...and no quiet period, because the window did return events.
    assert (str(integration.id), "pull_events_for_individual_quiet", "111") not in mock_state_store


@pytest.mark.asyncio
async def test_pull_events_tolerates_whitespace_in_sensor_type_labels(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # "gps, accessory-measurements" (space after comma) must yield BOTH sensors:
    # one fetch call each in the single catch-up window.
    gen, calls = make_counting_events_generator(0)
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(
            individual_overrides={"sensor_type_ids": "gps, accessory-measurements"},
        ),
    )

    assert calls["count"] == 2


@pytest.mark.asyncio
async def test_pull_events_advances_event_id_cursor_when_timestamps_unparseable(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Garbage timestamp, valid event_id: the transform drops the record and the
    # timestamp cursor can't move, but the event-id cursor must still advance so
    # the same junk isn't refetched every run.
    events = [{**_gps_event(100, "garbage-timestamp")}]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result["observations_sent"] == 0
    saved = mock_state_store.get((str(integration.id), "pull_events_for_individual", "111"))
    assert saved is not None
    assert IndividualState.parse_obj(saved).get_sensor_state(653).highest_event_id == 100


@pytest.mark.asyncio
async def test_pull_accessory_query_applies_settling_margin(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # An accessory-only individual with a saved cursor: the query start must be
    # pushed back by ACCESSORY_SETTLING_HOURS so multi-hour-late records are caught.
    from app.actions.client import IndividualState
    from datetime import datetime, timezone
    cursor_ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    state = IndividualState(individual_id="111", study_id="12345")
    state.update_sensor_state(7842954, cursor_ts, 500)
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = state.dict()

    captured = {}

    async def _gen(**kwargs):
        captured.update(kwargs)
        if False:
            yield {}
    mock_movebank_client.get_individual_events_by_time = _gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(individual_overrides={"sensor_type_ids": "accessory-measurements"}),
    )

    # First fetch for the accessory sensor starts >= 12h before the cursor.
    assert captured["sensor_type_ids"] == [7842954]
    assert captured["timestamp_start"] <= cursor_ts - timedelta(hours=12)


@pytest.mark.asyncio
async def test_pull_acquires_connection_slot(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    events = [_gps_event(100, "2026-01-01 10:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    slot = mocker.patch("app.actions.handlers.movebank_slot")

    await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    # The pull opened at least one connection under the shared semaphore,
    # keyed by the integration's Movebank username.
    assert slot.called
    slot.assert_called_with("user")
