"""What a merge does beyond the two objects it names (docs/PITFALLS.md section 25).

Gramps' person merge goes on to merge two of the survivor's families that now
have the same parents, and its family merge merges a differing father or
mother into the survivor's. Spouses, and a parent with their child, it refuses.
A dry run that named only the two objects asked about would let the model
approve one merge and get two, so ``merge_objects`` says what follows.
"""

from __future__ import annotations


async def _person(tools, given: str, gender: str = "male") -> dict:
    return await tools("add_person", given=given, surname="Wren", gender=gender)


async def test_a_person_merge_says_it_will_merge_their_families(tools):
    elias = await _person(tools, "Elias")
    copy = await _person(tools, "Elias")
    hannah = await _person(tools, "Hannah", "female")
    first = await tools("add_family", father=elias["gramps_id"], mother=hannah["gramps_id"])
    second = await tools("add_family", father=copy["gramps_id"], mother=hannah["gramps_id"])

    plan = await tools(
        "merge_objects", object_type="person", keep=elias["gramps_id"], drop=copy["gramps_id"]
    )
    assert plan["also_merges"] == [
        {
            "family": first["gramps_id"],
            "absorbs": second["gramps_id"],
            "because": "the two families would have the same parents",
        }
    ]
    assert f"also merge family {second['gramps_id']} into {first['gramps_id']}" in plan["message"]

    done = await tools(
        "merge_objects",
        object_type="person",
        keep=elias["gramps_id"],
        drop=copy["gramps_id"],
        dry_run=False,
    )
    assert "It also merged family" in done["message"]
    assert second["handle"] not in tools.fake.store["family"]
    assert tools.fake.store["person"][elias["handle"]]["family_list"] == [first["handle"]]
    assert tools.fake.store["person"][hannah["handle"]]["family_list"] == [first["handle"]]


async def test_a_person_merge_with_unrelated_families_merges_only_the_people(tools):
    elias = await _person(tools, "Elias")
    copy = await _person(tools, "Elias")
    await tools("add_family", father=elias["gramps_id"])
    await tools(
        "add_family",
        father=copy["gramps_id"],
        mother=(await _person(tools, "Ann", "female"))["gramps_id"],
    )
    plan = await tools(
        "merge_objects", object_type="person", keep=elias["gramps_id"], drop=copy["gramps_id"]
    )
    assert "also_merges" not in plan
    assert "also merge" not in plan["message"]


async def test_a_family_merge_says_it_will_merge_differing_parents(tools):
    elias = await _person(tools, "Elias")
    copy = await _person(tools, "Elias")
    first = await tools("add_family", father=elias["gramps_id"])
    second = await tools("add_family", father=copy["gramps_id"])
    plan = await tools(
        "merge_objects", object_type="family", keep=first["gramps_id"], drop=second["gramps_id"]
    )
    assert plan["also_merges"] == [
        {
            "person": elias["gramps_id"],
            "absorbs": copy["gramps_id"],
            "because": "the families have different fathers",
        }
    ]
    await tools(
        "merge_objects",
        object_type="family",
        keep=first["gramps_id"],
        drop=second["gramps_id"],
        dry_run=False,
    )
    assert copy["handle"] not in tools.fake.store["person"]


async def test_spouses_are_refused_before_anything_is_sent(tools):
    elias = await _person(tools, "Elias")
    hannah = await _person(tools, "Hannah", "female")
    await tools("add_family", father=elias["gramps_id"], mother=hannah["gramps_id"])
    plan = await tools(
        "merge_objects", object_type="person", keep=elias["gramps_id"], drop=hannah["gramps_id"]
    )
    assert "would be refused" in plan["message"] and "spouses" in plan["refused"]
    out = await tools(
        "merge_objects",
        object_type="person",
        keep=elias["gramps_id"],
        drop=hannah["gramps_id"],
        dry_run=False,
    )
    assert out["error"] == "merge_refused"
    assert tools.fake.merges == []


async def test_a_parent_and_child_are_refused(tools):
    elias = await _person(tools, "Elias")
    mercy = await _person(tools, "Mercy", "female")
    await tools("add_family", father=elias["gramps_id"], children=[mercy["gramps_id"]])
    out = await tools(
        "merge_objects",
        object_type="person",
        keep=mercy["gramps_id"],
        drop=elias["gramps_id"],
        dry_run=False,
    )
    assert out["error"] == "merge_refused"
    assert "parent with their own child" in out["message"]
    assert tools.fake.merges == []


async def test_the_fake_refuses_as_the_server_does(tools):
    """Without the service's check, the merge endpoint answers 409."""
    elias = await _person(tools, "Elias")
    hannah = await _person(tools, "Hannah", "female")
    await tools("add_family", father=elias["gramps_id"], mother=hannah["gramps_id"])
    from gramps_evidence_mcp.client import GrampsApiError

    try:
        await tools.service.client.merge("person", elias["handle"], hannah["handle"])
    except GrampsApiError as exc:
        assert exc.status == 409
    else:
        raise AssertionError("the fake merged spouses")


async def test_a_family_merge_that_would_merge_a_father_with_his_son_is_refused(tools):
    """Two Elias Wrens of successive generations: the father merge Gramps refuses."""
    elias = await _person(tools, "Elias")
    son = await _person(tools, "Elias")
    await tools("add_family", father=elias["gramps_id"], children=[son["gramps_id"]])
    first = await tools("add_family", father=elias["gramps_id"])
    second = await tools("add_family", father=son["gramps_id"])
    out = await tools(
        "merge_objects",
        object_type="family",
        keep=first["gramps_id"],
        drop=second["gramps_id"],
        dry_run=False,
    )
    assert out["error"] == "merge_refused", out
    assert "would merge their fathers" in out["message"]
    assert tools.fake.merges == []
