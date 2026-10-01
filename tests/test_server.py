"""Tests for the server layer: error envelopes and the reference tool."""

from __future__ import annotations

import inspect
import json
from pathlib import Path

import pytest
import respx

from gramps_evidence_mcp import server
from gramps_evidence_mcp.client import GrampsApiError
from gramps_evidence_mcp.config import Config, ConfigError, ReferenceFileConfig
from gramps_evidence_mcp.gedcom_ref import ReferenceLibrary
from gramps_evidence_mcp.service import CitationRequiredError, NotFoundError

from .conftest import valid_args

FIXTURE = Path(__file__).parent / "fixtures" / "sample_ancestry.ged"


def test_error_envelope_citation_required():
    env = server._error(CitationRequiredError("needs a citation"))
    assert env["error"] == "citation_required"


def test_error_envelope_not_found():
    assert server._error(NotFoundError("nope"))["error"] == "not_found"


def test_error_envelope_config():
    assert server._error(ConfigError("missing env"))["error"] == "config"


def test_error_envelope_api_permission_hint():
    env = server._error(GrampsApiError(403, "Forbidden", method="POST", path="/api/people/"))
    assert env["error"] == "api" and env["status"] == 403
    assert "editor" in env["message"]


def test_library_consult_flags_sources(tmp_path):
    """ReferenceLibrary.consult (what the consult_reference tool calls) returns
    per-file matches with sourced/unsourced flags and the trust note."""
    cfg = Config(
        api_url="http://x",
        username="u",
        password="p",
        cache_dir=tmp_path,
        reference_files=[ReferenceFileConfig(path=FIXTURE, label="Ancestry", trust="untrusted")],
    )
    library = ReferenceLibrary.from_config(cfg.reference_files, tmp_path)
    results = library.consult("John Smith", approx_birth_year=1899)
    assert results[0]["label"] == "Ancestry"
    assert results[0]["trust"] == "untrusted"
    john = results[0]["matches"][0]
    birth = next(f for f in john["facts"] if f["kind"] == "Birth")
    death = next(f for f in john["facts"] if f["kind"] == "Death")
    assert birth["has_source"] is True
    assert death["has_source"] is False


def test_state_library_from_config(tmp_path):
    """state.library_() builds a library lazily from config."""
    server.state.config = Config(
        api_url="http://x",
        username="u",
        password="p",
        cache_dir=tmp_path,
        reference_files=[ReferenceFileConfig(path=FIXTURE, label="Ref", trust="t")],
    )
    server.state.library = None
    library = server.state.library_()
    assert library.labels == ["Ref"]
    server.state.library = None
    server.state.config = None


def test_error_envelope_ambiguous_place():
    """Its own code, so a caller knows to pass a gramps_id or full title."""
    from gramps_evidence_mcp.service import AmbiguousPlaceError

    assert server._error(AmbiguousPlaceError("two"))["error"] == "ambiguous_place"


def test_error_envelope_multiple_enclosures():
    from gramps_evidence_mcp.service import MultipleEnclosuresError

    env = server._error(MultipleEnclosuresError("dated"))
    assert env["error"] == "multiple_enclosures"


def test_unexpected_error_logs_the_stack_but_not_the_message(caplog):
    """A message can quote record contents; the log holds ids and handles."""
    # Built at run time, as record contents are: the stack quotes source
    # lines, so a literal here would be in the log for the wrong reason.
    name = " ".join(["Jane", "Doe"])
    try:
        raise RuntimeError(f"{name}, born 1992, of 12 Elm Street")
    except RuntimeError as exc:
        env = server._error(exc)
    assert env["error"] == "unexpected"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in logged
    assert "test_unexpected_error_logs_the_stack" in logged
    assert "Jane Doe" not in logged


def test_reference_matches_withhold_probably_living_people(tmp_path):
    """Ancestry exports privatize nobody; the fixture's 1925 child has no death."""
    library = ReferenceLibrary.from_config(
        [ReferenceFileConfig(path=FIXTURE, label="Ancestry", trust="untrusted")],
        tmp_path,
    )
    shown = library.consult("Smith", None, current_year=2026)[0]
    withheld = library.consult("Smith", None, withhold_living=True, current_year=2026)[0]
    assert withheld["withheld_count"] == 1
    assert len(withheld["matches"]) == len(shown["matches"]) - 1
    assert all(m["birth_year"] != 1925 for m in withheld["matches"])


async def test_consult_reference_refuses_an_empty_name(tmp_path):
    """An empty substring matches everyone: a dump of every file."""
    import json

    server.state.config = Config(
        api_url="http://x",
        username="u",
        password="p",
        cache_dir=tmp_path,
        reference_files=[ReferenceFileConfig(path=FIXTURE, label="Ref", trust="t")],
    )
    server.state.library = None
    try:
        result = await server.mcp.call_tool("consult_reference", {"name": " "})
        assert json.loads(result.content[0].text)["error"] == "no_criteria"
    finally:
        server.state.library = None
        server.state.config = None


def test_a_reference_person_with_an_undated_death_is_not_withheld(tmp_path):
    """`1 DEAT Y` with no DATE records a death; the person is not living."""
    ged = tmp_path / "tree.ged"
    ged.write_text(
        "0 HEAD\n1 CHAR UTF-8\n"
        "0 @I1@ INDI\n1 NAME Hattie /Pembrook/\n1 BIRT\n2 DATE 1950\n1 DEAT Y\n"
        "0 @I2@ INDI\n1 NAME Lydia /Pembrook/\n1 BIRT\n2 DATE 1950\n"
        "0 TRLR\n"
    )
    library = ReferenceLibrary.from_config(
        [ReferenceFileConfig(path=ged, label="Test", trust="t")], tmp_path / "cache"
    )
    entry = library.consult("Pembrook", None, withhold_living=True, current_year=2026)[0]
    assert [m["name"] for m in entry["matches"]] == ["Hattie Pembrook"]
    assert entry["withheld_count"] == 1


# --------------------------------------------------------------------------- #
# The gramps-webapi version requirement
# --------------------------------------------------------------------------- #
@pytest.fixture
async def unstarted(fake, tmp_path):
    """The tool surface as a fresh process has it: configured, not yet connected."""
    saved = (server.state.config, server.state.client, server.state.service, server.state.library)
    server.state.config = Config(
        api_url="http://testserver", username="mcp", password="pw", cache_dir=tmp_path
    )
    server.state.client = server.state.service = server.state.library = None

    async def call(tool_name: str, /, **arguments):
        result = await server.mcp.call_tool(tool_name, arguments)
        return json.loads(result.content[0].text)

    with respx.mock(base_url="http://testserver") as router:
        router.route().mock(side_effect=fake.handle)
        try:
            yield call
        finally:
            if server.state.client is not None:
                await server.state.client.aclose()
            (
                server.state.config,
                server.state.client,
                server.state.service,
                server.state.library,
            ) = saved


async def test_every_tool_refuses_a_server_older_than_3_21_naming_it(fake, unstarted):
    fake.metadata["gramps_webapi"]["version"] = "3.20.1"
    outcomes = {}
    for tool in await server.mcp.list_tools():
        result = await unstarted(tool.name, **valid_args(tool))
        outcomes[tool.name] = result.get("error") if isinstance(result, dict) else None
    refused = {name for name, error in outcomes.items() if error == "unsupported_server"}
    needs_the_tree = {
        name for name in outcomes if "service_()" in inspect.getsource(getattr(server, name))
    }
    assert refused == needs_the_tree
    assert len(refused) >= len(outcomes) - 2
    message = (await unstarted("list_tags"))["message"]
    assert "3.20.1" in message and "3.21 or later" in message


async def test_an_upgraded_server_is_used_on_the_next_call(fake, unstarted):
    fake.metadata["gramps_webapi"]["version"] = "3.20.1"
    assert (await unstarted("list_tags"))["error"] == "unsupported_server"
    fake.metadata["gramps_webapi"]["version"] = "3.21.0"
    assert "error" not in await unstarted("list_tags")


@pytest.mark.parametrize("version", ["3.21.0", "3.22.3", "4.0.0.dev1", "", None])
async def test_a_current_or_unstated_version_is_accepted(fake, unstarted, version):
    if version is None:
        del fake.metadata["gramps_webapi"]
    else:
        fake.metadata["gramps_webapi"]["version"] = version
    assert "error" not in await unstarted("list_tags")


async def test_an_unreadable_version_is_retried_rather_than_remembered(fake, unstarted):
    fake.metadata_error = 503
    assert (await unstarted("list_tags"))["error"] == "api"
    assert "error" not in await unstarted("list_tags")
