# Architecture

How the server is put together. [PITFALLS.md](PITFALLS.md) covers
`gramps-webapi` behaviours that constrain the implementation; the
[README](../README.md) covers installation and the tool reference.

## Layers

```
server.py     83 MCP tool functions, their annotations, and the entry point.
              Parameter validation, no business logic.
service.py    Genealogy operations. Enforces the evidence model, resolves
              references, shapes results.
client.py     gramps-webapi REST client. JWT auth, retries, pagination.
mapping.py    Gramps object JSON <-> the dict shapes tools return.
models.py     Pydantic input models shared by the tool signatures.
privacy.py    Living-person assessment and redaction.
gedcom_ref.py Read-only reference layer over legacy GEDCOM files.
config.py     Environment variables and TOML.
```

Tools call the service; the service calls the client. No layer reaches past the
one below it.

## A REST client, never a database client

The server talks to `gramps-webapi` over HTTP and never opens the Gramps
database files. Desktop Gramps takes an exclusive lock on an open tree, so a
second writer would have to either take that lock or corrupt the database.
Making the API server the sole database owner removes the question: the desktop
UI and this server are both clients.

The package imports no `gramps.gen.*` module anywhere, which is the cheap way to
check the property still holds.

## The evidence model

`Repository -> Source -> Citation -> fact`.

Tools that record a fact require a citation, supplied either as an existing
handle or as inline source-and-citation fields that the service creates in one
call. Passing `require_citation=False` does not skip the requirement; it stamps
the event with an `UNSOURCED` attribute so `list_unsourced_facts` finds it
again.

Citations are not shared between claims. `cite_child_link` mints a citation for
the link itself rather than reusing the one on a birth event, because a single
citation object carries one confidence value and two claims rarely warrant the
same one.

## Writes

Every mutation goes through `service._mutate()`, which re-fetches the whole
object, edits the returned dict, and PUTs it back. Sending a partial object, or
one fetched with a `keys=` filter, silently drops the fields that were not
fetched -- see [PITFALLS.md](PITFALLS.md).

Writes are one object at a time. `POST /api/objects/` would bundle several into
a single transaction, but per-object writes keep the Gramps undo history legible
-- one fact per entry. `client.py` carries `create_objects` and
`delete_by_handles` for callers that want the bulk path.

## The reference layer

Legacy GEDCOM exports are parsed from disk and served by `consult_reference` as
untrusted hints. They are never merged and never written to the tree. Each file
is configured with a trust note that travels with every answer.

The parser handles GEDCOM 5.5.1 including the Ancestry dialect (`_APID`,
`_TREE`), reads lazily, and caches parsed output on disk.

## Privacy

`expose_private` defaults to false. Bulk output leaves out private records and
probably-living people -- born under 110 years ago with no recorded death, or
with neither date recorded. Lookups by id stay available: this is a local tool,
not a publishing surface. A single call lifts the filter with
`include_private`: the tool runs its work inside
`service.privacy_lifted()`, which sets a context-local flag for that call only
and logs the tool's name, and every check reads `service.exposing_private`,
which combines the flag with the configuration. The README's Privacy section lists the tools one by
one, including the ones deliberately left unfiltered.

The judgement is made in one place per shape of output:

- Person rows fetched raw, which carry no dates, go through
  `service._restricted_people`: one structured query per 500 people selects
  their birth and death dates and private flag.
- `query_records` adds the same paths to the caller's own `select`, and for a
  family both parents', and strips them from the rows it returns.
- `get_facts` and `run_report` ask the server to exclude living people, since
  the server computes their output from the whole tree.
- The reference layer judges each GEDCOM individual from the years it parsed.

Record contents are never logged. Log lines carry handles, ids and operation
names only, and httpx's per-request log is raised to WARNING because a
GrampsQL filter or a place name travels in the URL.

Ancestry exports do not privatize living people, and can carry a Social Security
number on a living person's `INDI` record. Treat every reference GEDCOM as
sensitive.

## Scope

This server talks to Gramps Web and to local GEDCOM files. Nothing else. Tools
for external record repositories belong in their own MCP servers, which a client
can run alongside this one.

### API coverage

Swept against the full `/api/openapi.json` of gramps-webapi 3.21.1 (137 paths).
Everything genealogically useful is covered. What is left out, and why:

**Deliberately excluded — destructive.**

- `POST /api/objects/delete/` deletes *every* object in a namespace. There is
  no dry run and no undo before the transaction lands. A tool that can empty
  the people table on one malformed argument does not belong on a surface an
  assistant drives.
- `POST /api/importers/{ext}/file/restore` replaces the tree's contents with an
  uploaded backup.
- `POST /api/trees/{id}/enable|disable|migrate|repair` — tree-level
  administration. `repair` is tempting as an audit tool, but it mutates with no
  preview.
- `POST /api/media/archive/upload/zip`.

**Excluded — not this server's job.**

- `/api/users/*`, `/api/token/create_owner/`, `/api/oidc/*` — accounts and
  authentication. Managed in the Gramps Web UI.
- `/api/config/`, `/api/translations/`, `/api/name-formats/`,
  `/api/name-groups/`, `/api/holidays/`, `/api/bookmarks/` — instance
  preferences and UI state.
- `/api/chat/` — Gramps Web's own assistant endpoint.
- `/api/media/{handle}/thumbnail|cropped|tile`, `/api/anniversaries.ics` —
  binary and calendar formats that a stdio tool surface cannot usefully carry.

**Excluded — not planned.** Face detection and the importers, with or without
their dry run. [ROADMAP.md](ROADMAP.md#not-planned) says why.

## DNA

DNA is a different evidence model from the rest of this server, and the tools
say so rather than presenting a match as a citation.

A match proves a biological relationship exists. It does not say which one:
shared centiMorgans constrain the possibilities and rarely resolve them, and
they say nothing about the paper trail. So `get_dna_matches` reports the two
figures an estimate actually rests on -- total shared cM and the largest
single segment, both computed here because the API returns segments without
sums -- and counts the matches with no common ancestor identified. That count
is the open research.

Y-DNA follows the direct paternal line only. A shared terminal clade
corroborates a surname line at a depth no record reaches; it does not prove a
named link.

`parse_dna_segments` exists because of a specific trap: the server answers
unreadable input with zero segments and HTTP 200. Passed through unmarked,
that reads as "this person shares no DNA" rather than "I could not read
that" -- a negative finding instead of a mistake. The tool reports the
difference.

`add_dna_match` records a match as evidence, in the shape Gramps Web itself
writes: an association on the tested person, pointing at the match, with
relationship `DNA` and the segment data in a note on it. The citation on the
association is what makes it evidence. Its source is the test -- the company
and the kit -- its page is where the match was read, and its confidence grades
the match. The match itself asserts only shared DNA; a relationship drawn from
it is a separate claim with its own citation. The segments are parsed before
anything is written, for the same reason `parse_dna_segments` exists, and a
second record of the same pair is refused because another company's report of
one match is the same DNA, and would double its centiMorgans.
[PITFALLS.md](PITFALLS.md#14-a-dna-match-is-an-association) has the storage
details.

## Operational notes

- Gramps Web slows as the tree grows. Long write batches should run in the
  background, and callers should be resumable and idempotent.
- Take a backup before bulk writes. `export_backup` produces a lossless Gramps
  XML file in a few seconds.
