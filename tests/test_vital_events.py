"""Birth, death and marriage given with their person or family, and event types.

``add_person`` and ``add_family`` said a birth's type defaults to Birth, a
death's to Death and a marriage's to Marriage, but the schema required the
type, so a caller that believed the description was refused before the tool
ran. And a type was sent to the server as given, which matches names
case-sensitively: "birth" was stored as a new custom type beside Birth.
"""

from __future__ import annotations

from gramps_evidence_mcp import server

CITED = {"source_title": "Register of Births", "page": "p. 12"}


def _event_schema(tool, param: str) -> dict:
    schema = tool.input_schema
    ref = schema["properties"][param]["anyOf"][0]["$ref"].rsplit("/", 1)[-1]
    return schema["$defs"][ref]


async def test_the_schema_lets_a_vital_event_leave_its_type_out():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    for tool, param in (
        ("add_person", "birth"),
        ("add_person", "death"),
        ("add_family", "marriage"),
    ):
        assert "type" not in _event_schema(tools[tool], param).get("required", []), (tool, param)
    other = tools["add_event_to_person"].input_schema
    event = other["$defs"][other["properties"]["event"]["$ref"].rsplit("/", 1)[-1]]
    assert "type" in event["required"], "elsewhere the type is still required"


async def test_a_birth_and_death_without_a_type_are_a_birth_and_a_death(tools):
    out = await tools(
        "add_person",
        given="Elias",
        surname="Wren",
        birth={"date": "about 1801", "citation": CITED},
        death={"date": "1866", "citation": {"source_title": "Register of Deaths", "page": "p. 3"}},
    )
    assert "error" not in out, out
    person = tools.fake.store["person"][out["handle"]]
    events = [tools.fake.store["event"][r["ref"]] for r in person["event_ref_list"]]
    assert [e["type"] for e in events] == ["Birth", "Death"]
    assert (person["birth_ref_index"], person["death_ref_index"]) == (0, 1)


async def test_a_marriage_without_a_type_is_a_marriage(tools):
    out = await tools("add_family", marriage={"date": "1828", "citation": CITED})
    assert "error" not in out, out
    family = tools.fake.store["family"][out["handle"]]
    (ref,) = family["event_ref_list"]
    assert tools.fake.store["event"][ref["ref"]]["type"] == "Marriage"


async def test_a_birth_given_as_another_event_is_kept_but_not_made_the_birth(tools):
    """As the server would compute it on the person's first update (PITFALLS 19)."""
    out = await tools("add_person", given="Mercy", birth={"type": "Baptism", "citation": CITED})
    person = tools.fake.store["person"][out["handle"]]
    (ref,) = person["event_ref_list"]
    assert tools.fake.store["event"][ref["ref"]]["type"] == "Baptism"
    assert person["birth_ref_index"] == -1


async def test_an_event_type_is_spelt_as_the_tree_spells_it(tools):
    person = await tools("add_person", given="Mercy")
    out = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": "census", "date": "1850", "citation": CITED},
    )
    assert tools.fake.store["event"][out["event_handle"]]["type"] == "Census"


async def test_a_type_the_tree_lacks_is_still_created(tools):
    person = await tools("add_person", given="Mercy")
    out = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": "Land Grant", "date": "1850", "citation": CITED},
    )
    assert "error" not in out, out
    assert tools.fake.store["event"][out["event_handle"]]["type"] == "Land Grant"
