import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from app.actions.client import IndividualState
from app.actions.configurations import (
    BackfillConfig,
    BackfillEventsForIndividualConfig,
    PullEventsForIndividualConfig,
    PullObservationsConfig,
)
from app.actions.handlers import (
    action_backfill,
    action_backfill_events_for_individual,
    action_pull_events_for_individual,
    action_pull_observations,
)
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


def _make_individual(**overrides):
    from app.actions.client import Individual
    return Individual.parse_obj({**INDIVIDUAL_ROW, **overrides})


@pytest.fixture
def mock_state_store(mocker):
    """In-memory stand-in for the module-level state_manager in handlers."""
    store = {}

    async def get_state(integration_id, action_id, source_id="no-source"):
        return store.get((str(integration_id), action_id, source_id), {})

    async def set_state(integration_id, action_id, state, source_id="no-source", expire=None):
        store[(str(integration_id), action_id, source_id)] = state

    async def delete_state(integration_id, action_id, source_id="no-source"):
        store.pop((str(integration_id), action_id, source_id), None)

    manager = mocker.patch("app.actions.handlers.state_manager")
    manager.get_state = AsyncMock(side_effect=get_state)
    manager.set_state = AsyncMock(side_effect=set_state)
    manager.delete_state = AsyncMock(side_effect=delete_state)
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


@pytest.mark.asyncio
async def test_backfill_seeds_queue_and_dispatches_up_to_k(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    from app.actions.configurations import BackfillConfig
    rows = [{**INDIVIDUAL_ROW, "id": str(i)} for i in range(5)]
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=rows)
    seen = {}
    configs = {}

    async def fake_seed(self, individual_ids, *, total, range_repr):
        seen["seeded"] = list(individual_ids); seen["total"] = total
    async def fake_next(self):
        return seen["seeded"].pop(0) if seen.get("seeded") else None
    async def fake_incr(self, n=1):
        return n
    async def fake_put_config(self, individual_id, config_json):
        configs[individual_id] = config_json
    async def fake_get_config(self, individual_id):
        return configs.get(individual_id)
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", fake_incr)
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", fake_put_config)
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config", fake_get_config)
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 5, "completed": 0, "observations_sent": 0, "in_flight": 0,
                                          "pending_remaining": 0, "range": "r"}))
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    mocker.patch("app.actions.handlers.settings.BACKFILL_MAX_CONCURRENCY", 3)

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 5
    assert result["dispatched"] == 3            # K, not all 5
    assert mock_trigger.await_count == 3
    _, kwargs = mock_trigger.await_args_list[0]
    assert kwargs["action_id"] == "backfill_events_for_individual"
    assert kwargs["config"].job_id == result["job_id"]


@pytest.mark.asyncio
async def test_backfill_respects_individual_filter(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    from app.actions.configurations import BackfillConfig
    rows = [{**INDIVIDUAL_ROW, "id": str(i)} for i in range(5)]
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=rows)
    captured = {}
    async def fake_seed(self, individual_ids, *, total, range_repr):
        captured["ids"] = list(individual_ids)
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all", individual_ids=["2", "4"]),
    )
    assert set(captured["ids"]) == {"2", "4"}


@pytest.mark.asyncio
async def test_backfill_acquires_connection_slot(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    from app.actions.configurations import BackfillConfig
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[])
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    slot = mocker.patch("app.actions.handlers.movebank_slot")
    await action_backfill(integration=integration, action_config=BackfillConfig(study_id="12345", start="all"))
    slot.assert_called_with("user")


@pytest.mark.asyncio
async def test_backfill_individual_finalizes_and_dispatches_next(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from app.actions.client import IndividualState
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    # One small window of GPS events fully inside [start, end): the step reaches
    # `end` in a single pass and finalizes.
    events = [_gps_event(10, "2024-01-02 00:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 1, "in_flight": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "completed"
    # The steady-state pull cursor was written from the backfill watermark (ts + event id).
    handed = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    st = IndividualState.parse_obj(handed)
    assert st.get_sensor_state(653).highest_event_id == 10
    # Queue empty -> no next dispatch.
    assert not mock_trigger.called
    # The backfill watermark is cleaned up after a successful finalize.
    assert (str(integration.id), "backfill_watermark", "job-1.111") not in mock_state_store


@pytest.mark.asyncio
async def test_backfill_individual_retriggers_self_when_budget_exhausted(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2026, 1, 1, tzinfo=timezone.utc)   # huge range
    events = [_gps_event(i, "2024-01-02 00:00:00.000") for i in range(3)]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    # Force the time budget to zero so the step stops after the first window.
    mocker.patch("app.actions.handlers.settings.MAX_ACTION_EXECUTION_TIME", 0)
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "continued"
    # It re-triggered ITSELF (same individual), not the next one.
    assert mock_trigger.await_count == 1
    _, kwargs = mock_trigger.await_args_list[0]
    assert kwargs["action_id"] == "backfill_events_for_individual"
    assert kwargs["config"].individual.id == "111"


@pytest.mark.asyncio
async def test_backfill_individual_sparse_sensor_reaches_end_without_livelock(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A sensor that returns NO events across a multi-window range must still
    # make bounded progress: the scan floor (`scan_from`) advances every
    # window regardless of whether events came back, so the step reaches
    # `end` and finalizes instead of restarting at `start` forever.
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, tzinfo=timezone.utc)  # 19 days -> multiple 5-day windows
    mock_movebank_client.get_individual_events_by_time = make_events_generator([])
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0, "in_flight": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    # Real-ish budget (not 0): plenty for a handful of mocked, near-instant windows.

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "completed"
    assert not mock_trigger.called  # no self re-trigger: bounded, not livelocked
    # The watermark is deleted on a successful finalize (see MINOR-4 fix) — its
    # absence here is proof the scan floor actually reached `end` in-bounds
    # rather than getting stuck mid-range.
    assert (str(integration.id), "backfill_watermark", "job-1.111") not in mock_state_store


@pytest.mark.asyncio
async def test_backfill_individual_resumes_scan_from_persisted_floor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Simulate a prior step that advanced the scan floor past `start` without
    # ever getting an event for its sensor (so the per-sensor cursor is still
    # unmoved, sitting at `start`). A fresh step must resume scanning from the
    # persisted scan_from, not recompute `current` from the unmoved per-sensor
    # cursor and rescan the already-covered span.
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from app.actions.client import IndividualState
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 20, tzinfo=timezone.utc)
    persisted_scan_from = datetime(2024, 1, 11, tzinfo=timezone.utc)  # past two 5-day windows
    seeded_state = IndividualState(individual_id="111", study_id="12345")
    blob = seeded_state.dict()
    blob["scan_from"] = persisted_scan_from.isoformat()
    mock_state_store[(str(integration.id), "backfill_watermark", "job-1.111")] = blob

    captured_starts = []

    async def _gen(**kwargs):
        captured_starts.append(kwargs["timestamp_start"])
        if False:
            yield {}
    mock_movebank_client.get_individual_events_by_time = _gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0, "in_flight": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "completed"
    # First fetch must start from the persisted scan floor, not from `start`
    # (which is where the never-advanced per-sensor cursor would place it).
    assert captured_starts[0] == persisted_scan_from


@pytest.mark.asyncio
async def test_backfill_seeds_pull_cursor_at_end_and_finalize_merges_forward(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # End-to-end: an individual with no existing pull cursor gets one seeded
    # to `now` (T0) when action_backfill starts, claiming [T0, +inf) so a
    # concurrent steady-state pull can't reach back into backfill's range.
    # After the sub-action finalizes, the pull cursor must carry backfill's
    # highest event-id forward without moving its timestamp backward.
    from app.actions.configurations import BackfillConfig, BackfillEventsForIndividualConfig
    from app.actions.client import IndividualState
    from datetime import datetime, timezone

    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    before = datetime.now(tz=timezone.utc)
    backfill_result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )
    after = datetime.now(tz=timezone.utc)
    job_id = backfill_result["job_id"]

    seeded = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    seeded_state = IndividualState.parse_obj(seeded)
    seeded_ss = seeded_state.get_sensor_state(653)
    assert seeded_ss.highest_event_id == 0
    assert before <= seeded_ss.latest_timestamp <= after

    # Now run the sub-action to completion for a small range fully in the past.
    events = [_gps_event(42, "2025-06-01 00:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 1, "in_flight": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())

    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 3, tzinfo=timezone.utc)
    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id=job_id, start=start, end=end,
        ),
    )
    assert result["status"] == "completed"

    merged = IndividualState.parse_obj(
        mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    )
    merged_ss = merged.get_sensor_state(653)
    # Backfill's event-id carries forward...
    assert merged_ss.highest_event_id == 42
    # ...but the pull cursor's timestamp never moves backward past what it
    # already claimed at seed time.
    assert merged_ss.latest_timestamp == seeded_ss.latest_timestamp


@pytest.mark.asyncio
async def test_backfill_individual_backs_off_when_no_slot(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from app.services.movebank_connections import NoConnectionSlot
    from datetime import datetime, timezone
    def _raise(*a, **k):
        raise NoConnectionSlot("full")
    mocker.patch("app.actions.handlers.movebank_slot", _raise)
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    assert result["status"] == "backoff"
    # Re-triggered self to retry later; did not finalize.
    assert mock_trigger.await_count == 1
    assert mock_trigger.await_args_list[0].kwargs["config"].individual.id == "111"


@pytest.mark.asyncio
async def test_backfill_individual_abandoned_after_max_attempts(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    mock_movebank_client.get_individual_events_by_time = mocker.MagicMock(side_effect=RuntimeError("mb down"))
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_attempts", AsyncMock(return_value=6))  # over the max
    mock_record_completion = mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mock_decr_in_flight = mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0, "in_flight": 0, "range": "r"}))
    warn = mocker.patch("app.actions.handlers.logger.warning")

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2026, 1, 1, tzinfo=timezone.utc),
        ),
    )
    assert result["status"] == "abandoned"
    assert warn.called
    # Finalize ran exactly once on the abandon path — no double-counting.
    assert mock_record_completion.await_count == 1
    assert mock_decr_in_flight.await_count == 1
    # Abandoned with observations=0 and no state: no pull cursor handed off.
    assert (str(integration.id), "pull_events_for_individual", "111") not in mock_state_store


@pytest.mark.asyncio
async def test_backfill_individual_finalize_error_is_not_misclassified_as_this_individuals_failure(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A clean fetch/send loop reaches `end` and finalizes; if the dispatch-next
    # trigger_action (part of finalize's post-processing) raises, that error must
    # propagate rather than being caught by this individual's except-Exception —
    # otherwise an already-completed individual would be retried/abandoned and
    # _finalize_backfill_individual would run a second time (double
    # record_completion / decr_in_flight).
    from app.actions.configurations import BackfillEventsForIndividualConfig
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    events = [_gps_event(10, "2024-01-02 00:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mock_record_completion = mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=False))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 2, "completed": 1, "observations_sent": 1, "in_flight": 1,
                                          "pending_remaining": 1, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    # dispatch-next rolls back + requeues the next individual when trigger fails,
    # then re-raises — mock both so the RuntimeError (not a Redis error) surfaces.
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=2))
    mocker.patch("app.actions.backfill_queue.BackfillJob.requeue", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value="222"))
    # A real stored config for the NEXT individual, so dispatch-next actually
    # reaches trigger_action (rather than short-circuiting on a missing blob).
    next_config = BackfillEventsForIndividualConfig(
        study_id="12345", individual={**INDIVIDUAL_ROW, "id": "222"}, job_id="job-1", start=start, end=end,
    )
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config",
                 AsyncMock(return_value=next_config.json()))
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=2))
    mocker.patch("app.actions.handlers.state_manager.set_if_absent", AsyncMock(return_value=True))
    # The dispatch-next trigger_action (inside finalize, via _dispatch_backfill_individual)
    # raises once the fetch loop has already completed successfully.
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock(side_effect=RuntimeError("pubsub down")))

    with pytest.raises(RuntimeError, match="pubsub down"):
        await action_backfill_events_for_individual(
            integration=integration,
            action_config=BackfillEventsForIndividualConfig(
                study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
            ),
        )

    # This individual's own completion was recorded exactly once — the finalize
    # error was NOT treated as this individual's failure (no retry/abandon).
    assert mock_record_completion.await_count == 1


@pytest.mark.asyncio
async def test_pull_accessory_query_not_widened_without_prior_events(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A freshly-seeded accessory cursor (highest_event_id == 0 — e.g. right
    # after _seed_pull_cursor_at_end at backfill start) must NOT be widened:
    # doing so would re-read [T-settling, T) with minimum_event_id=1 and
    # re-emit duplicates of everything already sent in that span.
    from app.actions.client import IndividualState
    from datetime import datetime, timezone
    cursor_ts = datetime(2026, 6, 1, 12, 0, tzinfo=timezone.utc)
    state = IndividualState(individual_id="111", study_id="12345")
    state.update_sensor_state(7842954, cursor_ts, 0)
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

    assert captured["sensor_type_ids"] == [7842954]
    # No widening: query starts exactly at the cursor, not settling hours before it.
    assert captured["timestamp_start"] == cursor_ts


@pytest.mark.asyncio
async def test_backfill_individual_density_window_applies(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A high-frequency individual must get a narrower, density-sized window in
    # backfill too (mirroring the pull's _compute_batch_window), not always
    # the 5-day default.
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 31, tzinfo=timezone.utc)  # 30-day range
    gen, calls = make_counting_events_generator(0)
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0, "in_flight": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345",
            individual={**INDIVIDUAL_ROW, "number_of_events": "60000"},
            job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "completed"
    # 30-day range at a ~2.5-day density window => 12 windows, not the 6 the
    # 5-day default would take.
    assert calls["count"] == 12


@pytest.mark.asyncio
async def test_backfill_individual_stops_at_per_step_record_backstop(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A dense window can return far more than density sizing expects. The
    # per-step backstop must stop taking more windows once this step's total
    # crosses it, persisting progress and re-triggering rather than risking
    # the invocation running past its execution budget.
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 1, tzinfo=timezone.utc)  # huge range, many windows available
    gen, calls = make_counting_events_generator(3000)  # 3000 events/window, GPS-only
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "continued"
    # 4 windows * 3000 = 12000 >= the 10000 backstop: stopped rather than
    # continuing to sweep the (still huge) remaining range in one invocation.
    assert calls["count"] == 4
    assert result["observations_sent"] == 12000
    assert mock_trigger.await_count == 1


@pytest.mark.asyncio
async def test_backfill_window_over_cap_is_discarded_then_sent_after_shrink(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A fully-fetched over-cap window is DISCARDED (not sent); the window shrinks
    # and persists, and only a later (floor-sized) window is actually sent. With
    # the fixed-count generator every fetch returns 3 (> cap 2), so the window
    # shrinks to the floor in one step, then the floor window is sent anyway.
    mocker.patch("app.actions.handlers.settings.MAX_RECORDS_PER_BACKFILL_WINDOW", 2)
    mocker.patch("app.actions.handlers.settings.MIN_BACKFILL_WINDOW_SECONDS", 259200)  # 3 days
    mocker.patch("app.actions.handlers.settings.BACKFILL_WINDOW_SHRINK_SAFETY", 0.8)
    gen, calls = make_counting_events_generator(3)  # 3 > cap 2 on every fetch
    mock_movebank_client.get_individual_events_by_time = gen
    send = mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-x",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 2, tzinfo=timezone.utc),  # 1-day range
        ),
    )

    # Two fetches (one discarded at 5d, one sent at the 3d floor) but only ONE
    # send — proving the over-cap fetch was discarded, not sent.
    assert calls["count"] == 2
    assert send.call_count == 1
    # The floor-sized window sent here also happens to finish this individual
    # (a 3-day window exceeds the 1-day range), so _finalize_backfill_individual
    # deletes the watermark blob as part of normal completion cleanup — checking
    # mock_state_store post-hoc would just see it gone. Assert against the
    # set_state call the shrink step made instead, which is what actually
    # proves the window was persisted at the floor.
    from app.actions import handlers as handlers_module
    persisted_windows = [
        call.args[2].get("window_seconds")
        for call in handlers_module.state_manager.set_state.call_args_list
        if call.args[1] == "backfill_watermark"
    ]
    assert persisted_windows and all(float(w) == 259200 for w in persisted_windows)


@pytest.mark.asyncio
async def test_backfill_honours_persisted_window_seconds(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A persisted window_seconds from a prior step is used instead of the density
    # estimate: the sub-action queries with end_at = current + persisted window.
    st = IndividualState(individual_id="111", study_id="12345")
    mock_state_store[(str(integration.id), "backfill_watermark", "job-x.111")] = {
        **st.dict(), "scan_from": "2024-01-01T00:00:00+00:00", "window_seconds": 60.0,
    }
    ends = []
    def gen(**kwargs):
        ends.append(kwargs.get("timestamp_end"))
        async def _agen():
            for _ in ():
                yield None
        return _agen()
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    mocker.patch("app.actions.handlers.settings.MIN_BACKFILL_WINDOW_SECONDS", 1)

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-x",
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, 0, 2, tzinfo=timezone.utc),  # 2-minute range
        ),
    )

    # First window ends 60s after the persisted scan_from, not 5 days later.
    assert ends[0] == datetime(2024, 1, 1, 0, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_backfill_individual_checks_deadline_between_sensor_fetches(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # The deadline must be checked BETWEEN per-sensor fetches within a window,
    # not only between windows: a dense window's own fetches can exceed the
    # budget, and there is no PubSub redelivery to recover a killed step.
    # Uses a real (tiny) deadline and a real sleep on the first sensor's fetch
    # rather than mocking time.monotonic, which asyncio's own loop internals
    # also call (mocking it globally breaks the event loop itself).
    import asyncio
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    calls = {"gps": 0, "accessory": 0}

    async def _gen(**kwargs):
        if kwargs["sensor_type_ids"] == [653]:
            calls["gps"] += 1
            await asyncio.sleep(0.1)  # simulate a slow first-sensor request
        else:
            calls["accessory"] += 1
        if False:
            yield {}
    mock_movebank_client.get_individual_events_by_time = _gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())
    # A tiny real budget: comfortably survives until after the GPS fetch is
    # requested, but is exhausted by the time the 0.1s sleep completes.
    mocker.patch("app.actions.handlers.settings.MAX_ACTION_EXECUTION_TIME", 0.01)
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345",
            individual={**INDIVIDUAL_ROW, "sensor_type_ids": "gps, accessory-measurements"},
            job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "continued"
    assert calls["gps"] == 1
    assert calls["accessory"] == 0  # deadline hit before this sensor's fetch
    assert mock_trigger.await_count == 1


@pytest.mark.asyncio
async def test_backfill_individual_resets_attempts_after_successful_step(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    events = [_gps_event(10, "2024-01-02 00:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 1, "in_flight": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    mock_reset = mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())

    result = await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    assert result["status"] == "completed"
    mock_reset.assert_awaited_once_with("111")


@pytest.mark.asyncio
async def test_backfill_skips_individuals_with_existing_cursor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillConfig
    from app.actions.client import IndividualState
    from datetime import datetime, timezone
    rows = [{**INDIVIDUAL_ROW, "id": str(i)} for i in range(3)]
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=rows)
    # Individual "1" already has a steady-state pull cursor: backfill is for
    # initial load only, so it must be skipped, not queued.
    existing_state = IndividualState(individual_id="1", study_id="12345")
    existing_state.update_sensor_state(653, datetime(2026, 1, 1, tzinfo=timezone.utc), 5)
    mock_state_store[(str(integration.id), "pull_events_for_individual", "1")] = existing_state.dict()

    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    warn = mocker.patch("app.actions.handlers.logger.warning")

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 2       # "1" excluded
    assert result["skipped_existing"] == 1
    assert warn.call_count == 1             # exactly one summary warning
    assert "skipped 1 individuals" in warn.call_args[0][0]


@pytest.mark.asyncio
async def test_backfill_skips_reseed_when_job_already_active(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    # A redelivered/double-clicked backfill command hashes to the SAME job_id.
    # If that job is already active, action_backfill must not re-seed (which
    # would re-zero counters and re-RPUSH/double-dispatch individuals already
    # in flight).
    from app.actions.configurations import BackfillConfig
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=True))
    # Genuinely active: work is in flight, so this must bail (not resume).
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 2, "completed": 0, "observations_sent": 0,
                                          "in_flight": 2, "pending_remaining": 0, "range": "r"}))
    mock_seed = mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mock_put_config = mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", AsyncMock())
    mock_next = mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["already_active"] is True
    assert not mock_seed.called
    assert not mock_trigger.called
    assert "job_id" in result
    assert mock_seed.await_count == 0
    assert mock_put_config.await_count == 0
    assert mock_next.await_count == 0
    assert mock_trigger.await_count == 0


@pytest.mark.asyncio
async def test_dispatch_backfill_individual_skips_missing_config_to_next_pending(mocker):
    # If the first individual's stored config is missing (e.g. a lost/evicted
    # Redis entry), the dispatch driver must advance to the next pending
    # individual instead of silently stalling the cascade.
    from app.actions.handlers import _dispatch_backfill_individual
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from datetime import datetime, timezone

    job = mocker.MagicMock()
    job.job_id = "job-1"
    job.snapshot = AsyncMock(return_value={
        "pending_remaining": 1, "total": 2, "completed": 0,
        "observations_sent": 0, "in_flight": 0, "range": "r",
    })
    good_config = BackfillEventsForIndividualConfig(
        study_id="12345", individual={**INDIVIDUAL_ROW, "id": "222"}, job_id="job-1",
        start=datetime(2024, 1, 1, tzinfo=timezone.utc), end=datetime(2024, 1, 3, tzinfo=timezone.utc),
    )
    # "111"'s config is missing; "222" (next in the pending queue) has one.
    job.get_individual_config = AsyncMock(side_effect=[None, good_config.json()])
    job.next_individual = AsyncMock(return_value="222")
    job.incr_in_flight = AsyncMock(return_value=1)
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    await _dispatch_backfill_individual("int-1", job, "111")

    job.get_individual_config.assert_any_call("111")
    job.get_individual_config.assert_any_call("222")
    assert mock_trigger.await_count == 1
    _, kwargs = mock_trigger.await_args_list[0]
    assert kwargs["config"].individual.id == "222"


@pytest.mark.asyncio
async def test_dispatch_backfill_individual_gives_up_after_exhausting_queue(mocker):
    # Every stored config is missing (e.g. the whole configs hash was lost):
    # the fallback loop must be bounded by the queue's length, not spin forever.
    from app.actions.handlers import _dispatch_backfill_individual

    job = mocker.MagicMock()
    job.job_id = "job-1"
    job.snapshot = AsyncMock(return_value={
        "pending_remaining": 2, "total": 3, "completed": 0,
        "observations_sent": 0, "in_flight": 0, "range": "r",
    })
    job.get_individual_config = AsyncMock(return_value=None)
    job.next_individual = AsyncMock(side_effect=["222", "333", None])
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    await _dispatch_backfill_individual("int-1", job, "111")

    assert mock_trigger.await_count == 0
    # Bounded: pending_remaining(2) + 1 for the passed-in id == 3 attempts max.
    assert job.get_individual_config.await_count <= 3


@pytest.mark.asyncio
async def test_backfill_resumes_stalled_job_when_nothing_in_flight(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    # A prior invocation seeded the job but crashed before dispatching: the job
    # exists, has pending work, and nothing is in flight. A re-run must RESUME
    # (dispatch from the queue), not bail out with already_active.
    from app.actions.configurations import BackfillConfig
    rows = [{**INDIVIDUAL_ROW, "id": str(i)} for i in range(3)]
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=rows)
    pending = ["0", "1", "2"]

    async def fake_next(self):
        return pending.pop(0) if pending else None
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 3, "completed": 0, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 3, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config",
                 AsyncMock(return_value='{"study_id":"12345","individual":' +
                           __import__("json").dumps(INDIVIDUAL_ROW) +
                           ',"job_id":"job-x","start":"2024-01-01T00:00:00+00:00","end":"2024-02-01T00:00:00+00:00"}'))
    mock_seed = mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    mocker.patch("app.actions.handlers.settings.BACKFILL_MAX_CONCURRENCY", 2)

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result.get("resumed") is True
    assert result["dispatched"] == 2            # up to K, from the stalled queue
    assert mock_trigger.await_count == 2
    assert not mock_seed.called                 # did NOT re-seed


@pytest.mark.asyncio
async def test_backfill_bails_when_job_genuinely_active(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    # Job exists AND has work in flight: a re-run must NOT dispatch anything.
    from app.actions.configurations import BackfillConfig
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[{**INDIVIDUAL_ROW, "id": "0"}])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 3, "completed": 1, "observations_sent": 10,
                                          "in_flight": 2, "pending_remaining": 1, "range": "r"}))
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result.get("already_active") is True
    assert not mock_trigger.called


@pytest.mark.asyncio
async def test_pull_observations_skips_on_no_connection_slot(
        mocker, integration, mock_auth_config, mock_movebank_client
):
    # A saturated Movebank connection budget must NOT fail the scheduled tick;
    # it skips cleanly and no sub-actions are triggered (next tick retries).
    from app.services.movebank_connections import NoConnectionSlot
    def _raise(*a, **k):
        raise NoConnectionSlot("full")
    mocker.patch("app.actions.handlers.movebank_slot", _raise)
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_pull_observations(
        integration=integration,
        action_config=PullObservationsConfig(study_id="12345"),
    )

    assert result == {"skipped": "no_connection_slot"}
    assert not mock_trigger.called


@pytest.mark.asyncio
async def test_pull_events_skips_on_no_connection_slot(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # NoConnectionSlot during the per-sensor fetch is a clean skip, not a failure.
    from app.services.movebank_connections import NoConnectionSlot
    def _raise(*a, **k):
        raise NoConnectionSlot("full")
    mocker.patch("app.actions.handlers.movebank_slot", _raise)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    result = await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    assert result["skipped"] == "no_connection_slot"


@pytest.mark.asyncio
async def test_dispatch_rolls_back_and_requeues_on_trigger_failure(mocker):
    # If trigger_action raises, in_flight must be rolled back and the individual
    # re-queued (not lost) — otherwise in_flight stays inflated and the resume
    # path can never fire.
    from app.actions.backfill_queue import BackfillJob
    from app.actions.handlers import _dispatch_backfill_individual
    calls = {"incr": 0, "decr": 0, "requeued": []}

    async def fake_snapshot(self):
        return {"total": 1, "completed": 0, "observations_sent": 0, "in_flight": 0,
                "pending_remaining": 0, "range": "r"}
    async def fake_get_config(self, iid):
        return ('{"study_id":"12345","individual":' + __import__("json").dumps(INDIVIDUAL_ROW) +
                ',"job_id":"job-x","start":"2024-01-01T00:00:00+00:00","end":"2024-02-01T00:00:00+00:00"}')
    async def fake_incr(self, n=1):
        calls["incr"] += 1; return 1
    async def fake_decr(self):
        calls["decr"] += 1; return 0
    async def fake_requeue(self, iid):
        calls["requeued"].append(iid)
    mocker.patch.object(BackfillJob, "snapshot", fake_snapshot)
    mocker.patch.object(BackfillJob, "get_individual_config", fake_get_config)
    mocker.patch.object(BackfillJob, "incr_in_flight", fake_incr)
    mocker.patch.object(BackfillJob, "decr_in_flight", fake_decr)
    mocker.patch.object(BackfillJob, "requeue", fake_requeue)
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock(side_effect=RuntimeError("pubsub down")))

    job = BackfillJob("int-1", "job-x")
    with pytest.raises(RuntimeError, match="pubsub down"):
        await _dispatch_backfill_individual("int-1", job, "111")

    assert calls["incr"] == 1
    assert calls["decr"] == 1              # rolled back
    assert calls["requeued"] == ["111"]    # not lost


@pytest.mark.asyncio
async def test_pull_first_run_stamps_coverage_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Fresh individual (no saved cursor): after the first run that persists a
    # cursor, coverage_start must equal default_start = timestamp_end - lookback.
    events = [_gps_event(100, "2026-06-30 10:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    await action_pull_events_for_individual(
        integration=integration,
        action_config=_sub_action_config(
            maximum_lookback_hours=24,
            individual_overrides={"timestamp_end": "2026-07-01 00:00:00.000"},
        ),
    )

    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    state = IndividualState.parse_obj(saved)
    assert state.coverage_start == datetime(2026, 6, 30, 0, 0, tzinfo=timezone.utc)  # end - 24h


@pytest.mark.asyncio
async def test_pull_second_run_preserves_coverage_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # A saved cursor with an existing coverage_start must not be overwritten.
    prior = IndividualState(individual_id="111", study_id="12345")
    prior.update_sensor_state(653, datetime(2026, 6, 1, tzinfo=timezone.utc), 50)
    prior.coverage_start = datetime(2020, 1, 1, tzinfo=timezone.utc)  # far-back floor
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = prior.dict()
    events = [_gps_event(100, "2026-06-30 10:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))

    await action_pull_events_for_individual(
        integration=integration, action_config=_sub_action_config()
    )

    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    assert IndividualState.parse_obj(saved).coverage_start == datetime(2020, 1, 1, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_seed_pull_cursor_stamps_coverage_start(mocker, integration, mock_state_store):
    from app.actions.handlers import _seed_pull_cursor_at_end
    end = datetime(2026, 7, 1, tzinfo=timezone.utc)
    await _seed_pull_cursor_at_end(str(integration.id), "12345", _make_individual(), end)
    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    assert IndividualState.parse_obj(saved).coverage_start == end


@pytest.mark.asyncio
async def test_finalize_sets_coverage_start_to_backfill_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    from app.actions.configurations import BackfillEventsForIndividualConfig
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2024, 1, 3, tzinfo=timezone.utc)
    events = [_gps_event(10, "2024-01-02 00:00:00.000")]
    mock_movebank_client.get_individual_events_by_time = make_events_generator(events)
    mocker.patch("app.actions.handlers.send_observations_to_gundi", AsyncMock(return_value=[]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.record_completion", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.decr_in_flight", AsyncMock(return_value=0))
    mocker.patch("app.actions.backfill_queue.BackfillJob.is_done", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 1, "observations_sent": 1,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mocker.patch("app.actions.backfill_queue.BackfillJob.reset_attempts", AsyncMock())

    await action_backfill_events_for_individual(
        integration=integration,
        action_config=BackfillEventsForIndividualConfig(
            study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-1", start=start, end=end,
        ),
    )

    saved = mock_state_store[(str(integration.id), "pull_events_for_individual", "111")]
    assert IndividualState.parse_obj(saved).coverage_start == start


@pytest.mark.asyncio
async def test_backfill_fills_behind_existing_coverage_start(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # An individual WITH a real cursor (coverage_start recorded) must be queued,
    # with end == coverage_start (not skipped).
    from app.actions.configurations import BackfillConfig
    cov = datetime(2026, 1, 1, tzinfo=timezone.utc)
    st = IndividualState(individual_id="111", study_id="12345")
    st.update_sensor_state(653, datetime(2026, 6, 1, tzinfo=timezone.utc), 500)
    st.coverage_start = cov
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    seeded = {}
    async def fake_seed(self, ids, *, total, range_repr): seeded["ids"] = list(ids)
    async def fake_put(self, iid, blob): seeded.setdefault("configs", {})[iid] = blob
    async def fake_next(self): return (seeded.get("ids") or []).pop(0) if seeded.get("ids") else None
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", fake_put)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config",
                 AsyncMock(side_effect=lambda iid: seeded["configs"][iid]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 0, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 1          # queued, not skipped
    cfg = BackfillEventsForIndividualConfig.parse_raw(seeded["configs"]["111"])
    assert cfg.end == cov                       # end == coverage_start


@pytest.mark.asyncio
async def test_backfill_legacy_seed_placeholder_full_backfill(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Legacy cursor: all sensors event_id == 0, no coverage_start -> treat its
    # timestamp as the floor (full backfill), do NOT skip.
    seed_ts = datetime(2026, 7, 1, tzinfo=timezone.utc)
    st = IndividualState(individual_id="111", study_id="12345")
    st.update_sensor_state(653, seed_ts, 0)     # event_id 0 => placeholder
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    seeded = {}
    async def fake_seed(self, ids, *, total, range_repr): seeded["ids"] = list(ids)
    async def fake_put(self, iid, blob): seeded.setdefault("configs", {})[iid] = blob
    async def fake_next(self): return (seeded.get("ids") or []).pop(0) if seeded.get("ids") else None
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", fake_put)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config",
                 AsyncMock(side_effect=lambda iid: seeded["configs"][iid]))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 0, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 1
    cfg = BackfillEventsForIndividualConfig.parse_raw(seeded["configs"]["111"])
    assert cfg.end == seed_ts


@pytest.mark.asyncio
async def test_backfill_skips_legacy_real_cursor_unknown_floor(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # Legacy real coverage (event_id > 0) with no coverage_start -> skip with warning.
    st = IndividualState(individual_id="111", study_id="12345")
    st.update_sensor_state(653, datetime(2026, 6, 1, tzinfo=timezone.utc), 500)  # real events, no coverage_start
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 0
    assert result["skipped_existing"] == 1
    assert not mock_trigger.called


@pytest.mark.asyncio
async def test_backfill_skips_individual_with_empty_range(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # An already-covered individual whose coverage_start is at or behind its
    # resolved start produces an empty [start, end) range. It must be neither
    # queued nor counted as skipped_existing (that counter is reserved for the
    # legacy real-cursor-without-floor case).
    resolved_start = datetime(2025, 1, 1, tzinfo=timezone.utc)  # INDIVIDUAL_ROW's timestamp_start
    st = IndividualState(individual_id="111", study_id="12345")
    st.update_sensor_state(653, datetime(2026, 6, 1, tzinfo=timezone.utc), 500)
    st.coverage_start = resolved_start
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
    mock_trigger = mocker.patch("app.actions.handlers.trigger_action", AsyncMock())

    result = await action_backfill(
        integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result["individuals"] == 0
    assert result["skipped_existing"] == 0
    assert not mock_trigger.called


@pytest.mark.asyncio
async def test_backfill_settling_warning_gated_by_real_coverage(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store, caplog
):
    # The settling-margin warning should only fire when the cursor reflects
    # real prior coverage (highest_event_id > 0 on at least one sensor). A
    # freshly-seeded/legacy-placeholder cursor (all event_id == 0) always has
    # end_dt == latest, which would otherwise trip the warning spuriously.
    from app import settings

    async def run_backfill():
        mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=False))
        mocker.patch("app.actions.backfill_queue.BackfillJob.seed", AsyncMock())
        mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", AsyncMock())
        mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", AsyncMock(return_value=None))
        mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
        return await action_backfill(
            integration=integration, action_config=BackfillConfig(study_id="12345", start="all"),
        )

    warning_text = "accessory rows near the boundary may duplicate"

    # Real cursor, span below the settling margin -> warning IS logged.
    latest = datetime(2026, 6, 1, tzinfo=timezone.utc)
    real_st = IndividualState(individual_id="111", study_id="12345")
    real_st.update_sensor_state(653, latest, 500)  # real events
    real_st.coverage_start = latest - timedelta(hours=settings.ACCESSORY_SETTLING_HOURS - 1)
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = real_st.dict()
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])

    caplog.clear()
    with caplog.at_level("WARNING"):
        await run_backfill()
    assert any(warning_text in rec.message for rec in caplog.records)

    # Zero-span placeholder cursor (all sensors event_id == 0) -> warning is NOT logged.
    placeholder_st = IndividualState(individual_id="111", study_id="12345")
    placeholder_st.update_sensor_state(653, latest, 0)
    mock_state_store[(str(integration.id), "pull_events_for_individual", "111")] = placeholder_st.dict()

    caplog.clear()
    with caplog.at_level("WARNING"):
        await run_backfill()
    assert not any(warning_text in rec.message for rec in caplog.records)


def test_display_name_precedence():
    from app.actions.handlers import _display_name
    # local_identifier wins when present
    assert _display_name(_make_individual()) == "tag-1"
    # empty local_identifier falls back to nick_name
    assert _display_name(_make_individual(local_identifier="")) == "Aquila"
    # empty local_identifier and nick_name fall back to ring_id
    assert _display_name(_make_individual(local_identifier="", nick_name="")) == "R1"


@pytest.mark.asyncio
async def test_backfill_cancelled_step_reraises_and_preserves_scan_from(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store, caplog
):
    # A hard cancellation mid-send must propagate (CancelledError is BaseException,
    # not caught by the retry/backoff handlers) and must not have advanced the
    # persisted scan_from past what was durably completed.
    gen, _calls = make_counting_events_generator(1)  # 1 event/fetch, under the cap
    mock_movebank_client.get_individual_events_by_time = gen
    mocker.patch("app.actions.handlers.send_observations_to_gundi",
                 AsyncMock(side_effect=asyncio.CancelledError()))

    with caplog.at_level("WARNING"):
        with pytest.raises(asyncio.CancelledError):
            await action_backfill_events_for_individual(
                integration=integration,
                action_config=BackfillEventsForIndividualConfig(
                    study_id="12345", individual=INDIVIDUAL_ROW, job_id="job-x",
                    start=datetime(2024, 1, 1, tzinfo=timezone.utc),
                    end=datetime(2024, 1, 2, tzinfo=timezone.utc),
                ),
            )

    # The except-CancelledError block's warning fired (the one thing this
    # handler adds on the hard-cancellation path).
    assert any("hard-cancelled" in rec.message for rec in caplog.records)

    # The send was cancelled before the per-window persist, so no watermark blob
    # was written for this individual (scan_from never advanced).
    assert (str(integration.id), "backfill_watermark", "job-x.111") not in mock_state_store


@pytest.mark.asyncio
async def test_backfill_restart_clears_job_and_reseeds(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # With restart=True, an existing (wedged) job is cleared and re-seeded rather
    # than returning already_active.
    from app.actions.configurations import BackfillConfig
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    # Stateful exists/clear: the job exists until restart clears it. If restart
    # did NOT run, exists()==True would short-circuit to already_active.
    jobstate = {"exists": True, "cleared": False}
    async def fake_exists(self): return jobstate["exists"]
    async def fake_clear(self): jobstate["exists"] = False; jobstate["cleared"] = True
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", fake_exists)
    mocker.patch("app.actions.backfill_queue.BackfillJob.clear", fake_clear)
    seeded = {}
    configs = {}
    async def fake_seed(self, ids, *, total, range_repr): seeded["ids"] = list(ids)
    async def fake_next(self): return (seeded.get("ids") or []).pop(0) if seeded.get("ids") else None
    async def fake_put_config(self, individual_id, config_json): configs[individual_id] = config_json
    async def fake_get_config(self, individual_id): return configs.get(individual_id)
    mocker.patch("app.actions.backfill_queue.BackfillJob.seed", fake_seed)
    mocker.patch("app.actions.backfill_queue.BackfillJob.put_individual_config", fake_put_config)
    mocker.patch("app.actions.backfill_queue.BackfillJob.next_individual", fake_next)
    mocker.patch("app.actions.backfill_queue.BackfillJob.incr_in_flight", AsyncMock(return_value=1))
    mocker.patch("app.actions.backfill_queue.BackfillJob.get_individual_config", fake_get_config)
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 0, "observations_sent": 0,
                                          "in_flight": 0, "pending_remaining": 0, "range": "r"}))
    mocker.patch("app.actions.handlers.trigger_action", AsyncMock())
    delete = mocker.patch("app.actions.handlers.state_manager.delete_state", AsyncMock())

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all", restart=True),
    )

    assert jobstate["cleared"] is True
    assert result.get("already_active") is not True
    # Watermark for the candidate individual was cleared (job_id is deterministic).
    assert any(call.kwargs.get("source_id", "").endswith(".111")
               or (len(call.args) >= 3 and str(call.args[2]).endswith(".111"))
               for call in delete.call_args_list)


@pytest.mark.asyncio
async def test_backfill_default_still_bails_when_job_active(
        mocker, integration, mock_auth_config, mock_movebank_client, mock_state_store
):
    # restart defaults False: an existing active job still returns already_active.
    from app.actions.configurations import BackfillConfig
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=[INDIVIDUAL_ROW])
    mocker.patch("app.actions.backfill_queue.BackfillJob.exists", AsyncMock(return_value=True))
    mocker.patch("app.actions.backfill_queue.BackfillJob.snapshot",
                 AsyncMock(return_value={"total": 1, "completed": 0, "observations_sent": 0,
                                          "in_flight": 1, "pending_remaining": 0, "range": "r"}))
    clear = mocker.patch("app.actions.backfill_queue.BackfillJob.clear", AsyncMock())

    result = await action_backfill(
        integration=integration,
        action_config=BackfillConfig(study_id="12345", start="all"),
    )

    assert result.get("already_active") is True
    assert not clear.called
