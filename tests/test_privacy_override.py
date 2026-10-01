"""Lifting the privacy filter for one call, at the user's request.

The operator's ``expose_private`` turns the filter off for everything. These
tests cover the other way: a caller passes ``include_private`` on one call,
without touching configuration, and the next call is filtered again.
"""

from __future__ import annotations

import json
import logging

import pytest

from gramps_evidence_mcp.server import mcp

#: Every tool whose bulk output the filter applies to.
FILTERED_TOOLS = {
    "search_people",
    "get_ancestors",
    "get_descendants",
    "query_objects",
    "query_records",
    "list_unsourced_facts",
    "find_duplicates",
    "get_timeline",
    "consolidated_timeline",
    "get_facts",
    "run_report",
    "consult_reference",
    "check_family_links",
}


async def _living(tools) -> dict:
    return await tools(
        "add_person",
        given="Jane",
        surname="Pembrook",
        birth={"type": "Birth", "date": "1990", "citation": {"source_title": "S", "page": "p"}},
    )


async def test_every_filtered_tool_takes_the_opt_in():
    """And no other tool does: an unfiltered tool has nothing to lift."""
    takes = {
        t.name
        for t in await mcp.list_tools()
        if "include_private" in (t.input_schema or {}).get("properties", {})
    }
    assert takes == FILTERED_TOOLS


async def test_the_opt_in_is_off_unless_asked_for():
    defaults = {
        t.name: t.input_schema["properties"]["include_private"].get("default")
        for t in await mcp.list_tools()
        if t.name in FILTERED_TOOLS
    }
    assert set(defaults.values()) == {False}


async def test_a_living_person_is_shown_when_asked_for_and_only_then(tools):
    person = await _living(tools)
    hidden = await tools("query_objects", object_type="person", keys="gramps_id,primary_name")
    assert hidden["results"][0]["redacted"] is True

    shown = await tools(
        "query_objects", object_type="person", keys="gramps_id,primary_name", include_private=True
    )
    assert shown["results"][0]["primary_name"]["first_name"] == "Jane"
    assert shown["redacted_count"] == 0

    # The lift lasted one call.
    again = await tools("query_objects", object_type="person", keys="gramps_id,primary_name")
    assert again["results"][0]["redacted"] is True
    assert person["gramps_id"] == again["results"][0]["gramps_id"]


async def test_search_and_walks_honour_the_opt_in(tools):
    await _living(tools)
    out = await tools("search_people", name="Pembrook", include_private=True)
    assert out["people"][0].get("redacted") is not True


async def test_facts_are_computed_over_everyone_when_asked(tools):
    out = await tools("get_facts", include_private=True)
    sent = tools.fake.facts_params[-1]
    assert "living" not in sent and "private" not in sent
    assert out["living_and_private"] == "included"


async def test_a_report_keeps_gramps_defaults_when_asked(tools):
    out = await tools("run_report", report_id="ancestor_report", include_private=True)
    _, params = tools.fake.report_runs[-1]
    assert "options" not in params
    assert out["privacy_options"] == {}


async def test_the_reference_layer_honours_the_opt_in(tools, tmp_path):
    from pathlib import Path

    from gramps_evidence_mcp import server
    from gramps_evidence_mcp.config import ReferenceFileConfig

    fixture = Path(__file__).parent / "fixtures" / "sample_ancestry.ged"
    server.state.config.reference_files = [
        ReferenceFileConfig(path=fixture, label="Ref", trust="t")
    ]
    server.state.library = None
    try:
        hidden = await tools("consult_reference", name="Smith")
        shown = await tools("consult_reference", name="Smith", include_private=True)
    finally:
        server.state.library = None
    assert hidden["results"][0]["withheld_count"] == 1
    assert shown["results"][0]["withheld_count"] == 0
    assert len(shown["results"][0]["matches"]) == len(hidden["results"][0]["matches"]) + 1


async def test_a_lift_is_logged_by_tool_name(tools, caplog):
    """The operator can see when living people were asked for, and by what."""
    await _living(tools)
    with caplog.at_level(logging.INFO, logger="gramps_evidence_mcp.service"):
        await tools("query_objects", object_type="person", include_private=True)
    assert any("lifted for query_objects" in r.getMessage() for r in caplog.records)
    assert not any("Jane" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("tool", sorted(FILTERED_TOOLS - {"consult_reference"}))
async def test_every_filtered_tool_makes_its_call_inside_the_lift(tools, tool, caplog):
    """A tool whose body forgot the block would accept the argument and
    ignore it. The lift logs the tool's name, so its absence is the tell.
    consult_reference is covered above: it reads files, not the service."""
    from .conftest import assert_reached_body, valid_args

    spec = next(t for t in await mcp.list_tools() if t.name == tool)
    with caplog.at_level(logging.INFO, logger="gramps_evidence_mcp.service"):
        result = await tools(tool, **valid_args(spec, include_private=True))
    assert_reached_body(tool, result)
    assert json.dumps(result)
    assert any(f"lifted for {tool}" in r.getMessage() for r in caplog.records)
