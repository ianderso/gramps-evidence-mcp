# Changelog

All notable changes to this project are recorded here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses
[semantic versioning](https://semver.org/). The tool surface is the public
interface: renaming or removing a tool or a parameter is a major release, and
adding one is a minor release.

## [Unreleased]

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

[Unreleased]: https://github.com/ianderso/gramps-evidence-mcp/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/ianderso/gramps-evidence-mcp/releases/tag/v1.0.0
