"""Matching and labelling for the study and individual pickers.

Pure functions over trimmed rows: the handlers do the I/O (Movebank, Redis)
and hand the lists here. Movebank has no name search, so the search the
portal's typeahead asks for happens in this module.
"""
from typing import Callable, List, Optional

from app.actions.core import ReferenceDataResponse, ReferenceOption

MAX_OPTIONS = 50

_STUDY_SEARCH_FIELDS = ("name", "id")
_INDIVIDUAL_SEARCH_FIELDS = ("local_identifier", "nick_name", "ring_id", "id")


def _text(value) -> str:
    return "" if value is None else str(value).strip()


def trim_study(row: dict) -> dict:
    """The part of a Movebank study row worth caching: what an option shows."""
    return {"id": _text(row.get("id")), "name": _text(row.get("name"))}


def trim_individual(row: dict) -> dict:
    return {
        "id": _text(row.get("id")),
        "local_identifier": _text(row.get("local_identifier")),
        "nick_name": _text(row.get("nick_name")),
        "ring_id": _text(row.get("ring_id")),
        "taxon": _text(row.get("taxon_canonical_name")),
    }


def _label(name: str, row_id: str) -> str:
    return f"{name} ({row_id})" if name else row_id


def study_option(study: dict) -> ReferenceOption:
    return ReferenceOption(value=study["id"], label=_label(study["name"], study["id"]))


def individual_option(ind: dict) -> ReferenceOption:
    # Same preference order as the name observations are sent under
    # (handlers._display_name), so the picker shows the name ER will show.
    name = ind["local_identifier"] or ind["nick_name"] or ind["ring_id"]
    return ReferenceOption(
        value=ind["id"],
        label=_label(name, ind["id"]),
        description=ind["taxon"] or None,
    )


def _search(rows: list, q: Optional[str], fields, to_option: Callable, *, empty_query_lists_all: bool) -> dict:
    query = (q or "").strip().lower()
    if not query and not empty_query_lists_all:
        # Too many to browse: tell the portal there is more once the user types.
        return ReferenceDataResponse(options=[], truncated=True).dict()

    ranked = []
    for row in rows:
        if query and not any(query in row[field].lower() for field in fields):
            continue
        option = to_option(row)
        label = (option.label or option.value).lower()
        if query and row["id"].lower() == query:
            rank = 0
        elif query and label.startswith(query):
            rank = 1
        else:
            rank = 2
        ranked.append((rank, label, option))
    ranked.sort(key=lambda item: item[:2])

    options = [option for _, _, option in ranked[:MAX_OPTIONS]]
    return ReferenceDataResponse(options=options, truncated=len(ranked) > MAX_OPTIONS).dict()


def search_studies(studies: list, q: Optional[str]) -> dict:
    return _search(studies, q, _STUDY_SEARCH_FIELDS, study_option, empty_query_lists_all=False)


def search_individuals(individuals: list, q: Optional[str]) -> dict:
    return _search(individuals, q, _INDIVIDUAL_SEARCH_FIELDS, individual_option, empty_query_lists_all=True)


def resolve(rows: list, ids: List[str], to_option: Callable) -> dict:
    """Label saved values: the requested rows, in the order asked. Unknown IDs
    are left out; the portal keeps showing those as raw values."""
    by_id = {row["id"]: row for row in rows}
    options = [to_option(by_id[i]) for i in ids if i in by_id]
    return ReferenceDataResponse(options=options, truncated=False).dict()
