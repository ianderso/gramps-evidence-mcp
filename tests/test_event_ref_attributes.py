"""A person's own part in a shared event: attributes on the reference (TOOL-REQUESTS #7).

The tree's convention puts each participant's line from a census sheet on an
``As enumerated`` attribute of their reference to the household's one census
event. add_event_ref took no attributes, and nothing could change one once
written, so sessions wrote whole people by REST to do it.
"""

from __future__ import annotations

CENSUS = {
    "type": "Census",
    "date": "1880",
    "citation": {"source_title": "1880 census, Brannock County", "page": "ED 12, p. 4"},
}


async def _household(tools):
    head = await tools("add_person", given="Amos", surname="Turnbull")
    added = await tools("add_event_to_person", person=head["gramps_id"], event=CENSUS)
    wife = await tools("add_person", given="Bertha", surname="Turnbull")
    return head, wife, added["event_handle"]


def _refs(tools, person, event) -> list[dict]:
    stored = tools.fake.store["person"][person["handle"]]
    return [r for r in stored.get("event_ref_list") or [] if r["ref"] == event]


def _attributes(ref: dict) -> list[tuple[str, str]]:
    return [(a["type"], a["value"]) for a in ref.get("attribute_list") or []]


async def test_a_reference_carries_the_persons_own_line(tools):
    _, wife, census = await _household(tools)
    out = await tools(
        "add_event_ref",
        person=wife["gramps_id"],
        event=census,
        attributes={"As enumerated": "Turnbull, Bertha, W, F, 34, wife", "age": "34"},
    )
    assert out["changed"] is True, out
    assert out["attributes"] == {"As enumerated": "Turnbull, Bertha, W, F, 34, wife", "Age": "34"}
    (ref,) = _refs(tools, wife, census)
    assert _attributes(ref) == [
        ("As enumerated", "Turnbull, Bertha, W, F, 34, wife"),
        ("Age", "34"),
    ]
    assert "with As enumerated, Age" in out["message"]
    assert tools.fake.partial_writes == []


async def test_a_name_no_list_knows_is_used_and_flagged(tools):
    """Gramps keeps no list of the names on event references (PITFALLS 29)."""
    _, wife, census = await _household(tools)
    out = await tools(
        "add_event_ref", person=wife["gramps_id"], event=census, attributes={"As enumerated": "x"}
    )
    assert out["new_attribute_names"] == ["As enumerated"]
    assert "check the spelling" in out["message"]
    types = await tools.service.client.types()
    assert "As enumerated" not in str(types["custom"])


async def test_a_name_on_any_event_reference_in_the_tree_is_not_new(tools):
    """TOOL-REQUESTS #33: "used nowhere in the tree" was said of a name on many.

    The other references to the same event were the only ones looked at, so
    each household event's first reference was warned about a name the tree
    uses widely. A name no list knows is now looked for on every person's
    event references, and spelt as found there.
    """
    _, wife, census = await _household(tools)
    await tools(
        "add_event_ref", person=wife["gramps_id"], event=census, attributes={"As enumerated": "x"}
    )
    other = await tools("add_person", given="Dora", surname="Pell")
    added = await tools("add_event_to_person", person=other["gramps_id"], event=CENSUS)
    out = await tools(
        "update_event_ref",
        person=other["gramps_id"],
        event=added["event_handle"],
        attributes={"as enumerated": "Pell, Dora, W, F, 51"},
    )
    assert "new_attribute_names" not in out, out
    assert "nowhere" not in out["message"]
    assert _attributes(_refs(tools, other, added["event_handle"])[0]) == [
        ("As enumerated", "Pell, Dora, W, F, 51")
    ]

    out = await tools(
        "update_event_ref",
        person=other["gramps_id"],
        event=added["event_handle"],
        attributes={"Enumerator's note": "illegible"},
    )
    assert out["new_attribute_names"] == ["Enumerator's note"], out
    assert "no event reference in the tree" in out["message"]


async def test_a_name_on_another_reference_to_the_event_is_spelt_as_there(tools):
    head, wife, census = await _household(tools)
    son = await tools("add_person", given="Cyrus", surname="Turnbull")
    await tools(
        "add_event_ref", person=wife["gramps_id"], event=census, attributes={"As enumerated": "x"}
    )
    out = await tools(
        "add_event_ref", person=son["gramps_id"], event=census, attributes={"as Enumerated": "y"}
    )
    assert "new_attribute_names" not in out, out
    assert _attributes(_refs(tools, son, census)[0]) == [("As enumerated", "y")]


async def test_a_near_miss_of_a_known_name_is_refused_unless_meant(tools):
    head, wife, census = await _household(tools)
    son = await tools("add_person", given="Cyrus", surname="Turnbull")
    await tools(
        "add_event_ref", person=wife["gramps_id"], event=census, attributes={"As enumerated": "x"}
    )
    out = await tools(
        "add_event_ref", person=son["gramps_id"], event=census, attributes={"As enumerted": "y"}
    )
    assert out["error"] == "unknown_type"
    assert "Did you mean 'As enumerated'" in out["message"]
    assert _refs(tools, son, census) == []
    meant = await tools(
        "add_event_ref",
        person=son["gramps_id"],
        event=census,
        attributes={"As enumerted": "y"},
        allow_new_type=True,
    )
    assert meant["changed"] is True


async def test_already_referenced_points_at_update_event_ref(tools):
    head, _, census = await _household(tools)
    out = await tools("add_event_ref", person=head["gramps_id"], event=census)
    assert out["error"] == "already_referenced"
    assert "update_event_ref" in out["message"]


# --------------------------------------------------------------------------- #
# update_event_ref
# --------------------------------------------------------------------------- #
async def test_an_attribute_is_set_in_place_keeping_its_evidence(tools):
    _, wife, census = await _household(tools)
    await tools(
        "add_event_ref",
        person=wife["gramps_id"],
        event=census,
        attributes={"As enumerated": "Turnbull, Bertha, 43", "Age": "43"},
    )
    stored = tools.fake.store["person"][wife["handle"]]
    stored["event_ref_list"][-1]["attribute_list"][0]["citation_list"] = ["c1"]

    out = await tools(
        "update_event_ref",
        person=wife["gramps_id"],
        event=census,
        attributes={
            "As enumerated": "Turnbull, Bertha, 34",
            "Age": None,
            "Occupation": "keeping house",
        },
    )

    assert out["changed"] is True, out
    (ref,) = _refs(tools, wife, census)
    assert _attributes(ref) == [
        ("As enumerated", "Turnbull, Bertha, 34"),
        ("Occupation", "keeping house"),
    ]
    assert ref["attribute_list"][0]["citation_list"] == ["c1"]
    assert "As enumerated set" in out["message"] and "Age removed" in out["message"]
    assert "Occupation added" in out["message"]
    assert tools.fake.partial_writes == []


async def test_a_second_attribute_of_the_name_is_removed(tools):
    _, wife, census = await _household(tools)
    await tools("add_event_ref", person=wife["gramps_id"], event=census)
    stored = tools.fake.store["person"][wife["handle"]]
    stored["event_ref_list"][-1]["attribute_list"] = [
        {"type": "Age", "value": "34", "private": False, "citation_list": [], "note_list": []},
        {"type": "Age", "value": "34", "private": False, "citation_list": [], "note_list": []},
    ]
    out = await tools(
        "update_event_ref", person=wife["gramps_id"], event=census, attributes={"Age": "34"}
    )
    assert _attributes(_refs(tools, wife, census)[0]) == [("Age", "34")]
    assert "1 more Age removed" in out["message"]


async def test_the_role_changes_and_an_unchanged_reference_is_not_written(tools):
    _, wife, census = await _household(tools)
    await tools("add_event_ref", person=wife["gramps_id"], event=census)
    out = await tools("update_event_ref", person=wife["gramps_id"], event=census, role="informant")
    assert "role Primary -> Informant" in out["message"]
    again = await tools(
        "update_event_ref", person=wife["gramps_id"], event=census, role="Informant"
    )
    assert again["changed"] is False


async def test_update_event_ref_refuses_what_it_cannot_do(tools):
    head, wife, census = await _household(tools)
    nothing = await tools("update_event_ref", person=head["gramps_id"], event=census)
    assert nothing["error"] == "nothing_to_do"
    missing = await tools(
        "update_event_ref", person=wife["gramps_id"], event=census, role="Witness"
    )
    assert missing["error"] == "not_referenced" and "add_event_ref" in missing["message"]
    stored = tools.fake.store["person"][head["handle"]]
    stored["event_ref_list"].append(dict(stored["event_ref_list"][0]))
    twice = await tools("update_event_ref", person=head["gramps_id"], event=census, role="Witness")
    assert twice["error"] == "ambiguous_reference"
    assert [r["role"] for r in _refs(tools, head, census)] == ["Primary", "Primary"]
