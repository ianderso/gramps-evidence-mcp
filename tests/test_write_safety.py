"""The whole-object write guarantee, enforced across the tool surface.

``PUT`` replaces a record with exactly what is sent, so a payload assembled
from a partial fetch destroys every field it omitted -- the defect recorded in
``docs/PITFALLS.md`` section 1, which cost parent links on 65 families.

``service._mutate`` exists to make that impossible. These tests prove the
property holds for every editing tool rather than trusting the convention:
the fake records any PUT that drops a field, and the sweep below asserts the
record stays empty.
"""

from __future__ import annotations

import pytest


async def _seed(tools) -> dict:
    """Build a small tree with the structure the edit tools operate on."""
    father = await tools(
        "add_person",
        given="Elias",
        surname="Ashbee",
        death={
            "type": "Death",
            "date": "1806",
            "citation": {"source_title": "Probate", "page": "p. 4"},
        },
    )
    child = await tools(
        "add_person",
        given="Mercy",
        surname="Ashbee",
        death={
            "type": "Death",
            "date": "1851",
            "citation": {"source_title": "Surrogate", "page": "img 51"},
        },
    )
    family = await tools(
        "add_family",
        father=father["gramps_id"],
        children=[child["gramps_id"]],
        marriage={
            "type": "Marriage",
            "date": "1784",
            "citation": {"source_title": "Register", "page": "img 7"},
        },
    )
    source = await tools("add_source", title="Pension File W.12345")
    return {
        "father": father["gramps_id"],
        "child": child["gramps_id"],
        "family": family["gramps_id"],
        "source": source["gramps_id"],
    }


async def test_no_editing_tool_ever_sends_a_partial_object(tools):
    """Drive every edit path, then assert nothing dropped a field.

    This is the structural guard. A new tool that builds its payload from a
    ``keys=`` fetch instead of going through ``_mutate`` fails here, not in
    production six months later.
    """
    ids = await _seed(tools)
    fake = tools.fake

    await tools("update_person", person=ids["child"], gender="female")
    await tools(
        "add_alternate_name",
        person=ids["child"],
        given="Mercie",
        surname="Ashbey",
        name_type="Also Known As",
    )
    await tools(
        "add_event_to_person",
        person=ids["child"],
        event={
            "type": "Residence",
            "date": "1800",
            "citation": {"source_title": "Census", "page": "p. 2"},
        },
    )
    await tools("set_private", object_type="person", target=ids["child"], private=True)
    await tools("set_private", object_type="person", target=ids["child"], private=False)
    await tools("tag_object", object_type="person", target=ids["child"], tag="Reviewed")
    await tools(
        "add_note", target_type="person", target=ids["child"], text="Checked against the register."
    )
    await tools(
        "add_attribute",
        object_type="person",
        target=ids["child"],
        name="Occupation",
        value="Farmer",
    )
    await tools(
        "add_url",
        object_type="person",
        target=ids["child"],
        url="https://example.org/mercy",
        description="Find a Grave",
    )
    await tools(
        "update_url",
        object_type="person",
        target=ids["child"],
        match="example.org",
        url_type="Web Search",
    )
    await tools(
        "cite_object",
        object_type="family",
        ref=ids["family"],
        citation={"source": ids["source"], "page": "img 51"},
    )
    await tools(
        "cite_child_link",
        family=ids["family"],
        child=ids["child"],
        citation={"source": ids["source"], "page": "img 51"},
    )
    await tools("update_source", source=ids["source"], author="US Govt")
    await tools(
        "add_child_to_family",
        family=ids["family"],
        child=ids["father"],
        frel="Adopted",
        mrel="Unknown",
    )
    out = await tools(
        "add_dna_match",
        person=ids["child"],
        match=ids["father"],
        segments="1,1000000,5000000,7.5,1200",
        citation={"source_title": "DNA test, kit A1", "page": "match list"},
    )
    assert out["changed"] is True, out

    assert fake.partial_writes == [], (
        f"a tool sent a partial object; every write must go through _mutate: {fake.partial_writes}"
    )


async def test_the_guard_itself_catches_a_partial_write(tools):
    """Prove the detector works, so a clean sweep above means something.

    Writes a deliberately truncated object straight through the client,
    bypassing ``_mutate`` the way a careless new tool would.
    """
    ids = await _seed(tools)
    fake = tools.fake
    service = tools.service

    full = await service._resolve("person", ids["child"])
    truncated = {
        "_class": "Person",
        "handle": full["handle"],
        "gramps_id": full["gramps_id"],
    }
    await service.client.update_object("person", full["handle"], truncated)

    assert fake.partial_writes, "the partial-write detector did not fire"
    dropped = fake.partial_writes[0][2]
    assert "event_ref_list" in dropped


async def test_update_object_fields_refuses_structural_lists_at_the_tool(tools):
    """The generic setter must not be a way around the dedicated tools."""
    ids = await _seed(tools)
    out = await tools(
        "update_object_fields",
        object_type="person",
        ref=ids["child"],
        fields={"event_ref_list": []},
    )
    assert out["error"] == "unsupported_fields"
    assert tools.fake.partial_writes == []


@pytest.mark.parametrize("private", [True, False])
async def test_privacy_toggle_preserves_the_rest_of_the_record(tools, private):
    """Setting one flag must not cost the events attached to the person."""
    ids = await _seed(tools)
    before = await tools("get_person", person=ids["child"])
    await tools("set_private", object_type="person", target=ids["child"], private=private)
    after = await tools("get_person", person=ids["child"])
    assert len(after["events"]) == len(before["events"])
    assert tools.fake.partial_writes == []
