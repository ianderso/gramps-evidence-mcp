"""The tools, called as a client calls them, against a real server.

The unit tests prove each tool against the fake; these prove the same paths
against gramps-webapi, which is where the 1.1.0 defects actually lived: links
the server duplicates, keys it keeps, dates it validates. Each test builds
what it needs in an empty tree and the tree is wiped after it.

Ported from the smoke test run against a real tree before 1.1.0 shipped; the
behaviour it only reported there ("reproduced" or "not reproduced") is
asserted here, because on a throwaway tree it can be set up deliberately.
"""

from __future__ import annotations

import uuid
from typing import Any

from .harness import live_version


def _t(value: Any) -> str:
    """A Gramps type as read back: a string, or a dict with string/value."""
    if isinstance(value, dict):
        return value.get("string") or str(value.get("value", ""))
    return "" if value is None else str(value)


async def _raw(live, object_type: str, ref: str) -> dict:
    return await live("get_object", object_type=object_type, ref=ref)


async def _people(live, *given: str) -> list[dict]:
    return [await live("add_person", given=g, surname="Wren") for g in given]


# --------------------------------------------------------------------------- #
# Session plumbing
# --------------------------------------------------------------------------- #
async def test_export_backup_writes_a_backup(live, tmp_path):
    await _people(live, "Ada")
    backup = await live("export_backup", dest_path=str(tmp_path / "backup.gramps"))
    assert "error" not in backup, backup
    assert backup["bytes"] > 0
    assert (tmp_path / "backup.gramps").stat().st_size == backup["bytes"]


async def test_the_server_is_supported_and_stores_open_spans(live):
    await live.client.require_supported_server()
    assert await live.service._open_spans_supported()
    assert await live.service._canonical_type("event_types", "census") == "Census"


# --------------------------------------------------------------------------- #
# Places
# --------------------------------------------------------------------------- #
async def test_add_place_writes_place_type_and_a_merge_keeps_the_finer_parent(live):
    state = await live("add_place", name="Ohio", place_type="State")
    county = await live(
        "add_place", name="Brannock", place_type="County", parent=state["gramps_id"]
    )
    town_a = await live(
        "add_place", name="Cedar Flat", place_type="City", parent=county["gramps_id"]
    )
    town_b = await live(
        "add_place", name="Cedar Flat", place_type="City", parent=state["gramps_id"]
    )
    stored = await _raw(live, "place", county["gramps_id"])
    assert _t(stored.get("place_type")) == "County"
    assert "type" not in stored

    plan = await live(
        "merge_objects", object_type="place", keep=town_a["gramps_id"], drop=town_b["gramps_id"]
    )
    assert plan["enclosures"]["result"] == [county["gramps_id"]], plan
    merged = await live(
        "merge_objects",
        object_type="place",
        keep=town_a["gramps_id"],
        drop=town_b["gramps_id"],
        dry_run=False,
    )
    assert "error" not in merged, merged
    parents = [r["ref"] for r in (await _raw(live, "place", town_a["gramps_id"]))["placeref_list"]]
    assert parents == [county["handle"]], "the server unions; the tool prunes (PITFALLS 21)"


async def test_a_stray_type_key_is_reported_and_cleaned_by_an_empty_update(live):
    place = await live.client.create_object(
        "place", {"_class": "Place", "name": {"value": "Brannock"}, "type": "County"}
    )
    shown = await live("get_place", place=place["gramps_id"])
    assert shown["stray_type_key"] == "County"
    out = await live("update_place", place=place["gramps_id"])
    assert out.get("repaired"), out
    stored = await _raw(live, "place", place["gramps_id"])
    assert _t(stored["place_type"]) == "County"
    assert "type" not in stored


# --------------------------------------------------------------------------- #
# Families and their links
# --------------------------------------------------------------------------- #
async def test_update_child_ref_sets_the_relationship_in_place(live):
    father, one, two = await _people(live, "Elias", "Mercy", "Hope")
    family = await live(
        "add_family", father=father["gramps_id"], children=[one["gramps_id"], two["gramps_id"]]
    )
    out = await live(
        "update_child_ref", family=family["gramps_id"], child=two["gramps_id"], frel="stepchild"
    )
    assert "error" not in out, out
    refs = (await _raw(live, "family", family["gramps_id"]))["child_ref_list"]
    assert [r["ref"] for r in refs] == [one["handle"], two["handle"]]
    assert (_t(refs[1]["frel"]), _t(refs[1]["mrel"])) == ("Stepchild", "Birth")


async def test_a_duplicated_family_link_is_found_and_repaired(live):
    """The server appends a new father's family without checking (PITFALLS 15)."""
    (spouse,) = await _people(live, "Elias")
    family = await live("add_family")
    client = live.client
    person = await client.get_object("person", spouse["handle"])
    person["family_list"] = [family["handle"]]
    await client.update_object("person", spouse["handle"], person)
    fam = await client.get_object("family", family["handle"])
    fam["father_handle"] = spouse["handle"]
    await client.update_object("family", family["handle"], fam)
    stored = await _raw(live, "person", spouse["gramps_id"])
    assert stored["family_list"] == [family["handle"], family["handle"]]

    found = await live("check_family_links", include_private=True)
    assert found["by_kind"] == {"duplicate_family_list": 1}, found

    repaired = await live("update_person", person=spouse["gramps_id"])
    assert repaired.get("repaired"), repaired
    stored = await _raw(live, "person", spouse["gramps_id"])
    assert stored["family_list"] == [family["handle"]]
    assert (await live("check_family_links", include_private=True))["problem_count"] == 0


async def test_detaching_a_doubly_linked_child_leaves_no_link(live):
    father, child = await _people(live, "Elias", "Mercy")
    family = await live("add_family", father=father["gramps_id"], children=[child["gramps_id"]])
    stored = await live.client.get_object("person", child["handle"])
    stored["parent_family_list"] = [family["handle"], family["handle"]]
    await live.client.update_object("person", child["handle"], stored)
    out = await live(
        "detach_object",
        parent_type="family",
        parent=family["gramps_id"],
        child_kind="child",
        child=child["gramps_id"],
    )
    assert "error" not in out, out
    left = (await _raw(live, "person", child["gramps_id"]))["parent_family_list"]
    assert family["handle"] not in left


# --------------------------------------------------------------------------- #
# Events
# --------------------------------------------------------------------------- #
async def _residence(live, person: dict, **extra: Any) -> dict:
    added = await live(
        "add_event_to_person",
        person=person["gramps_id"],
        event={
            "type": "Residence",
            "date": "1900",
            "citation": {"source_title": "Census 1900", "page": "sheet 4", "note": "finding"},
            **extra,
        },
    )
    assert "error" not in added, added
    return added


async def test_event_edits_type_dates_and_place(live):
    town = await live("add_place", name="Cedar Flat", place_type="City")
    (person,) = await _people(live, "Mercy")
    handle = (await _residence(live, person, place="Cedar Flat"))["event_handle"]
    assert (await _raw(live, "event", handle))["place"] == town["handle"]

    gramps_id = (await _raw(live, "event", handle))["gramps_id"]
    await live("update_event", event=handle, event_type="census")
    after = await _raw(live, "event", handle)
    assert (_t(after["type"]), after["gramps_id"]) == ("Census", gramps_id)
    refused = await live("update_event", event=handle, event_type="Censsus")
    assert refused["error"] == "unknown_type"

    await live("update_event", event=handle, date="from 4 May 1864 to 16 Sep 1864")
    assert (await _raw(live, "event", handle))["date"]["modifier"] == 5
    assert (await live("get_event", event=handle))["date"] == "from 1864-05-04 to 1864-09-16"
    await live("update_event", event=handle, date="from 1880")
    stored = (await _raw(live, "event", handle))["date"]
    assert (stored["modifier"], stored["dateval"][2]) == (7, 1880)
    await live("update_event", event=handle, date="between 1882 and 1883")
    assert (await live("get_event", event=handle))["date"] == "between 1882 and 1883"

    assert (await live("update_event", event=handle, place=""))["error"] == "empty_value"
    await live("update_event", event=handle, clear_place=True)
    assert not (await _raw(live, "event", handle)).get("place")


async def test_add_event_ref_shares_an_event_once(live):
    witness, person = await _people(live, "Elias", "Mercy")
    handle = (await _residence(live, person))["event_handle"]
    shared = await live("add_event_ref", person=witness["gramps_id"], event=handle, role="witness")
    assert shared["role"] == "Witness", shared
    backlinks = await live("get_backlinks", object_type="event", ref=handle)
    assert backlinks["referenced_by"]["person"]["count"] == 2
    again = await live("add_event_ref", person=witness["gramps_id"], event=handle)
    assert again["error"] == "already_referenced"


# --------------------------------------------------------------------------- #
# Names
# --------------------------------------------------------------------------- #
async def test_alternate_names_cited_retyped_uncited_and_removed(live):
    (person,) = await _people(live, "Hope")
    pid = person["gramps_id"]
    source = await live("add_source", title="Register of Marriages")
    alt = await live(
        "add_alternate_name",
        person=pid,
        given="Hope",
        surname="Ashbee",
        citation={"source": source["handle"], "page": "p. 2"},
    )
    names = (await _raw(live, "person", pid))["alternate_names"]
    assert alt["citation_handle"] in names[-1]["citation_list"]

    cited = await live(
        "cite_object",
        object_type="name",
        ref=pid,
        name={"primary": True},
        citation={"source": source["handle"], "page": "p. 3"},
    )
    assert cited["verified"] is True, cited

    await live(
        "update_alternate_name", person=pid, match={"surname": "Ashbee"}, name_type="married name"
    )
    names = (await _raw(live, "person", pid))["alternate_names"]
    assert _t(names[-1]["type"]) == "Married Name"
    assert alt["citation_handle"] in names[-1]["citation_list"]

    refused = await live(
        "update_alternate_name", person=pid, match={"surname": "Ashbee"}, remove=True
    )
    assert refused["error"] == "name_is_cited"
    uncited = await live(
        "uncite",
        object_type="name",
        ref=pid,
        name={"surname": "Ashbee"},
        citation=alt["citation_handle"],
    )
    assert uncited["changed"] is True, uncited
    removed = await live(
        "update_alternate_name", person=pid, match={"surname": "Ashbee"}, remove=True
    )
    assert removed["changed"] is True, removed
    assert not (await _raw(live, "person", pid)).get("alternate_names")


async def test_a_split_fix_corrects_the_name_in_place_with_a_note(live):
    person = await live("add_person", given="Mercy Ann", surname="Wren")
    fixed = await live(
        "update_person",
        person=person["gramps_id"],
        name={"given": "Mercy", "surname": "Ann Wren"},
        keep_old_as_alternate=False,
        reason="the transcription split the name at the wrong word",
    )
    stored = await _raw(live, "person", person["gramps_id"])
    assert not stored.get("alternate_names")
    assert stored["primary_name"]["first_name"] == "Mercy"
    assert fixed["note_handle"] in stored["note_list"]


# --------------------------------------------------------------------------- #
# Citations, sources, repositories, tags
# --------------------------------------------------------------------------- #
async def test_a_citation_holding_a_note_is_kept_or_carried(live):
    (person,) = await _people(live, "Mercy")
    handle = (await _residence(live, person))["event_handle"]
    citation = (await _raw(live, "event", handle))["citation_list"][0]
    source = (await _raw(live, "citation", citation))["source_handle"]

    kept = await live("uncite", object_type="event", ref=handle, citation=citation)
    assert kept["citation_deleted"] is False and kept["would_orphan"], kept

    second = await live("add_citation", citation={"source": source, "page": "sheet 5"})
    await live("cite_event", event=handle, citation={"citation": citation})
    carried = await live(
        "uncite", object_type="event", ref=handle, citation=citation, carry_to=second["handle"]
    )
    assert carried["citation_deleted"] is True, carried
    assert (await _raw(live, "citation", second["handle"]))["note_list"]

    refused = await live("delete_object", object_type="source", target=source)
    assert refused["error"] == "source_has_citations"


async def test_batches_repositories_and_tags(live):
    source = await live("add_source", title="Register of Deeds")
    citation = await live("add_citation", citation={"source": source["handle"], "page": "p. 4"})
    batch = await live(
        "update_citations",
        items=[
            {"citation": citation["handle"], "page": "p. 4b", "expect_page_prefix": "p. 4"},
            {"citation": citation["handle"], "page": "x", "expect_page_prefix": "not the page"},
        ],
    )
    assert batch["outcomes"] == {"applied": 1, "drifted": 1}, batch

    repo = await live("add_repository", name="County Library", repository_type="Library")
    linked = await live(
        "link_repositories",
        items=[{"source": source["handle"], "repository": repo["handle"], "media_type": "Book"}],
    )
    assert linked["outcomes"] == {"linked": 1}, linked
    reporefs = (await _raw(live, "source", source["handle"]))["reporef_list"]
    assert [_t(r["media_type"]) for r in reporefs] == ["Book"]

    second = await live(
        "add_source",
        title="Deed Book 3",
        repository=repo["handle"],
        call_number="DB-3",
        media_type="Book",
    )
    reporefs = (await _raw(live, "source", second["handle"]))["reporef_list"]
    assert _t(reporefs[0]["media_type"]) == "Book"
    narrow = await live(
        "detach_object",
        parent_type="source",
        parent=second["handle"],
        child_kind="repository",
        child=repo["handle"],
        call_number="DB-3",
    )
    assert narrow["changed"] is True, narrow

    (person,) = await _people(live, "Elias")
    await live("tag_object", object_type="person", target=person["gramps_id"], tag="To verify")
    untagged = await live(
        "detach_object",
        parent_type="person",
        parent=person["gramps_id"],
        child_kind="tag",
        child="To verify",
    )
    assert untagged["changed"] is True, untagged


# --------------------------------------------------------------------------- #
# DNA: what the server computes, which the fake is handed instead
# --------------------------------------------------------------------------- #
async def test_a_dna_match_with_a_parent_is_placed_by_the_server(live):
    """The relationship and common ancestors are Gramps' relationship calculator.

    The unit tests in ``tests/test_dna.py`` give the fake canned answers in
    the server's shape; this checks that shape, and what the tool makes of it.
    """
    father = await live("add_person", given="Elias", surname="Wren", gender="male")
    mother = await live("add_person", given="Hannah", surname="Ashbee", gender="female")
    child = await live("add_person", given="Mercy", surname="Wren", gender="female")
    await live(
        "add_family",
        father=father["gramps_id"],
        mother=mother["gramps_id"],
        children=[child["gramps_id"]],
    )
    out = await live(
        "add_dna_match",
        person=child["gramps_id"],
        match=mother["gramps_id"],
        segments="1,1000000,5000000,7.5,1200",
        citation={"source_title": "DNA test, kit A1", "page": "match list"},
    )
    assert out["changed"] is True, out

    (raw,) = await live.client.dna_matches(child["handle"])
    assert set(raw) == {
        "handle",
        "segments",
        "relation",
        "ancestor_handles",
        "ancestor_profiles",
        "person_ref_idx",
        "note_handles",
    }, sorted(raw)
    assert raw["ancestor_handles"] == [mother["handle"]]

    (match,) = (await live("get_dna_matches", person=child["gramps_id"]))["matches"]
    assert match["estimated_relationship"] == "mother"
    assert match["segments"][0]["side"] == "maternal"
    assert [a["handle"] for a in match["common_ancestors"]] == [mother["handle"]]


# --------------------------------------------------------------------------- #
# Vital events and event types
# --------------------------------------------------------------------------- #
async def test_vital_events_are_typed_and_indexed_as_the_server_keeps_them(live):
    """The birth and death references survive the server's recomputation (PITFALLS 19)."""
    out = await live(
        "add_person",
        given="Elias",
        surname="Wren",
        birth={"date": "1801", "citation": {"source_title": "Register", "page": "p. 1"}},
        death={
            "type": "burial",
            "date": "1866",
            "citation": {"source_title": "Register", "page": "p. 2"},
        },
    )
    assert "error" not in out, out

    async def shape():
        person = await live.client.get_object("person", out["handle"])
        types = [
            (await live.client.get_object("event", r["ref"]))["type"]
            for r in person["event_ref_list"]
        ]
        return types, person["birth_ref_index"], person["death_ref_index"]

    assert await shape() == (["Birth", "Burial"], 0, -1)
    await live("update_person", person=out["gramps_id"], gender="male")
    assert await shape() == (["Birth", "Burial"], 0, -1), "unchanged by the server's recompute"


async def test_type_names_are_spelt_as_gramps_does_and_unknown_ones_refused(live):
    """PITFALLS 26: nothing reaches the server as a near-miss of a standard name."""
    cited = {"source_title": "Register", "page": "p. 3"}
    person = await live("add_person", given="Ada", surname="Quillfeather")

    async def event(given_type, **extra):
        out = await live(
            "add_event_to_person",
            person=person["gramps_id"],
            event={"type": given_type, "date": "1851", "citation": cited, **extra},
        )
        if "error" in out:
            return out
        return (await live.client.get_object("event", out["event_handle"]))["type"]

    assert await event("stillbirth") == "Stillbirth", "standard, though the tree never used it"
    assert await event("Born") == "Birth"
    custom = (await live.client.types())["custom"]["event_types"]
    assert "stillbirth" not in custom and "Born" not in custom

    held = len((await live.client.get_object("person", person["handle"]))["event_ref_list"])
    refused = await event("Censsus")
    assert refused["error"] == "unknown_type"
    assert "Did you mean 'Census'?" in refused["message"]
    after = (await live.client.get_object("person", person["handle"]))["event_ref_list"]
    assert len(after) == held

    new = f"Land Grant {uuid.uuid4().hex[:8]}"
    assert await event(new, allow_new_type=True) == new
    assert new in (await live.client.types())["custom"]["event_types"]
    assert await event(new.upper()) == new, "the tree's own now, matched like a standard one"

    family = await live("add_family", father=person["gramps_id"], relationship="civil union")
    assert (await live.client.get_object("family", family["handle"]))["type"] == "Civil Union"
    await live("add_url", object_type="person", target=person["gramps_id"], url="https://x.test")
    (url,) = (await live.client.get_object("person", person["handle"]))["urls"]
    assert url["type"] == "Web Home"
    assert "Web Home" not in (await live.client.types())["custom"]["url_types"]
    place = await live("add_place", name="Wexcombe", place_type="village")
    assert (await live.client.get_object("place", place["handle"]))["place_type"] == "Village"


# --------------------------------------------------------------------------- #
# A record's history (gramps-webapi 3.22 and later)
# --------------------------------------------------------------------------- #
async def test_record_history_or_the_version_it_needs(live):
    person = await live("add_person", given="Elias", surname="Wren")
    if live_version() < (3, 22):
        out = await live("get_record_history", object_type="person", ref=person["gramps_id"])
        assert out["error"] == "unsupported_server", out
        assert "3.22 or later" in out["message"]
        return
    await live("update_person", person=person["gramps_id"], gender="male")
    await live("add_family", father=person["gramps_id"])
    out = await live("get_record_history", object_type="person", ref=person["gramps_id"])
    assert [c["change"] for c in out["changes"]] == ["edited", "edited", "added"], out
    assert all(c["user"] == "mcp" for c in out["changes"])

    await live("delete_object", object_type="person", target=person["gramps_id"])
    gone = await live("get_record_history", object_type="person", ref=person["handle"])
    assert gone["deleted"] is True
    assert gone["changes"][0]["change"] == "deleted"
