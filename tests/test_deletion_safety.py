"""Deleting without stranding evidence, and believing the tree over a late error.

From a research session:

- ``uncite(delete_if_orphan=True)`` deleted a citation whose note recorded a
  real finding, and another whose media list held the only link to a page
  image. "Orphaned" had been judged by what points at the citation, not by
  what the citation holds. The note and image were left attached to nothing,
  and the tool reported success.
- deleting a media object answered HTTP 500 although the delete had landed:
  gramps-webapi 3.21.1 commits the transaction before it updates its search
  index, and an error there is reported for the whole request.
"""

from __future__ import annotations


async def _event_with_citation(tools):
    person = await tools("add_person", given="Silas", surname="Wren")
    added = await tools(
        "add_event_to_person",
        person=person["gramps_id"],
        event={
            "type": "Baptism",
            "date": "1844",
            "citation": {
                "source_title": "St. Brannock register",
                "page": "p. 9",
                "note": "The register's 'Tuesday the 10 day' is not a Tuesday.",
            },
        },
    )
    event = tools.fake.store["event"][added["event_handle"]]
    citation = tools.fake.store["citation"][event["citation_list"][0]]
    return event, citation


async def _second_citation(tools):
    out = await tools("add_citation", citation={"source_title": "Index", "page": "p. 1"})
    return tools.fake.store["citation"][out["handle"]]


async def _media(tools, tmp_path, name="page.jpg"):
    path = tmp_path / name
    path.write_bytes(b"\xff\xd8\xff fake jpeg " + name.encode())
    out = await tools("add_media", file_path=str(path), description="Front page, 1914")
    return tools.fake.store["media"][out["handle"]]


# --------------------------------------------------------------------------- #
# a citation's own notes and media
# --------------------------------------------------------------------------- #
async def test_uncite_keeps_a_citation_whose_note_would_be_stranded(tools):
    event, citation = await _event_with_citation(tools)
    note = citation["note_list"][0]

    out = await tools(
        "uncite", object_type="event", ref=event["gramps_id"], citation=citation["gramps_id"]
    )

    assert out["citation_deleted"] is False
    assert "KEPT" in out["message"]
    assert out["would_orphan"] == {"note": [tools.fake.store["note"][note]["gramps_id"]]}
    assert citation["handle"] in tools.fake.store["citation"]
    assert note in tools.fake.store["note"]


async def test_carry_to_moves_the_note_and_then_deletes(tools):
    event, citation = await _event_with_citation(tools)
    note = citation["note_list"][0]
    survivor = await _second_citation(tools)

    out = await tools(
        "uncite",
        object_type="event",
        ref=event["gramps_id"],
        citation=citation["gramps_id"],
        carry_to=survivor["gramps_id"],
    )

    assert out["citation_deleted"] is True
    assert out["carried"]["to"] == survivor["gramps_id"]
    assert citation["handle"] not in tools.fake.store["citation"]
    assert note in tools.fake.store["citation"][survivor["handle"]]["note_list"]


async def test_a_citations_only_link_to_an_image_is_not_dropped(tools, tmp_path):
    event, citation = await _event_with_citation(tools)
    media = await _media(tools, tmp_path)
    await tools(
        "attach_media",
        target=citation["gramps_id"],
        target_type="citation",
        media_ref=media["gramps_id"],
    )
    survivor = await _second_citation(tools)
    # The note is carried too; this test is about the image.
    out = await tools(
        "uncite", object_type="event", ref=event["gramps_id"], citation=citation["gramps_id"]
    )
    assert out["citation_deleted"] is False
    assert media["gramps_id"] in out["would_orphan"]["media"]

    await tools("cite_event", event=event["gramps_id"], citation={"citation": citation["handle"]})
    out = await tools(
        "uncite",
        object_type="event",
        ref=event["gramps_id"],
        citation=citation["gramps_id"],
        carry_to=survivor["gramps_id"],
    )
    assert out["citation_deleted"] is True
    refs = [m["ref"] for m in tools.fake.store["citation"][survivor["handle"]]["media_list"]]
    assert refs == [media["handle"]]


async def test_a_note_held_elsewhere_too_does_not_block_the_delete(tools):
    event, citation = await _event_with_citation(tools)
    note = citation["note_list"][0]
    tools.fake.store["event"][event["handle"]]["note_list"] = [note]

    out = await tools(
        "uncite", object_type="event", ref=event["gramps_id"], citation=citation["gramps_id"]
    )
    assert out["citation_deleted"] is True
    assert note in tools.fake.store["note"]


async def test_delete_object_refuses_to_strand_a_note(tools):
    _, citation = await _event_with_citation(tools)
    out = await tools("delete_object", object_type="citation", target=citation["gramps_id"])
    assert out["error"] == "would_orphan"
    assert citation["handle"] in tools.fake.store["citation"]

    survivor = await _second_citation(tools)
    out = await tools(
        "delete_object",
        object_type="citation",
        target=citation["gramps_id"],
        carry_to=survivor["gramps_id"],
    )
    assert "error" not in out
    assert citation["handle"] not in tools.fake.store["citation"]
    assert tools.fake.store["citation"][survivor["handle"]]["note_list"]


async def test_carry_to_cannot_be_the_object_itself(tools):
    _, citation = await _event_with_citation(tools)
    out = await tools(
        "delete_object",
        object_type="citation",
        target=citation["gramps_id"],
        carry_to=citation["gramps_id"],
    )
    assert out["error"] == "invalid_carry_to"
    assert citation["handle"] in tools.fake.store["citation"]


async def test_a_source_with_citations_is_not_deleted(tools):
    """The server would delete its citations with it, off every fact."""
    _, citation = await _event_with_citation(tools)
    out = await tools("delete_object", object_type="source", target=citation["source_handle"])
    assert out["error"] == "source_has_citations"
    assert citation["source_handle"] in tools.fake.store["source"]


async def test_a_leaf_object_still_deletes_plainly(tools):
    out = await tools("add_note", text="Scratch.")
    result = await tools("delete_object", object_type="note", target=out["gramps_id"])
    assert result["message"].startswith("Deleted note")
    assert tools.fake.store["note"] == {}


# --------------------------------------------------------------------------- #
# a late server error after the delete landed
# --------------------------------------------------------------------------- #
async def test_a_500_after_a_landed_delete_is_reported_as_the_delete_it_was(tools, tmp_path):
    """The report's case: detach a re-encoded duplicate image and delete it."""
    source = await tools("add_source", title="Family Bible")
    media = await _media(tools, tmp_path)
    await tools(
        "attach_media",
        target=source["gramps_id"],
        target_type="source",
        media_ref=media["gramps_id"],
    )
    tools.fake.delete_error_after_commit = 500

    out = await tools(
        "detach_object",
        parent_type="source",
        parent=source["gramps_id"],
        child_kind="media",
        child=media["gramps_id"],
        delete_if_orphan=True,
    )

    assert "error" not in out
    assert out["deleted"] is True
    assert out["server_error_after_delete"] == 500
    assert out["file_kept"] is True
    assert media["handle"] not in tools.fake.store["media"]


async def test_a_500_on_a_delete_that_did_not_land_is_still_an_error(tools):
    """The re-read is what decides: an object still there was not deleted."""
    out = await tools("add_note", text="Scratch.")
    tools.fake.delete_error_before_commit = 500

    result = await tools("delete_object", object_type="note", target=out["gramps_id"])

    assert result["error"] == "api"
    assert result["status"] == 500
    assert out["handle"] in tools.fake.store["note"]
