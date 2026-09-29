# Contributing

Issues and pull requests are welcome. This file says how the project is put
together and what a change is expected to carry.

## Setting up

```bash
git clone https://github.com/ianderso/gramps-evidence-mcp
cd gramps-evidence-mcp
uv sync --extra dev
```

Before sending a change, run what CI runs:

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest
```

The suite runs against an in-memory fake of `gramps-webapi`, served through
[respx](https://lundberg.github.io/respx/), and needs no Gramps Web instance.
It must never reach a real one: a guard in `tests/conftest.py` fails any test
that opens a connection, and the suite ignores any `.env` or `GRAMPS_MCP_*`
setting in your shell.

## Where things live

| Path | What it holds |
| --- | --- |
| `src/gramps_evidence_mcp/server.py` | The tools. Their docstrings and `Field` descriptions *are* the published tool descriptions and schema. Also the annotations and the entry point. |
| `src/gramps_evidence_mcp/service.py` | The genealogy operations: the evidence model, reference resolution, privacy filtering, and `_mutate()`, through which every write goes. |
| `src/gramps_evidence_mcp/client.py` | The `gramps-webapi` REST client: authentication, retries, the endpoints. |
| `src/gramps_evidence_mcp/privacy.py` | The living-person rule and the redacted stub. |
| `src/gramps_evidence_mcp/gedcom_ref.py` | The read-only reference layer over GEDCOM files. |
| `docs/PITFALLS.md` | Where `gramps-webapi` behaves in ways its schema does not say, with the version each was seen on. |
| `docs/ROADMAP.md` | What is planned (nothing), how a new tool is judged, and what will not be built. |
| `tests/conftest.py` | The fake `gramps-webapi`, and the argument builder the whole-surface sweeps use. |
| `tests/test_tool_contract.py` | Tests over the tool surface as a client sees it. |
| `tests/test_write_safety.py` | The sweep that holds every editing tool to the whole-object write rule. |

## What a change carries

**A test that fails without it.** Bug fixes especially: reproduce the bug as a
test first. When the fake answers differently from the real server, fix the
fake too — a fake that accepts what the server rejects is how a bug ships with
a passing test.

**Evidence in, evidence out.** A tool that records a claim takes a citation,
or records the claim as `UNSOURCED`. A claim that deserves its own confidence
gets its own citation object; reusing one re-grades both claims.

**Whole-object writes.** Every mutation goes through `service._mutate()`,
which re-reads the whole object and writes it back whole. The sweep in
`tests/test_write_safety.py` fails any editing tool that PUTs a partial
object; a new editing tool belongs in it.

**Privacy on bulk output.** Anything that returns people it was not asked for
by id leaves out private records and probably-living people unless
`expose_private` is set. Add the tool to the table in the README's Privacy
section, or to the list of what is not filtered, saying why.

**Descriptions written for the model.** A tool's docstring is what a model
reads when choosing and calling it, so write it for that reader. The combined
descriptions have a ceiling (`DESCRIPTION_BUDGET` in
`tests/test_tool_contract.py`), because they are sent on every session before
any work happens. Raise it deliberately, in a pull request of its own, saying
why. Pull requests are squash-merged, so a commit of its own inside a larger
one would not survive the merge.

**Annotations.** A new tool declares whether it reads, adds, or changes and
removes (`READS`, `ADDS`, `EDITS`, `REMOVES` and the rest in `server.py`), so
a client knows which calls to ask about. A contract test checks them against
the tool's name.

**A structured result, never an exception.** Every tool catches its failures
and returns an `error` envelope. Sweep tests call every tool and fail if one
raises.

**A snapshot update, if the surface changed.** Renaming or adding a tool or
parameter changes what callers depend on, so it has to show up as a diff:

```bash
uv run python -m tests.regen_tool_snapshot
```

Commit the regenerated `tests/fixtures/tool_schema.json` with the change, and
add the tool to the README's tables — a test checks that too.

**Dated evidence for claims about the live API.** `gramps-webapi`'s schema is
silent on much of what matters, so behaviour is only trusted once seen. When a
change depends on how the server answers, record what was observed, on which
version and when, in `docs/PITFALLS.md` or beside the code that relies on it.
Never use data from a real tree in a test or a captured payload; invent the
people, places and record numbers.

## What will not be merged

Tools for the whole-tree destructive endpoints, file imports into the tree, and
writing DNA matches. See "Not planned" in [docs/ROADMAP.md](docs/ROADMAP.md).

## Releasing

1. Update `__version__` in `src/gramps_evidence_mcp/__init__.py`; the package
   version is read from there. Set the same version twice in `server.json`,
   once at the top and once on the package; a test holds the three together.
2. Move the changelog's entries under a heading for the new version.
3. Once that pull request is merged, tag the merge commit `vX.Y.Z` and
   publish a GitHub release from the tag.
4. Publishing the release runs `.github/workflows/release.yml`, which builds
   the tag, uploads it to PyPI by Trusted Publishing, and then publishes
   `server.json` to the MCP Registry; there is no token to manage. It refuses
   a tag that does not match `__version__` or `server.json`.
