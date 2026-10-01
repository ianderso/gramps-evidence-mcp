"""Citing, correcting and removing a person's names.

From a research session: no tool reached a Name's own citation list, so every
alternate name in the tree was uncited and name evidence was parked on the
person or on a birth event instead; nothing could retype or remove an
alternate name; and correcting a primary name split wrongly at entry
("Joan" / "M. Anderson") filed the wrong split as an invented variant.
"""

from __future__ import annotations

CITE = {"source_title": "Parish register, St. Brannock", "page": "f. 12, entry 40"}


async def _person(tools, given="Johanne", surname="Buttner"):
    return await tools("add_person", given=given, surname=surname)


def _stored(tools, person) -> dict:
    return tools.fake.store["person"][person["handle"]]


# --------------------------------------------------------------------------- #
# adding and citing
# --------------------------------------------------------------------------- #
async def test_an_alternate_name_can_be_added_with_its_citation(tools):
    person = await _person(tools)
    out = await tools(
        "add_alternate_name",
        person=person["gramps_id"],
        given="Hanne",
        surname="Bittner",
        name_type="Birth Name",
        citation=CITE,
    )
    assert out["citation_handle"]
    name = _stored(tools, person)["alternate_names"][0]
    assert name["citation_list"] == [out["citation_handle"]]
    # On the name, not the person.
    assert not _stored(tools, person).get("citation_list")
    shown = await tools("get_person", person=person["gramps_id"])
    assert shown["alternate_names"][0]["citation_count"] == 1
    assert shown["alternate_names"][0]["index"] == 0


async def test_an_existing_alternate_name_is_cited_on_the_name(tools):
    person = await _person(tools)
    await tools("add_alternate_name", person=person["gramps_id"], given="Hanne", surname="Bittner")
    out = await tools(
        "cite_object",
        object_type="name",
        ref=person["gramps_id"],
        name={"surname": "bittner"},
        citation=CITE,
    )
    assert out["verified"] is True
    assert "Bittner" in out["name"]
    assert _stored(tools, person)["alternate_names"][0]["citation_list"] == [out["citation_handle"]]


async def test_the_primary_name_can_be_cited(tools):
    person = await _person(tools)
    out = await tools(
        "cite_object",
        object_type="name",
        ref=person["gramps_id"],
        name={"primary": True},
        citation=CITE,
    )
    assert out["verified"] is True
    assert _stored(tools, person)["primary_name"]["citation_list"] == [out["citation_handle"]]


async def test_a_name_that_matches_nothing_leaves_no_citation_behind(tools):
    person = await _person(tools)
    out = await tools(
        "cite_object",
        object_type="name",
        ref=person["gramps_id"],
        name={"surname": "Nobody"},
        citation=CITE,
    )
    assert out["error"] == "not_found"
    assert "Johanne / Buttner" in out["message"]
    assert tools.fake.store["citation"] == {}


async def test_an_ambiguous_name_is_refused_with_the_listing(tools):
    person = await _person(tools)
    for kind in ("Also Known As", "Married Name"):
        await tools(
            "add_alternate_name",
            person=person["gramps_id"],
            given="Jean",
            surname="Maughlin",
            name_type=kind,
        )
    out = await tools(
        "cite_object",
        object_type="name",
        ref=person["gramps_id"],
        name={"given": "Jean"},
        citation=CITE,
    )
    assert out["error"] == "not_found"
    assert "2 names" in out["message"]
    narrowed = await tools(
        "cite_object",
        object_type="name",
        ref=person["gramps_id"],
        name={"given": "Jean", "type": "Married Name"},
        citation=CITE,
    )
    assert narrowed["verified"] is True


async def test_citing_a_name_needs_the_name(tools):
    person = await _person(tools)
    out = await tools("cite_object", object_type="name", ref=person["gramps_id"], citation=CITE)
    assert out["error"] == "name_required"


async def test_a_name_citation_can_be_uncited_and_cleaned_up(tools):
    person = await _person(tools)
    added = await tools(
        "add_alternate_name",
        person=person["gramps_id"],
        given="Hanne",
        surname="Bittner",
        citation=CITE,
    )
    out = await tools(
        "uncite",
        object_type="name",
        ref=person["gramps_id"],
        name={"surname": "Bittner"},
        citation=added["citation_handle"],
    )
    assert out["citation_deleted"] is True
    assert _stored(tools, person)["alternate_names"][0]["citation_list"] == []


# --------------------------------------------------------------------------- #
# correcting and removing alternate names
# --------------------------------------------------------------------------- #
async def test_a_married_name_filed_as_also_known_as_is_retyped_in_place(tools):
    person = await _person(tools, "Alice", "Hollis")
    await tools(
        "add_alternate_name",
        person=person["gramps_id"],
        given="Alice",
        surname="Ashby",
        citation=CITE,
    )
    citations = list(_stored(tools, person)["alternate_names"][0]["citation_list"])
    out = await tools(
        "update_alternate_name",
        person=person["gramps_id"],
        match={"surname": "Ashby", "type": "Also Known As"},
        name_type="married name",
    )
    assert out["changed"] is True
    name = _stored(tools, person)["alternate_names"][0]
    assert name["type"] == "Married Name"
    assert name["citation_list"] == citations
    assert tools.fake.partial_writes == []


async def test_one_of_two_identical_duplicates_is_removed(tools):
    person = await _person(tools, "Nora", "Pell")
    for _ in range(2):
        await tools("add_alternate_name", person=person["gramps_id"], surname="Calloway")
    out = await tools(
        "update_alternate_name",
        person=person["gramps_id"],
        match={"surname": "Calloway"},
        remove=True,
    )
    assert out["changed"] is True
    names = _stored(tools, person)["alternate_names"]
    assert len(names) == 1
    assert names[0]["surname_list"][0]["surname"] == "Calloway"


async def test_a_cited_name_is_not_removed(tools):
    person = await _person(tools, "Nora", "Pell")
    await tools(
        "add_alternate_name",
        person=person["gramps_id"],
        surname="Calloway",
        citation=CITE,
    )
    out = await tools(
        "update_alternate_name",
        person=person["gramps_id"],
        match={"surname": "Calloway"},
        remove=True,
    )
    assert out["error"] == "name_is_cited"
    assert len(_stored(tools, person)["alternate_names"]) == 1


async def test_the_primary_name_is_not_an_alternate(tools):
    person = await _person(tools)
    out = await tools(
        "update_alternate_name",
        person=person["gramps_id"],
        match={"primary": True},
        surname="X",
    )
    assert out["error"] == "not_an_alternate"


async def test_update_alternate_name_needs_something_to_do(tools):
    person = await _person(tools)
    await tools("add_alternate_name", person=person["gramps_id"], surname="Bittner")
    out = await tools(
        "update_alternate_name", person=person["gramps_id"], match={"surname": "Bittner"}
    )
    assert out["error"] == "nothing_to_do"


async def test_an_unknown_name_type_is_refused(tools):
    person = await _person(tools)
    await tools("add_alternate_name", person=person["gramps_id"], surname="Bittner")
    out = await tools(
        "update_alternate_name",
        person=person["gramps_id"],
        match={"surname": "Bittner"},
        name_type="Maried Name",
    )
    assert out["error"] == "unknown_type"


# --------------------------------------------------------------------------- #
# the primary name
# --------------------------------------------------------------------------- #
async def test_a_wrong_split_is_corrected_in_place_with_a_recorded_reason(tools):
    person = await _person(tools, "Joan", "M. Ashdown")
    await tools(
        "cite_object",
        object_type="name",
        ref=person["gramps_id"],
        name={"primary": True},
        citation=CITE,
    )
    cited = list(_stored(tools, person)["primary_name"]["citation_list"])

    out = await tools(
        "update_person",
        person=person["gramps_id"],
        name={"given": "Joan M.", "surname": "Ashdown"},
        keep_old_as_alternate=False,
        reason="Middle initial entered in the surname; no record uses that form.",
    )

    stored = _stored(tools, person)
    assert not stored.get("alternate_names")
    assert stored["primary_name"]["first_name"] == "Joan M."
    assert stored["primary_name"]["surname_list"][0]["surname"] == "Ashdown"
    assert stored["primary_name"]["citation_list"] == cited
    note = tools.fake.store["note"][out["note_handle"]]
    assert "'Joan / M. Ashdown'" in note["text"]["string"]
    assert "'Joan M. / Ashdown'" in note["text"]["string"]
    assert "no record uses that form" in note["text"]["string"]
    assert out["note_handle"] in stored["note_list"]


async def test_dropping_the_old_name_needs_a_reason(tools):
    person = await _person(tools, "Joan", "M. Ashdown")
    out = await tools(
        "update_person",
        person=person["gramps_id"],
        name={"given": "Joan M.", "surname": "Ashdown"},
        keep_old_as_alternate=False,
    )
    assert out["error"] == "reason_required"
    assert _stored(tools, person)["primary_name"]["first_name"] == "Joan"


async def test_a_replaced_name_is_still_kept_by_default(tools):
    person = await _person(tools, "George", "Leroy")
    await tools(
        "update_person", person=person["gramps_id"], name={"given": "George", "surname": "Lee"}
    )
    names = _stored(tools, person)["alternate_names"]
    assert [n["surname_list"][0]["surname"] for n in names] == ["Leroy"]
    assert names[0]["type"] == "Also Known As"


async def test_restating_the_same_name_files_no_variant(tools):
    """The whole stored name never equals a fresh one, so this used to add one."""
    person = await _person(tools)
    out = await tools(
        "update_person", person=person["gramps_id"], name={"given": "Johanne", "surname": "Buttner"}
    )
    assert out["changed"] is False
    assert not _stored(tools, person).get("alternate_names")


async def test_a_split_fix_can_drop_a_surname_prefix(tools):
    """name_payload omits an empty prefix, so it must be cleared explicitly."""
    person = await tools("add_person", given="Jan", surname="Dyke", name_prefix="van")
    await tools(
        "update_person",
        person=person["gramps_id"],
        name={"given": "Jan", "surname": "Vandyke"},
        keep_old_as_alternate=False,
        reason="One word in the register.",
    )
    surname = _stored(tools, person)["primary_name"]["surname_list"][0]
    assert (surname["surname"], surname.get("prefix")) == ("Vandyke", "")


async def test_an_alternate_name_with_no_surnames_can_be_edited(tools):
    person = await _person(tools)
    _stored(tools, person)["alternate_names"] = [
        {"_class": "Name", "first_name": "Hanne", "surname_list": [], "type": "Also Known As"}
    ]
    out = await tools(
        "update_alternate_name",
        person=person["gramps_id"],
        match={"index": 0},
        surname="Bittner",
    )
    assert out["changed"] is True
    assert _stored(tools, person)["alternate_names"][0]["surname_list"][0]["surname"] == "Bittner"
