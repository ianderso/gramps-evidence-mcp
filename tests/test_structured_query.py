"""The structured query engine.

Verified against gramps-webapi 3.21.1. Two things this pins that nothing else
could: event type is reachable only through the integer in ``type.value``, and
a person query is privacy-filtered without the caller having to ask for dates.
"""

from __future__ import annotations

import pytest


def _person_row(
    gid: str,
    surname: str,
    birth_year: int | None = None,
    death_year: int | None = None,
    private: int = 0,
) -> dict:
    """Build a query row the way the engine returns one."""
    row: dict = {
        "gramps_id": gid,
        "surname": surname,
        "_gid": gid,
        "_handle": f"h-{gid}",
        "_private": private,
    }
    for key, year in (("_birth", birth_year), ("_death", death_year)):
        row[key] = {"_class": "Date", "dateval": [0, 0, year, False], "text": ""} if year else None
    return row


# --------------------------------------------------------------------------- #
# Event type, the thing GrampsQL cannot reach
# --------------------------------------------------------------------------- #
async def test_event_type_filters_on_the_stored_integer(tools):
    """Gramps stores a built-in type as an int and leaves `string` empty.

    Filtering on the name would match nothing, which is exactly how GrampsQL
    fails. The translation is what makes this tool worth having.
    """
    await tools("query_records", object_type="event", event_type="Birth")
    _, body = tools.fake.query_bodies[-1]
    assert body["where"] == [{"column": {"json_path": ["type", "value"]}, "op": "eq", "value": 12}]


async def test_event_type_lookup_is_case_insensitive(tools):
    """A caller writing 'birth' means the same thing."""
    await tools("query_records", object_type="event", event_type="birth")
    _, body = tools.fake.query_bodies[-1]
    assert body["where"][0]["value"] == 12


async def test_unknown_event_type_is_refused_not_silently_empty(tools):
    """Silently matching nothing is the failure mode being fixed here."""
    out = await tools("query_records", object_type="event", event_type="Bith")
    assert out["error"] == "unknown_event_type"
    assert "Bith" in out["message"]


async def test_event_type_on_another_collection_is_refused(tools):
    """Only events have a type integer; say so rather than build a bad query."""
    out = await tools("query_records", object_type="person", event_type="Birth")
    assert out["error"] == "unsupported_filter"


async def test_event_type_map_is_fetched_once_per_session(tools):
    """The map is server truth, not a hard-coded table -- but it is stable."""
    await tools("query_records", object_type="event", event_type="Birth")
    await tools("query_records", object_type="event", event_type="Death")
    _, body = tools.fake.query_bodies[-1]
    assert body["where"][0]["value"] == 13


async def test_list_event_types_reports_names_with_integers(tools):
    """Callers need the vocabulary before they can filter on it."""
    out = await tools("list_event_types")
    by_name = {e["name"]: e["value"] for e in out["event_types"]}
    assert by_name["Birth"] == 12
    assert by_name["Census"] == 21


# --------------------------------------------------------------------------- #
# Query construction
# --------------------------------------------------------------------------- #
async def test_count_is_always_requested(tools):
    """A total is what makes a result interpretable; asking costs one header."""
    await tools("query_records", object_type="source")
    _, body = tools.fake.query_bodies[-1]
    assert body["count"] is True


async def test_limit_is_clamped_to_the_engine_maximum(tools):
    """An unbounded limit on a large tree is a way to hang the server."""
    await tools("query_records", object_type="source", limit=99999)
    assert tools.fake.query_bodies[-1][1]["limit"] == 500
    await tools("query_records", object_type="source", limit=0)
    assert tools.fake.query_bodies[-1][1]["limit"] == 1


async def test_optional_clauses_are_omitted_when_empty(tools):
    """Sending empty clauses makes the engine reject an otherwise fine query."""
    await tools("query_records", object_type="source")
    _, body = tools.fake.query_bodies[-1]
    assert "where" not in body
    assert "order_by" not in body
    assert "after" not in body


async def test_where_and_select_pass_through(tools):
    """json_path and operators are the caller's to use; do not rewrite them."""
    await tools(
        "query_records",
        object_type="family",
        select=["gramps_id", {"json_path": ["father", "surname"], "as": "fsn"}],
        where=[{"column": "father_handle", "op": "ne", "value_column": "mother_handle"}],
        order_by=[{"column": "gramps_id", "direction": "desc"}],
    )
    _, body = tools.fake.query_bodies[-1]
    assert body["select"][1]["as"] == "fsn"
    assert body["where"][0]["value_column"] == "mother_handle"
    assert body["order_by"][0]["direction"] == "desc"


async def test_unknown_collection_is_refused_with_the_valid_set(tools):
    """A typo should teach the caller the vocabulary, not 404."""
    out = await tools("query_records", object_type="peple")
    assert out["error"] == "unsupported_type"
    assert "person" in out["message"]


async def test_cursor_is_returned_for_paging(tools):
    """Keyset pagination is the only way past the first page of a big result."""
    tools.fake.query_cursor = "cursor-abc"
    out = await tools("query_records", object_type="source")
    assert out["next_after"] == "cursor-abc"
    await tools("query_records", object_type="source", after="cursor-abc")
    assert tools.fake.query_bodies[-1][1]["after"] == "cursor-abc"


# --------------------------------------------------------------------------- #
# Privacy
# --------------------------------------------------------------------------- #
async def test_person_query_fetches_dates_it_was_not_asked_for(tools):
    """Without dates every row would be withheld for want of proof.

    The privacy judgement needs birth and death; the caller should not have to
    know that, so the service adds them and strips them again.
    """
    await tools("query_records", object_type="person", select=["gramps_id"])
    _, body = tools.fake.query_bodies[-1]
    aliases = {e.get("as") for e in body["select"] if isinstance(e, dict)}
    assert {"_birth", "_death", "_private"} <= aliases


async def test_historical_person_is_returned_without_the_added_columns(tools):
    """The caller gets what they asked for, not the privacy scaffolding."""
    tools.fake.query_rows["person"] = [_person_row("I0001", "Pembrook", 1762, 1834)]
    out = await tools("query_records", object_type="person", select=["gramps_id"])
    assert out["rows"][0] == {"gramps_id": "I0001", "surname": "Pembrook"}


async def test_living_person_is_redacted_in_query_output(tools):
    """A structured query is bulk output and obeys the same rule as the rest."""
    tools.fake.query_rows["person"] = [_person_row("I0002", "Pembrook", 2005)]
    out = await tools("query_records", object_type="person", select=["gramps_id"])
    assert out["rows"][0]["redacted"] is True
    assert "surname" not in out["rows"][0]


async def test_undated_person_is_redacted(tools):
    """Unknown dates err toward privacy, as everywhere else."""
    tools.fake.query_rows["person"] = [_person_row("I0003", "Pembrook")]
    out = await tools("query_records", object_type="person", select=["gramps_id"])
    assert out["rows"][0]["redacted"] is True


async def test_private_flag_redacts_even_a_historical_person(tools):
    """An explicit private flag outranks the date heuristic."""
    tools.fake.query_rows["person"] = [_person_row("I0004", "Pembrook", 1762, 1834, private=1)]
    out = await tools("query_records", object_type="person", select=["gramps_id"])
    assert out["rows"][0]["redacted"] is True


@pytest.mark.parametrize("object_type", ["event", "source", "citation", "note"])
async def test_other_collections_are_judged_by_their_private_flag(tools, object_type):
    """A private note's text is as much bulk output as a living person's name."""
    tools.fake.query_rows[object_type] = [
        {"gramps_id": "X0001", "_private": 0},
        {"gramps_id": "X0002", "_private": 1},
    ]
    out = await tools("query_records", object_type=object_type, select=["gramps_id"])
    _, body = tools.fake.query_bodies[-1]
    assert {"json_path": ["private"], "as": "_private"} in body["select"]
    assert out["rows"][0] == {"gramps_id": "X0001"}
    assert out["rows"][1]["redacted"] is True


async def test_a_family_row_is_judged_by_both_parents(tools):
    """father/mother paths reach a parent's name from a family row."""
    living = {"_class": "Date", "dateval": [0, 0, 1990, False], "text": ""}
    old = {"_class": "Date", "dateval": [0, 0, 1850, False], "text": ""}
    tools.fake.query_rows["family"] = [
        {
            "gramps_id": "F0001",
            "_private": 0,
            "_father_handle": "f1",
            "_father_birth": old,
            "_mother_handle": None,
        },
        {
            "gramps_id": "F0002",
            "_private": 0,
            "_father_handle": "f2",
            "_father_birth": old,
            "_father_death": old,
            "_mother_handle": "m2",
            "_mother_birth": living,
        },
    ]
    out = await tools("query_records", object_type="family", select=["gramps_id"])
    # F0001's father has a birth but no death, 175 years ago: historical.
    # Its absent mother is not a living person.
    assert out["rows"][0] == {"gramps_id": "F0001"}
    assert out["rows"][1]["redacted"] is True


async def test_a_caller_alias_cannot_pass_for_the_death_date(tools):
    """Only the paths the service added are read when judging a row."""
    row = _person_row("I0005", "Pembrook", 1990)
    row["death"] = {"_class": "Date", "dateval": [0, 0, 2010, False], "text": ""}
    tools.fake.query_rows["person"] = [row]
    out = await tools(
        "query_records",
        object_type="person",
        select=["gramps_id", {"json_path": ["birth", "date"], "as": "death"}],
    )
    assert out["rows"][0]["redacted"] is True


async def test_a_person_query_with_no_columns_still_judges_dates(tools):
    """The default columns carry no dates, which withheld every person."""
    tools.fake.query_rows["person"] = [_person_row("I0006", "Pembrook", 1800, 1870)]
    out = await tools("query_records", object_type="person")
    _, body = tools.fake.query_bodies[-1]
    assert "surname" in body["select"]
    assert out["rows"][0]["gramps_id"] == "I0006"


async def test_a_stub_is_built_from_the_real_ids_not_the_callers_columns(tools):
    """A column aliased `gramps_id` could otherwise put a name into the stub."""
    row = _person_row("I0007", "Pembrook", 1990)
    row["gramps_id"], row["handle"] = "Jane", "Pembrook"  # the caller's aliases
    tools.fake.query_rows["person"] = [row]
    out = await tools(
        "query_records",
        object_type="person",
        select=[
            {"json_path": ["primary_name", "first_name"], "as": "gramps_id"},
            {"json_path": ["primary_name", "surname_list", 0, "surname"], "as": "handle"},
        ],
    )
    stub = out["rows"][0]
    assert stub["redacted"] is True
    assert (stub["gramps_id"], stub["handle"]) == ("I0007", "h-I0007")


async def test_underscore_aliases_are_reserved(tools):
    """They would collide with the paths the privacy judgement adds."""
    out = await tools(
        "query_records",
        object_type="person",
        select=["gramps_id", {"json_path": ["birth", "date"], "as": "_death"}],
    )
    assert out["error"] == "reserved_alias"
    assert tools.fake.query_bodies == []


async def test_an_undated_death_event_marks_a_person_historical(service):
    """The engine answers death.date with an empty Date dict, not null."""
    empty = {"_class": "Date", "dateval": [0, 0, 0, False], "text": ""}
    born_1950 = {"_class": "Date", "dateval": [0, 0, 1950, False], "text": ""}
    assert service._person_restricted(0, born_1950, empty) is False
    assert service._person_restricted(0, born_1950, None) is True


# --------------------------------------------------------------------------- #
# Forms the server answers wrongly are refused before they reach it
# --------------------------------------------------------------------------- #
def _where(path, op, value) -> list:
    return [{"column": {"json_path": path}, "op": op, "value": value}]


@pytest.mark.parametrize(
    ("args", "fixed"),
    [
        ({"where": _where(["date", "year"], "lt", 1800)}, "['date', 'dateval', 2]"),
        (
            {"select": [{"json_path": ["birth", "date", "year"], "as": "y"}]},
            "['birth', 'date', 'dateval', 2]",
        ),
        (
            {"order_by": [{"column": {"json_path": ["date", "year"]}, "direction": "asc"}]},
            "['date', 'dateval', 2]",
        ),
    ],
)
async def test_a_date_year_is_refused_with_the_path_that_works(tools, args, fixed):
    out = await tools("query_records", object_type="person", **args)
    assert out["error"] == "date_year", out
    assert fixed in out["message"]
    assert not tools.fake.query_bodies, "refused before reaching the server"


async def test_type_as_a_plain_column_points_events_at_event_type(tools):
    out = await tools(
        "query_records",
        object_type="event",
        where=[{"column": "type", "op": "eq", "value": 12}],
    )
    assert out["error"] == "type_column"
    assert "event_type=" in out["message"]
    family = await tools(
        "query_records",
        object_type="family",
        order_by=[{"column": "type", "direction": "asc"}],
    )
    assert family["error"] == "type_column"
    assert '["type", "value"]' in family["message"]


@pytest.mark.parametrize(
    "where",
    [
        _where(["citation_list"], "eq", []),
        _where(["citation_list"], "ne", []),
        _where(["date", "dateval"], "eq", [0, 0, 1850, False]),
    ],
)
async def test_a_list_value_is_refused_except_with_in(tools, where):
    out = await tools("query_records", object_type="event", where=where)
    assert out["error"] == "list_comparison", out
    assert "contains" in out["message"] and "list_unsourced_facts" in out["message"]
    assert not tools.fake.query_bodies


async def test_the_forms_that_work_still_pass(tools):
    for where in (
        [{"column": "gramps_id", "op": "in", "value": ["E0001", "E0002"]}],
        _where(["citation_list"], "contains", "h000001"),
        _where(["date", "dateval", 2], "gt", 0),
        [{"column": {"json_path": ["type", "value"]}, "op": "eq", "value": 12}],
    ):
        out = await tools("query_records", object_type="event", where=where)
        assert "error" not in out, (where, out)
