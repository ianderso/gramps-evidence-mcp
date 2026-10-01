# Roadmap

Everything this project set out to build is built: cited writes for every kind
of genealogical claim, the edit and merge tools that keep a tree correctable,
the audit set that finds where the evidence thins out, server-side queries,
reports and verification, DNA matches recorded as cited evidence, and the
read-only reference layer over legacy GEDCOMs. That is 89 tools; the
[README](../README.md#tool-reference) is the reference for what exists, and the
[changelog](../CHANGELOG.md) for what changed.

Nothing is queued. New work starts from a research task the tools cannot do —
open an issue saying what you were trying to record or find, and where the
tools stopped you.

## How a new tool is judged

- **A fact goes in with its evidence.** A tool that records a claim takes a
  citation, or records the claim as `UNSOURCED` so the audit finds it. A claim
  that deserves its own confidence gets its own citation object.
- **A write sends the whole object.** It re-reads the object in full and
  writes it back whole — an edit through `service._mutate()` — so a partial
  payload cannot be built. The sweep in `tests/test_write_safety.py` holds
  every editing tool to this.
- **Bulk output is privacy-filtered.** Anything that returns people it was not
  asked for by id leaves out private records and probably-living people, as
  the README's Privacy section lists tool by tool.
- **A failure is a result, never an exception**, and it says what to do next.
- **The description is written for the model**, and the combined descriptions
  stay under `DESCRIPTION_BUDGET` in `tests/test_tool_contract.py`, because
  they are sent on every session before any work happens. The tool declares
  annotations saying whether it reads, adds, or changes and removes.
- **Undocumented server behaviour gets a dated verification** against a live
  instance, recorded in [PITFALLS.md](PITFALLS.md) or beside the code that
  relies on it. The API's published schema is often silent on what matters —
  which parameters a query language ignores, what an endpoint does with an
  argument it does not know.

## Not planned

- **Importing a file into the tree**, with or without the API's dry run. An
  import brings a legacy tree's claims in wholesale, as tree data, which is
  exactly what the reference layer exists to prevent: there, a legacy claim is
  a hint that leads to a record, and the record is what gets cited. Imports
  remain possible through the Gramps Web interface, as a deliberate act.
- **Face detection** (`/api/media/{handle}/face_detection`). It returns
  rectangles on an image the model cannot see through this server, and Gramps
  Web's own interface already offers it where a person can look.
- **Single-entry composite undo** through `POST /api/transactions/`. That
  endpoint bypasses the server's allocation of handles and gramps_ids and its
  translation of type names, which this client would have to reimplement. One
  logical add appearing as a few undo entries is the smaller cost.
- **Authentication inside the server**, for the HTTP transport. An identity
  proxy in front of it does this better and is already how claude.ai's custom
  connectors expect to authenticate; see the README's Remote access section.
- **Destructive whole-tree endpoints**: deleting every object of a type,
  restoring from an uploaded backup, and tree administration (`repair`,
  `migrate`, `enable`, `disable`). None has a preview, and a tool surface an
  assistant drives should not be one malformed argument away from emptying
  the tree. [ARCHITECTURE.md](ARCHITECTURE.md#api-coverage) lists the rest of
  what is deliberately left out.
