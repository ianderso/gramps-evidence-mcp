"""Behavioural tests driven through ``mcp.call_tool``.

Everything here invokes a registered tool the way a client does, so declared
defaults resolve and results must survive JSON serialization. The service-level
suite proves the orchestration; this proves the surface over it.
"""

from __future__ import annotations

import json

import pytest

from gramps_evidence_mcp.client import GrampsApiError
from gramps_evidence_mcp.server import mcp

from .conftest import assert_reached_body, valid_args

#: Tools that never reach the service, so the error-routing sweep skips them.
_NO_SERVICE = {"consult_reference"}


async def _all_tools():
    return sorted(await mcp.list_tools(), key=lambda t: t.name)


# --------------------------------------------------------------------------- #
# Error routing
# --------------------------------------------------------------------------- #
async def test_every_tool_returns_an_error_envelope_instead_of_raising(monkeypatch, tools):
    """A tool must never propagate an exception to the transport.

    An uncaught exception takes down the call rather than telling the model
    what went wrong, and nothing else in the suite checks that a newly added
    tool wrapped its body at all.

    Arguments come from ``valid_args``, and every result is checked by
    ``assert_reached_body`` -- a tool that refuses the sweep's input before
    doing any work fails here rather than passing without being tested.
    """
    from gramps_evidence_mcp import server

    async def boom():
        raise GrampsApiError(500, "exploded", method="GET", path="/api/people/")

    monkeypatch.setattr(server.state, "service_", boom)

    raised: list[str] = []
    unshaped: list[str] = []
    for tool in await _all_tools():
        if tool.name in _NO_SERVICE:
            continue
        try:
            out = await tools(tool.name, **valid_args(tool))
        except Exception as exc:  # noqa: BLE001 - that is the failure we report
            raised.append(f"{tool.name}: {type(exc).__name__}")
            continue
        assert_reached_body(tool.name, out)
        if not isinstance(out, dict) or "error" not in out:
            unshaped.append(tool.name)

    assert raised == [], f"tools raised instead of returning an envelope: {raised}"
    assert unshaped == [], f"tools swallowed a failure silently: {unshaped}"


async def test_error_envelope_never_leaks_record_contents(monkeypatch, tools):
    """Errors are logged and returned without names, dates or places in them."""
    from gramps_evidence_mcp import server

    async def boom():
        raise GrampsApiError(403, "Forbidden", method="PUT", path="/api/people/h1")

    monkeypatch.setattr(server.state, "service_", boom)
    out = await tools("get_person", person="I0001")
    assert out["error"] == "api"
    assert out["status"] == 403
    assert "editor" in out["message"]


# --------------------------------------------------------------------------- #
# Round trips
# --------------------------------------------------------------------------- #
async def test_create_read_roundtrip_through_the_tool_surface(tools):
    """A person created through the tools reads back through the tools."""
    created = await tools(
        "add_person",
        given="Martha",
        surname="Ellery",
        gender="female",
        birth={
            "type": "Birth",
            "date": "12 Jan 1890",
            "place": "Columbus, Ohio, USA",
            "citation": {
                "source_title": "Ohio Birth Certificate #12345",
                "page": "cert. 12345",
                "confidence": "very_high",
            },
        },
    )
    assert created["gramps_id"] == "I0001"

    read = await tools("get_person", person=created["gramps_id"])
    assert read["name"] == "Martha Ellery"
    birth = next(e for e in read["events"] if e["type"] == "Birth")
    assert birth["citation_count"] == 1


async def test_every_tool_result_is_json_serializable(tools):
    """A datetime or Path in a result fails only at the wire; catch it here."""
    await tools("add_person", given="A", surname="B")
    for name, args in (
        ("db_stats", {}),
        ("list_tags", {}),
        ("search_people", {"name": "A"}),
        ("get_person", {"person": "I0001"}),
        ("list_object_types", {}),
        ("list_unsourced_facts", {}),
        ("list_transactions", {}),
    ):
        out = await tools(name, **args)
        json.dumps(out)  # raises if the tool returned something unserializable


async def test_defaults_resolve_rather_than_arriving_as_fieldinfo(tools):
    """Calling a tool function directly hands it FieldInfo; the wire must not.

    ``get_ancestors`` compares its ``generations`` default numerically, so an
    unresolved default raises TypeError rather than walking the tree.
    """
    await tools(
        "add_person",
        given="Root",
        surname="Person",
        death={"type": "Death", "date": "1899", "citation": {"source_title": "S", "page": "p"}},
    )
    out = await tools("get_ancestors", person="I0001")
    assert "error" not in out
    assert out["name"] == "Root Person"


# --------------------------------------------------------------------------- #
# Evidence-model invariant
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("tool_name", "args"),
    [
        ("add_person", {"given": "A", "surname": "B", "birth": {"type": "Birth", "date": "1900"}}),
        (
            "add_event_to_person",
            {"person": "I0001", "event": {"type": "Residence", "date": "1910"}},
        ),
    ],
)
async def test_uncited_fact_is_refused_by_default(tools, tool_name, args):
    """require_citation defaults True, and the refusal reaches the caller."""
    await tools("add_person", given="Seed", surname="Person")
    out = await tools(tool_name, **args)
    assert out["error"] == "citation_required"


async def test_escape_hatch_records_the_fact_and_flags_it(tools):
    """require_citation=False must tag, not silently skip the requirement."""
    await tools(
        "add_person",
        given="Unsourced",
        surname="Person",
        birth={"type": "Birth", "date": "1900"},
        require_citation=False,
    )
    flagged = await tools("list_unsourced_facts")
    assert flagged["unsourced_count"] >= 1
    assert flagged["facts"][0]["reason"] == "tagged-unsourced"


# --------------------------------------------------------------------------- #
# Privacy invariant at the tool boundary
# --------------------------------------------------------------------------- #
async def test_bulk_tools_redact_a_living_person(tools):
    """search_people is bulk output, so a probably-living person is withheld."""
    await tools(
        "add_person",
        given="Living",
        surname="Person",
        birth={"type": "Birth", "date": "2010", "citation": {"source_title": "S", "page": "p"}},
    )
    hits = await tools("search_people", name="Living")
    assert hits["count"] == 1
    assert hits["people"][0]["redacted"] is True
    assert "name" not in hits["people"][0]


async def test_direct_lookup_is_not_redacted(tools):
    """get_person is a deliberate act by the tree's owner, so it answers."""
    await tools(
        "add_person",
        given="Living",
        surname="Person",
        birth={"type": "Birth", "date": "2010", "citation": {"source_title": "S", "page": "p"}},
    )
    out = await tools("get_person", person="I0001")
    assert out["name"] == "Living Person"


async def test_tree_walks_redact_living_relatives(tools):
    """get_descendants is bulk-shaped output and must filter like the rest."""
    await tools(
        "add_person",
        given="Living",
        surname="Child",
        birth={"type": "Birth", "date": "2010", "citation": {"source_title": "S", "page": "p"}},
    )
    out = await tools("get_descendants", person="I0001")
    assert out["redacted"] is True
    assert "name" not in out


async def test_a_person_with_no_dates_is_treated_as_living(tools):
    """Unknown birth and no death errs toward privacy, not disclosure."""
    await tools("add_person", given="Undated", surname="Person")
    hits = await tools("search_people", name="Undated")
    assert hits["people"][0]["redacted"] is True


# --------------------------------------------------------------------------- #
# A tool argument cannot steer a request to another route
# --------------------------------------------------------------------------- #
async def test_a_filter_name_stays_inside_its_path_segment(tools):
    """`quote` kept `/`, and httpx collapses `..`: a crafted filter name once
    made delete_filter send DELETE /api/people/<handle>."""
    person = await tools("add_person", given="Josiah", surname="Pembrook")
    await tools("delete_filter", namespace="people", name=f"../../people/{person['handle']}")
    assert person["handle"] in tools.fake.store["person"]


async def test_dot_segments_are_refused(tools):
    for ref in (".", ".."):
        out = await tools("get_object", object_type="person", ref=ref)
        assert out["error"] in {"invalid_identifier", "not_found"}


def test_segments_are_escaped_whole():
    from gramps_evidence_mcp.client import InvalidIdentifierError, _seg

    assert _seg("../../people/abc") == "..%2F..%2Fpeople%2Fabc"
    for bad in ("", ".", ".."):
        with pytest.raises(InvalidIdentifierError):
            _seg(bad)
