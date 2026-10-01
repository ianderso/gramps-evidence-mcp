"""Typed getters, deliberate place creation, statistics and researcher details.

The typed getters exist because ``get_object`` returns the record as stored,
which is right before an edit and wrong for reading. Each of these adds the
one derived number that makes the object interpretable: whether a citation is
orphaned, whether a media file is duplicated, whether a place is parented.
"""

from __future__ import annotations

import pytest


async def _cited_person(tools) -> dict:
    """A person with a cited, placed birth -- enough to reach every getter."""
    return await tools(
        "add_person",
        given="Josiah",
        surname="Pembrook",
        death={
            "type": "Death",
            "date": "1834",
            "place": "Cedar Flat, Brannock, Ohio",
            "citation": {"source_title": "Pension W.12345", "page": "img 3", "confidence": "high"},
        },
    )


# --------------------------------------------------------------------------- #
# get_citation
# --------------------------------------------------------------------------- #
async def test_citation_reports_what_cites_it(tools):
    """A citation attached to a fact is in use, not debris."""
    await _cited_person(tools)
    out = await tools("get_citation", citation="C0001")
    assert out["page"] == "img 3"
    assert out["confidence"] == "high"
    assert out["cited_by_count"] >= 1


async def test_orphan_citation_reports_zero(tools):
    """Zero is the finding: nothing references this, so it is debris."""
    await tools("add_citation", citation={"source_title": "Loose", "page": "p1"})
    out = await tools("get_citation", citation="C0001")
    assert out["cited_by_count"] == 0


# --------------------------------------------------------------------------- #
# get_place
# --------------------------------------------------------------------------- #
async def test_place_created_by_an_event_has_no_parent(tools):
    """The failure mode add_place exists to avoid, pinned as behaviour."""
    await _cited_person(tools)
    out = await tools("get_place", place="P0001")
    assert out["enclosed_by"] == []
    assert out["type"] == "Unknown", "what the server stores for a place given no type"


async def test_deliberately_created_place_is_typed_and_parented(tools):
    """A place made on purpose can be correct from the start."""
    county = await tools(
        "add_place", name="Brannock", place_type="County", title="Brannock, Ohio, USA"
    )
    town = await tools(
        "add_place",
        name="Cedar Flat",
        place_type="Town",
        title="Cedar Flat, Brannock, Ohio, USA",
        parent=county["gramps_id"],
    )
    out = await tools("get_place", place=town["gramps_id"])
    assert out["type"] == "Town"
    assert out["title"] == "Cedar Flat, Brannock, Ohio, USA"
    assert out["enclosed_by"] == [county["handle"]]


async def test_add_place_refuses_a_parent_that_does_not_exist(tools):
    """Minting a parent from a typo is how duplicate hierarchies start."""
    out = await tools("add_place", name="Cedar Flat", parent="P9999")
    assert out["error"] == "not_found"


async def test_add_place_defaults_the_title_to_the_name(tools):
    """A place with no title would never match an event's place string."""
    place = await tools("add_place", name="Cedar Flat")
    out = await tools("get_place", place=place["gramps_id"])
    assert out["title"] == "Cedar Flat"


async def test_coordinates_round_trip(tools):
    """Coordinates are strings in Gramps; do not let them become floats."""
    place = await tools("add_place", name="Cedar Flat", latitude="40.2", longitude="-82.4")
    out = await tools("get_place", place=place["gramps_id"])
    assert out["latitude"] == "40.2"
    assert out["longitude"] == "-82.4"


# --------------------------------------------------------------------------- #
# get_note and get_media
# --------------------------------------------------------------------------- #
async def test_note_text_comes_back_whole(tools):
    """Notes hold reasoning; truncating one loses the argument it records."""
    person = await _cited_person(tools)
    long_text = "The clerk spelled the surname two ways. " * 20
    await tools("add_note", target_type="person", target=person["gramps_id"], text=long_text)
    out = await tools("get_note", note="N0001")
    assert out["text"] == long_text
    assert out["attached_to_count"] == 1


async def test_media_reports_how_many_objects_reference_it(tools, tmp_path):
    """One image cited from several facts is correct; copies are the defect."""
    person = await _cited_person(tools)
    image = tmp_path / "headstone.jpg"
    image.write_bytes(b"fake-jpeg-bytes")
    await tools(
        "attach_media",
        target_type="person",
        target=person["gramps_id"],
        file_path=str(image),
        description="Headstone",
    )
    out = await tools("get_media", media="O0001")
    assert out["description"] == "Headstone"
    assert out["referenced_by_count"] == 1


# --------------------------------------------------------------------------- #
# statistics and researcher
# --------------------------------------------------------------------------- #
async def test_facts_are_returned_unshaped(tools):
    """Contents vary by server version; shaping them here would hide fields."""
    out = await tools("get_facts")
    assert out["facts"] == tools.fake.facts
    assert out["scope"] == "whole tree"


async def test_facts_exclude_living_and_private_people_by_default(tools):
    """Any record can be held by a living person, not only "youngest living".

    So the server must leave them out before computing, which is what its
    living proxy does. Redacting the answer afterwards could not catch them.
    """
    out = await tools("get_facts")
    sent = tools.fake.facts_params[-1]
    assert sent["living"] == "ExcludeAll"
    assert sent["private"] == "true"
    assert out["living_and_private"] == "excluded"


async def test_facts_include_everyone_when_privacy_is_off(tools):
    tools.service.config.expose_private = True
    out = await tools("get_facts")
    sent = tools.fake.facts_params[-1]
    assert "living" not in sent and "private" not in sent
    assert out["living_and_private"] == "included"


async def test_facts_for_one_person_alone_are_refused_with_the_reason(tools):
    """The API answers a bare anchor with an unexplained 422.

    That was the reported failure: get_facts(person=<a gramps_id>) -> 422, while
    get_person on the same id worked. The endpoint has no per-person
    statistics, so the call is refused before it is sent, saying what to do.
    """
    person = await _cited_person(tools)
    out = await tools("get_facts", person=person["gramps_id"])
    assert out["error"] == "conflicting_arguments"
    assert "person_filter" in out["message"]
    assert tools.fake.facts_params == []


async def test_facts_over_ancestors_send_the_anchor_as_a_handle(tools):
    person = await _cited_person(tools)
    out = await tools("get_facts", person_filter="ancestors", person=person["gramps_id"])
    assert "error" not in out
    sent = tools.fake.facts_params[-1]
    assert sent["person"] == "Ancestors"
    assert sent["handle"] == person["handle"]
    assert out["scope"] == f"Ancestors of {person['gramps_id']}"


async def test_a_built_in_filter_without_an_anchor_is_refused(tools):
    out = await tools("get_facts", person_filter="Descendants")
    assert out["error"] == "no_target"
    assert tools.fake.facts_params == []


async def test_a_custom_filter_is_passed_by_name_and_takes_no_anchor(tools):
    out = await tools("get_facts", person_filter="Direct line")
    assert tools.fake.facts_params[-1]["person"] == "Direct line"
    assert out["scope"] == "custom filter Direct line"

    person = await _cited_person(tools)
    out = await tools("get_facts", person_filter="Direct line", person=person["gramps_id"])
    assert out["error"] == "conflicting_arguments"


async def test_facts_rank_is_passed_through(tools):
    await tools("get_facts", rank=3)
    assert tools.fake.facts_params[-1]["rank"] == "3"
    out = await tools("get_facts", rank=0)
    assert out["error"] == "invalid_rank"


async def test_facts_outlast_the_default_request_timeout(fake):
    """Measured at 42 s on a real tree with living people excluded.

    The client default is 30 s, which timed the call out every time.
    """
    import respx

    from gramps_evidence_mcp.client import FACTS_TIMEOUT, GrampsWebClient

    seen: list[dict] = []

    def record(request):
        seen.append(request.extensions["timeout"])
        return fake.handle(request)

    with respx.mock(base_url="http://testserver") as router:
        router.route().mock(side_effect=record)
        client = GrampsWebClient("http://testserver", "mcp", "pw", timeout=30.0)
        await client.facts()
        await client.aclose()
    assert seen[-1]["read"] == FACTS_TIMEOUT


async def test_researcher_details_are_readable(tools):
    """They travel inside every export, so they are worth checking first."""
    tools.fake.researcher = {"name": "A. Researcher", "email": "x@example.org"}
    out = await tools("get_researcher")
    assert out["name"] == "A. Researcher"


@pytest.mark.parametrize("tool_name", ["get_place", "get_citation", "get_note", "get_media"])
async def test_typed_getters_report_a_missing_object_cleanly(tools, tool_name):
    """A bad id is not_found, never an exception."""
    argument = tool_name.removeprefix("get_")
    out = await tools(tool_name, **{argument: "X9999"})
    assert out["error"] == "not_found"
