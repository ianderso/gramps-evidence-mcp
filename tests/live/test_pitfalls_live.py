"""Every server behaviour docs/PITFALLS.md asserts, checked against a real server.

Each test names its section. When one fails, either the server changed or the
document was wrong, and in both cases the document and anything relying on
it -- the service, the fake -- need correcting. Sections that describe this
project's own code rather than the server (4, 9, 10, 11) are covered by the
unit tests instead.
"""

from __future__ import annotations

import json
import uuid

import pytest

from gramps_evidence_mcp.client import GrampsApiError, new_handle
from gramps_evidence_mcp.mapping import MOD_FROM, MOD_SPAN, parse_date

from .harness import live_version


async def _person(client, given="Ada", surname="Wren", **extra) -> dict:
    payload = {
        "_class": "Person",
        "primary_name": {
            "_class": "Name",
            "first_name": given,
            "surname_list": [{"_class": "Surname", "surname": surname, "primary": True}],
        },
        "gender": 0,
        **extra,
    }
    return await client.create_object("person", payload)


async def _event(client, event_type="Birth", date="1850", **extra) -> dict:
    payload = {"_class": "Event", "type": event_type, "date": parse_date(date), **extra}
    return await client.create_object("event", payload)


def _ref(event_handle: str, role: str = "Primary") -> dict:
    return {"_class": "EventRef", "ref": event_handle, "role": role}


async def _source_with_citation(client) -> tuple[dict, dict]:
    source = await client.create_object("source", {"_class": "Source", "title": "Register"})
    citation = await client.create_object(
        "citation",
        {"_class": "Citation", "source_handle": source["handle"], "page": "p. 1"},
    )
    return source, citation


# --------------------------------------------------------------------------- #
# 1-3, 5, 6: reads and writes
# --------------------------------------------------------------------------- #
async def test_1_a_put_built_from_a_keys_fetch_destroys_the_rest(live):
    client = live.client
    birth = await _event(client)
    person = await _person(client, event_ref_list=[_ref(birth["handle"])], birth_ref_index=0)
    partial = await client.get_object("person", person["handle"], keys="handle,gramps_id")
    await client.update_object("person", person["handle"], partial)
    after = await client.get_object("person", person["handle"])
    assert after.get("event_ref_list") == []
    assert (after.get("primary_name") or {}).get("first_name", "") == ""


async def test_2_keys_also_nulls_backlinks_unless_named(live):
    client = live.client
    source, _ = await _source_with_citation(client)
    filtered = await client.get_object(
        "source", source["handle"], keys="handle,gramps_id", backlinks=True
    )
    assert not filtered.get("backlinks")
    named = await client.get_object(
        "source", source["handle"], keys="handle,gramps_id,backlinks", backlinks=True
    )
    assert named.get("backlinks")
    whole = await client.get_object("source", source["handle"], extend="all")
    assert "backlinks" not in whole, "extend=all does not imply backlinks"


async def test_3_backlinks_are_keyed_by_singular_type_name(live):
    client = live.client
    source, citation = await _source_with_citation(client)
    links = (await client.get_object("source", source["handle"], backlinks=True))["backlinks"]
    assert links == {"citation": [citation["handle"]]}


async def test_5_detaching_a_citation_does_not_delete_it(live):
    client = live.client
    _, citation = await _source_with_citation(client)
    event = await _event(client, citation_list=[citation["handle"]])
    stored = await client.get_object("event", event["handle"])
    stored["citation_list"] = []
    await client.update_object("event", event["handle"], stored)
    assert (await client.get_object("citation", citation["handle"]))["handle"]


async def test_6_a_stale_write_wins_silently(live):
    """No locking: a write from an old read undoes a newer one without a word."""
    client = live.client
    person = await _person(client)
    stale = await client.get_object("person", person["handle"])

    fresh = await client.get_object("person", person["handle"])
    fresh["gender"] = 1
    await client.update_object("person", person["handle"], fresh)

    stale["private"] = True
    await client.update_object("person", person["handle"], stale)
    after = await client.get_object("person", person["handle"])
    assert after["gender"] == 0, "the stale write silently undid the other one"


async def test_6_if_match_refuses_even_a_fresh_etag(live):
    """A tripwire. The PUT checks If-Match against a hash of the stored object;
    the GET's ETag is a hash of the response body (plus ":gzip" when
    compressed), so no tag a client can obtain ever matches, and only the
    wildcard passes -- checking nothing.

    If this fails, gramps-webapi has fixed it: make ``_mutate()`` send the
    ETag of its read, re-applying the edit once on 412, and update
    docs/PITFALLS.md section 6.
    """
    client = live.client
    person = await _person(client)
    path = f"/api/people/{person['handle']}"
    headers = client._auth_headers()
    for encoding in ("gzip", "identity"):
        read = await client._http.get(path, headers={**headers, "Accept-Encoding": encoding})
        etag = read.headers.get("ETag")
        assert etag, "the server sends an ETag on a read"
        resp = await client._http.put(path, json=read.json(), headers={**headers, "If-Match": etag})
        assert resp.status_code == 412, (encoding, resp.status_code)
    resp = await client._http.put(path, json=read.json(), headers={**headers, "If-Match": "*"})
    assert resp.status_code == 200


# --------------------------------------------------------------------------- #
# 7: GrampsQL
# --------------------------------------------------------------------------- #
async def test_7_grampsql_equality_is_a_single_equals(live):
    client = live.client
    await _source_with_citation(client)
    assert await client.list_objects("citation", gql='page = "p. 1"')
    with pytest.raises(GrampsApiError) as exc:
        await client.list_objects("citation", gql='page == "p. 1"')
    assert exc.value.status == 422


async def test_7_grampsql_substring_and_length(live):
    client = live.client
    await _source_with_citation(client)
    assert await client.list_objects("citation", gql='page ~ "p."')
    assert await client.list_objects("source", gql="media_list.length = 0")


async def test_7_a_field_the_object_lacks_matches_nothing_without_error(live):
    client = live.client
    await _source_with_citation(client)
    assert await client.list_objects("citation", gql="bogus_field = 1") == []


async def test_7_event_type_is_not_reachable_from_grampsql(live):
    client = live.client
    await _event(client, "Birth")
    assert await client.list_objects("event", gql='type = "Birth"') == []


async def test_7_a_source_has_no_citation_list_to_measure(live):
    """Citations point at their source; a source holds no list of them.

    So ``citation_list.length = 0`` cannot find an uncited source: with
    gramps-ql 0.5.0 it matches no source at all, cited or not.
    """
    client = live.client
    await _source_with_citation(client)
    await client.create_object("source", {"_class": "Source", "title": "Uncited"})
    assert await client.list_objects("source", gql="citation_list.length = 0") == []


async def test_7_booleans_compare_as_integers(live):
    client = live.client
    await _person(client, private=True)
    assert len(await client.list_objects("person", gql="private = 1")) == 1
    assert await client.list_objects("person", gql="private = true") == []


# --------------------------------------------------------------------------- #
# 8, 21: the server's merge
# --------------------------------------------------------------------------- #
async def test_8_the_server_merge_moves_lists_and_repoints_references(live):
    client = live.client
    note = await client.create_object(
        "note", {"_class": "Note", "text": {"_class": "StyledText", "string": "x"}}
    )
    keep = await client.create_object("source", {"_class": "Source", "title": "Bible"})
    drop = await client.create_object(
        "source", {"_class": "Source", "title": "Bible", "note_list": [note["handle"]]}
    )
    citation = await client.create_object(
        "citation", {"_class": "Citation", "source_handle": drop["handle"], "page": "p. 2"}
    )
    await client.merge("source", keep["handle"], drop["handle"])
    assert note["handle"] in (await client.get_object("source", keep["handle"]))["note_list"]
    moved = await client.get_object("citation", citation["handle"])
    assert moved["source_handle"] == keep["handle"]
    with pytest.raises(GrampsApiError):
        await client.get_object("source", drop["handle"])


async def test_21_a_place_merge_unions_the_enclosures(live):
    client = live.client
    state = await client.create_object("place", {"_class": "Place", "name": {"value": "S"}})
    county = await client.create_object(
        "place",
        {
            "_class": "Place",
            "name": {"value": "C"},
            "placeref_list": [{"_class": "PlaceRef", "ref": state["handle"]}],
        },
    )
    keep, drop = [
        await client.create_object(
            "place",
            {
                "_class": "Place",
                "name": {"value": "T"},
                "placeref_list": [{"_class": "PlaceRef", "ref": parent}],
            },
        )
        for parent in (county["handle"], state["handle"])
    ]
    await client.merge("place", keep["handle"], drop["handle"])
    parents = [
        r["ref"] for r in (await client.get_object("place", keep["handle"]))["placeref_list"]
    ]
    assert parents == [county["handle"], state["handle"]]


# --------------------------------------------------------------------------- #
# 12, 13: the structured query engine
# --------------------------------------------------------------------------- #
async def test_12_event_type_is_an_integer_to_the_query_engine(live):
    client = live.client
    birth = await _event(client, "Birth")
    await _event(client, "Death")
    assert (await client.get_object("event", birth["handle"]))["type"] == "Birth"
    mapping = await client.type_map("event_types")
    assert mapping.get("12") == "Birth"

    rows, _, _ = await client.structured_query(
        "event",
        {
            "select": ["handle"],
            "where": [{"column": {"json_path": ["type", "value"]}, "op": "eq", "value": 12}],
        },
    )
    assert [r["handle"] for r in rows] == [birth["handle"]]
    rows, _, _ = await client.structured_query(
        "event",
        {
            "select": ["handle"],
            "where": [{"column": {"json_path": ["type", "string"]}, "op": "eq", "value": "Birth"}],
        },
    )
    assert rows == []

    whole_column = {"select": ["handle"], "where": [{"column": "type", "op": "eq", "value": 12}]}
    if live_version() >= (3, 22):
        # Accepted, but compared as the whole {"string", "value"} object.
        rows, _, _ = await client.structured_query("event", whole_column)
        assert rows == []
    else:
        with pytest.raises(GrampsApiError) as exc:
            await client.structured_query("event", whole_column)
        assert exc.value.status == 422


async def test_13_named_columns_and_what_else_a_select_may_name(live):
    """3.21: an allowlist of columns. 3.22: any field of the stored object too."""
    client = live.client
    await _person(client)
    rows, _, _ = await client.structured_query("person", {"select": ["gramps_id", "surname"]})
    assert rows and rows[0]["surname"] == "Wren"
    if live_version() >= (3, 22):
        rows, _, _ = await client.structured_query("person", {"select": ["primary_name"]})
        assert rows[0]["primary_name"]["first_name"] == "Ada"
    else:
        with pytest.raises(GrampsApiError) as exc:
            await client.structured_query("person", {"select": ["primary_name"]})
        assert exc.value.status == 422
    with pytest.raises(GrampsApiError) as exc:
        await client.structured_query("person", {"select": ["bogus"]})
    assert exc.value.status == 422


async def test_13_a_path_the_object_lacks(live):
    """3.21: matches nothing, silently. 3.22: refused, naming the fields there are."""
    client = live.client
    await _person(client)
    query = {
        "select": ["handle"],
        "where": [{"column": {"json_path": ["bogus"]}, "op": "eq", "value": 1}],
    }
    if live_version() >= (3, 22):
        with pytest.raises(GrampsApiError) as exc:
            await client.structured_query("person", query)
        assert exc.value.status == 422
        assert "known fields" in str(exc.value)
    else:
        rows, _, _ = await client.structured_query("person", query)
        assert rows == []


@pytest.mark.parametrize(
    ("path", "op", "value"),
    [
        (["citation_list"], "eq", []),
        (["citation_list"], "ne", []),
        (["citation_list"], "eq", ["x"]),
        (["date", "dateval"], "eq", [0, 0, 1850, False]),
    ],
)
async def test_13_a_list_value_is_a_server_error_except_with_in(live, path, op, value):
    """What ``query_records`` refuses as ``list_comparison``."""
    client = live.client
    await _event(client)
    with pytest.raises(GrampsApiError) as exc:
        await client.structured_query(
            "event",
            {
                "select": ["handle"],
                "where": [{"column": {"json_path": path}, "op": op, "value": value}],
            },
        )
    assert exc.value.status == 500


async def test_13_contains_finds_a_value_in_a_list_and_in_takes_a_list(live):
    client = live.client
    _, citation = await _source_with_citation(client)
    cited = await _event(client, citation_list=[citation["handle"]])
    await _event(client, "Death")

    async def handles(where):
        rows, _, _ = await client.structured_query(
            "event", {"select": ["handle"], "where": [where]}
        )
        return [r["handle"] for r in rows]

    assert await handles(
        {"column": {"json_path": ["citation_list"]}, "op": "contains", "value": citation["handle"]}
    ) == [cited["handle"]]
    assert await handles({"column": "gramps_id", "op": "in", "value": [cited["gramps_id"]]}) == [
        cited["handle"]
    ]


async def test_13_there_is_no_stored_year_and_an_unknown_one_is_zero(live):
    """No ``year`` is stored unless a client writes back what it read (section
    24): 3.21 matches nothing on it, 3.22 refuses it.

    The year is ``dateval[2]``, and an undated event's is 0 -- so a filter
    for births before 1800 finds the people whose birth has no date.
    """
    client = live.client
    for label, text in (("dated", "1850"), ("undated", None)):
        birth = await _event(client, "Birth", date=text)
        await _person(
            client, given=label, event_ref_list=[_ref(birth["handle"])], birth_ref_index=0
        )

    async def below_1800(path):
        rows, _, _ = await client.structured_query(
            "person",
            {
                "select": ["given_name"],
                "where": [{"column": {"json_path": path}, "op": "lt", "value": 1800}],
            },
        )
        return [r["given_name"] for r in rows]

    if live_version() >= (3, 22):
        with pytest.raises(GrampsApiError) as exc:
            await below_1800(["birth", "date", "year"])
        assert exc.value.status == 422
    else:
        assert await below_1800(["birth", "date", "year"]) == []
    assert await below_1800(["birth", "date", "dateval", 2]) == ["undated"]


# --------------------------------------------------------------------------- #
# 14: DNA -- read from the source until now, never exercised live
# --------------------------------------------------------------------------- #
async def test_14_a_dna_match_is_an_association_the_server_reads_back(live):
    tested = await live("add_person", given="Ada", surname="Wren")
    match = await live("add_person", given="Bea", surname="Wren")
    out = await live(
        "add_dna_match",
        person=tested["gramps_id"],
        match=match["gramps_id"],
        segments="1,1000000,5000000,7.5,1200\n2,2000000,9000000,11.25,2400",
        citation={"source_title": "DNA test, kit A1", "page": "match list"},
    )
    assert out["changed"] is True, out
    stored = await live.client.get_object("person", tested["handle"])
    (ref,) = stored["person_ref_list"]
    assert (ref["ref"], ref["rel"]) == (match["handle"], "DNA")
    shown = await live("get_dna_matches", person=tested["gramps_id"])
    assert shown["match_count"] == 1
    assert shown["matches"][0]["segment_count"] == 2
    assert shown["matches"][0]["total_cM"] == 18.75
    assert shown["matches"][0]["largest_segment_cM"] == 11.25


# --------------------------------------------------------------------------- #
# 15, 16, 18, 19, 20, 22: what writes and deletes do
# --------------------------------------------------------------------------- #
async def test_15_a_new_father_gets_the_family_appended_without_a_check(live):
    client = live.client
    father = await _person(client)
    family = await client.create_object("family", {"_class": "Family"})
    stored = await client.get_object("person", father["handle"])
    stored["family_list"] = [family["handle"]]
    await client.update_object("person", father["handle"], stored)
    fam = await client.get_object("family", family["handle"])
    fam["father_handle"] = father["handle"]
    await client.update_object("family", family["handle"], fam)
    after = (await client.get_object("person", father["handle"]))["family_list"]
    assert after == [family["handle"], family["handle"]]


async def test_15_a_removed_child_loses_only_the_first_of_two_links(live):
    client = live.client
    child = await _person(client)
    family = await client.create_object(
        "family",
        {"_class": "Family", "child_ref_list": [{"_class": "ChildRef", "ref": child["handle"]}]},
    )
    stored = await client.get_object("person", child["handle"])
    assert stored["parent_family_list"] == [family["handle"]], "a new family links its child"
    stored["parent_family_list"] = [family["handle"], family["handle"]]
    await client.update_object("person", child["handle"], stored)
    fam = await client.get_object("family", family["handle"])
    fam["child_ref_list"] = []
    await client.update_object("family", family["handle"], fam)
    assert (await client.get_object("person", child["handle"]))["parent_family_list"] == [
        family["handle"]
    ]


async def test_15_a_parent_change_needs_the_old_parent_to_list_the_family(live):
    """``family_list.remove`` on the old parent: a ValueError, answered 400."""
    client = live.client
    mother = await _person(client)
    other = await _person(client, given="Ruth")
    family = await client.create_object(
        "family", {"_class": "Family", "mother_handle": mother["handle"]}
    )
    stored = await client.get_object("person", mother["handle"])
    assert stored["family_list"] == [family["handle"]], "a new family links its mother"
    stored["family_list"] = []
    await client.update_object("person", mother["handle"], stored)
    fam = await client.get_object("family", family["handle"])
    fam["mother_handle"] = other["handle"]
    with pytest.raises(GrampsApiError) as exc:
        await client.update_object("family", family["handle"], fam)
    assert exc.value.status == 400
    assert (await client.get_object("family", family["handle"]))["mother_handle"] == mother[
        "handle"
    ]
    assert (await client.get_object("person", other["handle"]))["family_list"] == []


async def test_16_a_delete_removes_references_to_the_object(live):
    client = live.client
    birth = await _event(client)
    person = await _person(client, event_ref_list=[_ref(birth["handle"])])
    await client.delete_object("event", birth["handle"])
    assert (await client.get_object("person", person["handle"]))["event_ref_list"] == []


async def test_16_a_deleted_source_takes_its_citations_with_it(live):
    client = live.client
    source, citation = await _source_with_citation(client)
    await client.delete_object("source", source["handle"])
    with pytest.raises(GrampsApiError) as exc:
        await client.get_object("citation", citation["handle"])
    assert exc.value.status == 404


async def test_16_a_deleted_citation_strands_its_note(live):
    client = live.client
    note = await client.create_object(
        "note", {"_class": "Note", "text": {"_class": "StyledText", "string": "finding"}}
    )
    source = await client.create_object("source", {"_class": "Source", "title": "Register"})
    citation = await client.create_object(
        "citation",
        {"_class": "Citation", "source_handle": source["handle"], "note_list": [note["handle"]]},
    )
    await client.delete_object("citation", citation["handle"])
    stranded = await client.get_object("note", note["handle"], backlinks=True)
    assert not stranded.get("backlinks")


async def test_18_a_stray_type_key_is_kept_and_ignored_until_3_23(live):
    client = live.client
    payload = {"_class": "Place", "name": {"value": "Brannock"}, "type": "County"}
    if live_version() >= (3, 23):
        with pytest.raises(GrampsApiError) as exc:
            await client.create_object("place", payload)
        assert exc.value.status == 400
        assert "$: unknown Place keys: 'type'" in exc.value.detail
        return
    place = await client.create_object("place", payload)
    stored = await client.get_object("place", place["handle"])
    assert "type" in stored
    assert stored["place_type"] == "Unknown"


async def test_18_an_unknown_key_at_any_depth_is_kept_until_3_23_then_refused(live):
    client = live.client
    nested = {"_class": "Place", "name": {"value": "Brannock", "lang_code": "en"}}
    if live_version() < (3, 23):
        place = await client.create_object("place", nested)
        assert (await client.get_object("place", place["handle"]))["name"]["lang_code"] == "en"
        return
    with pytest.raises(GrampsApiError) as exc:
        await client.create_object("place", nested)
    assert exc.value.status == 400
    assert "$.name: unknown PlaceName keys: 'lang_code'" in exc.value.detail
    place = await client.create_object("place", {"_class": "Place", "name": {"value": "Brannock"}})
    served = await client.get_object("place", place["handle"])
    with pytest.raises(GrampsApiError) as exc:
        await client.update_object("place", place["handle"], {**served, "type": "County"})
    assert exc.value.status == 400


async def test_19_a_person_update_recomputes_birth_and_death_a_create_does_not(live):
    client = live.client
    baptism = await _event(client, "Baptism")
    person = await _person(client, event_ref_list=[_ref(baptism["handle"])], birth_ref_index=0)
    stored = await client.get_object("person", person["handle"])
    assert stored["birth_ref_index"] == 0, "a create keeps the index it was sent"
    await client.update_object("person", person["handle"], stored)
    assert (await client.get_object("person", person["handle"]))["birth_ref_index"] == -1


async def test_19_an_event_type_change_recomputes_its_people(live):
    client = live.client
    birth = await _event(client, "Birth")
    person = await _person(client, event_ref_list=[_ref(birth["handle"])])
    stored = await client.get_object("person", person["handle"])
    await client.update_object("person", person["handle"], stored)
    assert (await client.get_object("person", person["handle"]))["birth_ref_index"] == 0
    event = await client.get_object("event", birth["handle"])
    event["type"] = "Baptism"
    await client.update_object("event", birth["handle"], event)
    assert (await client.get_object("person", person["handle"]))["birth_ref_index"] == -1


async def test_20_span_and_open_dates_are_stored_and_short_datevals_refused(live):
    client = live.client
    span = await _event(client, "Military Service", date="from 4 May 1864 to 16 Sep 1864")
    opened = await _event(client, "Residence", date="from 1880")
    assert (await client.get_object("event", span["handle"]))["date"]["modifier"] == MOD_SPAN
    assert (await client.get_object("event", opened["handle"]))["date"]["modifier"] == MOD_FROM
    short = parse_date("from 1864 to 1865")
    short["dateval"] = short["dateval"][:4]
    with pytest.raises(GrampsApiError) as exc:
        await client.create_object("event", {"_class": "Event", "type": "Residence", "date": short})
    assert exc.value.status == 400


async def test_20_metadata_names_the_gramps_version(live):
    meta = await live.client.metadata()
    assert str((meta.get("gramps") or {}).get("version", "")).startswith("6.0")


async def test_22_a_write_is_recorded_under_a_fixed_description(live):
    client = live.client
    person = await _person(client)
    stored = await client.get_object("person", person["handle"])
    stored["gender"] = 1
    await client.update_object("person", person["handle"], stored)
    (latest,) = await client.transactions(page=1, pagesize=1, sort="-id")
    assert latest["description"] == "Edit Person"


# --------------------------------------------------------------------------- #
# 24: a date's served year
# --------------------------------------------------------------------------- #
async def test_24_a_served_year_written_back_is_kept_and_served_stale_until_3_23(live):
    client = live.client
    event = await _event(client, "Residence", date="1850")
    served = await client.get_object("event", event["handle"])
    assert served["date"]["year"] == 1850, "served, though nothing stored it"
    if live_version() < (3, 22):
        rows, _, _ = await client.structured_query(
            "event",
            {
                "select": ["handle"],
                "where": [{"column": {"json_path": ["date", "year"]}, "op": "eq", "value": 1850}],
            },
        )
        assert rows == [], "not stored"

    served["date"]["dateval"][2] = 1860  # an edit that keeps the served year
    await client.update_object("event", event["handle"], served)
    stale = (await client.get_object("event", event["handle"]))["date"]
    if live_version() >= (3, 23):
        assert (stale["dateval"][2], stale["year"]) == (1860, 1860), "dropped, not stored"
        out = await live("update_event", event=event["handle"], description="Census")
        assert "repaired" not in out, out
        return
    assert (stale["dateval"][2], stale["year"]) == (1860, 1850)

    out = await live("update_event", event=event["handle"], description="Census")
    assert out["repaired"] == [
        "dropped 1 stale date year the server would have shown instead of the date's own"
    ], out
    fixed = (await client.get_object("event", event["handle"]))["date"]
    assert (fixed["dateval"][2], fixed["year"]) == (1860, 1860)


# --------------------------------------------------------------------------- #
# 25: a merge merges more than it names
# --------------------------------------------------------------------------- #
async def _family(client, father=None, mother=None) -> dict:
    payload = {"_class": "Family", "father_handle": father or "", "mother_handle": mother or ""}
    return await client.create_object("family", payload)


async def test_25_a_person_merge_merges_families_left_with_the_same_parents(live):
    client = live.client
    elias, copy = await _person(client, "Elias"), await _person(client, "Elias", "Wrenn")
    hannah = await _person(client, "Hannah", "Ashbee", gender=0)
    first = await _family(client, elias["handle"], hannah["handle"])
    second = await _family(client, copy["handle"], hannah["handle"])
    await client.merge("person", elias["handle"], copy["handle"])
    with pytest.raises(GrampsApiError) as exc:
        await client.get_object("family", second["handle"])
    assert exc.value.status == 404, "merged into the first"
    assert (await client.get_object("person", hannah["handle"]))["family_list"] == [first["handle"]]

    merged = await client.get_object("person", elias["handle"])
    assert {"type": "Merged Gramps ID", "value": copy["gramps_id"]}.items() <= merged[
        "attribute_list"
    ][0].items()
    assert merged["alternate_names"][0]["surname_list"][0]["surname"] == "Wrenn"


async def test_25_a_family_merge_merges_a_differing_father(live):
    client = live.client
    elias, copy = await _person(client, "Elias"), await _person(client, "Elias")
    first = await _family(client, elias["handle"])
    second = await _family(client, copy["handle"])
    await client.merge("family", first["handle"], second["handle"])
    with pytest.raises(GrampsApiError) as exc:
        await client.get_object("person", copy["handle"])
    assert exc.value.status == 404, "the second father was merged into the first"


async def test_25_spouses_and_a_parent_with_their_child_are_refused_409(live):
    client = live.client
    elias, hannah, mercy = (
        await _person(client, "Elias"),
        await _person(client, "Hannah", gender=0),
        await _person(client, "Mercy", gender=0),
    )
    await client.create_object(
        "family",
        {
            "_class": "Family",
            "father_handle": elias["handle"],
            "mother_handle": hannah["handle"],
            "child_ref_list": [{"_class": "ChildRef", "ref": mercy["handle"]}],
        },
    )
    for keep, drop in ((elias, hannah), (mercy, elias)):
        with pytest.raises(GrampsApiError) as exc:
            await client.merge("person", keep["handle"], drop["handle"])
        assert exc.value.status == 409


async def test_25_a_family_merge_that_would_merge_a_father_and_son_is_refused_409(live):
    client = live.client
    elias, son = await _person(client, "Elias"), await _person(client, "Elias")
    await client.create_object(
        "family",
        {
            "_class": "Family",
            "father_handle": elias["handle"],
            "child_ref_list": [{"_class": "ChildRef", "ref": son["handle"]}],
        },
    )
    first, second = await _family(client, elias["handle"]), await _family(client, son["handle"])
    with pytest.raises(GrampsApiError) as exc:
        await client.merge("family", first["handle"], second["handle"])
    assert exc.value.status == 409


# --------------------------------------------------------------------------- #
# 26: type names are matched exactly, and the rest kept as custom for good
# --------------------------------------------------------------------------- #
async def test_26_a_name_not_spelt_exactly_is_stored_as_a_lasting_custom_type(live):
    client = live.client
    lower = await _event(client, "census")
    assert (await client.get_object("event", lower["handle"]))["type"] == "census"
    exact = await _event(client, "Census")
    custom = (await client.types())["custom"]["event_types"]
    assert "census" in custom and "Census" not in custom
    new = f"Land Grant {uuid.uuid4().hex[:8]}"
    made = await _event(client, new)
    assert new in (await client.types())["custom"]["event_types"]
    for event in (lower, exact, made):
        await client.delete_object("event", event["handle"])
    assert new in (await client.types())["custom"]["event_types"], "never taken off the list"


async def test_26_every_vocabulary_the_tools_match_is_served(live):
    types = await live.client.types()
    for key in (
        "event_types",
        "event_role_types",
        "child_reference_types",
        "name_types",
        "family_relation_types",
        "place_types",
        "note_types",
        "repository_types",
        "source_media_types",
        "url_types",
        "attribute_types",
        "source_attribute_types",
    ):
        assert key in types["default"], key
    for kind in ("person", "family", "event", "media", "source"):
        assert f"{kind}_attribute_types" in types["custom"], kind
    assert "Stepchild" in types["default"]["child_reference_types"]
    assert "Custom" not in types["default"]["child_reference_types"]
    assert types["default"]["source_attribute_types"] == ["Unknown"]
    assert "Web Home" in types["default"]["url_types"]
    assert "Web Home Page" not in types["default"]["url_types"]


# --------------------------------------------------------------------------- #
# 7: GrampsQL over a list
# --------------------------------------------------------------------------- #
async def test_7_tilde_on_a_list_compares_the_list_and_any_reaches_the_items(live):
    client = live.client
    note = await client.create_object(
        "note", {"_class": "Note", "text": {"string": "Patent at glorecords.blm.gov"}}
    )
    linked = await client.create_object(
        "source",
        {
            "_class": "Source",
            "title": "Linked",
            "note_list": [note["handle"]],
            "attribute_list": [
                {"_class": "SrcAttribute", "type": "URL", "value": "https://blm.gov/x"}
            ],
        },
    )
    await client.create_object("source", {"_class": "Source", "title": "Plain"})

    async def ids(gql: str) -> list[str]:
        rows = await client.list_objects("source", gql=gql, keys="handle")
        return [r["handle"] for r in rows]

    assert await ids('attribute_list ~ "blm.gov"') == []
    assert await ids('note_list ~ "blm.gov"') == []
    assert len(await ids('media_list !~ "x"')) == 2, "negated, it matches everything"
    assert await ids('attribute_list.any.value ~ "BLM.GOV"') == [linked["handle"]]
    assert await ids('note_list.any.get_note.text.string ~ "glorecords"') == [linked["handle"]]
    assert await ids(f'note_list ~ "{note["handle"]}"') == [linked["handle"]]
    assert await ids(
        'attribute_list.any.value ~ "nowhere" OR note_list.any.get_note.text.string ~ "blm"'
    ) == [linked["handle"]]


# --------------------------------------------------------------------------- #
# 27: research tasks
# --------------------------------------------------------------------------- #
async def test_27_objects_keeps_a_handle_the_request_makes(live):
    """Gramps Web's New Task form links its source and note by its own handles."""
    client = live.client
    source_handle, note_handle = uuid.uuid4().hex, uuid.uuid4().hex
    await client.create_objects(
        [
            {
                "_class": "Source",
                "title": "Order the probate file",
                "handle": source_handle,
                "note_list": [note_handle],
            },
            {"_class": "Note", "handle": note_handle, "text": {"string": "d"}, "type": "To Do"},
        ]
    )
    source = await client.get_object("source", source_handle)
    assert source["note_list"] == [note_handle]
    assert (await client.get_object("note", note_handle))["type"] == "To Do"


async def test_27_the_has_tag_rule_matches_a_tag_by_exact_name(live):
    client = live.client
    todo = await client.create_object("tag", {"_class": "Tag", "name": "ToDo"})
    other = await client.create_object("tag", {"_class": "Tag", "name": "todo list"})
    tagged = await client.create_object(
        "source", {"_class": "Source", "title": "A task", "tag_list": [todo["handle"]]}
    )
    await client.create_object(
        "source", {"_class": "Source", "title": "Not one", "tag_list": [other["handle"]]}
    )
    await client.create_object("source", {"_class": "Source", "title": "Evidence"})
    rows = await client.list_objects(
        "source", rules={"rules": [{"name": "HasTag", "values": ["ToDo"]}]}, keys="handle"
    )
    assert [r["handle"] for r in rows] == [tagged["handle"]]


# --------------------------------------------------------------------------- #
# 28: a handle the request makes
# --------------------------------------------------------------------------- #
async def test_28_a_create_keeps_a_handle_the_request_makes_and_refuses_it_twice(live):
    client = live.client
    handle = new_handle()
    made = await client.create_object(
        "note", {"_class": "Note", "handle": handle, "text": {"string": "first"}}
    )
    assert made["handle"] == handle
    with pytest.raises(GrampsApiError) as exc:
        await client.create_object(
            "note", {"_class": "Note", "handle": handle, "text": {"string": "second"}}
        )
    assert exc.value.status == 400
    assert (await client.get_object("note", handle))["text"]["string"] == "first"


async def test_28_a_long_note_is_one_write(live):
    """No limit on a note's text: 200,000 characters in one POST (TOOL-REQUESTS #28)."""
    client = live.client
    text = "Inventory of the estate, line by line. " * 5200
    made = await client.create_object("note", {"_class": "Note", "text": {"string": text}})
    assert (await client.get_object("note", made["handle"]))["text"]["string"] == text


# --------------------------------------------------------------------------- #
# 29: attribute names on an event reference
# --------------------------------------------------------------------------- #
async def test_29_an_event_reference_attribute_name_joins_no_vocabulary(live):
    client = live.client
    census = await _event(client, "Census", "1880")
    name = f"Enumerated as {uuid.uuid4().hex[:8]}"
    ref = {
        **_ref(census["handle"]),
        "attribute_list": [{"_class": "Attribute", "type": name, "value": "line 12"}],
    }
    person = await _person(client, event_ref_list=[ref])
    stored = await client.get_object("person", person["handle"])
    kept = stored["event_ref_list"][0]["attribute_list"][0]["type"]
    assert (kept if isinstance(kept, str) else kept.get("string")) == name
    assert name not in json.dumps(await client.types())


async def test_29_grampsql_finds_a_custom_name_on_any_event_reference(live):
    """A type is an object to GrampsQL: a custom name in ``string``, a standard
    one with ``string`` empty; ``=`` ignores case."""
    client = live.client
    census = await _event(client, "Census", "1880")
    name = f"Enumerated as {uuid.uuid4().hex[:8]}"
    ref = {
        **_ref(census["handle"]),
        "attribute_list": [
            {"_class": "Attribute", "type": name, "value": "line 12"},
            {"_class": "Attribute", "type": "Age", "value": "34"},
        ],
    }
    person = await _person(client, event_ref_list=[ref])

    async def found(query: str) -> list[str]:
        rows = await client.list_objects("person", gql=query, keys="handle")
        return [r["handle"] for r in rows]

    path = "event_ref_list.any.attribute_list.any.type"
    assert await found(f'{path}.string = "{name}"') == [person["handle"]]
    assert await found(f'{path}.string = "{name.upper()}"') == [person["handle"]]
    assert await found(f'{path} = "{name}"') == []
    assert await found(f'{path}.string = "Age"') == []


# --------------------------------------------------------------------------- #
# 30: a record's history on 3.21 is in the whole log
# --------------------------------------------------------------------------- #
async def test_30_the_log_names_each_changes_record_and_pages_by_id(live):
    client = live.client
    person = await _person(client)
    stored = await client.get_object("person", person["handle"])
    stored["gender"] = 1
    await client.update_object("person", person["handle"], stored)
    edit, create = await client.transactions(page=1, pagesize=2, sort="-id")
    assert (edit["description"], create["description"]) == ("Edit Person", "New Person")
    assert [(c["obj_class"], c["obj_handle"], c["trans_type"]) for c in edit["changes"]] == [
        ("Person", person["handle"], 1)
    ]
    assert "old_data" not in edit["changes"][0]
    (older,) = await client.transactions(page=1, pagesize=1, sort="-id", before_id=edit["id"])
    assert older["id"] == create["id"]
    (change,) = (await client.transaction(edit["id"], old=True, new=True))["changes"]
    assert (change["old_data"]["gender"], change["new_data"]["gender"]) == (0, 1)


# --------------------------------------------------------------------------- #
# 31: Gramps Web's OCR and thumbnails
# --------------------------------------------------------------------------- #
def _drawn(fmt: str) -> bytes:
    """A blank page with a line of print, as PNG or as a scanner's one-image PDF."""
    import io

    from PIL import Image, ImageDraw

    img = Image.new("RGB", (600, 300), "white")
    ImageDraw.Draw(img).text((20, 20), "INVENTORY OF THE ESTATE", fill="black")
    buf = io.BytesIO()
    img.save(buf, format=fmt)
    return buf.getvalue()


async def _upload(client, content: bytes, mime: str, name: str) -> str:
    return (await client.create_media(content, mime))["handle"]


async def test_31_ocr_needs_a_language_and_answers_a_pdf_with_nothing(live):
    client = live.client
    pdf = await _upload(client, _drawn("PDF"), "application/pdf", "scan.pdf")
    with pytest.raises(GrampsApiError) as exc:
        await client._request("POST", f"/api/media/{pdf}/ocr")
    assert exc.value.status == 422
    assert await client.ocr_media(pdf, lang="eng") == {}


async def test_31_ocr_reads_an_image_with_tesseract_and_is_501_without(live):
    """The metadata says which: ``server.ocr`` is whether Tesseract runs."""
    client = live.client
    png = await _upload(client, _drawn("PNG"), "image/png", "page.png")
    server = (await client.metadata()).get("server") or {}
    assert isinstance(server.get("ocr"), bool)
    if server["ocr"]:
        text = await client.ocr_media(png, lang="eng")
        assert isinstance(text, str) and "ESTATE" in text.upper()
    else:
        assert server.get("ocr_languages") == []
        with pytest.raises(GrampsApiError) as exc:
            await client.ocr_media(png, lang="eng")
        assert exc.value.status == 501


async def test_31_a_thumbnail_is_avif_whatever_the_file(live):
    client = live.client
    png = await _upload(client, _drawn("PNG"), "image/png", "page.png")
    data = await client.media_thumbnail(png, 200)
    assert data[4:12] == b"ftypavif"


async def test_32_a_media_post_stores_its_body_as_the_file(live):
    """A Media object sent as JSON is not read as one: it becomes a .json file."""
    client = live.client
    sent = new_handle()
    media = await client.create_object(
        "media", {"_class": "Media", "handle": sent, "path": "page.png", "mime": "image/png"}
    )
    stored = await client.get_object("media", media["handle"])
    assert media["handle"] != sent
    assert stored["mime"] == "application/json"
    assert stored["path"].endswith(".json")
    assert stored["desc"] == ""


async def test_32_a_file_posted_is_stored_with_its_own_checksum_and_mime(live):
    import hashlib

    client = live.client
    page = _drawn("PNG")
    handle = await _upload(client, page, "image/png", "page.png")
    stored = await client.get_object("media", handle)
    assert stored["checksum"] == hashlib.md5(page).hexdigest()  # noqa: S324
    assert stored["mime"] == "image/png"
    assert stored["path"] == f"{stored['checksum']}.png"
    with pytest.raises(GrampsApiError) as exc:
        await client.upload_media_bytes(handle, page, "image/png")
    assert exc.value.status == 409
