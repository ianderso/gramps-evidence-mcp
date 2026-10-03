"""Type names are spelt as Gramps spells them, and an unknown one is refused.

gramps-webapi turns a type name into a type by exact, case-sensitive match
against Gramps' standard names; any other string is stored as a new custom
type, beside the standard one it was probably meant to be (docs/PITFALLS.md
section 26). So every tool that writes a type name matches it first, against
the standard names and the tree's own (``GET /api/types/``): ignoring case,
spacing and punctuation, then through a short list of unambiguous synonyms.
What still matches nothing is refused with the closest names, unless the
caller sets ``allow_new_type`` to mean a new custom type.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gramps_evidence_mcp.service import _TYPE_SYNONYMS, _type_key

CITED = {"source_title": "Parish Register of Wexcombe", "page": "f. 9"}

#: Gramps' standard names, as the fake serves them: recorded from a real
#: server by tests/live/capture_defaults.py.
STANDARD = json.loads((Path(__file__).parent / "fixtures" / "server_defaults.json").read_text())[
    "types"
]


async def _event_type(tools, person, given_type, **extra) -> dict:
    out = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": given_type, "date": "1851", "citation": CITED, **extra},
    )
    if "error" in out:
        return out
    return {"stored": tools.fake.store["event"][out["event_handle"]]["type"]}


@pytest.mark.parametrize(
    ("given", "stored"),
    [
        ("census", "Census"),
        ("  military   SERVICE ", "Military Service"),
        ("cause-of-death", "Cause Of Death"),
        ("number of marriages", "Number of Marriages"),
        ("Born", "Birth"),
        ("buried", "Burial"),
        ("Baptised", "Baptism"),
        ("Naturalisation", "Naturalization"),
        ("Marriage Licence", "Marriage License"),
    ],
)
async def test_an_event_type_maps_to_the_standard_name(tools, given, stored):
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    assert await _event_type(tools, person, given) == {"stored": stored}
    assert tools.fake.custom_types["event_types"] == {"Widowhood"}, "no custom type made"


async def test_a_standard_type_the_tree_has_never_used_is_simply_used(tools):
    """Gramps' vocabulary is fixed; the tree's use of it is not what decides."""
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    for name in ("Stillbirth", "Bas Mitzvah", "Divorce Filing", "Medical Information"):
        assert name not in tools.fake.custom_types["event_types"]
        assert await _event_type(tools, person, name.lower()) == {"stored": name}


async def test_a_near_miss_is_refused_with_the_closest_names_and_nothing_written(tools):
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    before = {typ: dict(objects) for typ, objects in tools.fake.store.items()}
    out = await _event_type(tools, person, "Censsus")
    assert out["error"] == "unknown_type"
    assert "'Censsus' is not an event type" in out["message"]
    assert "Did you mean 'Census'?" in out["message"]
    assert "allow_new_type" in out["message"]
    assert {typ: dict(objects) for typ, objects in tools.fake.store.items()} == before


async def test_a_new_custom_type_is_made_on_request_and_known_from_then_on(tools):
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    made = await _event_type(tools, person, "Land Grant", allow_new_type=True)
    assert made == {"stored": "Land Grant"}
    assert "Land Grant" in tools.fake.custom_types["event_types"]
    # Now the tree's own: matched as a standard name is, spelt as first made.
    assert await _event_type(tools, person, "land-grant") == {"stored": "Land Grant"}


async def test_the_trees_custom_type_is_matched_like_a_standard_one(tools):
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    assert await _event_type(tools, person, "WIDOWHOOD") == {"stored": "Widowhood"}


async def test_a_refused_death_leaves_no_person_and_no_birth_behind(tools):
    out = await tools(
        "add_person",
        given="Elias",
        surname="Wren",
        birth={"date": "1801", "citation": CITED},
        death={"type": "Dide", "date": "1866", "citation": CITED},
    )
    assert out["error"] == "unknown_type"
    assert not any(tools.fake.store[typ] for typ in ("person", "event", "citation", "source"))


async def test_a_refused_relationship_leaves_no_family_and_no_marriage(tools):
    out = await tools(
        "add_family",
        relationship="Handfasted",
        marriage={"date": "1828", "citation": CITED},
    )
    assert out["error"] == "unknown_type"
    assert not any(tools.fake.store[typ] for typ in ("family", "event", "citation", "source"))
    ok = await tools("add_family", relationship="civil union")
    assert tools.fake.store["family"][ok["handle"]]["type"] == "Civil Union"


async def test_a_refused_mother_relationship_adds_no_child(tools):
    family = await tools("add_family")
    child = await tools("add_person", given="Tobias", surname="Wren")
    out = await tools(
        "add_child_to_family",
        family=family["gramps_id"],
        child=child["gramps_id"],
        frel="Birth",
        mrel="Godchild",
    )
    assert out["error"] == "unknown_type"
    assert not tools.fake.store["family"][family["handle"]]["child_ref_list"]
    assert not tools.fake.store["person"][child["handle"]]["parent_family_list"]
    out = await tools(
        "add_child_to_family",
        family=family["gramps_id"],
        child=child["gramps_id"],
        frel="biological",
        mrel="step",
    )
    (ref,) = tools.fake.store["family"][family["handle"]]["child_ref_list"]
    assert (ref["frel"], ref["mrel"]) == ("Birth", "Stepchild")


async def test_every_kind_of_type_name_is_matched(tools):
    """One case per vocabulary a tool writes, each by spelling or synonym."""
    store = tools.fake.store
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    other = await tools("add_person", given="Hester", surname="Quillfeather")
    event = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": "Burial", "date": "1870", "citation": CITED},
    )
    await tools(
        "add_event_ref", person=other["gramps_id"], event=event["event_handle"], role="godmother"
    )
    assert store["person"][other["handle"]]["event_ref_list"][-1]["role"] == "Godparent"

    await tools(
        "add_alternate_name", person=person["gramps_id"], surname="Tolley", name_type="maiden name"
    )
    assert store["person"][person["handle"]]["alternate_names"][-1]["type"] == "Birth Name"

    await tools("add_url", object_type="person", target=person["gramps_id"], url="https://x.test")
    await tools(
        "add_url",
        object_type="person",
        target=person["gramps_id"],
        url="mailto:a@x.test",
        url_type="EMAIL",
    )
    assert [u["type"] for u in store["person"][person["handle"]]["urls"]] == ["Web Home", "E-mail"]

    await tools(
        "add_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="nickname",
        value="Addie",
    )
    assert store["person"][person["handle"]]["attribute_list"][-1]["type"] == "Nickname"

    repo = await tools("add_repository", name="County Record Office", repository_type="archives")
    assert store["repository"][repo["handle"]]["type"] == "Archive"
    source = await tools(
        "add_source",
        title="Wexcombe registers",
        repository=repo["gramps_id"],
        media_type="microfilm",
    )
    assert store["source"][source["handle"]]["reporef_list"][0]["media_type"] == "Film"

    note = await tools("add_note", text="Checked the bishop's transcripts.", note_type="research")
    assert store["note"][note["handle"]]["type"] == "Research"
    await tools(
        "update_object_fields",
        object_type="note",
        ref=note["gramps_id"],
        fields={"type": "to-do"},
    )
    assert store["note"][note["handle"]]["type"] == "To Do"

    place = await tools("add_place", name="Wexcombe", place_type="village")
    assert store["place"][place["handle"]]["place_type"] == "Village"
    await tools("update_place", place=place["gramps_id"], place_type="PARISH")
    assert store["place"][place["handle"]]["place_type"] == "Parish"
    assert not any(
        tools.fake.custom_types[k] for k in tools.fake.custom_types if k != "event_types"
    )


async def test_a_custom_attribute_name_is_known_on_every_kind_of_object(tools):
    """Gramps lists them per kind of object; a name in use elsewhere is no typo."""
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    first = await tools(
        "add_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="Height",
        value="5 ft 2 in",
    )
    assert first["error"] == "unknown_type"
    await tools(
        "add_attribute",
        object_type="person",
        target=person["gramps_id"],
        name="Height",
        value="5 ft 2 in",
        allow_new_type=True,
    )
    event = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={"type": "Military Service", "date": "1863", "citation": CITED},
    )
    out = await tools(
        "add_attribute",
        object_type="event",
        target=event["event_handle"],
        name="height",
        value="5 ft",
    )
    assert "error" not in out, out
    assert (
        tools.fake.store["event"][event["event_handle"]]["attribute_list"][-1]["type"] == "Height"
    )


async def test_a_source_attribute_name_is_its_own_vocabulary(tools):
    """Gramps 6 has no standard source attribute but Unknown: each is custom."""
    source = await tools("add_source", title="Wexcombe registers")
    out = await tools(
        "add_attribute", object_type="source", target=source["gramps_id"], name="URL", value="x"
    )
    assert out["error"] == "unknown_type"
    assert "Standard: none." in out["message"]


async def test_the_default_url_type_is_gramps_own():
    """1.1 and earlier wrote 'Web Home Page', which Gramps does not have."""
    from gramps_evidence_mcp import server

    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert tools["add_url"].input_schema["properties"]["url_type"]["default"] == "Web Home"
    assert "Web Home" in STANDARD["url_types"]


def test_every_synonym_names_a_standard_type_and_shadows_none():
    for vocabulary, synonyms in _TYPE_SYNONYMS.items():
        standard = {_type_key(name) for name in STANDARD[vocabulary]}
        for key, target in synonyms.items():
            assert key == _type_key(key), (vocabulary, key)
            assert _type_key(target) in standard, (vocabulary, target)
            assert key not in standard, f"{key!r} is already a {vocabulary} name"


def test_no_two_standard_names_match_alike():
    for vocabulary, names in STANDARD.items():
        keys = [_type_key(name) for name in names]
        assert len(keys) == len(set(keys)), vocabulary


async def test_an_accidental_custom_spelling_of_a_standard_name_is_never_offered(tools):
    """A tree may hold "census" or "Web Home Page" from before; neither is used."""
    tools.fake.custom_types["event_types"] |= {"census", "Born"}
    person = await tools("add_person", given="Ada", surname="Quillfeather")
    assert await _event_type(tools, person, "census") == {"stored": "Census"}
    out = await _event_type(tools, person, "Censsus")
    assert "Did you mean 'Census'?" in out["message"]
    assert "Custom: Widowhood." in out["message"]


async def test_a_sweep_refuses_only_the_row_whose_type_matches_nothing(tools):
    repo = await tools("add_repository", name="County Record Office", repository_type="Archive")
    first = await tools("add_source", title="Wexcombe registers")
    second = await tools("add_source", title="Wexcombe tithe map")
    out = await tools(
        "link_repositories",
        items=[
            {"source": first["gramps_id"], "repository": repo["gramps_id"], "media_type": "fiche"},
            {"source": second["gramps_id"], "repository": repo["gramps_id"], "media_type": "Fillm"},
        ],
    )
    assert [r["status"] for r in out["rows"]] == ["linked", "error"]
    assert "Did you mean 'Film'?" in out["rows"][1]["message"]
    assert tools.fake.store["source"][first["handle"]]["reporef_list"][0]["media_type"] == "Fiche"
    assert not tools.fake.store["source"][second["handle"]]["reporef_list"]
