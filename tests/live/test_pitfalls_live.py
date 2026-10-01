"""Every server behaviour docs/PITFALLS.md asserts, checked against a real server.

Each test names its section. When one fails, either the server changed or the
document was wrong, and in both cases the document and anything relying on
it -- the service, the fake -- need correcting. Sections that describe this
project's own code rather than the server (4, 9, 10, 11) are covered by the
unit tests instead.
"""

from __future__ import annotations

import pytest

from gramps_evidence_mcp.client import GrampsApiError
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


async def test_6_a_stale_write_wins_silently_unless_if_match_is_sent(live):
    """No locking by default. The server honours If-Match, which no client here sends."""
    client = live.client
    person = await _person(client)
    path = f"/api/people/{person['handle']}"
    first = await client._request("GET", path)
    etag = first.headers.get("ETag")
    stale = first.json()

    fresh = await client.get_object("person", person["handle"])
    fresh["gender"] = 1
    await client.update_object("person", person["handle"], fresh)

    stale["private"] = True
    await client.update_object("person", person["handle"], stale)  # no If-Match: accepted
    after = await client.get_object("person", person["handle"])
    assert after["gender"] == 0, "the stale write silently undid the other one"

    assert etag, "the server sends an ETag on a read"
    resp = await client._http.put(
        path, json=stale, headers={**client._auth_headers(), "If-Match": etag}
    )
    assert resp.status_code == 412


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


async def test_13_comparing_a_list_is_a_server_error(live):
    client = live.client
    await _event(client)
    with pytest.raises(GrampsApiError) as exc:
        await client.structured_query(
            "event",
            {
                "select": ["handle"],
                "where": [{"column": {"json_path": ["citation_list"]}, "op": "eq", "value": []}],
            },
        )
    assert exc.value.status == 500


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


async def test_18_a_stray_type_key_is_kept_and_ignored(live):
    client = live.client
    place = await client.create_object(
        "place", {"_class": "Place", "name": {"value": "Brannock"}, "type": "County"}
    )
    stored = await client.get_object("place", place["handle"])
    assert "type" in stored
    assert stored["place_type"] == "Unknown"


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
async def test_24_a_served_year_written_back_is_kept_and_served_stale(live):
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
    assert (stale["dateval"][2], stale["year"]) == (1860, 1850)

    out = await live("update_event", event=event["handle"], description="Census")
    assert out["repaired"] == [
        "dropped 1 stale date year the server would have shown instead of the date's own"
    ], out
    fixed = (await client.get_object("event", event["handle"]))["date"]
    assert (fixed["dateval"][2], fixed["year"]) == (1860, 1860)


# --------------------------------------------------------------------------- #
# Vocabularies the edit tools validate against
# --------------------------------------------------------------------------- #
async def test_type_vocabularies_carry_the_keys_the_tools_read(live):
    types = await live.client.types()
    for key in ("event_types", "event_role_types", "child_reference_types", "name_types"):
        assert key in types["default"], key
        assert key in types["custom"], key
    assert "Stepchild" in types["default"]["child_reference_types"]
    assert "Custom" not in types["default"]["child_reference_types"]
