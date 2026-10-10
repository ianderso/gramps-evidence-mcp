# gramps-evidence-mcp

[![CI](https://github.com/ianderso/gramps-evidence-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/ianderso/gramps-evidence-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/gramps-evidence-mcp?label=pypi)](https://pypi.org/project/gramps-evidence-mcp/)

<!-- mcp-name: io.github.ianderso/gramps-evidence-mcp -->

An [MCP](https://modelcontextprotocol.io) server that gives an AI assistant
**read/write access to a Gramps genealogy tree**, plus a **read-only
"reference layer"** over legacy GEDCOM exports.

**97 tools, built around evidence discipline.** The premise is that an assistant
turned loose on a family tree will happily invent a plausible ancestor, so the
write paths here are shaped to make every claim carry its source: facts are
created with citations attached, `uncite` deletes what it orphans, parent-child
links are cited independently because one citation object cannot carry two
confidences, and an audit set — `list_unsourced_facts`, `get_backlinks`,
`find_duplicates`, and server-side queries through `query_objects` and
`query_records` — exists to find the places where that discipline slipped.

Legacy trees (Ancestry / FamilySearch exports) are consulted through the
read-only reference layer as **untrusted hints**, never a source of truth.

The server is a **REST client of [Gramps Web](https://www.grampsweb.org/)
(`gramps-webapi`)**. It never touches the Gramps database files directly — see
[How it connects](#how-it-connects).

Living-person privacy filtering is **on by default** — see [Privacy](#privacy).

This is an independent project. It is not made, endorsed or supported by the
Gramps project.

---

## Contents

- [How it connects](#how-it-connects)
- [Setup](#setup)
- [Configuration](#configuration)
- [Client configuration](#client-configuration)
- [Remote access (Claude web / mobile)](#remote-access-claude-web--mobile)
- [The evidence model & transactions](#the-evidence-model--transactions)
- [Privacy](#privacy)
- [Tool reference](#tool-reference)
- [Reading documents: `ocr_media`](#reading-documents-ocr_media)
- [Reference layer (legacy GEDCOMs)](#reference-layer-legacy-gedcoms)
- [Worked example](#worked-example-a-person-with-a-cited-birth)
- [Development](#development)
- [Limitations](#limitations)
- [Repository layout](#repository-layout)

---

## How it connects

The server speaks HTTP to `gramps-webapi` and never opens the Gramps database
files. The API server is the sole owner of the database, so this server and the
Gramps Web frontend can both write without contending for the desktop app's
exclusive lock. The package imports no `gramps.gen.*` module and needs no
`sys.path` surgery.

One caveat: *side by side* means the Gramps Web frontend and this server, both
talking to the same `gramps-webapi`. It does **not** mean pointing the Gramps
desktop app at the same database file that `gramps-webapi` is serving -- that
reintroduces the two-writer problem. Treat the Gramps Web tree as the source of
truth and round-trip with the desktop app through export/import.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the layer breakdown.

---

## Setup

You need a running **Gramps Web** instance, and
[`uv`](https://docs.astral.sh/uv/) to run the server.

### Option A — you already run Gramps Web

If Gramps Web is already up anywhere you can reach, you **don't need the
bundled docker-compose**. Just point the MCP at it:

1. Note the API base URL, e.g. `http://gramps.example.org:5000`. The REST API
   lives under `…/api`.
2. Create a **dedicated MCP user** with at least the **editor** role (writes need
   editor; owner also works). From a shell on the Gramps Web host / container:
   ```bash
   gramps-web user add mcp 'A_STRONG_PASSWORD' --fullname 'MCP client' --role 3
   ```
   (On the official image the wrapper is `gramps-web`; the underlying command is
   `python3 -m gramps_webapi user add …`.)
3. Put the URL + credentials in your environment (see
   [Configuration](#configuration)).

### Option B — run Gramps Web locally with the bundled compose

A ready-to-go stack is in [`docker/docker-compose.yml`](docker/docker-compose.yml):
SQLite backend, **bind-mounted** data and media directories (a plain directory
tree you can copy or back up like any other), and host port **5555 → container
5000**. Port 5555 avoids macOS's AirPlay Receiver, which occupies 5000.

```bash
docker compose -f docker/docker-compose.yml up -d
```

Then create the first **owner** (browser wizard at http://localhost:5555, or CLI)
and the dedicated **MCP editor** user:

```bash
docker compose -f docker/docker-compose.yml exec grampsweb \
    gramps-web user add owner 'OWNER_PW' --fullname 'Owner' --role 4
docker compose -f docker/docker-compose.yml exec grampsweb \
    gramps-web user add mcp 'MCP_PW' --fullname 'MCP client' --role 3
```

The tree named by `GRAMPSWEB_TREE` (default `MyTree`) is created on first start.

### Install the MCP server

```bash
uvx gramps-evidence-mcp
```

That runs the server over stdio, which is how an MCP client starts it. You
normally put it in the client's configuration (below) rather than running it
yourself.

---

## Configuration

**Secrets live in the environment; everything else in a TOML file.** The TOML
file therefore never contains credentials.

### Environment variables

Set them in the client configuration, or in a `.env` file in the directory the
server starts in (see [`.env.example`](.env.example)). Real environment
variables win over the file.

| Var | Meaning |
| --- | --- |
| `GRAMPS_MCP_API_URL` | Base URL of `gramps-webapi`, e.g. `http://gramps.example.org:5000` |
| `GRAMPS_MCP_USERNAME` | The dedicated MCP user |
| `GRAMPS_MCP_PASSWORD` | Its password |
| `GRAMPS_MCP_CONFIG` | Path to the TOML config (default `./gramps_mcp.toml`, from the working directory) |
| `GRAMPS_MCP_EXPOSE_PRIVATE` | Override the TOML `expose_private` flag |
| `GRAMPS_MCP_TRANSPORT` | `stdio` (default) or `http` — see [Remote access](#remote-access-claude-web--mobile) |
| `GRAMPS_MCP_HOST`, `GRAMPS_MCP_PORT` | Where `http` listens (default `127.0.0.1:8090`) |
| `GRAMPS_MCP_TRANSKRIBUS_USERNAME`, `GRAMPS_MCP_TRANSKRIBUS_PASSWORD` | Optional. A Transkribus account (Scholar plan or above), for `ocr_media`'s handwriting routes. Both or neither. See [Reading documents](#reading-documents-ocr_media) |
| `GRAMPS_MCP_TRANSKRIBUS_PAGE_BUDGET` | Pages a calendar month `ocr_media` may send to Transkribus without the caller's per-call `spend_credits`. Default `0`: every page asks |
| `GRAMPS_MCP_TRANSKRIBUS_API_URL` | Transkribus' processing API (default `https://transkribus.eu/processing/v1`, Metagrapho v1) |

### TOML file (see [`gramps_mcp.example.toml`](gramps_mcp.example.toml))

```toml
[server]
expose_private = false          # see Privacy below

[[reference]]
path  = "~/gedcoms/export_one.ged"
label = "Ancestry (one branch)"
trust = "User-built; many hints unsourced. Verify everything."
```

A desktop client starts the server in a directory of its own choosing, so give
`GRAMPS_MCP_CONFIG` as an absolute path.

---

## Client configuration

**Claude Desktop** — add to `claude_desktop_config.json` (on macOS,
`~/Library/Application Support/Claude/`):

```json
{
  "mcpServers": {
    "gramps": {
      "command": "uvx",
      "args": ["gramps-evidence-mcp"],
      "env": {
        "GRAMPS_MCP_API_URL": "http://gramps.example.org:5000",
        "GRAMPS_MCP_USERNAME": "mcp",
        "GRAMPS_MCP_PASSWORD": "MCP_PW",
        "GRAMPS_MCP_CONFIG": "/path/to/gramps_mcp.toml"
      }
    }
  }
}
```

**Claude Code**:

```bash
claude mcp add gramps \
    -e GRAMPS_MCP_API_URL=http://gramps.example.org:5000 \
    -e GRAMPS_MCP_USERNAME=mcp -e GRAMPS_MCP_PASSWORD=MCP_PW \
    -e GRAMPS_MCP_CONFIG=/path/to/gramps_mcp.toml \
    -- uvx gramps-evidence-mcp
```

Any other MCP client that launches stdio servers works the same way: the
command is `uvx gramps-evidence-mcp`, with the variables above in its
environment. Restart the client after editing its configuration.

To run from a clone instead, replace `uvx gramps-evidence-mcp` with
`uv --directory /path/to/gramps-evidence-mcp run gramps-evidence-mcp`.

---

## Remote access (Claude web / mobile)

The configuration above uses the **stdio** transport: the client launches the
server as a local subprocess. **Claude web (claude.ai) and the mobile apps can't
do that** — they connect to a **remote server over HTTPS** using the *Streamable
HTTP* transport, added as a **Custom Connector**. Three differences to plan for:

1. **Transport** must be HTTP, not stdio.
2. **Reachability** — Anthropic's servers dial *out* to yours, so it needs a
   **public HTTPS URL**. A LAN address like `http://192.168.1.50:5000` won't work.
3. **Auth** — it's write access to a database of living relatives, so it must sit
   behind authentication. The server has none of its own. **Never expose it
   authless.**

> If you don't specifically need a browser/phone, **a desktop client already
> gives you the same models with this server working locally** (stdio, no
> public exposure). The steps below are only for genuine remote access.

### 1. Serve it over HTTP

```bash
GRAMPS_MCP_TRANSPORT=http uvx gramps-evidence-mcp
```

That serves the MCP endpoint at `http://127.0.0.1:8090/mcp`; `GRAMPS_MCP_HOST`
and `GRAMPS_MCP_PORT` change where. The other `GRAMPS_MCP_*` variables apply as
before.

Bound to a loopback address, the server accepts only requests whose `Host` is
`localhost` or `127.0.0.1` — the MCP SDK's protection against DNS rebinding. A
tunnel or proxy on the same machine must therefore send `Host: localhost`
(cloudflared's `httpHostHeader` setting does this). In a container, bind
`0.0.0.0` instead, as below, and let the container network do the isolating.

### 2. Deploy it next to Gramps Web

Run it in a container on the same host as Gramps Web so it talks to the API
over the internal network. A minimal `Dockerfile`:

```dockerfile
FROM python:3.13-slim
RUN pip install uv
ENV GRAMPS_MCP_TRANSPORT=http GRAMPS_MCP_HOST=0.0.0.0 GRAMPS_MCP_PORT=8090
EXPOSE 8090
CMD ["uvx", "gramps-evidence-mcp"]
```

Add it to your Gramps Web compose (same network), pointing at the API by service
name and reading secrets from the environment:

```yaml
  gramps_evidence_mcp:
    build: /path/to/dockerfile-dir
    restart: unless-stopped
    environment:
      GRAMPS_MCP_API_URL: "http://grampsweb:5000"   # internal service name
      GRAMPS_MCP_USERNAME: "mcp"
      GRAMPS_MCP_PASSWORD: "${MCP_PW}"
    # no host port needed if the tunnel (below) runs in the same compose network
```

### 3. Expose it publicly with TLS

A reverse tunnel avoids port-forwarding and gives you TLS and a stable
hostname. Any of these work:

- **Cloudflare Tunnel** — run `cloudflared` alongside the server and route a
  hostname such as `https://gramps-mcp.example.org` →
  `http://gramps_evidence_mcp:8090`.
- **Tailscale Funnel** — public HTTPS over your own tailnet.
- `ngrok http 8090` for a quick throwaway test.

Or terminate TLS yourself with any reverse proxy in front of port 8090.

### 4. Put auth in front

claude.ai custom connectors speak the MCP **OAuth 2.0** flow. Put an **identity
proxy** in front of the tunnel hostname — Cloudflare Access, Authelia,
oauth2-proxy and similar all tie into the OAuth flow the connector expects. The
server does not authenticate callers itself; see
[docs/ROADMAP.md](docs/ROADMAP.md).

### 5. Add the connector in claude.ai

**Settings → Connectors → Add custom connector →** paste
`https://gramps-mcp.example.org/mcp`, complete the auth prompt, and the 97 tools
appear in chat. (Custom connectors require a paid Claude plan; on
Team/Enterprise an admin may need to enable them.)

**Security reminder:** this endpoint can create, edit, and delete records in a
tree containing living people. Keep it behind auth, prefer a private tunnel over
an open port, and consider a Gramps Web user with a read-only role if you only
need lookups remotely — the server's write tools then fail with a permission
error instead of writing.

---

## The evidence model & transactions

The server enforces the Gramps evidence model:

```
Repository  →  Source  →  Citation  →  fact (Event / Attribute)
```

**Every fact-recording write requires a citation.** You either reference an
existing citation/source or create one inline. The only way to record a fact
*without* a citation is to explicitly pass `require_citation=False`, which stamps
the event with an **`UNSOURCED=true` attribute** so `list_unsourced_facts` can
find it later. Confidence levels map to Gramps' 0–4 scale
(`very_low, low, normal, high, very_high`).

**Type names are Gramps' own.** Gramps stores any type name it does not know
exactly — `"birth"`, `"Web Home Page"` — as a new custom type, beside the
standard one it was meant to be, and keeps it in the tree's vocabulary for
good. So every write matches a type name first: against Gramps' standard names
and the tree's custom ones, ignoring case and punctuation, then through a few
unambiguous synonyms ("Born" is Birth, "microfilm" is Film). A name that still
matches nothing is refused with the closest names; `allow_new_type` creates it
when a new custom type is meant ("Land Grant", a "Territory" place).
[docs/PITFALLS.md](docs/PITFALLS.md) section 26 has the details.

### How writes map to DbTxn transactions

`gramps-webapi` wraps every object create/update in its own server-side `DbTxn`
(labelled `New Person`, `Edit Event`, …), so **all writes go through proper
transactions and stay in Gramps' undo history**. A composite operation
(e.g. `add_person` with a cited birth) is performed as an ordered sequence of
these object writes — source → citation → event → person — with references wired
up as it goes.

**Design note (documented deviation):** `gramps-webapi` also offers a raw
`POST /api/transactions/` endpoint that can bundle several objects into a *single*
`DbTxn` with a *custom* description. We intentionally **don't** use it, because
that endpoint bypasses the server-side helpers that (a) auto-assign `handle` and
`gramps_id` and (b) coerce English type strings (`"Birth"`) into internal Gramps
type dicts. Using it would force this client to reimplement Gramps' ID allocation
and type internals and to invent gramps_ids, risking a desynced ID counter. The
per-object-transaction approach is safer and keeps undo coherent; the trade-off
is that one logical add appears as a few undo entries rather than one.

---

## Privacy

The tree contains living people. With `expose_private = false` (the default),
bulk output leaves out anyone **private or probably living**:

- **Probably living** means born less than **110 years** ago with no recorded
  death — or with neither a birth nor a death recorded, which errs toward
  privacy. A death event counts as recorded even without a date. 110 is the
  conventional genealogical "presumed dead" cutoff; it does not permanently
  hide clearly historical people.
- **Private** means the Gramps private flag, on a record of any type.

What that means tool by tool:

| Output | A private or probably-living person… |
| --- | --- |
| `search_people`, `get_ancestors`, `get_descendants`, `query_objects`, `query_records` | …appears as a **redacted stub**: ids only, no name or facts, so it can still be fetched deliberately. A `query_records` family row is judged by both parents. |
| `list_unsourced_facts` across the whole tree | …has their facts left out and counted, and appears once as a stub. |
| `find_duplicates` | …is left out. A namesake group is reported only while two historical people remain in it. |
| `get_timeline`, `consolidated_timeline` | …has their events left out and counted when they are folded in as a relative. The people you name are shown; a family's own members are judged like relatives, so a living couple's family timeline comes back empty. |
| `get_facts` | …is excluded by the server before it computes anything. |
| `run_report` | …is left out of any report that has the options for it — 21 of Gramps' 25 reports have `living_people`, and 24 `incl_private`: they are sent as "Not included" and false unless you pass either. Gramps' own default includes both. The result's `privacy_options` shows what was applied. |
| `consult_reference` | …is left out of the matches and counted. Exports from Ancestry and similar sites privatize nobody. |
| `check_family_links` | …has the link problems found on them left out and counted. |

**A lookup by id is not filtered.** `get_person`, `get_object`, the other typed
getters, and `list_unsourced_facts` for one named person answer in full. This is
a *personal* tool the tree's owner runs against their own data: the goal is to
prevent *accidental bulk leakage* — search dumps, tree walks, shared reports —
not to lock the owner out. Asking for one record by id is a deliberate act.

**Writes are never filtered.** Adding, citing, editing, merging and deleting
work on living and private people like anyone else.

**Ask, and one call shows everyone.** Every tool in the table takes
`include_private`. Passed as true, that call answers in full — living people,
private records, Gramps' own report defaults — and the next call is filtered
again. It is meant for when you ask for living relatives by name ("list my
cousins born after 1950"), and the tool descriptions tell the assistant so.
Each use is logged with the tool's name, never the records.

**Not filtered, by design or by limitation:**

- `export_backup` is a lossless backup, and a backup that drops living people
  cannot restore the tree.
- `verify_tree` returns Gramps' own findings as it words them.
- `get_dna_matches` answers for the person you name, but each match is another
  person — usually a living one — identified by handle, with segment data.
- Events, citations, notes and media are judged by their own private flag only.
  An event row carries no link back to its person, so a living person's birth
  event is visible to an event query unless the event itself is private.
- `list_research_tasks` judges a task by its own private flag, and its
  description by its note's. A task's title can name a living person; mark
  such a task private.
- Timeline entries are judged by their person, not by the event's own private
  flag, which the timeline endpoint does not report.
- `ocr_media` reads the media object you name, as a lookup by id does, private
  or not. It never sends a private one to Transkribus, a third party; a
  public image of a living person's record would go, so mark such a record
  private.
- A stub still says that a record matched. A query for a name and a birth year
  that returns a stub confirms that the id is a person fitting both. The filter
  prevents accidental disclosure, not a determined search.

To turn the filter off for every call instead, set `expose_private = true` in
the TOML file or `GRAMPS_MCP_EXPOSE_PRIVATE=true` in the client's configuration
— for a full audit, or a tree with no living people in it. That *reduces*
privacy protection for everything the assistant reads.

Logs never contain record contents — only handles, ids, and operation names.
Request URLs are kept out of the log too, because a query filter travels in one.

---

## Tool reference

**97 tools.** Every tool that mutates the tree re-fetches the *whole* object
before PUTting it back — edits through `service._mutate()` — see
[the `keys=` trap](docs/PITFALLS.md#1-keys-plus-put-destroys-unfetched-fields).

**Every tool declares MCP annotations** saying whether it only reads, adds, or
changes and removes, so a client can approve reads automatically and ask before
the rest. 45 tools only read. `ocr_media` reads, but is annotated as neither
read-only nor closed-world: it can fetch from the Library of Congress and the
Internet Archive, spend Transkribus credits, and store a transcript note.

**Unknown parameters are refused.** A misspelt or invented argument is an error
that lists the parameters the tool does take. It is not silently dropped, which
used to turn `query_objects(query=...)` — the parameter is `gql` — into an
unfiltered listing of the first 200 objects.

**Create**

| Tool | Purpose |
| --- | --- |
| `add_person` | Create a person, optionally with cited birth/death events; each event's type may be left out. |
| `add_family` | Link parents + children with an optional cited marriage. A child may be given with its relationship to each parent — a stepchild in the same call. |
| `add_event_to_person` | Add a cited event (residence, census, occupation…) to a person. |
| `add_event_to_family` | Add a dated/placed event (marriage, divorce…) to a family. |
| `add_child_to_family` | Add an existing person as a child of an existing family. |
| `add_event_ref` | Share an **existing** event with another person, in a role (Witness, Informant, Godparent…): one census entry or burial, one set of citations. What is the person's own — their line on the sheet, their age — goes on the reference as attributes (`As enumerated`, `Age`). |
| `add_alternate_name` | Add a non-primary name (AKA, married name, nickname) to a person, with the citation for that form of the name. |
| `add_source` | Create a Source (record set, book, certificate), optionally in a repository. |
| `add_citation` | Create/reuse a standalone Citation on a Source. |
| `add_repository` | Create a Repository (archive, library, cemetery, website). |
| `add_place` | Create a place deliberately, typed and parented, instead of letting one appear as a side effect of naming it in an event. |
| `add_note` | Create a note, optionally attached to an object. Re-reads the target and returns `verified: false` rather than claiming an attachment it can't demonstrate. Any length in one call; a server error says whether anything was written, and an attach that failed takes the new note back. |
| `add_media` | Upload an image, PDF, audio or video file as a standalone Media object; reuses an identical file by md5. |
| `attach_media` | Link a file (uploading it) or an existing Media object to an object. |
| `add_attribute` | Add a typed key/value attribute (`Attribute` on objects, `SrcAttribute` on sources/citations). |
| `add_url` | Add a web URL to a person, place or repository. |
| `add_dna_match` | Record a DNA match as evidence: stored as Gramps Web stores one, cited to the test that found it. Refuses segment data that does not parse, and a second record of the same pair. |

**Cite** — attaching evidence to a claim

| Tool | Purpose |
| --- | --- |
| `cite_event` | Attach a citation to an event that already exists. |
| `cite_object` | Attach a citation to any object that carries one — notably a **family**, and one of a person's **names** (`object_type="name"`), the primary or an alternate. |
| `cite_child_link` | Cite the parent-child link itself. Always mints the link **its own** citation, because one citation object cannot carry two different confidences. |
| `uncite` | Detach a citation, deleting it if that leaves it orphaned. **Always delete** — detach-without-delete has produced orphan debris twice. A citation that is the only holder of a note or image is kept instead, unless `carry_to` names a citation to move them to. |

**Edit** — correcting what is already there

| Tool | Purpose |
| --- | --- |
| `update_citation` | Locator, confidence, date — or **re-point** the citation at a different source. |
| `update_citations` | The same for many citations in one call — a sweep. `expect_page_prefix` reports a row whose page changed since the sweep was planned instead of overwriting it. |
| `update_event` | Type (checked against the tree's vocabulary), date, place, description, in place, keeping the event's id and everything attached to it. Clears a place or date no source states. |
| `update_source` | Title, author, pubinfo, abbrev. |
| `update_media` | Description, date, path. |
| `update_person` | Gender, primary name (the old one is kept as an alternate, unless it was a data-entry error — then it is corrected in place and the reason recorded in a note), privacy flag. |
| `update_alternate_name` | Correct, retype or remove one alternate name in place, keeping its citations. A cited name is not removed. |
| `update_child_ref` | A child's relationship to the father or mother — Birth, Stepchild, Adopted… — in place, keeping the link's citations and the birth order. |
| `move_child` | Move a child to another family, keeping the link's citations, notes and privacy, placed in birth order; every link back to the old family goes, a duplicated one too. |
| `set_family_parent` | Set, replace (when asked) or remove the father or mother of an existing family, keeping both sides of each person's link. |
| `update_event_ref` | A person's reference to a shared event, in place: the role, or an attribute set, added or removed by name, keeping its citations. |
| `update_attribute` | Set or remove ONE attribute on a person, family, event, media object, source or citation, in place, keeping its citations; picked by name, and by part of its value where the name repeats. A removed attribute's citations are named. |
| `update_object_fields` | Scalar fields on anything else (places, notes, repositories). Structural lists are refused. |
| `update_place` | Place type, parent enclosure, name, title, coordinates. The parent must already exist (never minted from a name), cycles are refused, and multi-entry dated enclosures are refused rather than flattened. |
| `update_url` | Edit or remove ONE existing URL entry on a person/place/repository, matched by substring — must match exactly one. The fix for a link filed under the wrong type. |
| `link_repository` | Link an existing source to an existing repository, with call number and medium. |
| `link_repositories` | The same for many sources in one call, each row reported. |
| `tag_object` | Attach a named Tag to an object, creating the tag if it doesn't exist. |
| `set_private` | Set or clear the Gramps private flag on an object. |
| `merge_objects` | Merge duplicates via the server's own merge, inside one transaction. Dry-run by default, and the dry run names what else Gramps would merge — a person merge can merge two families, a family merge two fathers — and what it refuses. For places, settles the survivor's enclosures rather than keeping both places' parents. |
| `detach_object` | Remove an event/media/note/tag/child/repository reference, or a place's extra parent; optionally delete if orphaned. A tag can be named; a repository link narrowed to one call number. |
| `delete_object` | Permanently delete an object by handle or Gramps ID. Refused when it would strand a note or image only it holds, and for a source that still has citations. |

**Read**

| Tool | Purpose |
| --- | --- |
| `get_person` | Full detail: name, every alternate name (+ citation counts), gender, events, attributes (+ citation counts), families, media. |
| `get_family` | Relationship, parents, children, event count. |
| `get_event` | Type, date (with its modifier: "between 1882 and 1883", never "1882"), place, description, citation count. |
| `get_source` | Title, author, pubinfo, abbrev — and its real citation count. |
| `get_repository` | Name, type, URLs, and the sources it holds. |
| `get_object` | Raw record for any type. Returns the record as stored, which is what an edit needs. |
| `get_place` | Name, title, type, enclosure chain, coordinates, URLs. |
| `get_citation` | Page, confidence, date, source — and `cited_by_count`, where zero means orphan debris. |
| `get_note` | Type and full text. Notes hold reasoning, so nothing is truncated. |
| `get_media` | Path, mime, checksum, and how many objects reference it. |
| `consolidated_timeline` | One timeline merging several people or families — a household moving through censuses together. |
| `get_timeline` | Chronological events for a person or family, each with age, citation count and best confidence, plus an `uncited_count`. |
| `get_relationship` | How two people are related, in words and in generation distances. `all_paths` for a tree where the answer is not unique. |
| `assess_living` | The server's own living/dead verdict, walking relatives, with optional estimated dates and reasoning. |
| `event_span` | Elapsed time between two events — the arithmetic behind every age-at-event check. |
| `get_facts` | Record-holders — oldest at death, youngest parent, most children — across the tree, or across one person's ancestors or descendants. An implausible holder is usually a data error. |
| `get_dna_matches` | DNA matches with total and largest shared centiMorgans, and any common ancestor identified. `unattributed_count` is the open research. |
| `get_ydna` | Y-DNA haplogroup, broadest clade to terminal. Speaks to the direct paternal line only. |
| `parse_dna_segments` | Parse pasted shared-segment data into segments and totals. Reports a parse failure as such, rather than as an absence of shared DNA. |
| `get_researcher` | Researcher details, which travel inside every export. |
| `list_reports` | The 25 reports Gramps can generate: Ahnentafel, descendant, family group, kinship, fan and relationship charts, statistics, end-of-line. |
| `get_report_options` | One report's default options. Reports take a whole option dict, so this is what an override merges over. |
| `run_report` | Generate a report. Living people and private records are left out unless you ask for them. |
| `search_people` | Name substring + optional birth-year range (privacy-filtered). |
| `get_ancestors` / `get_descendants` | Walk the tree N generations (privacy-filtered). |
| `list_tags` | Every tag with handle, name and colour. |
| `list_object_types` | The type names the write tools accept: Gramps' standard ones and the tree's custom ones. |

**Audit**

| Tool | Purpose |
| --- | --- |
| `query_objects` | **Filter any collection server-side with GrampsQL.** The workhorse for audits — read the syntax traps below. |
| `get_backlinks` | What references this object — the only correct way to ask "is this source cited?". |
| `find_duplicates` | Candidate duplicates by strategy: `media_checksum`, `source_title`, `citation_page`, `vital_events`, `person_name`. |
| `list_unsourced_facts` | Events with no citation, or tagged `UNSOURCED`. |
| `check_family_links` | Person↔family links checked in both directions: a family listed twice, a link one side lacks, a link to nothing. Each finding says how to repair it. |
| `db_stats` | Counts of people/families/events/citations/etc. |
| `query_records` | **The structured query engine.** Indexed columns, `json_path` into the stored object, relationship traversal, regex/like/in, ordering, keyset paging. The only way to filter events by type. Refuses the forms the server answers wrongly — `type` as a column, a date's `year`, a list compared with anything but `in` — and says what works. |
| `list_event_types` | The tree's event type vocabulary with the integers it stores. An unexpected name here is usually a typo Gramps accepted as a custom type. |
| `list_filter_rules` | Gramps' filter-rule vocabulary — "is a descendant of", "has a common ancestor with" — which neither GrampsQL nor `query_records` can express. |
| `list_custom_filters` | Saved filters on this instance, reusable by name. |
| `create_filter` | Save a reusable selection built from those rules. |
| `delete_filter` | Delete a saved filter. Touches the definition only. |
| `verify_tree` | Gramps' own genealogical plausibility checks — a mother at nine, a 120-year marriage, an unparseable date. A different audit from the citation sweeps. |
| `ocr_media` | Read a document image with the engine that suits it: existing OCR text or Tesseract for print, the image itself for an English hand, Transkribus for German and Norwegian handwriting, FamilySearch's index for census tables. **A finding aid, not evidence** — read the image before citing it. See [Reading documents](#reading-documents-ocr_media). |

**Research tasks** — Gramps Web's own task list

| Tool | Purpose |
| --- | --- |
| `add_research_task` | Add a task to Gramps Web's Tasks view, written as its New Task form writes one: a Source tagged `ToDo`, with `Status` and `Priority` source attributes and the description as a To Do note. |
| `list_research_tasks` | The tasks, in the Tasks view's order, with status, priority, tags, attributes and description; filtered by status, tag or attribute value. A private task is a redacted stub. |
| `update_research_task` | Set a task's status, priority, privacy or attributes — each **replaced**, never appended beside the old value, which Gramps Web would not show — or add a paragraph to its description. A private task's description note is private too; making the task public leaves a private note private unless `private_note=false`. |

A task made here and one made in Gramps Web are the same thing: either shows
in the Tasks view, and in desktop Gramps' To Do gramplet through its note. A
task is a to-do, not evidence, so it is never cited. These are not the
background jobs `list_jobs` reports.

**Ops**

| Tool | Purpose |
| --- | --- |
| `export_backup` | Full-tree Gramps XML dump to a new file; never replaces one. **Run this before any bulk write.** |
| `list_transactions` | Recent writes: what changed, when, by which user. Also how you check whether another session is writing. |
| `undo_transaction` | Undo a transaction, after a conflict check. Dry-run by default. Returns a `task_id`. |
| `list_jobs` | Recent background jobs for this tree, newest first. |
| `get_transaction` | One transaction in full, including the objects it changed. Read before undoing. |
| `get_record_history` | Who added, edited or deleted one record, and when — each change with its transaction. A deleted record by its handle. `field` narrows it to the writes that changed one field, with its value before and after. On gramps-webapi 3.21 it reads the whole transaction log instead, which is slower, and says so. |
| `get_job` | Whether a background job finished, and whether it worked. Undo, verification and reindex all dispatch to a worker. |
| `reindex_search` | Rebuild the full-text index behind Gramps Web's search box. The server updates it after each write; rebuild after a large import, or when search misses something the tree holds. |

**Reference layer** (read-only, never touches the tree)

| Tool | Purpose |
| --- | --- |
| `consult_reference` | Look a person up in the legacy GEDCOM exports as an untrusted **hint**. See [Reference layer](#reference-layer-legacy-gedcoms). |

### Querying with GrampsQL

`query_objects` filters in the database rather than pulling a collection and
sifting it in Python. The syntax has traps, verified on every CI run against
gramps-webapi 3.21.1, 3.22.3 and 3.23.1:

- Equality is a **single `=`**. `page == ""` is a parse error.
- `~` is substring: `description ~ "1871"`.
- `.length` works on any list: `media_list.length = 0`.
- **A field the object does not have matches nothing, without an error.** A
  zero count can mean a misspelt field.
- **`type` is such a field on events.** `type = "Birth"` matches nothing,
  silently, on a tree with hundreds of births. Use `query_records` with
  `event_type` instead.
- **A source has no `citation_list`** — citations point *at* sources. Use
  `get_backlinks` to find uncited sources. Querying `citation_list` on sources
  matches none, cited or not.
- **Booleans compare as integers**: `private = 1`, not `private = true`.
- **A list is searched through its items.** `~` on the list itself asks
  whether the value *is* an item, so `urls ~ "http"` matches nothing.
  `query_objects` refuses it and names the form that works: `.any.` (or
  `.all.`) reaches the items, and `get_<type>` follows a handle.
- **Every condition reads the whole collection**, object by object, so a
  GrampsQL read is allowed 120 seconds: an OR of two list conditions took 29
  seconds over 6,000 citations.

Useful ones:

```
citations   confidence >= 3 AND page = ""                high-confidence claims with no locator
sources     media_list.length = 0                        documents with no image attached
media       desc = ""                                    media nothing identifies
person      urls.any.path ~ "findagrave"                 a URL anywhere among a person's links
citation    note_list.any.get_note.text.string ~ "x"     a citation whose note mentions x
```

Every tool has an LLM-facing docstring explaining when to use it, parameter
semantics, and good citation practice.

---

## Reading documents: `ocr_media`

Gramps Web's own OCR is Tesseract, which reads print. The documents a family
history turns on are handwritten — wills, deeds, letters, German church books
in Kurrent, Norwegian parish registers — and which reader to trust depends on
the hand and the language. `ocr_media` routes by what the caller says the
document is (`doc_type`) and its language (`lang`):

| Document | Read by | Why |
| --- | --- | --- |
| `print` | Text the media already carries — a Transcript note, a PDF's text layer — or the Library of Congress's or the Internet Archive's OCR for a page a URL in its sources names; otherwise Tesseract, through Gramps Web | Existing text costs nothing; Tesseract reads print well |
| `hand`, English | The image, returned as MCP image content with a *diplomatic transcription* instruction for the calling model: spelling as written, line breaks and abbreviations kept, `[?]` where unsure. `second_witness` adds Transkribus, and the result asks for every name and number where the two readings differ | Vision models read 18th- and 19th-century English hands at 5.7–7 % character error, better than dedicated engines (Humphries et al.) |
| `hand`, German | Transkribus only (its German Kurrent model). Without Transkribus, refused — never a vision read, even when asked, and not for a German table or volume either | Vision models measure **48.8 %** character error on historical German (METATR, May 2026, READ-2016) |
| `hand`, Norwegian | Transkribus (NorHand 1820–1940), with the image to check it; without Transkribus, the image and a warning of its error rate | Vision models measure about 10 % on Norwegian (METATR, NorHand) |
| `hand`, other languages | Transkribus' general model (Text Titan II), with the image; without it, the image and a warning | No benchmark known for a vision read |
| `table` | Not read: FamilySearch's index (`get_records_on_image` in familysearch-mcp) | An engine reads across a row and loses which cell belongs to which person |
| `volume` | FamilySearch Full-Text Search first, then Transkribus page by page | Searching a volume beats reading it |

`engine` asks for one reader outright: `existing`, `tesseract`, `vision` (refused
for German handwriting) or `transkribus`. A returned image is scaled to what a
current Claude model reads without scaling it again (2576 pixels on the long
edge, 3.75 megapixels); `region=[x1, y1, x2, y2]`, in percent, returns part of
the page at full detail. A PDF is read a page at a time (`page`): its text
layer, and the scan embedded in the page; a page with no scan is rendered by
Gramps Web, which renders only the first.

Every result carries its provenance — `{engine, model, date}` — and the same
caveat: **the transcript is never the evidence; the citation stays on the
image.** `store=true` keeps a machine reading as a Transcript note on the media
object, headed with its engine, model and date, and Transkribus' PAGE XML in a
second, preformatted note, since Transkribus deletes it a day after the job. A
vision reading is the calling model's to keep, with `add_note`. A later call
for print finds a stored reading and returns it rather than reading again.

**Transkribus is optional, and costs money.** API jobs are charged half the
app's rate — about 0.5 credits, roughly €0.12, a page — so a page is sent only
when the call passes `spend_credits=true` or this month's
`GRAMPS_MCP_TRANSKRIBUS_PAGE_BUDGET` has room. Each result says what it spent
and how many pages the month has used; the count is kept in
`transkribus-usage.json` in the cache directory; a page is counted before it is
sent, and not sent if it cannot be counted. A job for the same file, page and
model within a day is fetched again, not paid for again; a failed one is
dropped, so the next call can try afresh. A private media object is never
sent, nor a page in a language no model here covers. Images go to READ-COOP in Austria; its documentation says
an image is used for its job only and not stored, and its terms let it use
submitted material to improve its products.

**The Transkribus path is built to the published API and has not yet been
exercised live.** It follows the Metagrapho v1 OpenAPI document and READ-COOP's
documented login, and is tested against fixtures of those shapes; no account
was used to build it. The Library of Congress and Internet Archive paths, the
image and PDF handling and Tesseract through a task queue were checked against
a live tree and the live archives on 2026-10-06.

---

## Reference layer (legacy GEDCOMs)

`consult_reference` searches the GEDCOM files listed in your TOML config and
returns, **per file**, matching individuals and their claimed facts. Crucially,
each fact is flagged **whether the legacy tree attached a source**, with the
source text if present — so the assistant can distinguish "Ancestry cites an
actual death certificate" from "unsourced guess."

- Parses **GEDCOM 5.5.1**, including the **Ancestry dialect** (`_APID`, `_TREE`,
  …). Custom underscore tags are carried through, not choked on.
- Files load **lazily** and their parsed form is **cached on disk** (keyed by
  path/size/mtime), so a big export is parsed once.
- Each file has a **trust note** surfaced in results.

These are **untrusted hints**. The intended loop: consult a hint → decide which
*real* record to hunt down → create the fact in the tree citing *that record* —
never copy a hint in as if it were sourced.

Your real GEDCOMs are never committed (`.gitignore` excludes `*.ged` except the
synthetic test fixture).

---

## Worked example: a person, with a cited birth

> **You:** Add Martha Ellery, female, born 12 Jan 1890 in Columbus, Ohio. I have
> her birth certificate (Ohio certificate #12345); cite it at very-high confidence.

The assistant calls **one** tool:

```jsonc
add_person({
  "given": "Martha", "surname": "Ellery", "gender": "female",
  "birth": {
    "type": "Birth",
    "date": "12 Jan 1890",
    "place": "Columbus, Ohio, USA",
    "citation": {
      "source_title": "Ohio Birth Certificate #12345",
      "page": "certificate no. 12345",
      "confidence": "very_high"
    }
  }
})
```

Behind the scenes the server: creates the **Source** "Ohio Birth Certificate
#12345" → creates a **Citation** on it (page + very-high confidence) → finds or
creates the **Place** "Columbus, Ohio, USA" → creates the **Birth Event** (dated,
placed, carrying the citation) → creates the **Person** referencing that event as
their primary birth. It returns the new `gramps_id` (e.g. `I0001`).

Ask **`list_unsourced_facts`** any time to see what still needs a source. If you
add a fact you can't yet source, pass `require_citation=false` and it'll be
tagged `UNSOURCED` for that audit list.

---

## Development

```bash
git clone https://github.com/ianderso/gramps-evidence-mcp
cd gramps-evidence-mcp
uv sync --extra dev
uv run pytest
```

The suite runs against an in-memory fake of `gramps-webapi` served through
[respx](https://lundberg.github.io/respx/), so it needs no Gramps Web instance,
and it fails any test that tries to open a real connection. It exercises the
tool surface the way a client calls it: every tool's schema, the refusal of
unknown arguments, the whole-object write rule across every editing tool, the
privacy filter on each bulk output, and the error envelope every tool returns
instead of raising.

A second suite, `tests/live`, runs against a real, throwaway gramps-webapi --
3.21.1, 3.22.3 and 3.23.1 in CI, installed from PyPI with no Docker needed. It checks
every server behaviour [docs/PITFALLS.md](docs/PITFALLS.md) describes, drives
the tools end to end, and runs the same scenarios against the fake and the
server and requires the same answers, so the fake cannot drift from what it
stands in for. [CONTRIBUTING.md](CONTRIBUTING.md) says how to run it.

[CONTRIBUTING.md](CONTRIBUTING.md) says what a change is expected to carry.

---

## Limitations

- **Requires gramps-webapi 3.21 or later** (Gramps 6.0); tested on every CI
  run against 3.21.1, 3.22.3 and 3.23.1. On an older server every tool answers with an
  `unsupported_server` error naming its version: 3.20 lacks the query
  endpoints the searches and privacy filter use. Your instance's
  `/api/openapi.json` is authoritative — check it if a call behaves
  unexpectedly, and see [docs/PITFALLS.md](docs/PITFALLS.md) for where the
  API's behaviour has surprised before.
- **No tree selector.** On gramps-webapi the tree is bound to the account you
  authenticate as; no data endpoint takes a tree parameter. To work against a
  different tree, use credentials belonging to it.
- Type names (`"Birth"`, `"Married"`) are matched against Gramps' English
  standard names and the tree's custom ones, ignoring case and punctuation,
  with a few clear synonyms ("Born" is Birth). Anything else is refused with
  the closest names unless `allow_new_type` asks for a new custom type, so a
  type name in another language is refused rather than stored: give the
  English name. See [docs/PITFALLS.md](docs/PITFALLS.md) section 26.
- `search_people`, `list_unsourced_facts` and `find_duplicates` read whole
  collections. That suits a personal tree of a few thousand people; they are not
  built for very large databases.
- `get_facts` is computed by the server on every call and takes tens of seconds
  on a tree of under a thousand people; the call is allowed three minutes.
- `ocr_media`'s Transkribus path has not been run against the live service.
  Gramps Web renders only a PDF's first page, so a later page with no scan
  embedded in it cannot be read as an image.

---

## Repository layout

```
src/gramps_evidence_mcp/    the MCP server
  server.py                 tool definitions (the 97 tools) and the entry point
  service.py                genealogy operations; edits go through _mutate()
  client.py                 gramps-webapi REST client
  mapping.py                Gramps object <-> JSON shapes
  models.py                 Pydantic input models shared by the tools
  privacy.py                living-person assessment and redaction
  gedcom_ref.py             read-only reference layer over legacy GEDCOMs
  ocr.py                    ocr_media's routing, images, archives and Transkribus
  config.py                 env vars + TOML
tests/                      against an in-memory fake; no live server needed
  live/                     against a throwaway gramps-webapi (CI: 3.21.1, 3.22.3, 3.23.1)
docker/                     docker-compose for a local Gramps Web
docs/
  ARCHITECTURE.md           how the server is put together
  PITFALLS.md               gramps-webapi behaviours that have cost real data
  ROADMAP.md                what is planned (nothing), and what is not
```

**[`docs/PITFALLS.md`](docs/PITFALLS.md) is required reading before writing a
script against `gramps-webapi` directly.** Every item in it was learned by losing
something: a `keys=` PUT that wiped parent links on 65 families, a citation
handle reused across two claims that mis-graded 107 parent-child links, and two
concurrent sessions that took the write path down for ~18 hours.
