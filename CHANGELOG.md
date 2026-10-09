# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/). The tool surface is the public
interface: renaming or removing a tool or a parameter is a major release, and
adding one is a minor release.

## [Unreleased]

### Changed

- The repository was recreated on 2026-10-09 with its history rewritten, so
  every commit from 1.0.1 on has a new hash, and links to an old commit no
  longer resolve. GitHub keeps the tag names v1.0.0 to v2.2.0 reserved for
  the original immutable releases, so those versions are tagged
  `release-1.0.0` to `release-2.2.0` here, with no GitHub release pages; the
  links below compare those tags. Versions 1.0.0 to 2.2.0 are unchanged on
  PyPI, and releases from here on are tagged `v*` as before.

## [2.2.0] — 2026-10-06

### Added

- `ocr_media` is a router. It reads a document image with the engine that
  suits it, chosen by the new `doc_type` (`print`, `hand`, `table`, `volume`)
  and the existing `lang`, and returns the text with `provenance`
  (`engine`, `model`, `date`):
  - **print**: text the media already carries -- a Transcript note, a PDF's
    text layer -- or the Library of Congress's or the Internet Archive's OCR
    for a page a URL in its sources names; otherwise Tesseract, through Gramps
    Web.
  - **English handwriting**: the image itself, as MCP image content scaled to
    what a current Claude model reads whole, with a diplomatic-transcription
    instruction (spelling as written, line breaks and abbreviations kept,
    `[?]` where unsure). `second_witness` adds a Transkribus reading and asks
    for every name and number where the two differ.
  - **German handwriting**: Transkribus only. Without it, refused; never a
    vision read, even when asked (48.8 % character error on historical
    German, METATR 2026).
  - **Norwegian and other handwriting**: Transkribus (NorHand, or Text Titan
    II), with the image to check it; without Transkribus, the image and a
    warning of its error rate.
  - **table**: not read; the result says to use FamilySearch's index
    (`get_records_on_image`), and why. **volume**: FamilySearch Full-Text
    Search first, then Transkribus page by page.

  New parameters, all optional: `doc_type`, `engine` (`auto`, `existing`,
  `tesseract`, `vision`, `transkribus`), `second_witness`, `spend_credits`,
  `store`, `page` (of a PDF) and `region` (part of the page, at full detail).
  `store` keeps a machine reading on the media object as a Transcript note
  headed with its provenance, and Transkribus' PAGE XML in a second note,
  through `_mutate()`; a later print read returns the stored one.
- Transkribus, optional, configured from the environment:
  `GRAMPS_MCP_TRANSKRIBUS_USERNAME`, `GRAMPS_MCP_TRANSKRIBUS_PASSWORD`,
  `GRAMPS_MCP_TRANSKRIBUS_PAGE_BUDGET` and `GRAMPS_MCP_TRANSKRIBUS_API_URL`,
  declared in `server.json`. Every page costs credits (about 0.5, EUR 0.12),
  so a page is sent only with `spend_credits` or within the monthly page
  budget, which defaults to none; a page is counted before it is sent, and
  not sent if it cannot be counted. Each result reports what it spent, and a
  job is reused, not paid for twice, within the day Transkribus keeps it; a
  failed job is dropped. A private media object is never sent, nor a page in
  a language no model here covers. **The Transkribus path is built to the
  published Metagrapho v1 API and its READ-COOP login, and tested against
  fixtures of those shapes; it has not been exercised against the live
  service.**
- `docs/PITFALLS.md` section 31: Gramps Web's OCR answers with a task on a
  server with a task queue, reads image files only, and serves thumbnails as
  AVIF. The live suite checks the last two; the first needs a task queue,
  which its throwaway server does not run.
- Dependencies: Pillow (12.3.0 or later) to scale, crop and convert page
  images, and pypdf (6.19.0 or later) to read a PDF's text layer and scans.

### Changed

- `ocr_media` is annotated as neither read-only nor closed-world: it can fetch
  from the Library of Congress and the Internet Archive, spend Transkribus
  credits, and with `store` add a note. A client that approved it
  automatically as a read now asks. Today's calls, `ocr_media(media, lang)`,
  work as before and answer the same keys (`handle`, `media`, `lang`, `text`,
  `caveat`) and more; `media` in the answer is now the gramps_id. A print
  read now returns text the media already carries before running Tesseract.
- The unit tests' fake stores an uploaded media file and serves it back,
  renders thumbnails as AVIF, and answers OCR as the server does: text served
  as `text/html`, `{}` for a file that is not an image, 422 without `lang`,
  501 without Tesseract, and a task when it plays a server with a task queue.

### Fixed

- `ocr_media` returned no text on a server with a task queue, as a live
  gramps-webapi 3.21.1 was seen to be: the server answers 202 with a task to
  poll, and the tool handed back `{"task": {...}}` as the text. It now polls
  the task. And a page whose text was a bare number, such as "1850", came
  back as the integer, because text served as `text/html` was parsed as JSON.

## [2.1.0] — 2026-10-06

### Added

- `set_family_parent`: set, replace or remove the father or mother of an
  existing family, so a wife a record names joins the family her husband and
  children are already in, rather than a second one. Both sides of each
  person's link are kept: an old parent's missing link is restored first,
  because without it gramps-webapi refuses the write (400), and both people
  are written afterwards to hold the family once or not at all. A different
  parent already in the role is replaced only with `replace=True`.
  `docs/PITFALLS.md` section 15.
- `move_child`: move a child to another family with the link's citations,
  notes and privacy, placed in birth order, optionally with a new `frel` or
  `mrel`. Detaching and re-adding lost both and put the child last. The new
  family is written first, so a failure part-way leaves the child in both
  families, never neither; then every link to the old family goes, a
  duplicated one too, and the new family takes its place in the child's own
  list, whose first entry Gramps reads as the main parents.
- `update_event_ref`: change a person's reference to a shared event in place
  -- its role, or an attribute set, added or removed by name -- keeping an
  attribute's citations and notes. Nothing could change one before.
- `add_event_ref` takes `attributes`, for what is the person's own in a shared
  event: their line on a census sheet (`As enumerated`), their age. Gramps
  keeps no list of the attribute names used on event references
  (`docs/PITFALLS.md` section 29), so a name is matched against the standard
  ones, the tree's custom ones and those on the event's other references; one
  matching none is written and reported as new, and one close to a known name
  is refused as a likely typo unless `allow_new_type`.
- `add_family`'s `children` takes `{person, frel, mrel}` beside a plain id, so
  a stepchild needs no second call.
- `get_record_history` works on gramps-webapi 3.21. It read 3.22's per-record
  history only, and answered `unsupported_server` on 3.21; there it now reads
  the whole transaction log, newest first, back to the record's creation, and
  says so (`read_from`, `log_complete`). That is slow on a large log -- most
  of a minute for an old record in 54,000 transactions.
  `docs/PITFALLS.md` section 30.
- `get_record_history(field=...)`: only the writes that changed one field of
  the stored record, each with its value before and after --
  `parent_family_list`, or a nested `primary_name.first_name` -- which is
  what finding the write behind a defect needs.

### Changed

- A write answered with a 5xx says what it wrote. gramps-webapi answers 500
  both for a write it rolled back and for one it committed before a later
  step failed, so the tool looks: a create by its handle, which the client
  now makes (`docs/PITFALLS.md` section 28), and an edit by reading the
  record again. The error keeps `"error": "api"` and adds `written`: `false`
  when nothing was written, so a retry is safe, or `null` when the write may
  have landed. A create that landed is taken as done. `add_note` whose
  attachment wrote nothing removes the new note again, so the call writes
  all or nothing.
- `detach_object`'s refusal to take only a person's side of a two-sided
  parent link points at `set_family_parent`.

### Fixed

- `add_note` answered HTTP 500 for a long note, wrote nothing, and said only
  "HTTP 500" (TOOL-REQUESTS #28). The length was not the cause: there is no
  limit on a note's text, and minutes later the session that met it wrote a
  6,000-character note in one call and the full 8,000 by an edit. The three
  failed requests each reached the database
  and none was committed, which on a SQLite tree is the commit's five-second
  wait for a lock running out while another request holds it
  (`docs/PITFALLS.md` section 6). The error now says that nothing was
  written and that retrying is safe.

## [2.0.0] — 2026-10-05

### Added

- gramps-webapi 3.23.1 is tested on every CI run, beside 3.21.1 and 3.22.3.
  The tools work on it unchanged. 3.22.3 stays in the matrix as the last
  release before 3.23 changed what a write may carry.
- Research tasks, in Gramps Web's own task list (its Tasks view):
  `add_research_task`, `list_research_tasks` and `update_research_task`. A
  task is written exactly as Gramps Web's New Task form writes one -- a
  Source tagged `ToDo`, with `Priority` and `Status` source attributes and
  its description in a To Do note -- so a task made by a tool and one made
  by hand are the same thing, and each shows where the other does.
  `update_research_task` replaces an attribute rather than appending one:
  Gramps Web reads the first `Status`, so a second one, all `add_attribute`
  could add, changed nothing anyone saw. A private task's description note is
  private too, which the form does not do: `add_research_task(private=True)`
  and `update_research_task(private=True)` mark the note, a note created or
  edited on a private task is made private, and making a task public leaves a
  private note private unless `private_note=False` asks otherwise.
  `docs/PITFALLS.md` section 27 records the shape and where it was read.
- `list_jobs` and `get_job`: the background-job tools under names that say
  what they are (see Removed).

### Changed

- `docs/PITFALLS.md` sections 18 and 24 say what 3.23 changed. A key the
  object's class lacks is refused with 400 rather than stored, and a date's
  served `year` written back is dropped rather than served stale. A stray key
  or a stale year that an older server stored is still served by 3.23, and
  an object read with a stray key cannot be written back as read; the tools
  remove the one this server ever wrote, a place's `type`, before writing.
  The live tests for both sections, and the unit tests' fake, follow the
  version they run against.
- The REST client no longer takes `oql`, which gramps-webapi 3.23 removed.
  No tool used it.
- `reindex_search`'s description named a `search_text` tool this server has
  never had, and said nothing refreshes the index after a write; the server
  updates it after every write, in the background. It now says what the
  index is for and when a rebuild helps.
- Tool descriptions are published as Python 3.13 compiles a docstring, on
  every Python: 3.11 and 3.12 kept four spaces of indentation on every line,
  which every session was sent and the description budget counted -- 29,736
  characters on 3.11 for what was 28,016 on 3.13.

### Removed

- **Breaking:** `list_tasks` and `get_task` are renamed `list_jobs` and
  `get_job`, with no alias. They report the server's background jobs --
  undo, verification, reindex, reports -- and were easily taken for the
  research task list. `list_jobs` answers `job_count` and `jobs` where
  `list_tasks` answered `task_count` and `tasks`; `get_job` takes the same
  `task_id` and answers as `get_task` did. Results that said to poll
  `get_task` now say `get_job`. A client or script calling the old names must
  change, so this is a major release.

### Fixed

- `query_objects` with `~` on a list field -- `urls ~ "http"`,
  `attribute_list ~ "blm.gov"` -- returned no rows and no error: GrampsQL
  compares the list itself, asking whether the value is one of its items.
  Such a query is refused now, as `gql_list_field`, naming the form that
  works: `urls.any.path ~ "http"`, `note_list.any.get_note.text.string ~ "x"`.
  A test for one handle in a list of handles (`tag_list ~ "<handle>"`) still
  runs, and says what it asked when it finds nothing. The tool's description
  and `docs/PITFALLS.md` section 7 teach `.any.` and `get_<type>`.
- An error with no message reached the caller as `{"error": "unexpected",
  "message": ""}`. The commonest was a request that outran the timeout:
  httpx's timeouts carry no text. A timeout is now `timeout`, and a failed
  connection `connection`, each saying what to do, and any other error
  names its kind when it has no message.
- A GrampsQL read is allowed 120 seconds rather than the 30 every request
  had. The server tests every object in the collection in Python, and an OR
  of two list conditions over 6,092 citations took 29 to 38 seconds.
- An API error reports the server's own message. gramps-webapi nests it
  (`{"error": {"code": 422, "message": ...}}`), and the tools showed the
  whole structure; an empty body is reported as such.

## [1.2.0] — 2026-10-03

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
- `allow_new_type` on every tool that writes a type name, to create a new
  custom type deliberately (see Changed).
- Contract tests for merges of every kind against a real server. The unit
  tests' fake now merges as Gramps does, and records each record's history.
  Its type vocabularies are Gramps' standard names, recorded from a real
  server and checked against one, and the custom names its stored objects
  carry, as Gramps keeps them.

### Fixed

- `add_person`'s birth and death, and `add_family`'s marriage, take no type
  when it is the obvious one, as their descriptions always said; the schema
  required it, so a call that believed the description was refused.
- `add_person` given a birth of another type, a Baptism say, made it the
  person's birth until the server unset it on the next edit. It is kept as an
  event and not made the birth, as the server itself computes it.
- A type name in another case -- an event created as "census" -- was stored
  as a new custom type beside Census, which type filters and Gramps' own
  birth logic never see. Every type name is now spelt as Gramps spells it
  (see Changed).
- `add_url`'s default type, and the type of the URL `add_repository` records,
  was "Web Home Page", which Gramps does not have: every such URL made a
  custom type. Both are now Web Home.
- `query_records` refuses the forms gramps-webapi answers wrongly, and says
  what works instead: `type` as a plain column, which matches nothing; a
  date's `year`, which is not stored; and a list compared with anything but
  `in`, which crashes the server.

### Changed

- Every type name a tool writes -- an event's type, a role, a family's or a
  child's relationship, a name's, place's, note's, repository's, medium's,
  attribute's or URL's type -- is matched against Gramps' standard names and
  the tree's custom ones (`GET /api/types/`), ignoring case, spacing and
  punctuation, then through a short list of unambiguous synonyms ("Born" is
  Birth, "microfilm" Film). A name that matches nothing is refused as
  `unknown_type` with the closest names, where 1.1 stored it as a new custom
  type; `allow_new_type` creates one when it is meant. A standard type the
  tree has never used is accepted like any other.
- `docs/PITFALLS.md` section 6 is corrected: the server's `If-Match` support
  cannot be used by any client, since a read's ETag never matches what a write
  checks. 1.1.0 said a stale tag is refused, implying a fresh one is accepted.
  A live test now fails the day gramps-webapi fixes it, so `_mutate()` can
  start sending it.
- PITFALLS no longer says every server claim is checked live, and names the
  four that are not. Section 13 states the list-comparison rule in full, and
  sections 25 (what a merge also merges) and 26 (type names are matched
  exactly, and Gramps keeps every custom name for good) are new.
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

[Unreleased]: https://github.com/ianderso/gramps-evidence-mcp/compare/release-2.2.0...HEAD
[2.2.0]: https://github.com/ianderso/gramps-evidence-mcp/compare/release-2.1.0...release-2.2.0
[2.1.0]: https://github.com/ianderso/gramps-evidence-mcp/compare/release-2.0.0...release-2.1.0
[2.0.0]: https://github.com/ianderso/gramps-evidence-mcp/compare/release-1.2.0...release-2.0.0
[1.2.0]: https://github.com/ianderso/gramps-evidence-mcp/compare/release-1.1.0...release-1.2.0
[1.1.0]: https://github.com/ianderso/gramps-evidence-mcp/compare/release-1.0.1...release-1.1.0
[1.0.1]: https://github.com/ianderso/gramps-evidence-mcp/compare/release-1.0.0...release-1.0.1
[1.0.0]: https://github.com/ianderso/gramps-evidence-mcp/tree/release-1.0.0
