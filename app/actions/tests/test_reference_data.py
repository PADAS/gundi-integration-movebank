from app.actions.reference_data import (
    MAX_OPTIONS,
    individual_option,
    resolve,
    search_individuals,
    search_studies,
    study_option,
    trim_individual,
    trim_study,
)

STUDIES = [
    {"id": "10", "name": "Mara Vultures"},
    {"id": "2", "name": "vulture tracking"},
    {"id": "33", "name": "Elephants"},
    {"id": "44", "name": ""},
]

INDIVIDUALS = [
    {"id": "7", "local_identifier": "", "nick_name": "Kito", "ring_id": "R1", "taxon": "Gyps africanus"},
    {"id": "8", "local_identifier": "", "nick_name": "", "ring_id": "", "taxon": ""},
    {"id": "9", "local_identifier": "TAG-0042", "nick_name": "Zuri", "ring_id": "K99", "taxon": "Gyps rueppelli"},
]


def _values(response):
    return [o["value"] for o in response["options"]]


def test_study_label_and_value():
    assert study_option(STUDIES[0]).dict(exclude_none=True) == {"value": "10", "label": "Mara Vultures (10)"}


def test_study_without_a_name_is_labelled_by_id():
    assert study_option(STUDIES[3]).label == "44"


def test_search_is_case_insensitive_and_trims_whitespace():
    # "vulture tracking" starts with the query, so it sorts ahead of "Mara Vultures".
    assert _values(search_studies(STUDIES, "  VULTURE ")) == ["2", "10"]


def test_exact_id_match_sorts_first():
    studies = STUDIES + [{"id": "210", "name": "Aardvarks"}]
    assert _values(search_studies(studies, "2")) == ["2", "210"]


def test_study_search_matches_a_partial_id():
    assert _values(search_studies(STUDIES, "33")) == ["33"]


def test_study_query_with_no_match_is_empty_and_not_truncated():
    assert search_studies(STUDIES, "zebra") == {"options": [], "cache_ttl_seconds": 300, "truncated": False}


def test_empty_or_blank_study_query_returns_nothing_and_says_truncated():
    for q in (None, "", "   "):
        assert search_studies(STUDIES, q) == {"options": [], "cache_ttl_seconds": 300, "truncated": True}


def test_study_search_caps_at_50_and_flags_truncation():
    many = [{"id": str(i), "name": f"Study {i:03d}"} for i in range(60)]
    out = search_studies(many, "study")
    assert MAX_OPTIONS == 50
    assert len(out["options"]) == 50 and out["truncated"] is True
    assert out["options"][0]["label"] == "Study 000 (0)"
    assert search_studies(many[:50], "study")["truncated"] is False


def test_individual_label_falls_back_through_names_then_id():
    assert individual_option(INDIVIDUALS[0]).dict() == {
        "value": "7", "label": "Kito (7)", "description": "Gyps africanus", "group": None,
    }
    assert individual_option(INDIVIDUALS[2]).label == "TAG-0042 (9)"
    nameless = individual_option(INDIVIDUALS[1])
    assert nameless.label == "8" and nameless.description is None


def test_individual_search_matches_ring_id_and_id():
    assert _values(search_individuals(INDIVIDUALS, "k99")) == ["9"]
    assert _values(search_individuals(INDIVIDUALS, "8")) == ["8"]
    # nick_name matches even when the label shows the local identifier
    assert _values(search_individuals(INDIVIDUALS, "zuri")) == ["9"]


def test_empty_individual_query_returns_the_first_50_by_label():
    many = [
        {"id": str(1000 + i), "local_identifier": f"Bird {59 - i:02d}", "nick_name": "", "ring_id": "", "taxon": ""}
        for i in range(60)
    ]
    out = search_individuals(many, None)
    assert len(out["options"]) == 50 and out["truncated"] is True
    assert out["options"][0]["label"] == "Bird 00 (1059)"
    assert search_individuals(INDIVIDUALS, "  ")["truncated"] is False
    assert len(search_individuals(INDIVIDUALS, "")["options"]) == 3


def test_resolve_keeps_request_order_and_skips_unknown_ids():
    out = resolve(STUDIES, ["33", "999", "10"], study_option)
    assert _values(out) == ["33", "10"] and out["truncated"] is False


def test_trim_helpers_tolerate_missing_columns():
    assert trim_study({"id": 5}) == {"id": "5", "name": ""}
    assert trim_study({"id": "5", "name": None}) == {"id": "5", "name": ""}
    assert trim_individual({"id": "9"}) == {
        "id": "9", "local_identifier": "", "nick_name": "", "ring_id": "", "taxon": "",
    }


def test_trim_keeps_only_what_the_options_need():
    row = {"id": "5", "name": " Mara ", "citation": "long text", "i_am_owner": "true"}
    assert trim_study(row) == {"id": "5", "name": "Mara"}
    ind = {"id": "9", "local_identifier": "A", "nick_name": "B", "ring_id": "C",
           "taxon_canonical_name": "Gyps", "sex": "f", "timestamp_start": "2020"}
    assert trim_individual(ind) == {
        "id": "9", "local_identifier": "A", "nick_name": "B", "ring_id": "C", "taxon": "Gyps",
    }
