"""Parents and children of existing families, changed in place (TOOL-REQUESTS #21).

A register named a wife for a man whose family had no mother, and nothing set
one: entering her meant a second family, with the children left in the
motherless one. Moving a child was a detach and a re-add, which drops the
link's citations and notes and puts the child last; with a duplicated link
(#2) it left a link only the child held.
"""

from __future__ import annotations

import pytest
from mcp.server.mcpserver.exceptions import ToolError

CITED = {"source_title": "Family register", "page": "p. 7"}


async def _person(tools, given, surname="Fenwick", gender="unknown", born=None):
    birth = {"type": "Birth", "date": born} if born else None
    return await tools(
        "add_person",
        given=given,
        surname=surname,
        gender=gender,
        birth=birth,
        require_citation=False,
    )


def _store(tools, kind, obj) -> dict:
    return tools.fake.store[kind][obj["handle"]]


# --------------------------------------------------------------------------- #
# set_family_parent
# --------------------------------------------------------------------------- #
async def test_a_mother_is_set_on_a_motherless_family_with_both_links(tools):
    father = await _person(tools, "Amos", gender="male")
    child = await _person(tools, "Ada")
    family = await tools("add_family", father=father["gramps_id"], children=[child["gramps_id"]])
    wife = await _person(tools, "Ruth", "Pell", gender="female")

    out = await tools(
        "set_family_parent", family=family["gramps_id"], role="mother", person=wife["gramps_id"]
    )

    assert out["changed"] is True, out
    assert (out["parent"], out["previous"]) == (wife["gramps_id"], None)
    assert _store(tools, "family", family)["mother_handle"] == wife["handle"]
    assert _store(tools, "person", wife)["family_list"] == [family["handle"]]
    assert _store(tools, "family", family)["child_ref_list"][0]["ref"] == child["handle"]
    assert "warning" not in out
    assert tools.fake.partial_writes == []


async def test_a_parent_already_set_is_replaced_only_when_asked(tools):
    first = await _person(tools, "Ruth", gender="female")
    second = await _person(tools, "Hester", gender="female")
    family = await tools("add_family", mother=first["gramps_id"])

    refused = await tools(
        "set_family_parent", family=family["gramps_id"], role="mother", person=second["gramps_id"]
    )
    assert refused["error"] == "parent_already_set"
    assert first["gramps_id"] in refused["message"] and "replace=True" in refused["message"]
    assert _store(tools, "family", family)["mother_handle"] == first["handle"]

    out = await tools(
        "set_family_parent",
        family=family["gramps_id"],
        role="mother",
        person=second["gramps_id"],
        replace=True,
    )
    assert out["previous"] == first["gramps_id"] and out["parent"] == second["gramps_id"]
    assert _store(tools, "person", first)["family_list"] == []
    assert _store(tools, "person", second)["family_list"] == [family["handle"]]


async def test_a_parent_is_removed_with_their_link(tools):
    father = await _person(tools, "Amos", gender="male")
    family = await tools("add_family", father=father["gramps_id"])
    out = await tools("set_family_parent", family=family["gramps_id"], role="father")
    assert out["changed"] is True and out["parent"] is None
    assert _store(tools, "family", family)["father_handle"] == ""
    assert _store(tools, "person", father)["family_list"] == []


async def test_an_old_parent_missing_the_link_is_restored_so_the_server_can_remove_it(tools):
    """The server's list.remove refuses the family write otherwise (PITFALLS 15)."""
    first = await _person(tools, "Ruth", gender="female")
    second = await _person(tools, "Hester", gender="female")
    family = await tools("add_family", mother=first["gramps_id"])
    _store(tools, "person", first)["family_list"] = []  # one-sided, as found in a tree

    out = await tools(
        "set_family_parent",
        family=family["gramps_id"],
        role="mother",
        person=second["gramps_id"],
        replace=True,
    )
    assert out["changed"] is True, out
    assert "restored" in out["message"]
    assert _store(tools, "person", first)["family_list"] == []
    assert _store(tools, "family", family)["mother_handle"] == second["handle"]


async def test_doubled_links_on_either_parent_end_up_single_or_gone(tools):
    first = await _person(tools, "Ruth", gender="female")
    second = await _person(tools, "Hester", gender="female")
    family = await tools("add_family", mother=first["gramps_id"])
    other = await tools("add_family")
    _store(tools, "person", first)["family_list"] = [family["handle"]] * 2
    _store(tools, "person", second)["family_list"] = [other["handle"], family["handle"]]

    await tools(
        "set_family_parent",
        family=family["gramps_id"],
        role="mother",
        person=second["gramps_id"],
        replace=True,
    )
    assert _store(tools, "person", first)["family_list"] == []
    assert _store(tools, "person", second)["family_list"] == [other["handle"], family["handle"]]


async def test_set_family_parent_refuses_what_makes_no_sense(tools):
    father = await _person(tools, "Amos", gender="male")
    child = await _person(tools, "Ada", gender="female")
    family = await tools("add_family", father=father["gramps_id"], children=[child["gramps_id"]])
    same = await tools(
        "set_family_parent", family=family["gramps_id"], role="father", person=father["gramps_id"]
    )
    assert same["changed"] is False
    other = await tools(
        "set_family_parent", family=family["gramps_id"], role="mother", person=father["gramps_id"]
    )
    assert other["error"] == "already_a_parent"
    own = await tools(
        "set_family_parent", family=family["gramps_id"], role="mother", person=child["gramps_id"]
    )
    assert own["error"] == "child_of_family"
    with pytest.raises(ToolError, match="'father' or 'mother'"):
        await tools(
            "set_family_parent", family=family["gramps_id"], role="wife", person=child["gramps_id"]
        )


async def test_a_parent_of_the_other_sex_is_set_with_a_warning(tools):
    family = await tools("add_family")
    woman = await _person(tools, "Ruth", gender="female")
    out = await tools(
        "set_family_parent", family=family["gramps_id"], role="father", person=woman["gramps_id"]
    )
    assert out["changed"] is True
    assert "recorded as female" in out["warning"]


# --------------------------------------------------------------------------- #
# move_child
# --------------------------------------------------------------------------- #
async def _two_families(tools):
    """A father's motherless family with three children, and his second family."""
    father = await _person(tools, "Amos", gender="male")
    wife = await _person(tools, "Ruth", "Pell", gender="female")
    kids = [
        await _person(tools, name, born=year)
        for name, year in (("Ada", "1851"), ("Silas", "1854"), ("Tobias", "1860"))
    ]
    old = await tools(
        "add_family", father=father["gramps_id"], children=[k["gramps_id"] for k in kids]
    )
    new = await tools("add_family", father=father["gramps_id"], mother=wife["gramps_id"])
    return old, new, kids


async def test_a_child_moves_with_the_links_evidence(tools):
    old, new, kids = await _two_families(tools)
    youngest = kids[2]
    cited = await tools(
        "cite_child_link", family=old["gramps_id"], child=youngest["gramps_id"], citation=CITED
    )
    ref = _store(tools, "family", old)["child_ref_list"][2]
    ref["note_list"] = ["n1"]
    ref["private"] = True

    out = await tools(
        "move_child",
        child=youngest["gramps_id"],
        from_family=old["gramps_id"],
        to_family=new["gramps_id"],
        frel="stepchild",
    )

    assert out["changed"] is True, out
    assert out["kept"] == {"citations": 1, "notes": 1}
    (moved,) = _store(tools, "family", new)["child_ref_list"]
    assert moved["citation_list"] == [cited["citation_handle"]]
    assert moved["note_list"] == ["n1"] and moved["private"] is True
    assert (moved["frel"], moved["mrel"]) == ("Stepchild", "Birth")
    assert [r["ref"] for r in _store(tools, "family", old)["child_ref_list"]] == [
        k["handle"] for k in kids[:2]
    ]
    assert _store(tools, "person", youngest)["parent_family_list"] == [new["handle"]]
    assert tools.fake.partial_writes == []


async def test_a_child_goes_into_birth_order(tools):
    old, new, kids = await _two_families(tools)
    for kid in (kids[0], kids[2]):
        await tools(
            "move_child",
            child=kid["gramps_id"],
            from_family=old["gramps_id"],
            to_family=new["gramps_id"],
        )
    out = await tools(
        "move_child",
        child=kids[1]["gramps_id"],
        from_family=old["gramps_id"],
        to_family=new["gramps_id"],
    )
    assert out["position"] == 2
    assert out["children"] == [k["gramps_id"] for k in kids]
    undated = await _person(tools, "Dora")
    await tools("add_child_to_family", family=old["gramps_id"], child=undated["gramps_id"])
    last = await tools(
        "move_child",
        child=undated["gramps_id"],
        from_family=old["gramps_id"],
        to_family=new["gramps_id"],
    )
    assert last["position"] == 4


async def test_a_doubled_link_goes_and_the_new_family_takes_the_old_ones_place(tools):
    old, new, kids = await _two_families(tools)
    adoptive = await tools("add_family")
    youngest = kids[2]
    await tools("add_child_to_family", family=adoptive["gramps_id"], child=youngest["gramps_id"])
    _store(tools, "person", youngest)["parent_family_list"] = [
        old["handle"],
        adoptive["handle"],
        old["handle"],
    ]
    await tools(
        "move_child",
        child=youngest["gramps_id"],
        from_family=old["gramps_id"],
        to_family=new["gramps_id"],
    )
    # The first entry is what Gramps reads as the main parents.
    assert _store(tools, "person", youngest)["parent_family_list"] == [
        new["handle"],
        adoptive["handle"],
    ]


async def test_move_child_refuses_what_it_cannot_do(tools):
    old, new, kids = await _two_families(tools)
    stranger = await _person(tools, "Ezra", "Pell")
    assert (
        await tools(
            "move_child",
            child=stranger["gramps_id"],
            from_family=old["gramps_id"],
            to_family=new["gramps_id"],
        )
    )["error"] == "not_a_child"
    assert (
        await tools(
            "move_child",
            child=kids[0]["gramps_id"],
            from_family=old["gramps_id"],
            to_family=old["gramps_id"],
        )
    )["error"] == "same_family"
    await tools("add_child_to_family", family=new["gramps_id"], child=kids[1]["gramps_id"])
    both = await tools(
        "move_child",
        child=kids[1]["gramps_id"],
        from_family=old["gramps_id"],
        to_family=new["gramps_id"],
    )
    assert both["error"] == "already_a_child" and "detach_object" in both["message"]


async def test_a_failure_part_way_leaves_the_child_in_both_and_says_how_to_finish(tools):
    old, new, kids = await _two_families(tools)
    original = tools.service._mutate
    calls = []

    async def failing(object_type, ref, fn, **kwargs):
        calls.append(ref)
        if len(calls) == 2:
            raise RuntimeError("the second write failed")
        return await original(object_type, ref, fn, **kwargs)

    tools.service._mutate = failing
    out = await tools(
        "move_child",
        child=kids[0]["gramps_id"],
        from_family=old["gramps_id"],
        to_family=new["gramps_id"],
    )
    assert out["error"] == "partly_moved"
    assert "detach_object" in out["message"]
    assert kids[0]["handle"] in [r["ref"] for r in _store(tools, "family", new)["child_ref_list"]]
    assert kids[0]["handle"] in [r["ref"] for r in _store(tools, "family", old)["child_ref_list"]]


# --------------------------------------------------------------------------- #
# add_family with each child's relationships
# --------------------------------------------------------------------------- #
async def test_add_family_takes_a_stepchild_in_one_call(tools):
    own = await _person(tools, "Ada")
    step = await _person(tools, "Tobias")
    out = await tools(
        "add_family",
        children=[own["gramps_id"], {"person": step["gramps_id"], "frel": "stepchild"}],
    )
    refs = _store(tools, "family", out)["child_ref_list"]
    assert [(r["ref"], r["frel"], r["mrel"]) for r in refs] == [
        (own["handle"], "Birth", "Birth"),
        (step["handle"], "Stepchild", "Birth"),
    ]
    bad = await tools("add_family", children=[{"person": own["gramps_id"], "frel": "Stepkid"}])
    assert bad["error"] == "unknown_type"
