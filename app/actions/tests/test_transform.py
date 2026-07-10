from datetime import datetime, timedelta, timezone

from app.actions.transform import build_observation, chunks, human_friendly_timedelta


GPS_EVENT = {
    "event_id": "100",
    "timestamp": "2026-01-01 10:00:00.000",
    "location_lat": "1.5",
    "location_long": "2.5",
    "individual_id": "111",
    "sensor_type_id": "653",
    "ground_speed": "3.2",
}


def test_build_observation_maps_gps_event():
    obs = build_observation(event=GPS_EVENT, device_name="Aquila")
    assert obs["source"] == "111"
    assert obs["source_name"] == "Aquila"
    assert obs["type"] == "tracking-device"
    assert obs["recorded_at"] == "2026-01-01T10:00:00+00:00"
    assert obs["location"] == {"lat": 1.5, "lon": 2.5}
    # Everything except coordinates and timestamp lands in additional.
    assert obs["additional"]["ground_speed"] == "3.2"
    assert obs["additional"]["event_id"] == "100"
    assert "location_lat" not in obs["additional"]
    assert obs["additional"]["subject_name"] == "Aquila"
    assert "loaded_at" in obs["additional"]


def test_build_observation_fudges_missing_coordinates():
    event = {**GPS_EVENT, "location_lat": "", "location_long": "", "sensor_type_id": "7842954"}
    obs = build_observation(event=event, device_name="Aquila")
    assert obs["location"] == {"lat": 0.0, "lon": 0.0}
    # +1ms so it doesn't collide with a GPS record at the same instant.
    assert obs["recorded_at"] == "2026-01-01T10:00:00.001000+00:00"


def test_build_observation_computes_update_latency():
    event = {**GPS_EVENT, "update_ts": "2026-01-01 12:30:00.000"}
    obs = build_observation(event=event, device_name="Aquila")
    assert obs["additional"]["update_latency"] == "2h, 30m"


def test_build_observation_drops_unparseable_timestamp():
    assert build_observation(event={**GPS_EVENT, "timestamp": "garbage"}, device_name="A") is None


def test_build_observation_drops_future_events():
    future = (datetime.now(tz=timezone.utc) + timedelta(days=2)).strftime("%Y-%m-%d %H:%M:%S.000")
    assert build_observation(event={**GPS_EVENT, "timestamp": future}, device_name="A") is None


def test_build_observation_drops_events_without_individual_id():
    assert build_observation(event={**GPS_EVENT, "individual_id": ""}, device_name="A") is None


def test_human_friendly_timedelta():
    assert human_friendly_timedelta(timedelta(days=2, hours=3, minutes=4)) == "2d, 3h, 4m"
    assert human_friendly_timedelta(timedelta(seconds=30)) == "0m"


def test_chunks():
    assert list(chunks([1, 2, 3, 4, 5], 2)) == [[1, 2], [3, 4], [5]]
    assert list(chunks([], 2)) == []


def test_build_observation_converts_offset_aware_timestamps_to_utc():
    event = {**GPS_EVENT, "timestamp": "2026-01-01 10:00:00+02:00"}
    obs = build_observation(event=event, device_name="Aquila")
    assert obs["recorded_at"] == "2026-01-01T08:00:00+00:00"


def test_human_friendly_timedelta_negative():
    assert human_friendly_timedelta(timedelta(hours=-1)) == "-1h"
    assert human_friendly_timedelta(timedelta(days=-2, hours=-3)) == "-2d, 3h"


def test_build_observation_drops_unparseable_coordinates():
    event = {**GPS_EVENT, "location_lat": "N/A", "location_long": "N/A"}
    assert build_observation(event=event, device_name="Aquila") is None
