"""Relationship, living assessment, timelines, spans and reindexing.

These read views the server computes rather than data this server stores, so
the tests pin the shaping and the boundaries: what happens when two people
are unrelated, when a timeline is empty, when an index rebuild is dispatched.
"""

from __future__ import annotations

import pytest


async def _two_people(tools) -> tuple[str, str, str, str]:
    """Create two people and return their gramps_ids and handles."""
    a = await tools("add_person", given="Josiah", surname="Pembrook")
    b = await tools("add_person", given="Mercy", surname="Ashbee")
    return a["gramps_id"], b["gramps_id"], a["handle"], b["handle"]


# --------------------------------------------------------------------------- #
# get_relationship
# --------------------------------------------------------------------------- #
async def test_relationship_is_reported_in_words_and_distances(tools):
    """The wording is for reading; the distances are for reasoning."""
    a, b, ha, hb = await _two_people(tools)
    tools.fake.relationships[(ha, hb)] = {
        "relationship_string": "second cousin",
        "distance_common_origin": 3,
        "distance_common_other": 3,
    }
    out = await tools("get_relationship", person1=a, person2=b)
    assert out["relationship"] == "second cousin"
    assert out["generations_to_common_ancestor"] == 3
    assert out["related"] is True


async def test_unrelated_people_report_related_false(tools):
    """A -1 distance means no common ancestor, not a zero-generation one."""
    a, b, _, _ = await _two_people(tools)
    out = await tools("get_relationship", person1=a, person2=b)
    assert out["related"] is False
    assert out["generations_to_common_ancestor"] is None


async def test_all_paths_returns_every_relationship(tools):
    """In an endogamous tree two people are often related more than one way."""
    a, b, ha, hb = await _two_people(tools)
    tools.fake.relationships[(ha, hb)] = [
        {
            "relationship_string": "first cousin",
            "distance_common_origin": 2,
            "distance_common_other": 2,
        },
        {
            "relationship_string": "second cousin",
            "distance_common_origin": 3,
            "distance_common_other": 3,
        },
    ]
    out = await tools("get_relationship", person1=a, person2=b, all_paths=True)
    assert out["path_count"] == 2
    assert out["paths"][0]["relationship"] == "first cousin"


async def test_relationship_accepts_gramps_ids_not_just_handles(tools):
    """Callers work in gramps_ids; resolution is the service's job."""
    a, b, ha, hb = await _two_people(tools)
    tools.fake.relationships[(ha, hb)] = {
        "relationship_string": "father",
        "distance_common_origin": 1,
        "distance_common_other": 0,
    }
    assert (await tools("get_relationship", person1=a, person2=b))["relationship"] == "father"


# --------------------------------------------------------------------------- #
# assess_living
# --------------------------------------------------------------------------- #
async def test_living_verdict_is_returned_plainly(tools):
    """The common case is a yes or no."""
    a, _, ha, _ = await _two_people(tools)
    tools.fake.living[ha] = True
    out = await tools("assess_living", person=a)
    assert out["living"] is True
    assert "estimated_birth" not in out


async def test_explain_adds_the_reasoning_and_its_source(tools):
    """Knowing which relative drove the estimate is what makes it checkable."""
    a, _, ha, _ = await _two_people(tools)
    tools.fake.living[ha] = False
    tools.fake.living_dates[ha] = {
        "birth": "about 1762",
        "death": "about 1834",
        "explain": "Estimated from child's birth",
        "other": {"gramps_id": "I0002"},
    }
    out = await tools("assess_living", person=a, explain=True)
    assert out["estimated_birth"] == "about 1762"
    assert out["explanation"] == "Estimated from child's birth"
    assert out["derived_from"] == "I0002"


async def test_living_assessment_does_not_change_bulk_redaction(tools):
    """The server's opinion is advisory; this server's own filter still rules.

    A person the server calls dead is still withheld from bulk output when
    the local rule says otherwise, because the local rule is what the privacy
    guarantee rests on.
    """
    person = await tools(
        "add_person",
        given="Recent",
        surname="Person",
        birth={"type": "Birth", "date": "2010", "citation": {"source_title": "S", "page": "p"}},
    )
    tools.fake.living[person["handle"]] = False
    assert (await tools("assess_living", person=person["gramps_id"]))["living"] is False
    hits = await tools("search_people", name="Recent")
    assert hits["people"][0]["redacted"] is True


# --------------------------------------------------------------------------- #
# get_timeline
# --------------------------------------------------------------------------- #
async def test_timeline_counts_uncited_events(tools):
    """The count is the audit: how much of this life rests on nothing."""
    a, _, ha, _ = await _two_people(tools)
    tools.fake.timelines[ha] = [
        {
            "gramps_id": "E0001",
            "type": "Birth",
            "date": "1762",
            "age": "0",
            "citations": 2,
            "confidence": 4,
        },
        {"gramps_id": "E0002", "type": "Residence", "date": "1801", "age": "39", "citations": 0},
    ]
    out = await tools("get_timeline", target=a)
    assert out["event_count"] == 2
    assert out["uncited_count"] == 1
    assert out["events"][0]["confidence"] == 4


async def test_timeline_withholds_a_living_relatives_events(tools):
    """Relatives folded into a timeline are bulk output; the anchor is not.

    Offspring are the likeliest to be alive, and their events carry names,
    dates and places.
    """
    anchor = await tools("add_person", given="Josiah", surname="Pembrook")
    child = await tools(
        "add_person",
        given="Jane",
        surname="Pembrook",
        birth={"type": "Birth", "date": "1990", "citation": {"source_title": "S", "page": "p"}},
    )
    elder = await tools(
        "add_person",
        given="Elias",
        surname="Ashbee",
        birth={"type": "Birth", "date": "1850", "citation": {"source_title": "S", "page": "p"}},
    )
    tools.fake.timelines[anchor["handle"]] = [
        {
            "gramps_id": "E0101",
            "type": "Residence",
            "date": "1900",
            "person": {"handle": anchor["handle"]},
            "citations": 1,
        },
        {
            "gramps_id": "E0102",
            "type": "Birth",
            "date": "1990",
            "label": "Birth of Daughter",
            "person": {"handle": child["handle"]},
            "citations": 1,
        },
        {
            "gramps_id": "E0103",
            "type": "Birth",
            "date": "1850",
            "label": "Birth of Father",
            "person": {"handle": elder["handle"]},
            "citations": 1,
        },
    ]
    out = await tools("get_timeline", target=anchor["gramps_id"], offspring=1)
    assert [e["gramps_id"] for e in out["events"]] == ["E0101", "E0103"]
    assert out["withheld_count"] == 1

    tools.service.config.expose_private = True
    out = await tools("get_timeline", target=anchor["gramps_id"], offspring=1)
    assert out["event_count"] == 3


async def test_empty_timeline_is_not_an_error(tools):
    """A person with no events is a normal state for a new entry."""
    a, _, _, _ = await _two_people(tools)
    out = await tools("get_timeline", target=a)
    assert out["event_count"] == 0
    assert out["uncited_count"] == 0


async def test_timeline_rejects_an_unsupported_object_type(tools):
    """Only people and families have timelines; say so rather than 404."""
    a, _, _, _ = await _two_people(tools)
    out = await tools("get_timeline", target=a, object_type="source")
    assert out["error"] == "unsupported_type"


# --------------------------------------------------------------------------- #
# event_span
# --------------------------------------------------------------------------- #
async def test_span_between_two_events(tools):
    """The arithmetic behind every age-at-event plausibility check."""
    person = await tools(
        "add_person",
        given="Spanned",
        surname="Person",
        birth={"type": "Birth", "date": "1762", "citation": {"source_title": "S", "page": "p"}},
        death={"type": "Death", "date": "1834", "citation": {"source_title": "S", "page": "p"}},
    )
    detail = await tools("get_person", person=person["gramps_id"])
    e1, e2 = detail["events"][0]["gramps_id"], detail["events"][1]["gramps_id"]
    resolved = await tools("get_object", object_type="event", ref=e1)
    other = await tools("get_object", object_type="event", ref=e2)
    tools.fake.spans[(resolved["handle"], other["handle"])] = "72 years"
    out = await tools("event_span", event1=e1, event2=e2)
    assert out["span"] == "72 years"


async def test_span_for_undated_events_is_empty_not_an_error(tools):
    """Undated events are normal; the tool must not fail on them."""
    person = await tools(
        "add_person",
        given="Undated",
        surname="Person",
        birth={"type": "Birth", "citation": {"source_title": "S", "page": "p"}},
        death={"type": "Death", "citation": {"source_title": "S", "page": "p"}},
    )
    detail = await tools("get_person", person=person["gramps_id"])
    out = await tools(
        "event_span",
        event1=detail["events"][0]["gramps_id"],
        event2=detail["events"][1]["gramps_id"],
    )
    assert out["span"] == ""


# --------------------------------------------------------------------------- #
# reindex_search
# --------------------------------------------------------------------------- #
async def test_reindex_returns_a_task_to_poll(tools):
    """Reindexing is dispatched; the caller needs the handle to follow it."""
    out = await tools("reindex_search")
    assert out["task_id"] == "reindex1"
    assert "get_job" in out["message"]


@pytest.mark.parametrize(("full", "expected"), [(True, "1"), (False, None)])
async def test_full_flag_is_only_sent_when_set(tools, full, expected):
    """An incremental rebuild is the default; do not force a full one."""
    await tools("reindex_search", full=full)
    assert tools.fake.reindex_calls[-1].get("full") == expected
