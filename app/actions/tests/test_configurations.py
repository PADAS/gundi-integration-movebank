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
    from app.actions.configurations import BackfillConfig
    config = BackfillConfig(
        study_id="12345",
        individual_ids=["111", "222"],
        start="2024-01-01",
        backfill_max_concurrency=4,
    )
    assert config.individual_ids == ["111", "222"]
    assert config.start == "2024-01-01"
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


def test_backfill_config_rejects_cancel_and_restart_together():
    from app.actions.configurations import BackfillConfig
    import pydantic
    with pytest.raises(pydantic.ValidationError):
        BackfillConfig(study_id="12345", start="all", cancel=True, restart=True)


def test_backfill_config_normalizes_individual_ids():
    from app.actions.configurations import BackfillConfig
    # Execution dedups these via set(), so the canonical form must too — the
    # job_id is hashed from them, and cancel/restart have to reproduce it.
    config = BackfillConfig(study_id="12345", start="all", individual_ids=["222", "111", "222"])
    assert config.individual_ids == ["111", "222"]


# --- Searchable reference pickers -------------------------------------------

from app.actions.configurations import (  # noqa: E402
    BackfillConfig,
    ListIndividualsQuery,
    ListStudiesQuery,
)
from app.actions.core import ReferenceActionConfiguration, discover_actions  # noqa: E402

STUDY_REF = {
    "action": "list_studies", "target": "self", "params": {}, "allow_free_text": True,
    "search": {"param": "q", "min_chars": 2}, "resolve": {"param": "ids"},
}


def test_pull_config_study_id_is_a_searchable_reference():
    ui = PullObservationsConfig.ui_schema()
    assert ui["study_id"]["gundi:reference"] == STUDY_REF
    # The portal picks the widget; one without reference support keeps a text field.
    assert "ui:widget" not in ui["study_id"]


def test_backfill_config_references_and_keeps_its_field_order():
    ui = BackfillConfig.ui_schema()
    assert ui["study_id"]["gundi:reference"] == STUDY_REF
    assert "ui:widget" not in ui["study_id"]
    assert ui["individual_ids"] == {"items": {"gundi:reference": {
        "action": "list_individuals", "target": "self",
        "params": {"study_id": {"$data": "../study_id"}}, "allow_free_text": True,
        "search": {"param": "q", "min_chars": 2}, "resolve": {"param": "ids"},
    }}}
    assert ui["ui:order"] == [
        "study_id", "individual_ids", "start", "backfill_max_concurrency", "dry_run", "restart", "cancel",
    ]


def test_reference_annotations_do_not_leak_into_the_json_schema():
    for model in (PullObservationsConfig, BackfillConfig):
        assert "gundi:reference" not in model.schema_json()


@pytest.mark.parametrize("raw,expected", [
    ("12,13", ["12", "13"]),
    (12, ["12"]),
    (["12", "12", " 13 "], ["12", "13"]),
    (["12,13", 14], ["12", "13", "14"]),
    (None, None),
    ([], None),
    ("", None),
    (" , ", None),
])
def test_query_ids_are_coerced(raw, expected):
    assert ListStudiesQuery(ids=raw).ids == expected
    assert ListIndividualsQuery(study_id=1, ids=raw).ids == expected


def test_individuals_query_study_id_is_a_stripped_string():
    assert ListIndividualsQuery(study_id=123).study_id == "123"
    assert ListIndividualsQuery(study_id=" 123 ").study_id == "123"
    assert ListIndividualsQuery(study_id="  ").study_id is None


def test_queries_accept_no_params_at_all():
    # A widget that predates search support fetches with no params; that must
    # be a clean empty answer, never a 422.
    assert ListStudiesQuery().q is None and ListStudiesQuery().ids is None
    assert ListIndividualsQuery().study_id is None


def test_query_models_are_reference_actions():
    assert issubclass(ListStudiesQuery, ReferenceActionConfiguration)
    assert issubclass(ListIndividualsQuery, ReferenceActionConfiguration)


def _annotated_actions(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "gundi:reference":
                yield value["action"]
            else:
                yield from _annotated_actions(value)


@pytest.mark.xfail(strict=True, reason="handlers land in the next task")
def test_every_annotated_action_is_a_registered_reference_action():
    handlers = discover_actions("app.actions.handlers", "action_")
    annotated = set()
    for model in (PullObservationsConfig, BackfillConfig):
        annotated.update(_annotated_actions(model.ui_schema()))
    assert annotated == {"list_studies", "list_individuals"}
    for action in annotated:
        assert action in handlers, f"no handler for annotated action '{action}'"
        assert issubclass(handlers[action][1], ReferenceActionConfiguration)
