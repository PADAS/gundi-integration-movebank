import json

import redis.asyncio as redis_asyncio
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from app.actions.configurations import AuthenticateConfig, ListIndividualsQuery, ListStudiesQuery
from app.actions.core import ReferenceActionConfiguration, discover_actions
from app.actions.handlers import action_list_individuals, action_list_studies
from app.services.errors import IntegrationRateLimitError
from app.services import movebank_connections
from app.services.movebank_connections import NoConnectionSlot

USERNAME = "mb-account-name"
PASSWORD = "s3cret-passw0rd"

STUDY_ROWS = [
    {"id": "10", "name": "Mara Vultures", "citation": "long text", "i_am_owner": "true"},
    {"id": "2", "name": "vulture tracking", "citation": "", "i_am_owner": "false"},
    {"id": "33", "name": "Elephants", "citation": "", "i_am_owner": "false"},
]

INDIVIDUAL_ROWS = [
    {"id": "7", "local_identifier": "", "nick_name": "Kito", "ring_id": "R1",
     "taxon_canonical_name": "Gyps africanus", "sex": "m"},
    {"id": "9", "local_identifier": "TAG-0042", "nick_name": "Zuri", "ring_id": "K99",
     "taxon_canonical_name": "Gyps rueppelli", "sex": "f"},
]


@pytest.fixture(autouse=True)
def auth_config(mocker):
    mocker.patch(
        "app.actions.client.get_auth_config",
        return_value=AuthenticateConfig(username=USERNAME, password=PASSWORD),
    )


@pytest.fixture
def cache(mocker):
    """An empty reference cache: every read misses, writes are recorded."""
    get = mocker.patch("app.actions.handlers.reference_cache.get_cached_list", AsyncMock(return_value=None))
    set_ = mocker.patch("app.actions.handlers.reference_cache.set_cached_list", AsyncMock(return_value=None))
    return get, set_


def _values(result):
    return [o["value"] for o in result["options"]]


@pytest.mark.asyncio
async def test_list_studies_searches_the_fetched_list_and_caches_it(integration, mock_movebank_client, cache):
    _, cache_set = cache
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))

    assert result["options"] == [
        {"value": "10", "label": "Mara Vultures (10)", "description": None, "group": None}
    ]
    assert result["truncated"] is False
    cache_set.assert_awaited_once()
    assert cache_set.await_args.args[1] == [
        {"id": "10", "name": "Mara Vultures"},
        {"id": "2", "name": "vulture tracking"},
        {"id": "33", "name": "Elephants"},
    ]


@pytest.mark.asyncio
async def test_list_studies_cache_hit_makes_no_movebank_request(
        mocker, integration, mock_connection_slot, cache
):
    cache_get, cache_set = cache
    cache_get.return_value = [{"id": "10", "name": "Mara Vultures"}]
    client_class = mocker.patch("app.actions.client.MovebankClient")

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="vult"))

    assert _values(result) == ["10"]
    client_class.assert_not_called()
    mock_connection_slot.assert_not_called()
    cache_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_studies_empty_query_returns_nothing_truncated(integration, mock_movebank_client, cache):
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery())

    assert result["options"] == [] and result["truncated"] is True


@pytest.mark.asyncio
async def test_list_studies_resolves_saved_ids_in_order(integration, mock_movebank_client, cache):
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)
    mock_movebank_client.get_accessible_study = AsyncMock()

    result = await action_list_studies(
        integration=integration, action_config=ListStudiesQuery(ids=["33", "10"], q="ignored")
    )

    assert _values(result) == ["33", "10"]
    assert result["truncated"] is False
    mock_movebank_client.get_accessible_study.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_studies_resolves_an_id_missing_from_the_cache_with_one_live_lookup(
        integration, mock_movebank_client, cache
):
    cache_get, cache_set = cache
    cache_get.return_value = [{"id": "10", "name": "Mara Vultures"}]
    mock_movebank_client.get_accessible_study = AsyncMock(return_value={"id": "77", "name": "New Study", "citation": "x"})

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(ids=["77", "10"]))

    assert result["options"][0] == {"value": "77", "label": "New Study (77)", "description": None, "group": None}
    assert _values(result) == ["77", "10"]
    mock_movebank_client.get_accessible_study.assert_awaited_once_with(study_id="77")
    cache_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_studies_unknown_id_is_left_out_not_an_error(integration, mock_movebank_client, cache):
    cache_get, _ = cache
    cache_get.return_value = [{"id": "10", "name": "Mara Vultures"}]
    mock_movebank_client.get_accessible_study = AsyncMock(return_value=None)

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(ids=["999", "10"]))

    assert _values(result) == ["10"]


@pytest.mark.asyncio
async def test_list_studies_when_movebank_is_busy_raises_the_busy_message(
        mocker, integration, mock_movebank_client, cache
):
    _, cache_set = cache

    @asynccontextmanager
    async def _no_slot(username, *, ttl_seconds=600):
        raise NoConnectionSlot()
        yield

    mocker.patch("app.actions.handlers.movebank_slot", side_effect=_no_slot)
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)

    with pytest.raises(IntegrationRateLimitError, match="Movebank is busy"):
        await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))

    cache_set.assert_not_awaited()
    mock_movebank_client.get_accessible_studies.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_studies_falls_back_to_live_fetch_when_the_cache_is_unavailable(
        integration, mock_movebank_client, cache
):
    # get_cached_list answers None on a Redis outage and set_cached_list swallows
    # its own failure, so the handler sees a miss on every call and still answers.
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)

    for _ in range(2):
        result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="eleph"))
        assert _values(result) == ["33"]

    assert mock_movebank_client.get_accessible_studies.await_count == 2


@pytest.mark.asyncio
async def test_list_studies_takes_a_connection_slot_for_the_account(
        integration, mock_movebank_client, mock_connection_slot, cache
):
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)

    await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))

    mock_connection_slot.assert_called_once()
    assert mock_connection_slot.call_args.args[0] == USERNAME


@pytest.mark.asyncio
async def test_list_individuals_without_a_study_returns_empty_and_touches_nothing(
        mocker, integration, mock_connection_slot, cache
):
    cache_get, cache_set = cache
    client_class = mocker.patch("app.actions.client.MovebankClient")

    result = await action_list_individuals(integration=integration, action_config=ListIndividualsQuery(q="kit"))

    assert result["options"] == [] and result["truncated"] is False
    client_class.assert_not_called()
    cache_get.assert_not_awaited()
    cache_set.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_individuals_is_keyed_by_study_and_searches(integration, mock_movebank_client, cache):
    cache_get, cache_set = cache
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=INDIVIDUAL_ROWS)

    result = await action_list_individuals(
        integration=integration, action_config=ListIndividualsQuery(study_id="5", q="kit")
    )

    assert result["options"] == [
        {"value": "7", "label": "Kito (7)", "description": "Gyps africanus", "group": None}
    ]
    mock_movebank_client.get_individuals_by_study.assert_awaited_once_with(study_id="5")
    key = cache_get.await_args.args[0]
    assert key.startswith("movebank:reference:individuals:") and key.endswith(":5")
    assert cache_set.await_args.args[0] == key
    assert cache_set.await_args.args[1][0] == {
        "id": "7", "local_identifier": "", "nick_name": "Kito", "ring_id": "R1", "taxon": "Gyps africanus",
    }


@pytest.mark.asyncio
async def test_list_individuals_empty_query_lists_the_study(integration, mock_movebank_client, cache):
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=INDIVIDUAL_ROWS)

    result = await action_list_individuals(integration=integration, action_config=ListIndividualsQuery(study_id="5"))

    assert _values(result) == ["7", "9"]  # by label: "Kito (7)", "TAG-0042 (9)"


@pytest.mark.asyncio
async def test_list_individuals_resolves_saved_ids(integration, mock_movebank_client, cache):
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=INDIVIDUAL_ROWS)

    result = await action_list_individuals(
        integration=integration, action_config=ListIndividualsQuery(study_id="5", ids=["9", "404", "7"])
    )

    assert _values(result) == ["9", "7"]


@pytest.mark.asyncio
async def test_reference_responses_and_cache_writes_carry_no_credentials(integration, mock_movebank_client, cache):
    cache_get, cache_set = cache
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=INDIVIDUAL_ROWS)

    results = [
        await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara")),
        await action_list_individuals(integration=integration, action_config=ListIndividualsQuery(study_id="5")),
    ]

    written = json.dumps(results) + json.dumps(
        [list(call.args) for call in cache_get.await_args_list + cache_set.await_args_list]
    )
    assert USERNAME not in written
    assert PASSWORD not in written


@pytest.mark.asyncio
async def test_reference_handlers_are_not_activity_logged(
        mocker, integration, mock_movebank_client, cache
):
    publish = mocker.patch("app.services.activity_logger.publish_event", AsyncMock(return_value=None))
    # The portal calls these on every keystroke; they must not write an
    # activity-log entry per call.
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=INDIVIDUAL_ROWS)

    await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))
    await action_list_individuals(integration=integration, action_config=ListIndividualsQuery(study_id="5"))

    publish.assert_not_awaited()


def test_reference_actions_are_discovered_with_their_query_models():
    handlers = discover_actions("app.actions.handlers", "action_")
    assert handlers["list_studies"][1] is ListStudiesQuery
    assert handlers["list_individuals"][1] is ListIndividualsQuery
    for action in ("list_studies", "list_individuals"):
        assert issubclass(handlers[action][1], ReferenceActionConfiguration)
        assert getattr(handlers[action][0], "action_title", None)


# --- Final-review findings ---------------------------------------------------

@pytest.mark.asyncio
async def test_a_different_password_for_the_same_username_misses_the_cache(
        mocker, integration, mock_movebank_client, cache
):
    cache_get, _ = cache
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)

    await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))
    mocker.patch(
        "app.actions.client.get_auth_config",
        return_value=AuthenticateConfig(username=USERNAME, password="someone-elses-guess"),
    )
    await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))

    first_key, second_key = (call.args[0] for call in cache_get.await_args_list)
    assert first_key != second_key


@pytest.mark.asyncio
async def test_redis_outage_on_the_connection_slot_still_answers(
        mocker, integration, mock_movebank_client, cache
):
    # The real slot, with Redis refusing: the cache read has already failed
    # open, and the slot must not turn the outage into a failed lookup.
    redis_client = mocker.MagicMock()
    redis_client.eval = AsyncMock(side_effect=redis_asyncio.ConnectionError("redis is down"))
    mocker.patch("app.services.movebank_connections._client", return_value=redis_client)
    mocker.patch("app.actions.handlers.movebank_slot", movebank_connections.movebank_slot)
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))

    assert _values(result) == ["10"]
    redis_client.eval.assert_awaited_once()


@pytest.mark.asyncio
async def test_a_failing_study_lookup_does_not_lose_the_other_labels(integration, mock_movebank_client, cache):
    cache_get, _ = cache
    cache_get.return_value = [{"id": "10", "name": "Mara Vultures"}]

    async def _get_study(study_id):
        if study_id == "500":
            raise RuntimeError("Movebank answered 500")
        return {"id": study_id, "name": "New Study"}

    mock_movebank_client.get_accessible_study = AsyncMock(side_effect=_get_study)

    result = await action_list_studies(
        integration=integration, action_config=ListStudiesQuery(ids=["500", "10", "77"])
    )

    assert _values(result) == ["10", "77"]


@pytest.mark.asyncio
async def test_a_saved_value_that_is_not_an_id_is_never_sent_to_movebank(integration, mock_movebank_client, cache):
    # allow_free_text means a saved value can be a typo; Movebank IDs are numeric.
    cache_get, _ = cache
    cache_get.return_value = [{"id": "10", "name": "Mara Vultures"}]
    mock_movebank_client.get_accessible_study = AsyncMock(return_value=None)

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(ids=["12a", "10"]))

    assert _values(result) == ["10"]
    mock_movebank_client.get_accessible_study.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [[{"id": "1"}], [1, 2], [{"name": "no id"}]])
async def test_a_cached_list_of_the_wrong_shape_is_refetched(integration, mock_movebank_client, cache, stale):
    # An entry written by another revision of the code must read as a miss,
    # not crash every lookup until its TTL runs out.
    cache_get, cache_set = cache
    cache_get.return_value = stale
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=STUDY_ROWS)
    mock_movebank_client.get_individuals_by_study = AsyncMock(return_value=INDIVIDUAL_ROWS)

    studies = await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))
    individuals = await action_list_individuals(
        integration=integration, action_config=ListIndividualsQuery(study_id="5", q="kit")
    )

    assert _values(studies) == ["10"] and _values(individuals) == ["7"]
    assert cache_set.await_count == 2


@pytest.mark.asyncio
async def test_rows_without_an_id_are_neither_offered_nor_cached(integration, mock_movebank_client, cache):
    # e.g. a non-CSV body parsed as rows. A blank option is useless, and a
    # cached row without an id would make the entry read as the wrong shape.
    _, cache_set = cache
    mock_movebank_client.get_accessible_studies = AsyncMock(return_value=[{"name": "Mara ghost"}, *STUDY_ROWS])

    result = await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="mara"))

    assert _values(result) == ["10"]
    assert all(row["id"] for row in cache_set.await_args.args[1])


# --- PR 41 review: partially shared studies ----------------------------------

@pytest.mark.asyncio
async def test_a_partially_shared_study_is_searchable_and_labelled(mocker, integration, cache):
    # The real client against Movebank's CSV, not a handler mock: the pinned
    # movebank-client filters out a study when some of its data is hidden from
    # the account, and a mocked get_studies() would never show that.
    import httpx
    from movebank_client import MovebankClient as UpstreamClient

    header = "id,name,i_can_see_data,there_are_data_which_i_cannot_see\n"
    partial = "20,Shared Eagles,true,true\n"

    async def _call_api(self, url="", *, params=(), cookies=None):
        body = header + ("" if ("study_id", "99") in params else partial)
        return httpx.Response(200, content=body.encode("utf8"))

    mocker.patch.object(UpstreamClient, "_call_api", _call_api)
    cache_get, _ = cache

    searched = await action_list_studies(integration=integration, action_config=ListStudiesQuery(q="eagle"))
    assert searched["options"] == [
        {"value": "20", "label": "Shared Eagles (20)", "description": None, "group": None}
    ]

    # Saved value missing from a cached list: resolved by the single-study lookup.
    cache_get.return_value = [{"id": "10", "name": "Mara Vultures"}]
    resolved = await action_list_studies(integration=integration, action_config=ListStudiesQuery(ids=["20", "99"]))
    assert _values(resolved) == ["20"]
