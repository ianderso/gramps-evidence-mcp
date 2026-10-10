"""One attribute set or removed in place (TOOL-REQUESTS #32).

A research session moved a number out of an Occupation attribute -- "Custodian
(school) at death, 1963; Social Security Number = ..." -- into an attribute of
its own, and could not then trim the old one: ``add_attribute`` only appends,
``update_object_fields`` refuses ``attribute_list`` and ``detach_object`` has
no attribute kind. So the number was left in two places.
"""

from __future__ import annotations

OCCUPATION = "Custodian (school) at death, 1963; Social Security Number = 000-00-0000"


async def _person_with(tools, *attributes: tuple[str, str]) -> dict:
    person = await tools("add_person", given="Hester", surname="Crane")
    for name, value in attributes:
        out = await tools(
            "add_attribute",
            object_type="person",
            target=person["gramps_id"],
            name=name,
            value=value,
            allow_new_type=True,
        )
        assert "error" not in out, out
    return person


def _attributes(tools, person) -> list[tuple[str, str]]:
    stored = tools.fake.store["person"][person["handle"]]
    return [
        (a["type"] if isinstance(a["type"], str) else a["type"]["string"], a["value"])
        for a in stored.get("attribute_list") or []
    ]


async def _cite_first_attribute(tools, person) -> str:
    """Give the person's first attribute a citation, as the Gramps Web UI can."""
    citation = await tools("add_citation", citation={"source_title": "SSDI", "page": "entry"})
    stored = tools.fake.store["person"][person["handle"]]
    stored["attribute_list"][0]["citation_list"] = [citation["handle"]]
    return citation["gramps_id"]


async def test_a_value_is_set_in_place_keeping_its_citations(tools):
    person = await _person_with(
        tools, ("Occupation", OCCUPATION), ("Social Security Number", "000-00-0000")
    )
    cited = await _cite_first_attribute(tools, person)
    out = await tools(
        "update_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="occupation",
        value="Custodian (school) at death, 1963",
    )
    assert "error" not in out, out
    assert out["attribute"] == {
        "type": "Occupation",
        "value": "Custodian (school) at death, 1963",
        "citation_count": 1,
    }
    assert _attributes(tools, person) == [
        ("Occupation", "Custodian (school) at death, 1963"),
        ("Social Security Number", "000-00-0000"),
    ]
    stored = tools.fake.store["person"][person["handle"]]["attribute_list"][0]
    assert [tools.fake.store["citation"][h]["gramps_id"] for h in stored["citation_list"]] == [
        cited
    ]


async def test_a_removed_attribute_names_the_citations_it_leaves(tools):
    person = await _person_with(tools, ("Occupation", OCCUPATION), ("Nickname", "Hettie"))
    cited = await _cite_first_attribute(tools, person)
    out = await tools(
        "update_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="Occupation",
        remove=True,
    )
    assert out["citations_left"] == [cited], out
    assert cited in out["message"]
    assert _attributes(tools, person) == [("Nickname", "Hettie")]
    assert cited in {c["gramps_id"] for c in tools.fake.store["citation"].values()}


async def test_several_of_one_name_are_told_apart_by_match(tools):
    person = await _person_with(
        tools, ("Occupation", "Farmer, 1850"), ("Occupation", "Miller, 1860")
    )
    args = {"object_type": "person", "target": person["gramps_id"], "name": "Occupation"}
    out = await tools("update_attribute", value="Farmer", **args)
    assert out["error"] == "ambiguous", out
    assert "Farmer, 1850" in out["message"] and "Miller, 1860" in out["message"]
    assert _attributes(tools, person)[0] == ("Occupation", "Farmer, 1850")

    out = await tools("update_attribute", match="MILLER", value="Miller, 1860-1870", **args)
    assert "error" not in out, out
    assert _attributes(tools, person) == [
        ("Occupation", "Farmer, 1850"),
        ("Occupation", "Miller, 1860-1870"),
    ]


async def test_one_of_two_identical_attributes_is_removed(tools):
    """Copies alike in every field are interchangeable, so either one will do."""
    person = await _person_with(tools, ("Occupation", "Farmer"), ("Occupation", "Farmer"))
    out = await tools(
        "update_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="Occupation",
        remove=True,
    )
    assert "error" not in out, out
    assert _attributes(tools, person) == [("Occupation", "Farmer")]


async def test_a_missing_attribute_lists_what_is_there(tools):
    person = await _person_with(tools, ("Nickname", "Hettie"))
    out = await tools(
        "update_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="Occupation",
        value="Farmer",
    )
    assert out["error"] == "not_found"
    assert "Nickname: Hettie" in out["message"]
    assert _attributes(tools, person) == [("Nickname", "Hettie")]


async def test_a_source_attribute_is_set_in_place(tools):
    source = await tools("add_source", title="Pension File W.12345")
    await tools(
        "add_attribute",
        object_type="source",
        target=source["gramps_id"],
        name="Bears-On",
        value="I0035",
        allow_new_type=True,
    )
    out = await tools(
        "update_attribute",
        object_type="source",
        target=source["gramps_id"],
        name="Bears-On",
        value="I0035, I0010",
    )
    assert "error" not in out, out
    [attr] = tools.fake.store["source"][source["handle"]]["attribute_list"]
    assert attr["value"] == "I0035, I0010"


async def test_an_unchanged_value_writes_nothing(tools):
    person = await _person_with(tools, ("Nickname", "Hettie"))
    puts = len([r for r in tools.fake.requests if r[0] == "PUT"])
    out = await tools(
        "update_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="Nickname",
        value="Hettie",
    )
    assert out["changed"] is False, out
    assert len([r for r in tools.fake.requests if r[0] == "PUT"]) == puts


async def test_value_or_remove_and_an_object_that_has_attributes(tools):
    person = await _person_with(tools, ("Nickname", "Hettie"))
    args = {"object_type": "person", "target": person["gramps_id"], "name": "Nickname"}
    assert (await tools("update_attribute", **args))["error"] == "nothing_to_do"
    both = await tools("update_attribute", value="Hetty", remove=True, **args)
    assert both["error"] == "nothing_to_do"
    place = await tools("add_place", name="Cedar Flat", place_type="City")
    out = await tools(
        "update_attribute", object_type="place", target=place["gramps_id"], name="X", value="Y"
    )
    assert out["error"] == "unsupported"


async def test_get_person_shows_the_attributes(tools):
    """TOOL-REQUESTS #35: a person's attributes were seen only through get_object."""
    person = await _person_with(tools, ("Occupation", OCCUPATION), ("Nickname", "Hettie"))
    await _cite_first_attribute(tools, person)
    out = await tools("get_person", person=person["gramps_id"])
    assert out["attributes"] == [
        {"type": "Occupation", "value": OCCUPATION, "citation_count": 1, "private": False},
        {"type": "Nickname", "value": "Hettie", "citation_count": 0, "private": False},
    ]
