import pytest
from pydantic import ValidationError

from app.actions.configurations import (
    AuthenticateConfig,
    PullEventsForIndividualConfig,
    PullObservationsConfig,
)
from app.actions.core import InternalActionConfiguration, PullActionConfiguration


INDIVIDUAL = {
    "id": "111",
    "local_identifier": "tag-1",
    "nick_name": "Aquila",
    "ring_id": "R1",
    "sex": "f",
    "taxon_canonical_name": "Aquila chrysaetos",
    "timestamp_start": "2025-01-01 00:00:00.000",
    "timestamp_end": "2026-07-01 00:00:00.000",
    "number_of_events": 100,
    "number_of_deployments": 1,
    "sensor_type_ids": "gps",
    "taxon_detail": "",
}


def test_pull_observations_config_requires_study_id():
    with pytest.raises(ValidationError):
        PullObservationsConfig()
    config = PullObservationsConfig(study_id="12345")
    assert config.maximum_lookback_hours == 24
    assert isinstance(config, PullActionConfiguration)


def test_pull_events_for_individual_config_is_internal():
    config = PullEventsForIndividualConfig(study_id="12345", individual=INDIVIDUAL)
    # Internal actions are not registered in the portal.
    assert isinstance(config, InternalActionConfiguration)
    assert config.individual.id == "111"
    # Round-trips through dict (this is how trigger_action serializes it).
    restored = PullEventsForIndividualConfig.parse_obj(config.dict())
    assert restored.individual.nick_name == "Aquila"


def test_auth_config_unchanged():
    config = AuthenticateConfig(username="u", password="p")
    assert config.password.get_secret_value() == "p"
