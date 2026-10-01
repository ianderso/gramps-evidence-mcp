"""Place merges, repository links, tags, and the batch edits a sweep needs.

From a research session: a place merge left the survivor enclosed by both
places' parents (a town or county beside a coarser one), and said nothing;
add_source linked its repository with medium Unknown where link_repository
takes one; one of two links to the same repository could not be removed;
removing a tag needed its handle; and a 174-row citation sweep had to be a
REST script, outside the server's checks, because each tool call is its own
approval.
"""

from __future__ import annotations


async def _place(tools, name, place_type, parent=""):
    return await tools("add_place", name=name, place_type=place_type, parent=parent)


async def _hierarchy(tools):
    state = await _place(tools, "Iowa", "State")
    county = await _place(tools, "Polk County", "County", state["gramps_id"])
    return state, county


def _parents(tools, place) -> list[str]:
    stored = tools.fake.store["place"][place["handle"]]
    return [r["ref"] for r in stored.get("placeref_list") or []]


# --------------------------------------------------------------------------- #
# place merge enclosures
# --------------------------------------------------------------------------- #
async def test_a_merge_drops_the_coarser_of_two_parents(tools):
    """Des Moines in Polk County, and a duplicate Des Moines in Iowa."""
    state, county = await _hierarchy(tools)
    keep = await _place(tools, "Des Moines", "City", county["gramps_id"])
    drop = await _place(tools, "Des Moines", "City", state["gramps_id"])

    plan = await tools(
        "merge_objects", object_type="place", keep=keep["gramps_id"], drop=drop["gramps_id"]
    )
    assert plan["dry_run"] is True
    assert plan["enclosures"]["result"] == [county["gramps_id"]]
    assert plan["enclosures"]["pruned_as_coarser"] == [state["gramps_id"]]

    out = await tools(
        "merge_objects",
        object_type="place",
        keep=keep["gramps_id"],
        drop=drop["gramps_id"],
        dry_run=False,
    )
    assert "error" not in out
    assert _parents(tools, keep) == [county["handle"]]


async def test_two_unrelated_parents_are_refused_before_merging(tools):
    state, county = await _hierarchy(tools)
    other = await _place(tools, "Linn County", "County", state["gramps_id"])
    keep = await _place(tools, "Cedar Flat", "Town", county["gramps_id"])
    drop = await _place(tools, "Cedar Flat", "Town", other["gramps_id"])

    plan = await tools(
        "merge_objects", object_type="place", keep=keep["gramps_id"], drop=drop["gramps_id"]
    )
    assert plan["enclosures"]["ambiguous"] is True
    assert "would be refused" in plan["message"]

    out = await tools(
        "merge_objects",
        object_type="place",
        keep=keep["gramps_id"],
        drop=drop["gramps_id"],
        dry_run=False,
    )
    assert out["error"] == "ambiguous_enclosures"
    assert tools.fake.merges == []

    chosen = await tools(
        "merge_objects",
        object_type="place",
        keep=keep["gramps_id"],
        drop=drop["gramps_id"],
        dry_run=False,
        enclosures="keep_drop",
    )
    assert "error" not in chosen
    assert _parents(tools, keep) == [other["handle"]]


async def test_keep_both_keeps_the_union(tools):
    state, county = await _hierarchy(tools)
    keep = await _place(tools, "Des Moines", "City", county["gramps_id"])
    drop = await _place(tools, "Des Moines", "City", state["gramps_id"])
    await tools(
        "merge_objects",
        object_type="place",
        keep=keep["gramps_id"],
        drop=drop["gramps_id"],
        dry_run=False,
        enclosures="keep_both",
    )
    assert _parents(tools, keep) == [county["handle"], state["handle"]]


async def test_a_dated_enclosure_is_never_pruned(tools):
    """A dated enclosure is a deliberate alternative: a territory before a state."""
    state, county = await _hierarchy(tools)
    territory = await _place(tools, "Iowa Territory", "Territory")
    keep = await _place(tools, "Des Moines", "City", county["gramps_id"])
    drop = await _place(tools, "Des Moines", "City")
    tools.fake.store["place"][drop["handle"]]["placeref_list"] = [
        {
            "_class": "PlaceRef",
            "ref": territory["handle"],
            "date": {"modifier": 8, "dateval": [0, 0, 1846, False]},
        }
    ]
    await tools(
        "merge_objects",
        object_type="place",
        keep=keep["gramps_id"],
        drop=drop["gramps_id"],
        dry_run=False,
    )
    assert _parents(tools, keep) == [county["handle"], territory["handle"]]


async def test_an_extra_parent_left_by_an_earlier_merge_can_be_detached(tools):
    """update_place refuses a place with several parents; this is the way out."""
    state, county = await _hierarchy(tools)
    town = await _place(tools, "Des Moines", "City", county["gramps_id"])
    tools.fake.store["place"][town["handle"]]["placeref_list"].append(
        {"_class": "PlaceRef", "ref": state["handle"]}
    )
    refused = await tools("update_place", place=town["gramps_id"], parent=county["gramps_id"])
    assert refused["error"] == "multiple_enclosures"

    out = await tools(
        "detach_object",
        parent_type="place",
        parent=town["gramps_id"],
        child_kind="enclosure",
        child=state["gramps_id"],
    )
    assert out["changed"] is True
    assert _parents(tools, town) == [county["handle"]]


async def test_detaching_an_enclosure_never_deletes_the_parent(tools):
    state, county = await _hierarchy(tools)
    out = await tools(
        "detach_object",
        parent_type="place",
        parent=county["gramps_id"],
        child_kind="enclosure",
        child=state["gramps_id"],
        delete_if_orphan=True,
    )
    assert out["error"] == "conflicting_arguments"
    assert _parents(tools, county) == [state["handle"]]


async def test_an_unknown_enclosure_choice_is_refused(tools):
    _, county = await _hierarchy(tools)
    keep = await _place(tools, "A", "Town", county["gramps_id"])
    drop = await _place(tools, "B", "Town", county["gramps_id"])
    out = await tools(
        "merge_objects",
        object_type="place",
        keep=keep["gramps_id"],
        drop=drop["gramps_id"],
        enclosures="both",
    )
    assert out["error"] == "unsupported_choice"


# --------------------------------------------------------------------------- #
# sources, repositories, tags
# --------------------------------------------------------------------------- #
async def test_add_source_links_its_repository_with_the_medium_given(tools):
    repo = await tools("add_repository", name="County Library", repository_type="Library")
    out = await tools(
        "add_source",
        title="History of Brannock County (1898)",
        repository=repo["gramps_id"],
        call_number="977.1 B82",
        media_type="Book",
    )
    ref = tools.fake.store["source"][out["handle"]]["reporef_list"][0]
    assert ref["media_type"] == "Book"
    assert ref["call_number"] == "977.1 B82"


async def test_one_of_two_links_to_the_same_repository_can_be_removed(tools):
    repo = await tools("add_repository", name="State Archives")
    source = await tools("add_source", title="Marriage register", repository=repo["gramps_id"])
    stored = tools.fake.store["source"][source["handle"]]
    stored["reporef_list"][0]["call_number"] = "Vol. 3"
    stored["reporef_list"].append(dict(stored["reporef_list"][0], call_number="Personal copy"))

    out = await tools(
        "detach_object",
        parent_type="source",
        parent=source["gramps_id"],
        child_kind="repository",
        child=repo["gramps_id"],
        call_number="Personal copy",
    )

    assert out["changed"] is True
    left = tools.fake.store["source"][source["handle"]]["reporef_list"]
    assert [r["call_number"] for r in left] == ["Vol. 3"]


async def test_call_number_only_narrows_a_repository_link(tools):
    person = await tools("add_person", given="Ada", surname="Wren")
    out = await tools(
        "detach_object",
        parent_type="person",
        parent=person["gramps_id"],
        child_kind="note",
        child="N0001",
        call_number="x",
    )
    assert out["error"] == "conflicting_arguments"


async def test_a_tag_is_removed_by_name(tools):
    person = await tools("add_person", given="Ada", surname="Wren")
    await tools("tag_object", object_type="person", target=person["gramps_id"], tag="Unproven")
    out = await tools(
        "detach_object",
        parent_type="person",
        parent=person["gramps_id"],
        child_kind="tag",
        child="Unproven",
    )
    assert out["changed"] is True
    assert tools.fake.store["person"][person["handle"]]["tag_list"] == []
    # The tag itself survives for other objects.
    assert len(tools.fake.store["tag"]) == 1


async def test_tagging_twice_is_no_change(tools):
    person = await tools("add_person", given="Ada", surname="Wren")
    await tools("tag_object", object_type="person", target=person["gramps_id"], tag="Unproven")
    again = await tools(
        "tag_object", object_type="person", target=person["gramps_id"], tag="Unproven"
    )
    assert "already carries" in again["message"]


# --------------------------------------------------------------------------- #
# batches
# --------------------------------------------------------------------------- #
async def test_link_repositories_reports_each_row(tools):
    repo = await tools("add_repository", name="State Archives")
    linked = await tools("add_source", title="Already linked", repository=repo["gramps_id"])
    fresh = await tools("add_source", title="Not yet linked")

    out = await tools(
        "link_repositories",
        items=[
            {"source": fresh["gramps_id"], "repository": repo["gramps_id"], "media_type": "Book"},
            {"source": linked["gramps_id"], "repository": repo["gramps_id"]},
            {"source": "S9999", "repository": repo["gramps_id"]},
        ],
    )

    assert out["outcomes"] == {"linked": 1, "already_linked": 1, "missing": 1}
    assert [r["status"] for r in out["rows"]] == ["linked", "already_linked", "missing"]
    ref = tools.fake.store["source"][fresh["handle"]]["reporef_list"][0]
    assert ref["media_type"] == "Book"
    assert tools.fake.partial_writes == []


async def test_update_citations_applies_and_refuses_drifted_rows(tools):
    made = []
    for page in ("Burial, plot 4", "Burial, plot 5", "Interment register p. 2"):
        out = await tools("add_citation", citation={"source_title": "Cemetery", "page": page})
        made.append(out["gramps_id"])

    out = await tools(
        "update_citations",
        items=[
            {
                "citation": made[0],
                "page": "Section B, plot 4",
                "expect_page_prefix": "Burial",
            },
            {"citation": made[1], "page": "Burial, plot 5"},
            {"citation": made[2], "page": "Section B, plot 9", "expect_page_prefix": "Burial"},
            {"citation": "C9999", "confidence": "high"},
        ],
    )

    assert out["outcomes"] == {"applied": 1, "unchanged": 1, "drifted": 1, "missing": 1}
    drifted = out["rows"][2]
    assert drifted["live_page"] == "Interment register p. 2"
    by_id = {c["gramps_id"]: c for c in tools.fake.store["citation"].values()}
    assert by_id[made[0]]["page"] == "Section B, plot 4"
    assert by_id[made[2]]["page"] == "Interment register p. 2"
    assert tools.fake.partial_writes == []


async def test_update_citations_refuses_an_empty_row_and_carries_on(tools):
    a = await tools("add_citation", citation={"source_title": "Cemetery", "page": "p. 1"})
    out = await tools(
        "update_citations",
        items=[{"citation": a["gramps_id"]}, {"citation": a["gramps_id"], "confidence": "low"}],
    )
    assert [r["status"] for r in out["rows"]] == ["error", "applied"]
