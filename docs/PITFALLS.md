# Pitfalls

`gramps-webapi` behaviours that constrain this server's implementation. Read
before adding a tool that mutates, or before calling the API directly.

Found on gramps-webapi 3.20.1. Sections 2, 3, 7, 12 and 13 were re-verified
against 3.21.1 on 2026-09-29, read-only, on a tree of about 850 people.
Sections 15 to 22 come from research sessions against a live tree on
2026-09-30, explained from the gramps-webapi 3.21.1 and Gramps 6.0 source on
2026-10-01; each says which.

## 1. `keys=` plus `PUT` destroys unfetched fields

`PUT` replaces the whole record. An object fetched with `keys=` and PUT back
loses every field outside the key set -- parent handles, events, citations,
notes.

Fetch the full object, mutate what you own, PUT it back. In this codebase that
is structural: every write goes through `service._mutate()`, so a partial
payload cannot be built through the tool surface. Direct `client` calls have no
such protection.

Take a full dump before any bulk write. `export_backup` does it in one call.

## 2. `keys=` also nulls `backlinks`

The filter applies to the whole response:

```python
get_object(..., keys="handle,gramps_id", backlinks=True)  # backlinks: null
```

Every object then looks unreferenced, which reads as a finding rather than a
bug. List `backlinks` in `keys`, or omit `keys` entirely.

`extend="all"` does not imply backlinks. Ask for them explicitly.

## 3. Backlinks are keyed by singular type name

`"citation"`, not the plural REST namespace `"citations"`. Code indexing by the
namespace finds nothing. `service` maps both spellings.

## 4. A citation carries one confidence

Reusing a citation handle for a second claim re-grades that claim to the
original's confidence, silently. A distinct claim needs its own citation
object; `cite_child_link` always mints one.

## 5. Detaching a citation does not delete it

Orphan citations are invisible in the UI and inflate every count. Any pass that
detaches must also delete. `uncite(delete_if_orphan=True)` does both.

## 6. No optimistic locking -- one writer at a time

Two sessions doing read-modify-write on the same object silently lose one edit,
with nothing afterwards to show it happened. `_mutate()` makes an individual
write whole-object safe; it cannot make two sessions safe from each other.

Prolonged concurrent writing has also produced HTTP 500 on every write while
reads stayed healthy. `list_transactions` shows recent write activity -- check
it before starting a write session.

## 7. GrampsQL syntax

GrampsQL runs over raw object JSON, not the profile view.

- Equality is a single `=`. `page == ""` is a 422 parse error.
- `~` is substring match.
- `.length` works on any list field: `media_list.length = 0`.
- **A field the object does not have is not an error. It matches nothing.**
  `bogus_field = 1` returns zero rows with HTTP 200, which reads as a finding.
  Two traps are this rule in disguise:
  - Event `type` is not queryable *here*: `type = "Birth"` matches nothing on
    a tree full of births (744 of them, when first found). Use
    `query_records` instead; see section 12.
  - Sources have no `citation_list`; citations point at sources. On 3.21.1
    `citation_list.length = 0` matches no source, so every source looks cited;
    on 3.20.1 it matched every source, so every source looked uncited. Neither
    is an answer. Find uncited sources through `backlinks`.
- Booleans compare as integers. `private = 1` finds private records;
  `private = true` matches nothing.

Queryable fields verified: `gramps_id`, `page`, `confidence`, `description`,
`desc`, `title`, `checksum`, `private`, `change`, and `<list>.length`.

## 8. A hand-rolled merge must move every list

Moving `citation_list` backlinks and `media_list` to the survivor but not
`note_list` orphans the dropped object's notes. A merge is complete only when
every list it carries is accounted for: citation backlinks, `media_list`,
`note_list`, `attribute_list`, tags, URLs.

`merge_objects` reports what the dropped object carries as `drop_carries` and
dry-runs by default. Prefer it over a raw-API merge.

## 9. Your instance is authoritative for endpoint shapes

`/api/openapi.json` on your own server is the truth. It is how the "no tree
selector" question settles: no data endpoint takes a tree parameter, because
the tree is bound to the account you authenticate as.

## 10. Place resolution matches on title, not name

`add_event_to_person(place="Columbus")` creates a new bare place even when a
place named Columbus exists, because find-or-create matches the place's
**title** ("Columbus, Ohio, USA"). Passing a gramps_id is worse: it
creates a place literally titled `P0000`.

`find_or_create_place` now resolves handle or gramps_id first, then exact
title, then an exact unique name -- ambiguous names raise rather than guess --
and creates only as a last resort. Pass the full title, and verify the event's
`place_handle` afterwards.

## 11. A failed write may have half-landed

A tool call can return an error after having already created and attached
objects. Retrying then duplicates them. Re-read the target after any errored
write; the error is not evidence that nothing was written.

## The shape of both data-loss defects

Each reported success against a value it never wrote. The fix in both cases was
structural -- make the unsafe payload impossible to construct -- not a warning
telling the next caller to be careful. `add_note` follows the same principle:
it re-reads the target after writing and returns `verified: false` rather than
claiming an attachment it cannot demonstrate.

## 12. Event type lives in an integer, not a string

The structured query engine (`POST /api/{type}/query/`) does reach event type,
but not the way the read API suggests. `GET /api/events/{handle}` renders the
type as a plain string, `"Baptism"`. The *stored* object holds a dict:

```json
{"_class": "EventType", "string": "", "value": 15}
```

`string` is empty for every built-in type -- the identity is the integer. So:

- `{"column": "type", ...}` is rejected: `type` is not an allowed column.
- `{"json_path": ["type", "string"]}` matches nothing, because it is empty.
- `{"json_path": ["type", "value"], "op": "eq", "value": 12}` works.

The integers come from `GET /api/types/default/event_types/map`, which is what
`list_event_types` reads and what `query_records(event_type=...)` translates
through. Do not hard-code them.

Verified against 3.21.1 on a tree of 2,480 events: Birth 12 (744), Death 13
(411), Burial 19 (332), Marriage 1 (172), Census 21 (113).

## 13. The query engine reads columns, not arbitrary fields

`select` and `where` accept a plain column name only from a per-collection
allowlist; anything else must be a `json_path`. The allowlists are narrow:

| Collection | Columns |
| --- | --- |
| person | `gramps_id`, `handle`, `given_name`, `surname`, `gender`, `birth_ref_index`, `death_ref_index`, `private`, `change` |
| family | `gramps_id`, `handle`, `father_handle`, `mother_handle`, `private`, `change` |
| event | `gramps_id`, `handle`, `description`, `place`, `private`, `change` |
| citation | `gramps_id`, `handle`, `page`, `confidence`, `source_handle`, `private`, `change` |
| source | `gramps_id`, `handle`, `title`, `author`, `pubinfo`, `abbrev`, `private`, `change` |
| place | `gramps_id`, `handle`, `title`, `code`, `lat`, `long`, `enclosed_by`, `private`, `change` |
| media | `gramps_id`, `handle`, `path`, `mime`, `desc`, `checksum`, `private`, `change` |
| note | `gramps_id`, `handle`, `format`, `private`, `change` |
| tag | `handle`, `name`, `color`, `priority`, `change` |
| repository | `gramps_id`, `handle`, `name`, `private`, `change` |

Two more traps:

- **Comparing a list raises HTTP 500.** `{"column": {"json_path":
  ["citation_list"]}, "op": "eq", "value": []}` crashes the server rather than
  returning uncited rows. Use `get_backlinks` or `list_unsourced_facts`.
- **An unknown year is stored as `0`, not null.** A filter for
  `birth.date.year < 1800` therefore matches every undated person. Add a
  `gt 0` condition, or accept the noise knowingly.

## 14. A DNA match is an association

gramps-webapi has no DNA object. A match is a `PersonRef` in the tested
person's `person_ref_list`, pointing at the match, with `rel` set to the
string `"DNA"`:

```json
{"_class": "PersonRef", "ref": "<match handle>", "rel": "DNA",
 "note_list": ["<note holding the segment rows>"],
 "citation_list": ["<citation naming the test>"]}
```

`GET /api/people/{handle}/dna/matches` walks that list, keeps the `DNA`
associations, and parses segments from every note on the association *and*
every note on a citation attached to it. So a second note of segments on the
same association adds to the match's total, and a second association to the
same person is a second match. Unreadable segment text parses to nothing,
with HTTP 200 -- the same trap as the parser endpoint, which is why
`add_dna_match` parses before it writes.

Read from the gramps-webapi 3.21.1 source
(`gramps_webapi/api/resources/dna.py` and its endpoint tests) and from the
Gramps Web frontend, which writes the same shape, on 2026-09-29. Not yet
exercised against a live instance.

## 15. A family write maintains its members' links -- imperfectly

Writing a family updates the people it names, in the same transaction
(`add_family_update_refs` and `update_family_update_refs` in
`api/resources/util.py`). Two details there create defects:

- A **new father or mother** of an existing family gets the family appended to
  their `family_list` with no check for one already there, so a person can
  list the same family twice.
- A **removed child** loses the family through `Person.remove_parent_family_handle`,
  which is `list.remove`: it takes the first of two entries and leaves the
  second, so a duplicate becomes a link only the person holds.

Seen in a research session against a live tree on 2026-09-30: detaching a child
listed twice left exactly that one-sided link, and a both-directions check then
found eleven more duplicates. Read from the gramps-webapi 3.21.1 and Gramps
6.0 source on 2026-10-01.

So every person write through `_mutate()` drops a repeated family from either
list, `detach_object(child_kind="child")` removes every remaining link from the
child, and `check_family_links` reports duplicates and one-sided links in both
directions. A person write does not cascade into families.

## 16. A delete removes references -- and a source takes its citations

`DELETE` removes "the object and its references" (`api/resources/delete.py`):
every object pointing at it loses that reference in the same transaction, so a
delete leaves no dangling handle. Two consequences are easy to miss:

- **What the deleted object held is not considered.** A citation's own note
  (a transcription, a finding) or media (the page image) is left attached to
  nothing. Seen twice in research sessions on 2026-09-30. `uncite`,
  `detach_object` and `delete_object` check for notes and media only the object
  reaches, and keep it unless `carry_to` moves them first.
- **Deleting a source deletes every citation of it**, removing each from every
  fact it supports. `delete_object` refuses a source that still has citations.

The media **file** is never removed: no code path in the server deletes one,
on local storage or S3. A deleted media object leaves its file behind.

Read from the gramps-webapi 3.21.1 source on 2026-10-01.

## 17. A delete can answer 500 after it has landed

`DELETE` commits its transaction and *then* removes the object from the search
indices and schedules a reindex. An error in either answers HTTP 500 with an
HTML page, for a delete that has happened. Seen in a research session on
2026-09-30, deleting a media object: the result was an error, and
`list_transactions` showed the edit and the delete both done. A caller trusting
the status would retry, or believe the tree unchanged. Explained from the
3.21.1 source on 2026-10-01 (`delete` in `api/resources/base.py`).

On a 5xx from a delete, the service looks the object up again and reports the
delete as done, with the status it came back with, when the object is gone.

## 18. A place's type is `place_type`; an unknown key is kept and ignored

Gramps' field is `place_type`. A payload carrying `type` instead is accepted:
the server converts the string to a type object, stores it under the stray key,
and never reads it, so the place stays Unknown while a reader of the raw record
sees the type it was given. `add_place` in 1.0.x did this; eight places created
on 2026-09-30 were found that way on a live tree. Read in `fix_object_dict`
(`api/resources/util.py`, 3.21.1) on 2026-10-01.

`_mutate()` removes a stray `type` from any place it writes, moving it into
`place_type` when that is unset, and `get_place` reports one it finds.

## 19. The server recomputes birth and death

Every person write runs `set_birth_death_index`: the birth and death are the
first Birth and Death events the person holds in the Primary role, whatever the
payload said. An event write whose type moves into or out of Birth or Death
recomputes it for every person referencing the event. So changing an event's
type in place needs no work on the people sharing it. Read from the 3.21.1 and
Gramps 6.0 source on 2026-10-01.

## 20. Dates: a span is not a range, and the server checks the shape

Gramps distinguishes "between X and Y" (`MOD_RANGE`, 4: it happened once,
somewhere in the interval) from "from X to Y" (`MOD_SPAN`, 5: it lasted the
whole interval), and since Gramps 5.2 has an open-ended "from X" (`MOD_FROM`,
7) and "to X" (`MOD_TO`, 8). The server refuses a date whose `dateval` is too
short for its modifier: 4 values for a simple or open-ended date, 8 for a
range or span (`_validate_date`, 3.21.1). `GET /api/metadata/` names the Gramps
version under `gramps.version`; the parser keeps "from X" as text on a server
older than 5.2. Read on 2026-10-01.

## 21. A place merge unions the enclosures

`Place.merge` appends every PlaceRef of the dropped place not equal to one the
survivor holds (`_merge_placeref_list`, Gramps 6.0). Merging two copies of a
town, one placed in its county and one in its state, leaves the survivor with
both as undated parents, which Gramps reads as alternatives; the first drives
the title. Seen on three merges in a research session on 2026-09-30.
`merge_objects` settles the survivor's enclosures, and refuses before merging
when it cannot.

## 22. A write takes no description

A `PUT` is recorded as "Edit Person", "Edit Event" and so on (`put` in
`api/resources/base.py`, 3.21.1): there is no way to attach a reason to the
transaction. Where a correction needs one on record -- a primary name corrected
without keeping the old form -- it goes in a note on the object.
