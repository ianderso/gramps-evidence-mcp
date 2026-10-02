# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/). The tool surface is the public
interface: renaming or removing a tool or a parameter is a major release, and
adding one is a minor release.

## [Unreleased]

## [1.2.0] — 2026-10-02

What an audit of the documentation and the live suite found open after 1.1.0.

### Added

- `get_record_history`: who added, edited or deleted one record, and when,
  newest first, each change with the transaction `get_transaction` reads. A
  deleted record is found by its handle. It reads the per-record history
  gramps-webapi added in 3.22; on 3.21 it answers `unsupported_server`.
- `merge_objects` says what else a merge merges. Gramps' person merge goes on
  to merge two families left with the same parents, and its family merge
  merges a differing father or mother; the dry run and the result list these
  as `also_merges`. A merge Gramps refuses -- two spouses, a parent and their
  own child -- is reported by the dry run and refused, as `merge_refused`,
  before anything is sent.
- Contract tests for merges of every kind against a real server. The unit
  tests' fake now merges as Gramps does, and records each record's history.

### Fixed

- `add_person`'s birth and death, and `add_family`'s marriage, take no type
  when it is the obvious one, as their descriptions always said; the schema
  required it, so a call that believed the description was refused.
- `add_person` given a birth of another type, a Baptism say, made it the
  person's birth until the server unset it on the next edit. It is kept as an
  event and not made the birth, as the server itself computes it.
- An event created with its type in another case -- "census" -- was stored as
  a new custom type beside Census, which type filters and Gramps' own birth
  logic never see. A created event's type is spelt as the tree spells it; a
  type the tree lacks is still created, as a custom one.
- `query_records` refuses the forms gramps-webapi answers wrongly, and says
  what works instead: `type` as a plain column, which matches nothing; a
  date's `year`, which is not stored; and a list compared with anything but
  `in`, which crashes the server.

### Changed

- `docs/PITFALLS.md` section 6 is corrected: the server's `If-Match` support
  cannot be used by any client, since a read's ETag never matches what a write
  checks. 1.1.0 said a stale tag is refused, implying a fresh one is accepted.
  A live test now fails the day gramps-webapi fixes it, so `_mutate()` can
  start sending it.
- PITFALLS no longer says every server claim is checked live, and names the
  four that are not. Section 13 states the list-comparison rule in full, and
  sections 25 (what a merge also merges) and 26 (type names are matched
  exactly) are new.
- `SECURITY.md` counted 43 read-only tools; there are 45.

## [1.1.0] — 2026-10-01

Fixes for what a day of research sessions against a live tree found the tools
could not do, or did wrong. Behaviour the fixes rely on is recorded in
[docs/PITFALLS.md](docs/PITFALLS.md) sections 15 to 24, and -- like every
other claim there -- is now checked against real gramps-webapi 3.21.1 and
3.22.3 servers on every CI run.

### Added

- `update_child_ref`: change a child's relationship to the father or mother —
  a stepson held as a birth child — in place, keeping the link's citations,
  notes and place in the birth order.
- `check_family_links`: audit person↔family links in both directions — a family
  listed twice, a child listed twice, a link one side lacks, a link to nothing
  — with a repair for each finding. Privacy-filtered.
- `add_event_ref`: share an existing event with another person, in a role,
  instead of copying it.
- `update_alternate_name`: correct, retype or remove one alternate name in
  place. A cited name is not removed.
- `update_citations` and `link_repositories`: the sweeps that needed REST
  scripts, as one call each. Each row is its own write and reports its own
  outcome; `expect_page_prefix` refuses a row whose page changed since the
  sweep was planned.
- `update_event` takes `event_type`, checked against the tree's vocabulary
  (`allow_new_type` for a deliberate new custom type), and `clear_place` and
  `clear_date`.
- Names can be cited: `cite_object(object_type="name", name=...)` on the
  primary or an alternate name, `uncite` likewise, and `add_alternate_name`
  takes a `citation`. `get_person` lists every name with its citation count.
- `update_person` takes `keep_old_as_alternate=False` with a required
  `reason`, for a name split wrongly at entry: the name is corrected in place,
  keeping its citations, and the reason and old form go in a Research note.
- `add_source` takes the repository link's `media_type`, as `link_repository`
  does.
- `detach_object` takes a tag by name, narrows a repository link to one
  `call_number`, and gains `parent_family` and `family` kinds that remove a
  link only the person holds, and an `enclosure` kind that takes an extra
  parent off a place — which `update_place` refuses to touch.
- `merge_objects` takes `enclosures` for places (see Fixed).
- `uncite` and `delete_object` take `carry_to` (see Changed).
- A live test suite, `tests/live`, run in CI against throwaway gramps-webapi
  3.21.1 and 3.22.3 servers installed from PyPI. It checks each server
  behaviour in PITFALLS, drives the tools end to end, and runs the same
  scenarios against the unit tests' fake and the real server, requiring the
  same answers. It found the three fixes below marked *(live suite)*, and
  corrected PITFALLS sections 6, 7, 12, 13 and 19.

### Fixed

- `add_place` wrote the place type to a stray `type` key, leaving the place
  Unknown; `get_place` read the same stray key, so the defect did not show. It
  writes `place_type` now, `get_place` reports `place_type` and flags a stray
  key, and any edit of the place repairs one.
- "from 4 May 1864 to 16 Sep 1864" was stored as a range ("between"); it is a
  span now. A lone "from 1880" or "to 1890" was stored as a plain year, the
  word dropped; it is an open-ended date now on Gramps 5.2 and later, and kept
  as text on an older server.
- `get_event` and every other output showed a range as its first year, and
  dropped "about", "before", "estimated" and the like. Dates are rendered as
  Gramps' English displayer renders them, in ISO form.
- Detaching a child listed twice in its `parent_family_list` left a link only
  the person held. Every remaining link is removed now, and every person write
  removes a repeated family from either list, so the next edit of an affected
  person repairs it.
- A delete answered HTTP 500 after it had landed (the server's search-index
  step) was reported as a failure. It is reported as the delete it was, with
  the late status.
- A place merge left the survivor enclosed by both places' parents. With
  `enclosures="auto"`, the default, an undated parent that encloses another is
  dropped, and a merge that would leave two unrelated undated parents is
  refused before it runs.
- `update_person(name=...)` filed an alternate name even when the name was
  unchanged, because it compared whole stored names with a fresh one.
- `add_event_to_person`, `add_event_to_family` and `add_note` created the event
  or note before resolving the target, so a bad reference left an orphan.
- `add_family` given the same child twice listed them twice.
- Parallel tool calls made after the access token expired -- the first calls
  after a 15-minute pause -- could fail with HTTP 429: each renewed the token,
  and the server takes one renewal a second. The token is renewed once for all
  of them now, and a token request refused for its rate is retried once.
  *(live suite)*
- An edit wrote back the `year` the server adds to every date it serves; the
  server stored it, and served it in place of the date's own year after the
  date changed. Every write drops it now, which also repairs a stale one, and
  says so. *(live suite)*

### Changed

- Every write goes through `_mutate()`, as CONTRIBUTING has said it does. A
  write that changes nothing is skipped, so tagging an already-tagged object or
  setting a flag to its current value no longer lands in the transaction log.
- `uncite(delete_if_orphan=True)` no longer deletes a citation that is the only
  holder of a note or image; it keeps it, says what it holds, and deletes it
  once `carry_to` names a citation to move them to. `detach_object` keeps an
  orphaned object for the same reason.
- `delete_object` refuses to strand a note or image only the object holds
  (`carry_to` moves them first), and refuses a source with citations, which the
  server would delete with it. Its description no longer says a delete leaves
  dangling references: the server removes them.
- `update_event` refuses an empty `place` or `date` rather than treating it as
  a place to resolve or a date to clear; `clear_place` and `clear_date` say so.
- `add_child_to_family` and `link_repository` report a child or link already
  present as no change, and point at `update_child_ref` and `detach_object`.
- gramps-webapi 3.21 or later is required, and on an older server every tool
  answers with an `unsupported_server` error naming its version. 1.0 said it
  was built against 3.20.1, but 3.20 has no structured query endpoint, so
  `query_records` and every privacy-filtered read failed there with HTTP 404
  partway through a call. *(live suite)*

### Security

- Requires PyJWT 2.15.0 or later. Before 2.15.0, a token with a deeply nested
  payload made PyJWT raise a raw `RecursionError` instead of its
  `DecodeError`. PyJWT reaches this server only as a dependency of the MCP
  SDK: the server decodes no JWT itself — its Gramps Web tokens are opaque to
  it — and does not use the SDK's own authentication, so no path from a
  caller to the defect is known. The floor makes sure no install of this
  release runs an affected version, which the lockfile update alone would not:
  the lockfile pins this repository's development environment, not what an
  installer resolves.

## [1.0.1] — 2026-09-29

### Fixed

- The server reports its version to MCP clients when a session starts. It
  sent an empty string, so a client could not show which release it was
  talking to.

### Changed

- Tested against the MCP SDK 2.2.0, pydantic 2.13.5, python-dotenv 1.2.3 and
  pyjwt 2.14.0, which the lockfile now pins. The package's own requirements
  are unchanged, so an installed copy could already use these.

## [1.0.0] — 2026-09-29

The first public release.

### Tools

83 tools: 82 over a Gramps Web tree, and one over local GEDCOM files. The
[README](README.md#tool-reference) describes each.

- Create, each fact with its citation: `add_person`, `add_family`,
  `add_event_to_person`, `add_event_to_family`, `add_child_to_family`,
  `add_alternate_name`, `add_source`, `add_citation`, `add_repository`,
  `add_place`, `add_note`, `add_media`, `attach_media`, `add_attribute`,
  `add_url`, and `add_dna_match`, which records a match cited to the test that
  found it.
- Cite: `cite_event`, `cite_object`, `cite_child_link`, `uncite`.
- Edit and correct: `update_citation`, `update_event`, `update_source`,
  `update_media`, `update_person`, `update_object_fields`, `update_place`,
  `update_url`, `link_repository`, `tag_object`, `set_private`,
  `merge_objects`, `detach_object`, `delete_object`.
- Read: typed getters for every object, timelines, relationships, living
  assessment, event spans, record-holders (`get_facts`), DNA matches and
  Y-DNA, reports, searches and tree walks.
- Audit: `query_objects` (GrampsQL), `query_records` (the structured query
  engine, and the only way to filter events by type), `get_backlinks`,
  `find_duplicates`, `list_unsourced_facts`, `verify_tree`, custom filters,
  and `ocr_media`.
- Operations: `export_backup`, `list_transactions`, `get_transaction`,
  `undo_transaction`, `list_tasks`, `get_task`, `reindex_search`.
- The reference layer: `consult_reference`, over legacy GEDCOM exports as
  untrusted hints.

### Behaviour worth knowing

- Every fact-recording write takes a citation, or records the fact as
  `UNSOURCED` so `list_unsourced_facts` finds it again.
- Every write re-reads the whole object and writes it back whole, so a partial
  payload cannot drop fields the caller never saw.
- With `expose_private` off, the default, bulk output leaves out private
  records and probably-living people, including in timelines, record-holder
  statistics, the reference layer, and every report that has the options for
  it. A lookup by id is not filtered, and any filtered tool shows everyone for
  one call when passed `include_private`. The README lists the behaviour tool
  by tool, and what is not filtered.
- A tool refuses a parameter it does not define, naming the ones it takes, and
  the published schemas say `additionalProperties: false`.
- Every tool declares MCP annotations saying whether it reads, adds, or
  changes and removes.
- `export_backup` creates a new file and never replaces one. `add_media` and
  `attach_media` upload only images, PDFs, audio and video.
- `GRAMPS_MCP_TRANSPORT=http` serves Streamable HTTP for a remote client; the
  server does no authentication of its own.
- A `.env` file is read from the working directory only. Logs carry ids,
  handles and operation names, never record contents.

[Unreleased]: https://github.com/ianderso/gramps-evidence-mcp/compare/v1.1.0...HEAD
[1.1.0]: https://github.com/ianderso/gramps-evidence-mcp/compare/v1.0.1...v1.1.0
[1.0.1]: https://github.com/ianderso/gramps-evidence-mcp/compare/v1.0.0...v1.0.1
[1.0.0]: https://github.com/ianderso/gramps-evidence-mcp/releases/tag/v1.0.0
