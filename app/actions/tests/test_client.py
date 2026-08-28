from unittest.mock import AsyncMock

import pytest
from movebank_client import MovebankClient as _MovebankClient

from app.actions.client import MovebankClient


ATTRS = [{"short_name": "gps_dop", "sensor_type_id": "653"}]


@pytest.fixture
def mb_client():
    return MovebankClient(
        base_url="https://www.movebank.org", username="u", password="p"
    )


@pytest.fixture
def upstream_fetch(mocker):
    """Stub the movebank-client implementation that performs the HTTP call."""
    return mocker.patch.object(
        _MovebankClient, "get_study_attributes", new=AsyncMock(return_value=ATTRS)
    )


@pytest.fixture
def cache(mocker):
    get = mocker.patch(
        "app.actions.client.get_cached_study_attributes", new=AsyncMock(return_value=None)
    )
    set_ = mocker.patch(
        "app.actions.client.set_cached_study_attributes", new=AsyncMock(return_value=None)
    )
    return get, set_


@pytest.mark.asyncio
async def test_a_redis_hit_skips_the_movebank_request(mb_client, upstream_fetch, cache):
    get_cached, set_cached = cache
    get_cached.return_value = ATTRS

    assert await mb_client.get_study_attributes(study_id="1", sensor_type_id="653") == ATTRS

    upstream_fetch.assert_not_awaited()  # the whole point: no Movebank round-trip
    set_cached.assert_not_awaited()      # nothing new to write


@pytest.mark.asyncio
async def test_a_redis_miss_fetches_then_populates_the_cache(mb_client, upstream_fetch, cache):
    get_cached, set_cached = cache

    assert await mb_client.get_study_attributes(study_id="1", sensor_type_id="653") == ATTRS

    upstream_fetch.assert_awaited_once()
    set_cached.assert_awaited_once_with("https://www.movebank.org", "1", "653", ATTRS)


@pytest.mark.asyncio
async def test_second_call_on_the_same_client_hits_neither_redis_nor_movebank(
    mb_client, upstream_fetch, cache
):
    get_cached, set_cached = cache

    await mb_client.get_study_attributes(study_id="1", sensor_type_id="653")
    await mb_client.get_study_attributes(study_id="1", sensor_type_id="653")

    # The in-process cache short-circuits the repeat, so neither layer is re-consulted.
    assert get_cached.await_count == 1
    assert upstream_fetch.await_count == 1


@pytest.mark.asyncio
async def test_a_redis_hit_warms_the_in_process_cache(mb_client, upstream_fetch, cache):
    get_cached, set_cached = cache
    get_cached.return_value = ATTRS

    await mb_client.get_study_attributes(study_id="1", sensor_type_id="653")
    await mb_client.get_study_attributes(study_id="1", sensor_type_id="653")

    assert get_cached.await_count == 1


@pytest.mark.asyncio
async def test_a_failed_fetch_is_not_cached(mb_client, upstream_fetch, cache):
    get_cached, set_cached = cache
    upstream_fetch.return_value = None  # movebank-client returns None when the fetch fails

    assert await mb_client.get_study_attributes(study_id="1", sensor_type_id="653") is None

    # Caching a failure would pin every individual to attributes='all' for the TTL.
    set_cached.assert_not_awaited()


@pytest.mark.asyncio
async def test_an_empty_attribute_list_is_cached(mb_client, upstream_fetch, cache):
    get_cached, set_cached = cache
    upstream_fetch.return_value = []  # a study that advertises no attributes

    assert await mb_client.get_study_attributes(study_id="1", sensor_type_id="653") == []

    set_cached.assert_awaited_once_with("https://www.movebank.org", "1", "653", [])


# --- The call path that actually generated the redundant traffic -------------
# get_individual_events_by_time() calls get_study_attributes() for every GPS
# query. These tests drive that real method with only the HTTP layer stubbed,
# so they fail if the override ever stops being reached.

EVENT_CSV = (
    "event_id,individual_id,sensor_type_id,timestamp,location_long,location_lat\n"
    "1,111,653,2026-08-28 00:00:00.000,1.0,2.0\n"
)
ATTRS_CSV = "short_name,sensor_type_id\ngps_dop,653\n"


def _response(body: str):
    import httpx

    return httpx.Response(200, text=body, request=httpx.Request("GET", "https://x"))


@pytest.fixture
def http(mocker):
    """Stub _call_api — every Movebank HTTP request passes through it."""
    def _dispatch(url="", *, params=(), cookies=None):
        entity = dict(params).get("entity_type")
        return _response(ATTRS_CSV if entity == "study_attribute" else EVENT_CSV)

    return mocker.patch.object(
        MovebankClient, "_call_api", new=AsyncMock(side_effect=_dispatch)
    )


def _entity_types(call_api):
    return [dict(c.kwargs["params"]).get("entity_type") for c in call_api.await_args_list]


@pytest.mark.asyncio
async def test_gps_fetch_makes_one_movebank_request_when_attributes_are_cached(
    mb_client, http, cache
):
    from datetime import datetime, timezone

    get_cached, set_cached = cache
    get_cached.return_value = ATTRS

    events = [
        e async for e in mb_client.get_individual_events_by_time(
            study_id="1",
            individual_id="111",
            timestamp_start=datetime(2026, 8, 27, tzinfo=timezone.utc),
            timestamp_end=datetime(2026, 8, 28, tzinfo=timezone.utc),
        )
    ]

    assert len(events) == 1
    # The regression this fix exists to prevent: a cached study must cost one
    # request (the events), not two (study_attribute + events).
    assert _entity_types(http) == ["event"]
    # And the cached attribute list still narrows the query.
    assert "gps_dop" in dict(http.await_args.kwargs["params"])["attributes"]


@pytest.mark.asyncio
async def test_gps_fetch_falls_back_to_two_requests_on_a_cold_cache(mb_client, http, cache):
    from datetime import datetime, timezone

    get_cached, set_cached = cache  # get_cached returns None: cold

    [e async for e in mb_client.get_individual_events_by_time(
        study_id="1",
        individual_id="111",
        timestamp_start=datetime(2026, 8, 27, tzinfo=timezone.utc),
        timestamp_end=datetime(2026, 8, 28, tzinfo=timezone.utc),
    )]

    assert _entity_types(http) == ["study_attribute", "event"]
    set_cached.assert_awaited_once()  # so the next individual pays only one
