from datetime import datetime, timezone

from app.actions.client import Individual, IndividualState, SensorState, generate_individuals


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
    "sensor_type_ids": "gps,accessory-measurements",
    "taxon_detail": "",
}


def test_individual_coerces_naive_timestamps_to_utc():
    ind = Individual.parse_obj(INDIVIDUAL_ROW)
    assert ind.timestamp_start == datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert ind.timestamp_end == datetime(2026, 7, 1, tzinfo=timezone.utc)
    assert ind.number_of_events == 100


def test_individual_tolerates_empty_timestamps():
    row = {**INDIVIDUAL_ROW, "timestamp_start": "", "timestamp_end": ""}
    ind = Individual.parse_obj(row)
    assert ind.timestamp_start is None
    assert ind.timestamp_end is None


def test_generate_individuals_skips_bad_rows():
    bad_row = {**INDIVIDUAL_ROW, "number_of_events": "not-a-number"}
    result = list(generate_individuals([INDIVIDUAL_ROW, bad_row]))
    assert len(result) == 1
    assert result[0].id == "111"


def test_individual_state_creates_default_sensor_state():
    state = IndividualState(individual_id="111", study_id="12345")
    sensor_state = state.get_sensor_state(653)
    assert sensor_state.highest_event_id == 0
    assert sensor_state.latest_timestamp is None


def test_individual_state_update_and_roundtrip():
    state = IndividualState(individual_id="111", study_id="12345")
    ts = datetime(2026, 1, 1, tzinfo=timezone.utc)
    state.update_sensor_state(653, ts, 42)

    restored = IndividualState.parse_obj(state.dict())
    assert restored.get_sensor_state(653).highest_event_id == 42
    assert restored.get_sensor_state(653).latest_timestamp == ts
    # An unrelated sensor still gets a fresh default state.
    assert restored.get_sensor_state(7842954).highest_event_id == 0
