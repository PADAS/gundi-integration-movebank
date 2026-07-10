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
