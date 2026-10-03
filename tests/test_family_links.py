"""Links between people and families: editing them in place, and auditing them.

From a research session: two stepsons were held as birth children and nothing
changed a ChildRef's relationship, so the fix was detach-and-re-add, which
drops the link's citations and moves the child to the end of the birth order.
One of them was listed twice in his parent_family_list; the detach removed
the family from the child once, leaving a link only the person held. Eleven
more such duplicates were found by a script, and no tool reported them.
"""

from __future__ import annotations

BORN = {"type": "Birth", "date": "1850"}


async def _person(tools, given, surname="Fenwick"):
    return await tools(
        "add_person", given=given, surname=surname, birth=BORN, require_citation=False
    )


async def _family_of_three(tools):
    father = await _person(tools, "Amos")
    kids = [await _person(tools, name) for name in ("Bertha", "Cyrus", "Dora")]
    family = await tools(
        "add_family", father=father["gramps_id"], children=[k["gramps_id"] for k in kids]
    )
    return family, kids


def _family(tools, family) -> dict:
    return tools.fake.store["family"][family["handle"]]


def _person_store(tools, person) -> dict:
    return tools.fake.store["person"][person["handle"]]


# --------------------------------------------------------------------------- #
# update_child_ref
# --------------------------------------------------------------------------- #
async def test_a_stepchild_is_corrected_in_place(tools):
    family, kids = await _family_of_three(tools)
    middle = kids[1]
    await tools(
        "cite_child_link",
        family=family["gramps_id"],
        child=middle["gramps_id"],
        citation={"source_title": "Guardianship papers", "page": "f. 3"},
    )
    cited = list(_family(tools, family)["child_ref_list"][1]["citation_list"])

    out = await tools(
        "update_child_ref", family=family["gramps_id"], child=middle["gramps_id"], frel="stepchild"
    )

    assert out["changed"] is True
    assert "frel Birth -> Stepchild" in out["message"]
    refs = _family(tools, family)["child_ref_list"]
    assert [r["ref"] for r in refs] == [k["handle"] for k in kids]  # birth order kept
    assert refs[1]["frel"] == "Stepchild"
    assert refs[1]["mrel"] == "Birth"
    assert refs[1]["citation_list"] == cited
    assert tools.fake.partial_writes == []


async def test_update_child_ref_refuses_what_it_cannot_do(tools):
    family, kids = await _family_of_three(tools)
    stranger = await _person(tools, "Ezra", "Pell")
    assert (
        await tools("update_child_ref", family=family["gramps_id"], child=kids[0]["gramps_id"])
    )["error"] == "nothing_to_do"
    assert (
        await tools(
            "update_child_ref",
            family=family["gramps_id"],
            child=stranger["gramps_id"],
            mrel="Birth",
        )
    )["error"] == "not_a_child"
    bad = await tools(
        "update_child_ref", family=family["gramps_id"], child=kids[0]["gramps_id"], mrel="Godchild"
    )
    assert bad["error"] == "unknown_type"
    assert "Stepchild" in bad["message"]


async def test_adding_a_child_already_present_points_at_update_child_ref(tools):
    family, kids = await _family_of_three(tools)
    out = await tools(
        "add_child_to_family",
        family=family["gramps_id"],
        child=kids[0]["gramps_id"],
        frel="Adopted",
    )
    assert out["changed"] is False
    assert "update_child_ref" in out["message"]


async def test_add_family_lists_a_child_once(tools):
    child = await _person(tools, "Bertha")
    family = await tools("add_family", children=[child["gramps_id"], child["handle"]])
    assert len(_family(tools, family)["child_ref_list"]) == 1


# --------------------------------------------------------------------------- #
# detaching a child with a duplicated link
# --------------------------------------------------------------------------- #
async def test_detaching_a_child_leaves_no_one_sided_link(tools):
    """The server removes only the first of two links; the second must go too."""
    family, kids = await _family_of_three(tools)
    child = _person_store(tools, kids[2])
    child["parent_family_list"] = [family["handle"], family["handle"]]

    await tools(
        "detach_object",
        parent_type="family",
        parent=family["gramps_id"],
        child_kind="child",
        child=kids[2]["gramps_id"],
    )

    assert family["handle"] not in _person_store(tools, kids[2]).get("parent_family_list", [])
    check = await tools("check_family_links", include_private=True)
    assert check["problem_count"] == 0, check


# --------------------------------------------------------------------------- #
# check_family_links
# --------------------------------------------------------------------------- #
async def test_the_audit_finds_each_kind_of_link_problem(tools):
    family, kids = await _family_of_three(tools)
    spouse = await _person(tools, "Amos")
    lone = await _person(tools, "Fern")
    store = tools.fake.store["person"]
    store[kids[0]["handle"]]["parent_family_list"] = [family["handle"]] * 2
    spouse_family = await tools("add_family", father=spouse["gramps_id"])
    store[spouse["handle"]]["family_list"] = [spouse_family["handle"]] * 2
    store[lone["handle"]]["parent_family_list"] = [family["handle"], "h-gone"]
    store[kids[1]["handle"]]["parent_family_list"] = []
    _family(tools, family)["child_ref_list"].append(
        dict(_family(tools, family)["child_ref_list"][2])
    )

    out = await tools("check_family_links", include_private=True)

    assert out["by_kind"] == {
        "duplicate_parent_family_list": 1,
        "duplicate_family_list": 1,
        "one_sided_child_link": 2,
        "missing_family": 1,
        "duplicate_child_ref": 1,
    }
    dup = next(p for p in out["problems"] if p["kind"] == "duplicate_parent_family_list")
    assert dup == {
        "kind": "duplicate_parent_family_list",
        "person": kids[0]["gramps_id"],
        "family": family["gramps_id"],
        "count": 2,
        "repair": f"Any edit of the person removes it: update_person(person="
        f"'{kids[0]['gramps_id']}') with nothing else.",
    }


async def test_the_suggested_repair_for_a_duplicate_works(tools):
    family, kids = await _family_of_three(tools)
    _person_store(tools, kids[0])["parent_family_list"] = [family["handle"]] * 2

    await tools("update_person", person=kids[0]["gramps_id"])

    assert (await tools("check_family_links", include_private=True))["problem_count"] == 0


async def test_the_audit_withholds_living_people(tools):
    family, kids = await _family_of_three(tools)
    living = await tools(
        "add_person",
        given="Gail",
        surname="Fenwick",
        birth={"type": "Birth", "date": "1990"},
        require_citation=False,
    )
    tools.fake.store["person"][living["handle"]]["parent_family_list"] = ["h-gone"]
    _person_store(tools, kids[0])["parent_family_list"] = [family["handle"]] * 2

    out = await tools("check_family_links")
    assert out["problem_count"] == 1
    assert out["withheld_count"] == 1
    assert all(p["person"] != living["gramps_id"] for p in out["problems"])


# --------------------------------------------------------------------------- #
# repairing a link only the person holds
# --------------------------------------------------------------------------- #
async def test_a_one_sided_link_can_be_removed_from_the_person(tools):
    family, _ = await _family_of_three(tools)
    lone = await _person(tools, "Fern")
    _person_store(tools, lone)["parent_family_list"] = [family["handle"], "h-gone"]

    first = await tools(
        "detach_object",
        parent_type="person",
        parent=lone["gramps_id"],
        child_kind="parent_family",
        child=family["gramps_id"],
    )
    gone = await tools(
        "detach_object",
        parent_type="person",
        parent=lone["gramps_id"],
        child_kind="parent_family",
        child="h-gone",
    )
    assert first["changed"] is True and gone["changed"] is True
    assert _person_store(tools, lone)["parent_family_list"] == []


async def test_a_two_sided_link_is_not_cut_from_one_side(tools):
    family, kids = await _family_of_three(tools)
    out = await tools(
        "detach_object",
        parent_type="person",
        parent=kids[0]["gramps_id"],
        child_kind="parent_family",
        child=family["gramps_id"],
    )
    assert out["error"] == "two_sided_link"
    assert family["handle"] in _person_store(tools, kids[0])["parent_family_list"]


async def test_removing_a_persons_link_never_deletes_the_family(tools):
    family, _ = await _family_of_three(tools)
    lone = await _person(tools, "Fern")
    _person_store(tools, lone)["parent_family_list"] = [family["handle"]]
    out = await tools(
        "detach_object",
        parent_type="person",
        parent=lone["gramps_id"],
        child_kind="parent_family",
        child=family["gramps_id"],
        delete_if_orphan=True,
    )
    assert out["error"] == "conflicting_arguments"
    assert family["handle"] in tools.fake.store["family"]


async def test_the_audit_message_counts_what_it_shows(tools):
    family, kids = await _family_of_three(tools)
    living = await tools(
        "add_person",
        given="Gail",
        surname="Fenwick",
        birth={"type": "Birth", "date": "1990"},
        require_citation=False,
    )
    tools.fake.store["person"][living["handle"]]["parent_family_list"] = ["h-gone"]
    _person_store(tools, kids[0])["parent_family_list"] = [family["handle"]] * 2
    out = await tools("check_family_links")
    assert out["message"].startswith("1 link problem(s).")
    assert "1 more about private or living people withheld" in out["message"]
