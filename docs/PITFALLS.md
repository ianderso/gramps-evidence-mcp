# Pitfalls

`gramps-webapi` behaviours that constrain this server's implementation. Read
before adding a tool that mutates, or before calling the API directly.

**The claims here about the server are checked on every CI run**, with the
exceptions below. The live suite (`tests/live`, see CONTRIBUTING.md) starts a
throwaway gramps-webapi 3.21.1, 3.22.3 and 3.23.1 and runs `test_pitfalls_live.py`,
whose tests are named for these sections. Where the two versions differ, the
section says so. Sections 4, 9, 10 and 11 describe this project's own code and
are covered by the unit tests.

Not checked, because a healthy throwaway server cannot show them: the HTTP 500
answered to a delete that has landed (section 17), which needs a failing
search index; that the server never removes a media file (section 16), which
needs an upload; the 500s on every write under prolonged concurrent writing
(section 6); how a server on Gramps older than 5.2 stores "from X"
(section 20), since no supported server runs one; and what 3.23 does with a
stray key or a `year` an older server stored (sections 18 and 24), which needs
a tree written by one version and served by another. Each says where it came
from.

The sections were first found on gramps-webapi 3.20.1 and 3.21.1, in research
sessions against a live tree, and explained from the gramps-webapi and Gramps
6.0 source; the dates below record that. 3.20 has no structured query
endpoint, so this server requires 3.21 or later and refuses an older one.

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

The server has `If-Match` support that no client can use. A `PUT` with
`If-Match` is refused with 412 unless the tag equals a hash of the *stored*
object (`hash_object` in `api/resources/util.py`), but a read's `ETag` is a
hash of the *response body*, suffixed `:gzip` when compressed (`emit.py`).
No tag the API hands out ever matches, fresh or stale; only `If-Match: *`
passes, which checks nothing. Verified on 3.21.1, 3.22.3 and 3.23.1, where a test in
the live suite holds it as a tripwire: when gramps-webapi fixes this, the test
fails, and `_mutate()` should start sending the tag of its read. Until then,
`_mutate()` narrows the race to one round trip -- it reads and writes back to
back -- and that is the most a client can do.

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
  - Sources have no `citation_list`; citations point at sources. With
    gramps-ql 0.5.0 (what 3.21.1 to 3.23.1 install) `citation_list.length = 0`
    matches no source, cited or not, so every source looks cited; on 3.20.1 it
    matched every source. Neither is an answer. Find uncited sources through
    `backlinks`.
- Booleans compare as integers. `private = 1` finds private records;
  `private = true` matches nothing.
- **`~` on a list compares the list itself.** It asks whether the value *is*
  one of the items (`"http" in urls`), never whether an item contains it, so
  `urls ~ "http"` matched no person on a tree where 298 have a URL, and
  `media_list !~ "x"` matched all 1,538 sources. A list of objects is
  searched through its items with `.any.` (or `.all.`), and a handle is
  followed to its object with `get_<type>`:
  - `urls.any.path ~ "blm.gov"`, `attribute_list.any.value ~ "blm.gov"`;
  - `note_list.any.get_note.text.string ~ "blm.gov"`;
  - `alternate_names.any.surname_list.any.surname ~ "Ray"`.

  On a list of handles `~` is a real test: `tag_list ~ "<handle>"` finds what
  carries that tag. `query_objects` refuses every other comparison of a whole
  list, and names the form that works.
- **Every condition reads the whole collection, in Python, on the server.**
  On 3.21.1 against 6,092 citations, on 2026-10-05: one condition on a list
  13 s, two joined by OR 29 s, the same two following each note 38 s.
  `query_objects` allows a GrampsQL read 120 s (`GQL_TIMEOUT`); other
  requests keep `request_timeout`, 30 s by default.

Queryable fields verified: `gramps_id`, `page`, `confidence`, `description`,
`desc`, `title`, `checksum`, `private`, `change`, `<list>.length`, and
`<list>.any.<field>` with `get_<type>`.

The list semantics are gramps-ql's (`_match_values` in `gramps_ql/gql.py`,
0.5.0, read on 2026-10-05) and were seen on a live 3.21.1 tree the same day;
the live suite checks them.

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

- `{"column": "type", ...}` is rejected on 3.21 (not an allowed column). On
  3.22 it is accepted and compared with the whole stored dict, so `eq 12` and
  `eq "Birth"` both match nothing.
- `{"json_path": ["type", "string"]}` matches nothing, because it is empty.
- `{"json_path": ["type", "value"], "op": "eq", "value": 12}` works, on both.

The integers come from `GET /api/types/default/event_types/map`, which is what
`list_event_types` reads and what `query_records(event_type=...)` translates
through. Do not hard-code them. `query_records` refuses `type` as a plain
column, pointing at `event_type` (or, on another collection, at
`["type", "value"]`).

Verified against 3.21.1 on a tree of 2,480 events: Birth 12 (744), Death 13
(411), Burial 19 (332), Marriage 1 (172), Census 21 (113).

## 13. The query engine reads columns, not arbitrary fields

On 3.21, `select` and `where` accept a plain column name only from a
per-collection allowlist; anything else must be a `json_path`, and a path to a
field the object lacks matches nothing, silently. On 3.22 a plain name may be
any field of the stored object, and an unknown field or path is refused with
422 naming the fields there are -- the better behaviour, but a query written
against one version can fail or change meaning on the other. The 3.21
allowlists are narrow:

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

Two more traps, both refused by `query_records` with the form that works:

- **A list as the value of any operator but `in` is an HTTP 500**, on both
  versions: `citation_list eq []`, `ne []`, `eq ["x"]`, and a whole `dateval`
  compared with a list all crash the server rather than answering. `contains`
  finds one value in a list field. The engine cannot test a list for
  emptiness -- comparing an element with null is a 422 -- so uncited facts are
  `list_unsourced_facts`'s job, and what cites an object `get_backlinks`'.
- **There is no stored `year`; the year is `dateval[2]`, and an unknown one
  is `0`, not null.** `birth.date.year` matches nothing on 3.21 and is refused
  on 3.22 -- except on records some client wrote a served year back to
  (section 24), which makes it worse than useless. Filter on
  `["birth", "date", "dateval", 2]`, and add a `gt 0` condition: `lt 1800`
  alone matches every undated person.

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

The relationship and common ancestors a match reports are computed by Gramps'
relationship calculator from the family links, so a parent of unknown gender
is a "first ancestor", not a mother or father.

Read from the gramps-webapi 3.21.1 source
(`gramps_webapi/api/resources/dna.py` and its endpoint tests) and from the
Gramps Web frontend, which writes the same shape, on 2026-09-29; verified live
since 1.1.0.

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

## 18. A place's type is `place_type`; an unknown key is kept -- until 3.23

Gramps' field is `place_type`. Through 3.22, a payload carrying `type` instead
is accepted: the server converts the string to a type object, stores it under
the stray key, and never reads it, so the place stays Unknown while a reader of
the raw record sees the type it was given. `add_place` in 1.0.x did this; eight
places created on 2026-09-30 were found that way on a live tree. Read in
`fix_object_dict` (`api/resources/util.py`, 3.21.1) on 2026-10-01.

That is one case of a general rule through 3.22: the server keeps any key a
write carries that the object's class lacks, at any depth, and serves it back
-- which is how section 24's `year` gets stored.

From 3.23 it refuses such a key with 400, naming where it is:
`$: unknown Place keys: 'type'`, `$.name: unknown PlaceName keys: 'lang_code'`
(`_validate_keys`, `api/resources/util.py`). A date's served `year` is the one
key it drops instead (section 24). A key an older server stored is still
served, so a client that writes back what it read is refused until the key is
gone: written with 3.22.3 and served by 3.23.1 on 2026-10-05, a stray place
`type` came back from a read, and the place written back as read was refused.
The live suite runs one version per tree and cannot stage that.

`_mutate()` removes a stray `type` from any place it writes, moving it into
`place_type` when that is unset, and `get_place` reports one it finds. On 3.23
that repair is what lets the tools write such a place at all. A stray key of
any other kind, which no version of this server writes, would make its object
unwritable through the tools until removed.

Every version checks the types of the fields the class has: a null where the
schema wants a string, list or object is refused with 400
(`$.description: None is not of type 'string'`), while a family's null parent
handle is stored as `""` and an event's null place stays null. The fake in
`tests/conftest.py` records which, field by field, from a real server, and
refuses an unknown key when it plays 3.23 or later.

## 19. The server recomputes birth and death -- on an update

Every person update (`PUT`) runs `set_birth_death_index`: the birth and death
are the first Birth and Death events the person holds in the Primary role,
whatever the payload said. An event update whose type moves into or out of
Birth or Death recomputes it for every person referencing the event. So
changing an event's type in place needs no work on the people sharing it.

A **create** (`POST`) keeps the indices it was sent, right or wrong, until the
person's first update. `add_person` sets them as that update would: a birth
given as a Baptism is kept as an event, but is not made the birth only to be
unset by the next edit. Read from the 3.21.1 and Gramps 6.0 source on
2026-10-01; the create case found by the live suite.

## 20. Dates: a span is not a range, and the server checks the shape

Gramps distinguishes "between X and Y" (`MOD_RANGE`, 4: it happened once,
somewhere in the interval) from "from X to Y" (`MOD_SPAN`, 5: it lasted the
whole interval), and since Gramps 5.2 has an open-ended "from X" (`MOD_FROM`,
7) and "to X" (`MOD_TO`, 8). The server refuses a date whose `dateval` is too
short for its modifier: 4 values for a simple or open-ended date, 8 for a
range or span (`_validate_date`, 3.21.1). `GET /api/metadata/` names the Gramps
version under `gramps.version`; the parser keeps "from X" as text on a server
older than 5.2. Read on 2026-10-01.

A date's `sortval` -- the day number of its start, which orders events -- is
recomputed by the server on every write, whatever was sent.

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

## 23. Tokens expire, and their endpoints take one request a second

An access token lasts 15 minutes. An expired one is answered 401, which is
what tells a client to renew it; a malformed one is answered 422, which does
not. `POST /api/token/` and `POST /api/token/refresh/` each allow one request a
second per address (`@limiter.limit("1/second")` in `api/resources/token.py`)
and answer a second within the same second with 429 and no `Retry-After`.

An MCP client runs tool calls in parallel, so after a quiet spell several
requests find the token expired at once. Until 1.1.0 each renewed it: the
first refresh succeeded, the second was refused and fell back to logging in,
and from the third on both were refused and the tool call failed with 429.
The client now renews once, under a lock, for every request that saw the old
token, and waits out one 429 from a token endpoint (another client behind the
same address can still cause one). Found by the live suite.

## 24. A date's served `year` goes stale once written back -- until 3.23

Every date the server serves carries `year`, which is not a field of a Gramps
date and is not stored. Through 3.22, a client that writes back what it read
stores it, and from then on the server serves the stored `year` instead of
computing it, however the date changes:

```text
GET   date.dateval [0, 0, 1850, false]   year 1850   (computed)
PUT   the same date with dateval[2] = 1860, year left as read
GET   date.dateval [0, 0, 1860, false]   year 1850   (stored, stale)
```

On 3.21 a query on `date.year` then matches the records some client happened
to write back, by a year that may be wrong (section 13). No tool reads `year`
-- the year is `dateval[2]` -- but `_mutate()` drops it from every date it
writes, which also repairs a stale one, and says so when it does. Found by the
live suite, on 3.21.1 and 3.22.3.

3.23 drops `year` from every date it is sent -- the one key it computes on
read (section 18) -- so a written-back year is never stored. One an older
server stored is still served, until the date's object is next written by any
client: written with 3.22.3 and served by 3.23.1 on 2026-10-05, a stale 1850
was served for a date of 1860, and the event written back as read was served
1860. The live suite checks the drop on 3.23.1.

## 25. A merge merges more than it names

The server merges with Gramps' own merge queries (`gramps/gen/merge`), which
go further than the two objects asked about:

- **A person merge merges families.** After the merge, the first two of the
  survivor's families that have the same parents -- one each record had with
  the same spouse -- are merged too: children, events and citations combined,
  the second family deleted. That is the server's default (`family_merger`).
- **A family merge merges parents.** The survivor keeps its father and
  mother; a different father or mother in the other family is merged into
  them, as a person merge.
- **Some merges are refused with 409:** two spouses, and a parent with their
  own child.
- **The survivor keeps a trace of the other.** A person merge files the
  other's primary name as the first alternate name and adds a "Merged Gramps
  ID" attribute holding the other's id; every list is combined, equivalent
  entries merged rather than repeated (the same event in the same role, the
  same attribute type and value), and birth and death are recomputed.

`merge_objects` reports the families or people a merge would also merge, as
`also_merges` in its dry run and its result, and refuses a merge Gramps would
refuse before sending it. Verified on 3.21.1, 3.22.3 and 3.23.1, where the contract
tests hold the unit tests' fake to every one of these.

## 26. Type names are matched exactly, and the rest kept for good

The server converts a type given as a string -- an event's type, a role, a
name's type, a child's relationship, a place's, note's, repository's, medium's,
attribute's or URL's type -- by exact, case-sensitive lookup among Gramps'
English standard names, then the localized ones (`_set_type_from_string`,
`api/resources/util.py`). Anything else becomes a new custom type with that
string. So `"birth"` is not Birth: it is a custom type beside it, which
filters and Gramps' own birth logic never see. Gramps adds each custom name to
the tree's vocabulary when it stores an object carrying it, and never takes
one off, even once nothing uses it (`DbGeneric.commit_*`, saved as metadata
when the database closes after each request): `GET /api/types/` lists it under
`custom` from then on.

The standard names are Gramps', not the tree's: `GET /api/types/` lists every
one under `default` whether the tree has used it or not -- 46 event types on
Gramps 6.0, Stillbirth and Bas Mitzvah among them. Gramps 6 has no standard
source attribute but Unknown, so every source or citation attribute name is a
custom one. Custom attribute names are listed by the kind of object that
carries them (`person_attribute_types`, `event_attribute_types`, and so on;
a citation's go with a source's).

So every tool that writes a type name matches it first, against
`GET /api/types/` read afresh on each call:

1. Gramps' standard names, ignoring case, spacing and punctuation: "census",
   "cause-of-death" and "E-MAIL" are Census, Cause Of Death and E-mail.
2. A short list of synonyms, each meaning one standard name unambiguously
   (`_TYPE_SYNONYMS` in `service.py`): "Born" is Birth, "buried" Burial,
   "godmother" Godparent, "maiden name" Birth Name, "microfilm" Film, "Web
   Home Page" Web Home. A near-miss is never on it.
3. The tree's custom names, matched the same way and spelt as first made. One
   that a standard name or a synonym already matches -- a "census" or "Web
   Home Page" an earlier client left -- is never used or offered. An attribute
   on a person, family, event or media matches a custom name from any of
   those four lists.

Anything else is refused as `unknown_type`, with the closest names ("Did you
mean 'Census'?") and both lists. `allow_new_type` makes it a new custom type,
for one that is meant: "Land Grant", a "Territory" place, the first use of a
source attribute.

This covers an event's type wherever an event is created or retyped, the role
in `add_event_ref`, a family's relationship and a child's (`add_family`,
`add_child_to_family`, `update_child_ref`), a name's type
(`add_alternate_name`, `update_alternate_name`), a place's (`add_place`,
`update_place`), a note's (`add_note`), a repository's (`add_repository`), a
source's medium at a repository (`add_source`, `link_repository`,
`link_repositories`), an attribute's name (`add_attribute`), a URL's type
(`add_url`, `update_url`), and the `type` of a note, family or repository set
through `update_object_fields`. 1.1 and earlier wrote "Web Home Page" as
`add_url`'s default and as a repository's home-page URL; Gramps has no such
name, so each made a custom type. Both now write Web Home. A custom name
already in a tree stays in its vocabulary, and the objects carrying it keep
it until retyped (`update_event`, `update_url`, `update_place`, ...).
Verified on 3.21.1, 3.22.3 and 3.23.1, where the contract tests also hold the fake's
standard names to the server's.
