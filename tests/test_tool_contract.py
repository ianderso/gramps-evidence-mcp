"""Contract tests over the registered MCP tool surface.

These assert properties of the tools *as a client sees them*: their names,
their JSON schema, and the size of the description block shipped on every
session. The rest of the suite exercises the service layer, which means a
``Field`` typo or a dropped docstring changes the published contract without
failing a single test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from gramps_evidence_mcp.server import mcp

SNAPSHOT = Path(__file__).parent / "fixtures" / "tool_schema.json"

#: Ceiling on the combined tool descriptions, which are sent to the model on
#: every session. Raise it deliberately, not by accident.
#:
#: History: 20,000 at 53 tools; 24,000 at 62; 28,000 at 82. Per-tool
#: descriptions have stayed around 310 chars throughout, so every rise has
#: been the tool count rather than prose bloat. If this needs raising again,
#: consider whether the surface should be grouped instead -- 25kB of
#: descriptions ships before any work happens.
DESCRIPTION_BUDGET = 28_000


async def _tools() -> list:
    return sorted(await mcp.list_tools(), key=lambda t: t.name)


def _params(tool) -> dict:
    return (tool.input_schema or {}).get("properties", {}) or {}


async def test_every_tool_has_a_description():
    """A tool with no description is invisible to the model choosing tools."""
    missing = [t.name for t in await _tools() if not (t.description or "").strip()]
    assert missing == []


async def test_every_parameter_has_a_description():
    """An undescribed parameter gets guessed at, and guesses write bad data."""
    undocumented = [
        f"{t.name}.{name}"
        for t in await _tools()
        for name, spec in _params(t).items()
        if not (spec.get("description") or "").strip()
    ]
    assert undocumented == []


async def test_no_parameter_leaks_a_python_repr():
    """A FieldInfo or PydanticUndefined in a schema means a broken default."""
    leaked = [
        f"{t.name}.{name}"
        for t in await _tools()
        for name, spec in _params(t).items()
        if "FieldInfo" in json.dumps(spec) or "PydanticUndefined" in json.dumps(spec)
    ]
    assert leaked == []


async def test_tool_names_and_parameters_match_the_snapshot():
    """Renaming a tool or a parameter breaks callers; make it a visible diff.

    Regenerate deliberately with::

        python -m tests.regen_tool_snapshot
    """
    current = {t.name: sorted(_params(t)) for t in await _tools()}
    expected = json.loads(SNAPSHOT.read_text())
    assert current == expected


async def test_every_registered_tool_appears_in_the_readme():
    """A tool the README does not list is a tool nobody will find.

    The reference table is the only place the surface is described for a
    human, and it went stale twice before this test existed.
    """
    import re

    doc = (Path(__file__).parent.parent / "README.md").read_text()
    documented = set(re.findall(r"\| `([a-z_]+)`", doc))
    for pair in re.findall(r"`([a-z_]+)` / `([a-z_]+)`", doc):
        documented.update(pair)
    missing = sorted({t.name for t in await _tools()} - documented)
    assert missing == [], f"tools missing from the README table: {missing}"


async def test_readme_does_not_document_a_tool_that_was_removed():
    """A table entry for a tool that no longer exists is worse than none."""
    import re

    doc = (Path(__file__).parent.parent / "README.md").read_text()
    documented = set(re.findall(r"\| `([a-z_]+)`", doc))
    for pair in re.findall(r"`([a-z_]+)` / `([a-z_]+)`", doc):
        documented.update(pair)
    names = {t.name for t in await _tools()}
    # Only consider entries that look like tool names, not field names.
    stale = sorted(
        n
        for n in documented - names
        if n.count("_") >= 1
        and (
            n.startswith(
                (
                    "add_",
                    "get_",
                    "list_",
                    "update_",
                    "cite_",
                    "find_",
                    "query_",
                    "verify_",
                    "merge_",
                    "detach_",
                    "set_",
                    "tag_",
                    "link_",
                    "attach_",
                    "export_",
                    "undo_",
                    "search_",
                    "consult_",
                    "reindex_",
                    "event_",
                    "db_",
                    "ocr_",
                    "uncite",
                    "delete_",
                )
            )
        )
    )
    assert stale == [], f"README documents tools that do not exist: {stale}"


async def test_description_block_stays_within_budget():
    """Every byte here is spent on every session, before any work happens."""
    total = sum(len(t.description or "") for t in await _tools())
    assert total <= DESCRIPTION_BUDGET, (
        f"tool descriptions total {total} chars, over the {DESCRIPTION_BUDGET} budget"
    )


async def test_required_parameters_have_no_default():
    """A required parameter with a default is a contradiction in the schema."""
    bad = []
    for tool in await _tools():
        schema = tool.input_schema or {}
        for name in schema.get("required", []):
            if "default" in (schema.get("properties", {}).get(name) or {}):
                bad.append(f"{tool.name}.{name}")
    assert bad == []


@pytest.mark.parametrize(
    "tool_name",
    [
        "add_person",
        "add_family",
        "add_event_to_person",
        "add_event_to_family",
    ],
)
async def test_fact_tools_expose_the_citation_escape_hatch(tool_name):
    """Every fact-recording tool must offer require_citation, and default True.

    If one ever ships without it, uncited facts land with nothing to audit.
    """
    tool = next(t for t in await _tools() if t.name == tool_name)
    spec = _params(tool).get("require_citation")
    assert spec is not None, f"{tool_name} has no require_citation"
    assert spec.get("default") is True


async def test_no_schema_carries_an_auto_generated_title():
    """Titles are ~11% of the published block and say nothing.

    Pydantic derives one from each field's own name, so a property called
    ``source`` ships ``"title": "Source"`` and the argument wrapper ships
    ``"title": "add_personArguments"``. ``compact_schemas`` strips them at
    import; this asserts none creeps back through a new tool.
    """

    def schema_titles(node, path=""):
        """Yield every ``title`` *keyword*, ignoring parameters named title."""
        if isinstance(node, dict):
            if isinstance(node.get("title"), str):
                yield f"{path}.title"
            for keyword, value in node.items():
                if keyword in ("properties", "$defs", "definitions"):
                    if isinstance(value, dict):
                        for name, sub in value.items():
                            yield from schema_titles(sub, f"{path}.{keyword}.{name}")
                elif keyword != "title":
                    yield from schema_titles(value, f"{path}.{keyword}")
        elif isinstance(node, list):
            for item in node:
                yield from schema_titles(item, path)

    found = [
        t for tool in await _tools() for t in schema_titles(tool.input_schema or {}, tool.name)
    ]
    assert found == []


async def test_compaction_actually_removed_something():
    """A no-op optimisation should not be left in place looking like one."""
    from gramps_evidence_mcp.server import SCHEMA_CHARS_SAVED

    assert SCHEMA_CHARS_SAVED > 5_000


async def test_compaction_is_idempotent():
    """It runs at import; running it again must not corrupt the schemas."""
    from gramps_evidence_mcp.server import compact_schemas

    assert compact_schemas() == 0


async def test_tools_still_validate_their_arguments_after_compaction():
    """Titles are documentation-only, so validation must be unaffected.

    A tool given a wrong-typed argument must still be rejected -- validation
    runs against the pydantic models, not the published schema, but that is
    worth proving rather than assuming.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError):
        await mcp.call_tool("get_ancestors", {"person": "I0001", "generations": "not-a-number"})


async def test_every_tool_refuses_a_parameter_it_does_not_define(tools):
    """A misnamed argument must fail loudly, on every tool.

    Reported from real use: ``query_objects(object_type="source",
    query="1870 census")`` was accepted, the filter discarded -- the
    parameter is ``gql`` -- and the first 200 sources returned as though they
    matched. No tool is skipped: on a write tool the same defect records
    something other than what the caller asked for.

    Runs against the in-memory fake, not a bare ``mcp.call_tool``. Should the
    refusal ever regress, every tool body runs for real, write tools
    included, and they must not find a live tree or the network.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    from .conftest import valid_args

    accepted = []
    for tool in await _tools():
        try:
            await tools(tool.name, **valid_args(tool, not_a_parameter="x"))
        except ToolError as exc:
            assert "has no parameter 'not_a_parameter'" in str(exc), tool.name
            continue
        accepted.append(tool.name)
    assert accepted == [], f"accepted an undefined parameter: {accepted}"


async def test_the_refusal_names_what_the_tool_does_take(tools):
    """So a caller that guessed a name can correct itself in one step.

    The call from the report, verbatim.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await tools("query_objects", object_type="place", query="all")
    message = str(exc.value)
    assert "query_objects has no parameter 'query'" in message
    # Exactly the published parameters, gql among them.
    tool = next(t for t in await _tools() if t.name == "query_objects")
    assert f"It takes: {', '.join(sorted(_params(tool)))}." in message


async def test_a_write_tool_refuses_before_writing_anything(tools):
    """A dropped argument on a write tool records the wrong thing, not nothing.

    ``add_person`` takes a birth as ``birth``. A caller that guessed
    ``birth_date`` used to get a person created with no birth at all, and a
    success result to go with it. The refusal has to land before the write.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await tools("add_person", given="Mercy", surname="Ashbee", birth_date="1790")
    assert "has no parameter 'birth_date'" in str(exc.value)
    assert not any(tools.fake.store.values()), "something was written"


async def test_every_published_schema_forbids_additional_properties():
    """A client that validates against the schema can refuse before sending."""
    loose = [
        t.name
        for t in await _tools()
        if (t.input_schema or {}).get("additionalProperties") is not False
    ]
    assert loose == []


async def test_refusing_unknown_arguments_is_idempotent():
    """It runs at import; running it again must not wrap the models twice."""
    from gramps_evidence_mcp.server import refuse_unknown_arguments

    assert refuse_unknown_arguments() == 0


def test_no_document_names_a_specific_deployment():
    """Docs must not assume the author's own hardware or hosting.

    A reader running Gramps Web on a laptop, a VPS or a Kubernetes cluster
    should not have to mentally translate from someone else's NAS.
    """
    import re
    from pathlib import Path

    root = Path(__file__).parent.parent
    banned = re.compile(
        r"truenas|synology|unraid|qnap|proxmox|homelab|\byourdomain\b|\bmydomain\b",
        re.I,
    )
    offenders = []
    for doc in [
        root / "README.md",
        *(root / "docs").glob("*.md"),
        root / "gramps_mcp.example.toml",
        root / ".env.example",
    ]:
        if not doc.exists():
            continue
        for number, line in enumerate(doc.read_text().splitlines(), 1):
            if banned.search(line):
                offenders.append(f"{doc.name}:{number}: {line.strip()[:70]}")
    assert offenders == []


# --------------------------------------------------------------------------- #
# Unknown keys inside the nested inputs are refused too
# --------------------------------------------------------------------------- #
async def test_an_unknown_event_key_is_refused_before_writing(tools):
    """Found while porting the argument refusal: one level down, same gap.

    ``{"type": "Birth", "when": "1790"}`` -- the key is ``date`` -- wrote a
    person with a birth that had no date, and reported success.
    """
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await tools(
            "add_person", given="Mercy", surname="Ashbee", birth={"type": "Birth", "when": "1790"}
        )
    message = str(exc.value)
    assert "EventInput has no field 'when'" in message
    assert "date" in message.split("It takes:")[1]
    assert not any(tools.fake.store.values()), "something was written"


async def test_an_unknown_citation_key_is_refused_before_writing(tools):
    """``pages`` for ``page`` used to create a citation with no page."""
    from mcp.server.mcpserver.exceptions import ToolError

    with pytest.raises(ToolError) as exc:
        await tools("add_citation", citation={"source_title": "X", "pages": "p. 4"})
    assert "CitationInput has no field 'pages'" in str(exc.value)
    assert not any(tools.fake.store.values()), "something was written"


async def test_the_nested_input_schemas_forbid_additional_properties():
    """Published, so a validating client refuses the bad key before sending."""
    loose = []
    for tool in await _tools():
        for name, sub in ((tool.input_schema or {}).get("$defs") or {}).items():
            if sub.get("type") == "object" and sub.get("additionalProperties") is not False:
                loose.append(f"{tool.name}.{name}")
    assert loose == []


# --------------------------------------------------------------------------- #
# Annotations: what a client reads to decide which calls need approval
# --------------------------------------------------------------------------- #
async def test_every_tool_declares_whether_it_writes():
    """Without annotations a client must treat every tool alike."""
    missing = [
        t.name
        for t in await _tools()
        if t.annotations is None or t.annotations.read_only_hint is None
    ]
    assert missing == []


@pytest.mark.parametrize(
    ("prefixes", "read_only", "destructive"),
    [
        (("get_", "list_", "search_", "query_", "find_", "consult_"), True, None),
        (("add_", "cite_", "attach_", "tag_", "link_", "create_"), False, False),
        (("update_", "set_", "delete_", "merge_", "undo_", "detach_", "uncite"), False, True),
    ],
)
async def test_annotations_agree_with_the_tool_name(prefixes, read_only, destructive):
    """A name that says "read" on a tool annotated as a write, or the reverse,
    is how a client ends up auto-approving a delete."""
    wrong = [
        t.name
        for t in await _tools()
        if t.name.startswith(prefixes)
        and (
            t.annotations.read_only_hint is not read_only
            or (destructive is not None and t.annotations.destructive_hint is not destructive)
        )
    ]
    assert wrong == []
