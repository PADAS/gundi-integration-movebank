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
    from app.actions.core import ExecutableActionMixin
    assert issubclass(AuthenticateConfig, ExecutableActionMixin)


def test_backfill_config_whole_study_all_data():
    from app.actions.configurations import BackfillConfig
    from app.actions.core import ExecutableActionMixin
    config = BackfillConfig(study_id="12345", start="all")
    assert config.individual_ids is None
    assert config.start == "all"
    assert issubclass(BackfillConfig, ExecutableActionMixin)


def test_backfill_config_dated_and_filtered():
    from datetime import datetime, timezone
    from app.actions.configurations import BackfillConfig
    config = BackfillConfig(
        study_id="12345",
        individual_ids=["111", "222"],
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        backfill_max_concurrency=4,
    )
    assert config.individual_ids == ["111", "222"]
    assert config.start.year == 2024
    assert config.backfill_max_concurrency == 4


def test_backfill_config_rejects_malformed_start_string():
    from app.actions.configurations import BackfillConfig
    # A garbage string must not silently fall through to full-history "all" —
    # only a real datetime or the literal "all" is accepted.
    with pytest.raises(ValidationError):
        BackfillConfig(study_id="12345", start="not-a-real-date")


def test_backfill_config_rejects_zero_concurrency():
    from app.actions.configurations import BackfillConfig
    with pytest.raises(ValidationError):
        BackfillConfig(study_id="12345", start="all", backfill_max_concurrency=0)


def test_backfill_individual_config_is_internal_and_roundtrips():
    from app.actions.configurations import BackfillEventsForIndividualConfig
    from app.actions.core import InternalActionConfiguration
    from datetime import datetime, timezone
    cfg = BackfillEventsForIndividualConfig(
        study_id="12345", individual=INDIVIDUAL, job_id="job-1",
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    assert isinstance(cfg, InternalActionConfiguration)
    restored = BackfillEventsForIndividualConfig.parse_obj(cfg.dict())
    assert restored.individual.id == "111"
    assert restored.job_id == "job-1"
