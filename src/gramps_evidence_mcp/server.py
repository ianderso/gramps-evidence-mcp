"""MCP tool definitions over a Gramps Web tree. Transport is stdio.

Docstrings and ``Field`` descriptions in this module are published as the tool
descriptions and JSON schema, so they are written for the model calling the
tool rather than for a developer reading the source. Business logic lives in
:mod:`gramps_evidence_mcp.service`.

The practice the tool surface enforces: every fact gets a citation, pointing at
a specific record rather than a whole database. ``consult_reference`` gathers
hints from legacy trees; hints are not sources.
"""

from __future__ import annotations

import asyncio
import json
import logging
import traceback
from typing import Any

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field, model_validator

from . import __version__
from .client import (
    ENDPOINTS,
    GrampsApiError,
    GrampsWebClient,
    InvalidIdentifierError,
    UnsupportedServerError,
)
from .config import Config, ConfigError, load_config, transport_settings
from .gedcom_ref import ReferenceLibrary
from .models import (
    CitationEdit,
    CitationInput,
    Confidence,
    EventInput,
    Gender,
    NameMatch,
    NameParts,
    RepositoryLink,
    VitalEventInput,
)
from .service import (
    AmbiguousPlaceError,
    CitationRequiredError,
    GrampsService,
    InvalidCarryTargetError,
    MultipleEnclosuresError,
    NotFoundError,
    UnknownTypeError,
)

logger = logging.getLogger("gramps_evidence_mcp")

#: MCP tool annotations, so a client can tell a read from a write and ask
#: before the ones that change or remove something. "Destructive" is the
#: protocol's word for any change that is not purely additive: an edit that
#: replaces a value counts, as does a merge or a delete. The tree is the
#: operator's own, so nothing here reaches an open world.
READS = ToolAnnotations(read_only_hint=True, open_world_hint=False)
ADDS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
EDITS = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=True,
    open_world_hint=False,
)
REMOVES = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=True,
    idempotent_hint=False,
    open_world_hint=False,
)
#: Creates a new local file and never replaces one.
WRITES_LOCAL_FILE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=False,
)
#: Leaves the tree alone but makes the server do work: a report file, an index.
RUNS_ON_SERVER = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=True,
    open_world_hint=False,
)

# MCPServer is the high-level server in mcp>=2.0, formerly FastMCP.
mcp = MCPServer(
    "gramps-evidence-mcp",
    version=__version__,
    instructions=(
        "Read/write tools over a self-hosted Gramps Web genealogy tree. "
        "Golden rule: every fact gets a citation. Use consult_reference "
        "for untrusted legacy-tree hints, then cite the real underlying record."
    ),
)


# --------------------------------------------------------------------------- #
# lazily-built shared state (config + authenticated client + service)
# --------------------------------------------------------------------------- #
class _State:
    def __init__(self) -> None:
        self.config: Config | None = None
        self.client: GrampsWebClient | None = None
        self.service: GrampsService | None = None
        self.library: ReferenceLibrary | None = None
        self._lock = asyncio.Lock()

    async def service_(self) -> GrampsService:
        async with self._lock:
            if self.service is None:
                cfg = self.config or load_config()
                self.config = cfg
                if self.client is None:
                    client = GrampsWebClient(
                        cfg.api_url,
                        cfg.username,
                        cfg.password,
                        timeout=cfg.request_timeout,
                    )
                    await client.login()
                    self.client = client
                # Checked until it passes, so an upgrade needs no restart.
                await self.client.require_supported_server()
                self.service = GrampsService(self.client, cfg)
            return self.service

    def library_(self) -> ReferenceLibrary:
        if self.library is None:
            cfg = self.config or load_config()
            self.config = cfg
            self.library = ReferenceLibrary.from_config(cfg.reference_files, cfg.gedcom_cache_dir)
        return self.library


state = _State()


def _error(exc: Exception) -> dict:
    """Uniform, LLM-actionable error envelope (no record contents leaked)."""
    if isinstance(exc, CitationRequiredError):
        return {"error": "citation_required", "message": str(exc)}
    if isinstance(exc, NotFoundError):
        return {"error": "not_found", "message": str(exc)}
    if isinstance(exc, ConfigError):
        return {"error": "config", "message": str(exc)}
    if isinstance(exc, InvalidIdentifierError):
        return {"error": "invalid_identifier", "message": str(exc)}
    if isinstance(exc, AmbiguousPlaceError):
        return {"error": "ambiguous_place", "message": str(exc)}
    if isinstance(exc, MultipleEnclosuresError):
        return {"error": "multiple_enclosures", "message": str(exc)}
    if isinstance(exc, UnknownTypeError):
        return {"error": "unknown_type", "message": str(exc)}
    if isinstance(exc, InvalidCarryTargetError):
        return {"error": "invalid_carry_to", "message": str(exc)}
    if isinstance(exc, UnsupportedServerError):
        return {"error": "unsupported_server", "message": str(exc)}
    if isinstance(exc, GrampsApiError):
        hint = ""
        if exc.status in (401, 403):
            hint = (
                " The MCP API user needs role >= editor to write. Check "
                "GRAMPS_MCP_USERNAME/PASSWORD and the user's role."
            )
        return {"error": "api", "status": exc.status, "message": exc.detail + hint}
    if isinstance(exc, httpx.TimeoutException):
        # httpx's timeouts carry no message, so this used to reach the caller
        # as "unexpected" with an empty one (TOOL-REQUESTS #27).
        return {
            "error": "timeout",
            "message": f"gramps-webapi did not answer in time ({type(exc).__name__}). "
            "The server may be busy, or the request slow: a GrampsQL query reads "
            "every object in the collection. Narrow it, or use query_records. A "
            "write may still have landed: re-read before retrying one. The limit "
            "is request_timeout in the TOML file.",
        }
    if isinstance(exc, httpx.TransportError):
        return {
            "error": "connection",
            "message": f"Could not reach gramps-webapi ({type(exc).__name__}"
            + (f": {exc}" if str(exc) else "")
            + "). Check that it is running at GRAMPS_MCP_API_URL. A write may "
            "have landed before the connection failed: re-read before retrying one.",
        }
    # The stack, not the message: a message can quote record contents, and
    # the log holds ids and handles only.
    logger.error(
        "unexpected tool error %s\n%s",
        type(exc).__name__,
        "".join(traceback.format_tb(exc.__traceback__)),
    )
    return {
        "error": "unexpected",
        "message": str(exc) or f"{type(exc).__name__}, with no message.",
    }


def _include_private() -> Any:
    """The per-call opt-in every privacy-filtered tool takes."""
    return Field(
        default=False,
        description="Show living people and private records in full. Only when the "
        "user asks for them; they are withheld by default.",
    )


def _allow_new_type() -> Any:
    """The opt-in every tool that writes a type name takes (PITFALLS 26)."""
    return Field(
        default=False,
        description="Accept a type that is neither a Gramps standard type nor one of "
        "the tree's custom types, creating it as a new custom type. Only when meant: "
        "a near-miss is refused with the closest names.",
    )


# ==========================================================================  #
# WRITE TOOLS
# ==========================================================================  #
@mcp.tool(annotations=ADDS)
async def add_person(
    given: str = Field(description="Given/first name(s), e.g. 'John Robert'."),
    surname: str = Field(default="", description="Family name / surname."),
    gender: Gender = Field(default=Gender.unknown, description="female, male, or unknown."),
    name_prefix: str = Field(default="", description="Surname prefix, e.g. 'van', 'de'."),
    name_suffix: str = Field(default="", description="Suffix, e.g. 'Jr.', 'III'."),
    birth: VitalEventInput | None = Field(
        default=None,
        description="Optional birth event. Include a citation unless recording "
        "as unsourced. The event type defaults to 'Birth'.",
    ),
    death: VitalEventInput | None = Field(
        default=None, description="Optional death event; type defaults to 'Death'."
    ),
    require_citation: bool = Field(
        default=True,
        description="If True (default), any birth/death event MUST carry a citation "
        "or the call fails. Set False to record the event anyway, stamped with the "
        "UNSOURCED attribute so list_unsourced_facts can find it later.",
    ),
) -> dict:
    """Create a new person, optionally with cited birth and/or death events.

    Use this to add someone to the tree. Good practice: attach the birth event
    with a citation to the *specific* record that proves it (a birth or baptism
    certificate, a census entry), setting the citation's confidence honestly.
    If you only have a legacy-tree hint with no underlying record, either omit
    the event or set require_citation=False so it's flagged for follow-up.

    Returns the new person's handle and gramps_id.
    """
    try:
        svc = await state.service_()
        name = NameParts(given=given, surname=surname, prefix=name_prefix, suffix=name_suffix)
        return await svc.add_person(name, gender, birth, death, require_citation)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_event_to_person(
    person: str = Field(description="Person handle or gramps_id (e.g. 'I0007')."),
    event: EventInput = Field(description="The event to add (type, date, place, citation)."),
    require_citation: bool = Field(
        default=True,
        description="Require a citation (default). False records it UNSOURCED.",
    ),
) -> dict:
    """Add a dated/placed event (fact) to an existing person.

    Use for facts beyond birth/death: residence, occupation, census, baptism,
    immigration, marriage-adjacent events, etc. The event's citation should point
    at the record establishing the fact. Adding a 'Birth'/'Death' event will set
    the person's primary birth/death reference if not already set.
    """
    try:
        svc = await state.service_()
        return await svc.add_event_to_person(person, event, require_citation)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_family(
    father: str | None = Field(default=None, description="Father: handle or gramps_id."),
    mother: str | None = Field(default=None, description="Mother: handle or gramps_id."),
    children: list[str] | None = Field(default=None, description="Child handles or gramps_ids."),
    marriage: VitalEventInput | None = Field(
        default=None,
        description="Optional marriage event; type defaults to 'Marriage'. Cite it.",
    ),
    relationship: str = Field(
        default="Married",
        description="Family relationship type, e.g. 'Married', 'Unmarried', 'Civil Union'.",
    ),
    require_citation: bool = Field(
        default=True, description="Require a citation on the marriage event (default)."
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Create a family linking parents and children, with an optional cited marriage.

    All members must already exist (create them first with add_person). Creating
    the family automatically links each person's family/parent-family lists, so
    you don't need to update the individuals separately. Cite the marriage event
    to a marriage record where possible.
    """
    try:
        svc = await state.service_()
        return await svc.add_family(
            father,
            mother,
            children,
            marriage,
            relationship,
            require_citation,
            allow_new_type=allow_new_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_source(
    title: str = Field(description="Source title, e.g. '1900 U.S. Federal Census'."),
    author: str | None = Field(default=None, description="Author/creator of the source."),
    publication_info: str | None = Field(
        default=None, description="Publication info (publisher, date, series)."
    ),
    abbreviation: str | None = Field(default=None, description="Short abbreviation."),
    repository: str | None = Field(
        default=None, description="Repository handle/gramps_id that holds this source."
    ),
    call_number: str | None = Field(
        default=None, description="Call number / reference within the repository."
    ),
    media_type: str = Field(
        default="Unknown",
        description="Medium of the source at that repository, e.g. 'Book', "
        "'Film', 'Electronic'. Only used with repository.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Create a Source (a body of evidence: a record set, book, certificate, website).

    In the Gramps evidence model a Source is what you cite *through* a Citation.
    Create the source once, then create citations against it for each fact it
    supports. Optionally link it to a Repository (where the source is held).
    """
    try:
        svc = await state.service_()
        return await svc.add_source(
            title,
            author,
            publication_info,
            abbreviation,
            repository,
            call_number,
            media_type,
            allow_new_type=allow_new_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_citation(citation: CitationInput = Field(description="Citation details.")) -> dict:
    """Create a standalone Citation on a Source (or reuse an existing one).

    Usually you don't call this directly -- pass a CitationInput to add_person /
    add_event_to_person / add_family instead, which creates the citation and
    attaches it to the fact in one step. Use this when you want a reusable
    citation handle to attach to several facts.
    """
    try:
        svc = await state.service_()
        return await svc.add_citation(citation)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_repository(
    name: str = Field(description="Repository name, e.g. 'National Archives (NARA)'."),
    repository_type: str = Field(
        default="Archive",
        description="Type: 'Library', 'Archive', 'Cemetery', 'Church', 'Web site', etc.",
    ),
    url: str | None = Field(default=None, description="Optional website URL."),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Create a Repository (an institution or place that holds sources).

    Repositories sit at the top of the evidence model: Repository -> Source ->
    Citation -> fact. Create these for archives, libraries, cemeteries, or
    websites you'll cite sources from.
    """
    try:
        svc = await state.service_()
        return await svc.add_repository(name, repository_type, url, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_note(
    text: str = Field(description="The note text."),
    note_type: str = Field(default="General", description="Note type, e.g. 'General', 'Research'."),
    target: str | None = Field(
        default=None, description="Optional object handle/gramps_id to attach the note to."
    ),
    target_type: str | None = Field(
        default=None,
        description="Type of the target: 'person', 'family', 'event', 'source', "
        "'citation', 'place', 'repository', 'media'.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Create a research/general note, optionally attached to an object.

    Use notes for research logs, reasoning about conflicting evidence, or
    transcriptions. Attach to a person/event/source by giving target + target_type.
    """
    try:
        svc = await state.service_()
        return await svc.add_note(target, target_type, text, note_type, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def attach_media(
    target: str = Field(description="Object handle/gramps_id to attach media to."),
    target_type: str = Field(
        default="person",
        description="Type of the target object: 'person', 'event', 'source', etc.",
    ),
    file_path: str | None = Field(
        default=None,
        description="Absolute path to a local image, PDF, audio or video file to "
        "upload. Use this OR media_ref.",
    ),
    description: str = Field(
        default="",
        description="What the document IS, in archival terms. Only used when uploading a new file.",
    ),
    media_ref: str | None = Field(
        default=None,
        description="Handle or gramps_id of a Media object ALREADY in the tree. "
        "Prefer this when the same document supports several people.",
    ),
) -> dict:
    """Attach an image or document to an object -- a new upload, or one already
    in the tree.

    With file_path, the bytes are uploaded into the tree's managed media
    directory (so don't point at files you don't want copied); if that exact
    file is already present it is reused rather than duplicated. With media_ref,
    an existing Media object is linked.

    One image should be ONE Media object, linked from each object it belongs to
    and cited once per fact it proves. Uploading the same photograph separately
    for the husband and the wife creates duplicates that have to be unpicked
    later. The write is verified afterwards: `verified: false` means nothing
    attached, whatever the rest of the result says.
    """
    try:
        svc = await state.service_()
        return await svc.attach_media(
            target, target_type, file_path, description, media_ref=media_ref
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def cite_event(
    event: str = Field(description="Event handle or gramps_id (e.g. 'E0007')."),
    citation: CitationInput = Field(description="Citation to attach to the event."),
) -> dict:
    """Attach a citation to an event that ALREADY exists.

    Use this to source an event you didn't create with an inline citation -- e.g.
    one added through the Gramps web UI, or an unsourced event surfaced by
    list_unsourced_facts. The citation is resolved/created (existing handle/id, or
    an inline source_title + page + confidence) and appended to the event's
    citation list (no duplicates). Does not change any other event field.
    """
    try:
        svc = await state.service_()
        return await svc.cite_event(event, citation)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_event(
    event: str = Field(description="Event handle or gramps_id (e.g. 'E0007')."),
    event_type: str | None = Field(
        default=None,
        description="New event type, e.g. 'Census', 'Visit'. Must already be a "
        "standard or custom type in the tree (list_object_types) unless "
        "allow_new_type is set. Omit to leave unchanged.",
    ),
    date: str | None = Field(
        default=None,
        description="New date, Gramps style: '1899', '12 JAN 1899', 'ABT 1900', "
        "'BEF 1950'; 'BET 1898 AND 1901' happened once within the range; "
        "'FROM 4 MAY 1864 TO 16 SEP 1864' lasted the whole span; 'FROM 1880' or "
        "'TO 1890' is open at one end. Omit to leave unchanged.",
    ),
    place: str | None = Field(
        default=None,
        description="New place: existing handle/gramps_id, exact title, or "
        "exact unique name — created only if nothing matches. Omit to leave "
        "unchanged.",
    ),
    description: str | None = Field(
        default=None, description="New free-text description. Omit to leave unchanged."
    ),
    clear_place: bool = Field(
        default=False,
        description="Remove the event's place, e.g. one no source states. An "
        "empty place string is refused rather than read as this.",
    ),
    clear_date: bool = Field(default=False, description="Remove the event's date."),
    allow_new_type: bool = Field(
        default=False,
        description="Accept an event_type the tree does not have yet, creating "
        "it as a custom type. Only for a deliberate new type, never a typo.",
    ),
) -> dict:
    """Edit an existing event in place: its type, date, place or description.

    Only what you pass changes. The event keeps its id, citations, notes, media
    and every person sharing it, so correct a wrong type or an unsupported place
    here rather than replacing the event. Citations: cite_event.
    """
    try:
        svc = await state.service_()
        return await svc.update_event(
            event,
            date,
            place,
            description,
            clear_place,
            event_type=event_type,
            clear_date=clear_date,
            allow_new_type=allow_new_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_event_ref(
    person: str = Field(description="Handle or gramps_id of the person to add the event to."),
    event: str = Field(description="Handle or gramps_id of the EXISTING event, e.g. 'E0007'."),
    role: str = Field(
        default="Primary",
        description="The person's role in it: 'Primary', 'Witness', 'Informant', "
        "'Godparent', 'Family', 'Clergy', or a custom role the tree already has.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Share an existing event with another person, in a role.

    For one census entry, residence or burial that several people took part in:
    each references the same event, so its citations and later corrections serve
    all of them. Refused if the person already has it. A new fact is
    add_event_to_person.
    """
    try:
        svc = await state.service_()
        return await svc.add_event_ref(person, event, role, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=REMOVES)
async def delete_object(
    object_type: str = Field(
        description="Type to delete: 'person', 'family', 'event', 'place', "
        "'source', 'citation', 'repository', 'media', 'note', 'tag'."
    ),
    target: str = Field(description="Handle or gramps_id of the object to delete."),
    carry_to: str | None = Field(
        default=None,
        description="Another object of the same type to receive the notes and images "
        "that only this one holds. Without it, such a delete is refused.",
    ),
) -> dict:
    """Permanently delete an object from the tree by handle or gramps_id.

    DESTRUCTIVE. The server also removes every reference to it. Refused when it
    is the only holder of a note or image (pass carry_to to move them), and for a
    source with citations, which the server would delete with it.
    """
    if object_type not in ENDPOINTS:
        return {
            "error": "invalid_object_type",
            "message": f"Unknown object type '{object_type}'. Must be one of: "
            + ", ".join(sorted(ENDPOINTS))
            + ".",
        }
    try:
        svc = await state.service_()
        return await svc.delete_object(object_type, target, carry_to=carry_to)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def tag_object(
    object_type: str = Field(
        description="Type of the object to tag: 'person', 'family', 'event', "
        "'source', 'citation', 'place', 'repository', 'media', 'note'."
    ),
    target: str = Field(description="Handle or gramps_id of the object to tag."),
    tag: str = Field(
        description="Tag name (found-or-created by exact match), e.g. 'Verified'. To "
        "REMOVE a tag: detach_object(child_kind='tag', child=<tag name>)."
    ),
    color: str | None = Field(
        default=None,
        description="Optional hex color for a newly-created tag, e.g. '#FF8800'. "
        "Defaults to '#4444FF'. Ignored if the tag already exists.",
    ),
) -> dict:
    """Attach a named Tag to an object, creating the Tag if it doesn't exist yet.

    Tags are lightweight cross-cutting labels ('Verified', 'Needs review',
    'DNA-confirmed'). Matching is by exact name; an existing tag is reused.
    """
    try:
        svc = await state.service_()
        return await svc.tag_object(object_type, target, tag, color)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_attribute(
    object_type: str = Field(
        description="Type of the object: 'person', 'event', 'family', 'media', "
        "'source', 'citation'."
    ),
    target: str = Field(description="Handle or gramps_id of the object."),
    name: str = Field(
        description="Attribute type/name, e.g. 'Occupation', 'Identification Number', "
        "or a custom name the tree already has."
    ),
    value: str = Field(description="Attribute value, e.g. 'Blacksmith'."),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Add a typed key/value attribute to an object.

    Sources and citations use a SrcAttribute; everything else uses an Attribute.
    The right class is chosen automatically from object_type.
    """
    try:
        svc = await state.service_()
        return await svc.add_attribute(object_type, target, name, value, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_url(
    object_type: str = Field(
        description="Type of the object: 'person', 'place', or 'repository' only."
    ),
    target: str = Field(description="Handle or gramps_id of the object."),
    url: str = Field(description="The URL, e.g. 'https://www.findagrave.com/memorial/123'."),
    description: str = Field(default="", description="Optional link description."),
    url_type: str = Field(
        default="Web Home",
        description="URL type: 'Web Home', 'Web Search', 'E-mail', 'FTP', or a custom "
        "type the tree already has.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Add a web URL to a person, place, or repository.

    Only these three object types carry a URL list. For sources/citations, record
    a web address as an attribute (add_attribute) instead.
    """
    try:
        svc = await state.service_()
        return await svc.add_url(object_type, target, url, description, url_type, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_url(
    object_type: str = Field(
        description="Type of the object: 'person', 'place', or 'repository' only."
    ),
    target: str = Field(description="Handle or gramps_id of the object."),
    match: str = Field(
        description="Case-insensitive substring identifying WHICH url entry to "
        "edit, tested against each entry's path and description (e.g. "
        "'findagrave.com/memorial/123'). Must match exactly one entry; matching "
        "none or several returns the candidate list instead of guessing.",
    ),
    url: str | None = Field(default=None, description="New URL path. Omit to keep."),
    description: str | None = Field(
        default=None, description="New link description. Omit to keep."
    ),
    url_type: str | None = Field(
        default=None,
        description="New URL type, e.g. 'Web Home', 'Web Search', or a custom type "
        "the tree already has. Omit to keep.",
    ),
    remove: bool = Field(
        default=False, description="Remove the matched entry instead of editing it."
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Edit or remove ONE existing URL entry on a person, place, or repository.

    add_url can only append -- this corrects an entry already there (the classic
    case: a Find a Grave link filed under the wrong type). The other fields on
    the object are untouched.
    """
    try:
        svc = await state.service_()
        return await svc.update_url(
            object_type,
            target,
            match,
            url=url,
            description=description,
            url_type=url_type,
            remove=remove,
            allow_new_type=allow_new_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def set_private(
    object_type: str = Field(
        description="Type of the object: 'person', 'family', 'event', 'source', etc."
    ),
    target: str = Field(description="Handle or gramps_id of the object."),
    private: bool = Field(
        default=True, description="True to mark private (default), False to un-mark."
    ),
) -> dict:
    """Set (or clear) the Gramps private flag on an object.

    Private records are withheld from bulk output (queries, searches, tree
    walks, timelines, reports) unless a call passes include_private.
    """
    try:
        svc = await state.service_()
        return await svc.set_private(object_type, target, private)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_source(
    source: str = Field(description="Source handle or gramps_id (e.g. 'S0001')."),
    title: str | None = Field(default=None, description="New title. Omit to leave unchanged."),
    author: str | None = Field(default=None, description="New author. Omit to leave unchanged."),
    publication_info: str | None = Field(
        default=None, description="New publication info. Omit to leave unchanged."
    ),
    abbreviation: str | None = Field(
        default=None, description="New abbreviation. Omit to leave unchanged."
    ),
) -> dict:
    """Edit an existing source's title, author, publication info, and/or abbreviation.

    Only the fields you provide are changed; the rest are left as-is.
    """
    try:
        svc = await state.service_()
        return await svc.update_source(source, title, author, publication_info, abbreviation)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def link_repository(
    source: str = Field(description="Source handle or gramps_id (e.g. 'S0001')."),
    repository: str = Field(description="Repository handle or gramps_id (e.g. 'R0001')."),
    call_number: str | None = Field(
        default=None, description="Call number / reference within the repository."
    ),
    media_type: str = Field(
        default="Unknown",
        description="Medium of the source at the repository, e.g. 'Book', "
        "'Film', 'Electronic', 'Unknown'.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Link an existing source to an existing repository that holds it.

    Adds a repository reference (with an optional call number) to the source.
    Both objects must already exist. Duplicate links to the same repository are
    skipped.
    """
    try:
        svc = await state.service_()
        return await svc.link_repository(
            source, repository, call_number, media_type, allow_new_type
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def link_repositories(
    items: list[RepositoryLink] = Field(
        min_length=1,
        max_length=500,
        description="Rows of {source, repository, call_number?, media_type?}.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Link many sources to their repositories in one call -- a sweep.

    Each row is link_repository: its own write and transaction, so a failed row
    does not stop the rest. Each row reports linked, already_linked, missing or
    error.
    """
    try:
        svc = await state.service_()
        return await svc.link_repositories(items, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_event_to_family(
    family: str = Field(description="Family handle or gramps_id (e.g. 'F0001')."),
    event: EventInput = Field(description="The event to add (type, date, place, citation)."),
    require_citation: bool = Field(
        default=True,
        description="Require a citation (default). False records it UNSOURCED.",
    ),
) -> dict:
    """Add a dated/placed event (fact) to an existing family.

    Use for family-level facts: marriage, divorce, residence, census. The event
    is added with the 'Family' role. Cite it to the record establishing the fact.
    """
    try:
        svc = await state.service_()
        return await svc.add_event_to_family(family, event, require_citation)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_child_to_family(
    family: str = Field(description="Family handle or gramps_id (e.g. 'F0001')."),
    child: str = Field(description="Child person handle or gramps_id."),
    frel: str = Field(
        default="Birth",
        description="Relationship to the father, e.g. 'Birth', 'Adopted', 'Stepchild'.",
    ),
    mrel: str = Field(
        default="Birth",
        description="Relationship to the mother, e.g. 'Birth', 'Adopted', 'Stepchild'.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Add an existing person as a child of an existing family.

    Links the child both ways: a ChildRef is added to the family and the family
    is added to the child's parent-family list. The child must already exist.
    Duplicate children are skipped.
    """
    try:
        svc = await state.service_()
        return await svc.add_child_to_family(family, child, frel, mrel, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_child_ref(
    family: str = Field(description="Family handle or gramps_id (e.g. 'F0001')."),
    child: str = Field(description="The child's person handle or gramps_id."),
    frel: str | None = Field(
        default=None,
        description="Relationship to the father: 'Birth', 'Adopted', 'Stepchild', "
        "'Foster', 'Sponsored', 'Unknown', 'None'. Omit to keep.",
    ),
    mrel: str | None = Field(
        default=None, description="Relationship to the mother, the same values. Omit to keep."
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Change a child's relationship to the father or mother -- a stepson held
    as a birth child -- in place.

    The child keeps the link's citations, notes and its place in the birth
    order, which detaching and re-adding the child loses.
    """
    try:
        svc = await state.service_()
        return await svc.update_child_ref(
            family, child, frel=frel, mrel=mrel, allow_new_type=allow_new_type
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def check_family_links(
    limit: int = Field(default=200, ge=1, le=2000, description="Maximum findings to return."),
    include_private: bool = _include_private(),
) -> dict:
    """Audit the links between people and families, in both directions.

    Finds a family a person lists twice, a child a family lists twice, a link
    one side holds and the other lacks, and links to objects that do not
    exist. Each finding says how to repair it. Reports only.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "check_family_links"):
            return await svc.check_family_links(limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_alternate_name(
    person: str = Field(description="Person handle or gramps_id (e.g. 'I0001')."),
    given: str = Field(default="", description="Given/first name(s) for the alternate name."),
    surname: str = Field(default="", description="Family name / surname."),
    name_prefix: str = Field(default="", description="Surname prefix, e.g. 'van', 'de'."),
    name_suffix: str = Field(default="", description="Suffix, e.g. 'Jr.', 'III'."),
    nickname: str = Field(default="", description="Nickname."),
    name_type: str = Field(
        default="Also Known As",
        description="Kind of alternate name, e.g. 'Also Known As', 'Birth Name', 'Married Name'.",
    ),
    citation: CitationInput | None = Field(
        default=None,
        description="The record that gives this form of the name. It goes on the name "
        "itself -- 'this record spells it so' -- not on the person. Cite an existing "
        "name with cite_object(object_type='name').",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Add an alternate (non-primary) name to a person, cited to the record using it.

    Use for maiden/married names, aliases, anglicized forms, or nicknames-of-record.
    The person's primary name is left unchanged. Correct or remove one later
    with update_alternate_name.
    """
    try:
        svc = await state.service_()
        name = NameParts(
            given=given,
            surname=surname,
            prefix=name_prefix,
            suffix=name_suffix,
            nick=nickname,
        )
        return await svc.add_alternate_name(person, name, name_type, citation, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ==========================================================================  #
# READ TOOLS
# ==========================================================================  #
@mcp.tool(annotations=READS)
async def get_person(
    person: str = Field(description="Person handle or gramps_id, e.g. 'I0001'."),
) -> dict:
    """Get full detail for one person: name, gender, events (with citation counts),
    family links, and media count.

    Direct lookup is allowed even for living/private individuals (this is your own
    local tool); only bulk/list tools filter them. Use this to inspect someone
    before adding facts, or to check whether an event is already cited.
    """
    try:
        svc = await state.service_()
        return await svc.get_person(person)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def search_people(
    name: str = Field(description="Name substring to match (case-insensitive)."),
    birth_year_min: int | None = Field(default=None, description="Earliest birth year."),
    birth_year_max: int | None = Field(default=None, description="Latest birth year."),
    include_private: bool = _include_private(),
) -> dict:
    """Search people by name substring and optional birth-year range.

    Bulk output: probably-living people (born < 110 years ago with no recorded
    death) and records marked private are returned as redacted stubs (ids only)
    unless include_private is set. Fetch a specific person by id with
    get_person if you need their detail.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "search_people"):
            results = await svc.search_people(name, birth_year_min, birth_year_max)
            return {"count": len(results), "people": results}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_family(
    family: str = Field(description="Family handle or gramps_id, e.g. 'F0001'."),
) -> dict:
    """Get a family: relationship type, parent handles, child handles, event count."""
    try:
        svc = await state.service_()
        return await svc.get_family(family)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_ancestors(
    person: str = Field(description="Root person handle or gramps_id."),
    generations: int = Field(default=3, ge=1, le=8, description="How many generations up."),
    include_private: bool = _include_private(),
) -> dict:
    """Walk a person's ancestors up to N generations (a nested parents tree).

    Living/private ancestors appear as redacted stubs unless include_private is set.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "get_ancestors"):
            return await svc.get_ancestors(person, generations)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_descendants(
    person: str = Field(description="Root person handle or gramps_id."),
    generations: int = Field(default=3, ge=1, le=8, description="How many generations down."),
    include_private: bool = _include_private(),
) -> dict:
    """Walk a person's descendants up to N generations (a nested children tree).

    Living/private descendants appear as redacted stubs unless include_private is set.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "get_descendants"):
            return await svc.get_descendants(person, generations)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_unsourced_facts(
    person: str | None = Field(
        default=None, description="Optional: restrict to one person (handle/gramps_id)."
    ),
    include_private: bool = _include_private(),
) -> dict:
    """Audit: list events that lack a citation or are tagged UNSOURCED.

    This is the core quality query for a fully-cited tree -- run it to find
    facts that still need a source. Returns each offending event with its person,
    type, date, and the reason ('no-citation' or 'tagged-unsourced').
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "list_unsourced_facts"):
            return await svc.list_unsourced_facts(person)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def db_stats() -> dict:
    """Counts of people, families, events, citations, sources, repositories,
    places, media, and notes in the tree. A quick health/overview check."""
    try:
        svc = await state.service_()
        return await svc.db_stats()
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_tags() -> dict:
    """List all tags in the tree with their handle, name, and color.

    Use to see what labels already exist before tagging (tag_object matches by
    exact name).
    """
    try:
        svc = await state.service_()
        tags = await svc.list_tags()
        return {"count": len(tags), "tags": tags}
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_source(
    source: str = Field(description="Source handle or gramps_id, e.g. 'S0001'."),
) -> dict:
    """Get a source: title, author, publication info, abbreviation, linked
    repositories, media/note counts, attributes, and citation count.

    Use to inspect a source before citing through it or editing it.
    """
    try:
        svc = await state.service_()
        return await svc.get_source(source)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_repository(
    repository: str = Field(description="Repository handle or gramps_id, e.g. 'R0001'."),
) -> dict:
    """Get a repository: name, type, URLs, address count, and (if available)
    the number of sources it holds."""
    try:
        svc = await state.service_()
        return await svc.get_repository(repository)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_event(
    event: str = Field(description="Event handle or gramps_id, e.g. 'E0001'."),
) -> dict:
    """Get an event: type, date, place handle, description, citation count, and
    attributes. Use to inspect an event before citing or editing it."""
    try:
        svc = await state.service_()
        return await svc.get_event(event)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ==========================================================================  #
# QUERY + AUDIT
# ==========================================================================  #
@mcp.tool(annotations=READS)
async def query_objects(
    object_type: str = Field(
        description="One of: person, family, event, place, source, citation, "
        "repository, media, note, tag."
    ),
    gql: str | None = Field(
        default=None,
        description="GrampsQL filter, applied server-side over the RAW object "
        "JSON. Single '=' for equality (NOT '=='), '~' for substring, "
        "'<list>.length' for sizes, combined with AND/OR. Examples: "
        "'confidence >= 3 AND page = \"\"' (high-confidence citations with no "
        "locator), 'media_list.length = 0' (sources with no image), "
        "'desc = \"\"' (undescribed media), 'description ~ \"1871\"'. "
        "A LIST is searched through its items with '.any.' (or '.all.'), and a "
        "handle followed with 'get_<type>': 'urls.any.path ~ \"blm.gov\"', "
        "'attribute_list.any.value ~ \"x\"', 'note_list.any.get_note.text.string "
        "~ \"x\"'. '~' on the list itself asks whether the value IS an item, so it "
        "is refused. "
        "TRAPS: a field the object lacks is not an error, it matches nothing "
        "-- event 'type' is one (use query_records). Booleans compare as 0/1: "
        "'private = 1', never 'private = true'. A query reads the whole "
        "collection, so each OR'd condition adds time.",
    ),
    gramps_ids: list[str] | None = Field(
        default=None, description="Fetch these specific gramps_ids (e.g. ['S0001','S0002'])."
    ),
    handles: list[str] | None = Field(
        default=None, description="Fetch these specific handles in one request."
    ),
    keys: str | None = Field(
        default=None,
        description="Comma-separated fields to return, e.g. "
        "'gramps_id,title,media_list'. Strongly recommended -- whole objects are "
        "large. NEVER build a write payload from a keys= result: writes replace "
        "the whole record.",
    ),
    sort: str | None = Field(
        default=None, description="Sort key; prefix '-' for descending (e.g. '-change')."
    ),
    limit: int = Field(default=200, ge=1, le=2000, description="Max rows to return."),
    page: int = Field(default=1, ge=1, description="Page of results, 1-based."),
    include_private: bool = _include_private(),
) -> dict:
    """Query any collection with a server-side filter. The workhorse for audits.

    Use this instead of fetching a collection and filtering it yourself:
    whole-collection pulls are slow on any real tree, and the filter runs in
    the database. Typical audit questions it answers directly:

    * uncited high-confidence claims: citations where `confidence >= 3 AND page = ""`
    * documents with no image: sources where `media_list.length = 0`
    * anonymous media: media where `desc = ""`
    * a URL anywhere: `urls.any.path ~ "blm.gov"`; a list is searched through
      its items, never with `~` on the list itself

    To ask "what cites this source?" use get_backlinks -- a source has no
    citation_list, because citations point at IT, and reading citation_list on a
    source reports zero for every source in the tree.

    Private records and living people come back as redacted stubs.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "query_objects"):
            return await svc.query_objects(
                object_type,
                gql=gql,
                gramps_ids=gramps_ids,
                handles=handles,
                keys=keys,
                sort=sort,
                limit=limit,
                page=page,
            )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_backlinks(
    object_type: str = Field(description="Type of the object being pointed AT."),
    ref: str = Field(description="Handle or gramps_id of that object."),
) -> dict:
    """List everything that references this object, grouped by type.

    The right way to ask "is this source actually cited?", "which facts rest on
    this citation?", or "is it safe to delete this?" -- an object with zero
    backlinks is orphaned; one with backlinks will leave dangling references if
    deleted.
    """
    try:
        svc = await state.service_()
        return await svc.get_backlinks(object_type, ref)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def find_duplicates(
    kind: str = Field(
        description="media_checksum (same file uploaded twice), source_title "
        "(same document entered twice), citation_page (same source+page cited "
        "more than once, flagging any graded differently), vital_events (a "
        "person with two Births), person_name (same name, possible same person)."
    ),
    limit: int = Field(default=50, ge=1, le=500, description="Max groups to return."),
    include_private: bool = _include_private(),
) -> dict:
    """Find likely-duplicate objects. Reports only -- it never merges anything.

    Duplicates are not merely untidy: a duplicate SOURCE makes a single-sourced
    fact look corroborated, which is a false evidentiary claim. But the reverse
    error is just as real -- an index entry and the register page it indexes are
    TWO documents and must stay separate. This tool finds candidates; deciding
    which are truly the same document is yours. Merge with merge_objects.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "find_duplicates"):
            return await svc.find_duplicates(kind, limit=limit)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_object_types() -> dict:
    """The tree's type vocabularies: Gramps' standard names and its own custom ones.

    The write tools take any of these, in any case. A name in neither is
    refused unless allow_new_type asks for a NEW custom type, which then stays
    in the tree's vocabulary for good.
    """
    try:
        svc = await state.service_()
        return await svc.list_object_types()
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_object(
    object_type: str = Field(
        description="person, family, event, place, source, "
        "citation, repository, media, note, or tag."
    ),
    ref: str = Field(description="Handle or gramps_id."),
    keys: str | None = Field(
        default=None, description="Comma-separated fields to return. Omit for the whole record."
    ),
) -> dict:
    """Read any object's raw record -- including places, media, notes and
    citations, which have no shaped getter.

    Returns the record as stored, which is what you want before editing one. For
    people and sources the shaped getters (get_person, get_source) are easier to
    read.
    """
    try:
        svc = await state.service_()
        return await svc.get_object(object_type, ref, keys=keys)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ==========================================================================  #
# EDIT: typed updates
# ==========================================================================  #
@mcp.tool(annotations=EDITS)
async def update_citation(
    citation: str = Field(description="Citation handle or gramps_id (e.g. 'C0001')."),
    page: str | None = Field(
        default=None,
        description="The locator: WHERE in the source this fact appears "
        "('p. 45, entry 12', 'ED 12, sheet 4A, dwelling 57', memorial number). "
        "Omit to leave unchanged.",
    ),
    confidence: Confidence | None = Field(
        default=None,
        description="Re-grade this citation. very_high is for an original record "
        "read from an image, and nothing else.",
    ),
    date: str | None = Field(default=None, description="Date recorded/accessed."),
    source: str | None = Field(
        default=None,
        description="RE-POINT this citation at a different source (handle or "
        "gramps_id). Use when a fact was cited to a compiled bucket but the real "
        "record is in the tree, or when a container source has been split.",
    ),
) -> dict:
    """Edit a citation's locator, confidence, date, or the source it points at.

    A page-less citation on a long document is not a locator, and a confidence
    is a per-instance judgement, not a property of the source class.

    IMPORTANT: a citation carries ONE confidence, and it belongs to ONE claim.
    Every fact attached to this citation shares whatever you set here. If the
    same page supports a second, different claim (a census page proving both
    "this child appears here" and "these are her parents"), make a SECOND
    citation on the same source and page -- do not re-grade this one.
    """
    try:
        svc = await state.service_()
        return await svc.update_citation(
            citation, page=page, confidence=confidence, date=date, source_ref=source
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_citations(
    items: list[CitationEdit] = Field(
        min_length=1,
        max_length=500,
        description="Rows of {citation, page?, confidence?, expect_page_prefix?}.",
    ),
) -> dict:
    """Re-write many citations' pages or confidences in one call -- a sweep.

    Each row is update_citation: its own write and transaction. A row whose
    live page no longer starts with expect_page_prefix is reported as drifted,
    not overwritten. Rows report applied, unchanged, drifted, missing or error.
    """
    try:
        svc = await state.service_()
        return await svc.update_citations(items)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_media(
    media: str = Field(description="Media handle or gramps_id."),
    description: str | None = Field(
        default=None,
        description="What this document IS ('1900 US census, Cedar Flat, Brannock "
        "Co., Ohio, ED 12 sheet 4A'). Files are stored under checksum names, so "
        "without this the media list says nothing about the document.",
    ),
    date: str | None = Field(default=None, description="Date of the document/photo."),
    path: str | None = Field(default=None, description="Stored path/filename."),
) -> dict:
    """Edit a media object's description, date, or path."""
    try:
        svc = await state.service_()
        return await svc.update_media(media, description=description, date=date, path=path)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_person(
    person: str = Field(description="Person handle or gramps_id (e.g. 'I0001')."),
    gender: Gender | None = Field(default=None, description="female, male, or unknown."),
    name: NameParts | None = Field(
        default=None,
        description="New PRIMARY name. The current primary name is kept as an "
        "alternate rather than discarded, unless keep_old_as_alternate is False.",
    ),
    private: bool | None = Field(default=None, description="Gramps private flag."),
    keep_old_as_alternate: bool = Field(
        default=True,
        description="False only to fix a data-entry error -- a name split wrongly "
        "between given and surname, a typo no record contains -- where keeping the "
        "old form would invent a variant. The name is then corrected in place, "
        "keeping its citations, and reason is recorded in a note on the person.",
    ),
    reason: str | None = Field(
        default=None,
        description="Why the old form is not kept. Required with keep_old_as_alternate=False.",
    ),
) -> dict:
    """Edit a person's gender, primary name, or privacy flag.

    Replacing the primary name preserves the old one as an 'Also Known As': a
    name in the tree came from some record, and dropping it loses the link to
    whatever document used it. To add a name without replacing the primary one,
    use add_alternate_name.
    """
    try:
        svc = await state.service_()
        return await svc.update_person(
            person,
            gender=gender,
            name=name,
            private=private,
            keep_old_as_alternate=keep_old_as_alternate,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_alternate_name(
    person: str = Field(description="Person handle or gramps_id."),
    match: NameMatch = Field(
        description="Which alternate name, e.g. {'surname': 'Calloway', 'type': "
        "'Also Known As'}. get_person lists them with their index."
    ),
    given: str | None = Field(default=None, description="New given name(s). Omit to keep."),
    surname: str | None = Field(default=None, description="New surname. Omit to keep."),
    name_prefix: str | None = Field(default=None, description="New surname prefix."),
    name_suffix: str | None = Field(default=None, description="New suffix."),
    nickname: str | None = Field(default=None, description="New nickname."),
    name_type: str | None = Field(
        default=None,
        description="New type, e.g. 'Married Name' for a name filed as 'Also Known As'.",
    ),
    remove: bool = Field(
        default=False,
        description="Remove the name. Refused while it carries citations or notes; "
        "of identical duplicates, one is removed.",
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Correct, retype or remove one alternate name, in place.

    Edited in place, the name keeps its citations. The primary name is
    update_person(name=...); a new name is add_alternate_name.
    """
    try:
        svc = await state.service_()
        return await svc.update_alternate_name(
            person,
            match,
            given=given,
            surname=surname,
            prefix=name_prefix,
            suffix=name_suffix,
            nickname=nickname,
            name_type=name_type,
            remove=remove,
            allow_new_type=allow_new_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_object_fields(
    object_type: str = Field(description="Type of object to edit."),
    ref: str = Field(description="Handle or gramps_id."),
    fields: dict = Field(
        description="Scalar fields to set, e.g. {'name': 'Cedar Flat, Brannock, Ohio, USA'} "
        "on a place, or {'text': '...'} on a note."
    ),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Set scalar fields on any object -- the escape hatch for places, notes,
    repositories and the rest.

    Only scalar fields are settable. Structural lists (citation_list,
    event_ref_list, media_list, ...) are refused on purpose: replacing one
    wholesale is exactly how references get silently dropped. Each has its own
    tool -- cite_object, detach_object, tag_object, attach_media.
    """
    try:
        svc = await state.service_()
        return await svc.update_object_fields(object_type, ref, fields, allow_new_type)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=EDITS)
async def update_place(
    place: str = Field(description="Place handle or gramps_id (e.g. 'P0001')."),
    place_type: str | None = Field(
        default=None,
        description="New place type: 'Country', 'State', 'County', 'City', "
        "'Town', 'Village', 'Parish', 'Farm', 'Building', etc., or a custom type the "
        "tree already has. Omit to leave unchanged.",
    ),
    parent: str | None = Field(
        default=None,
        description="Handle or gramps_id of the ENCLOSING place (e.g. the county "
        "a city sits in). Must already exist -- never created from a name. "
        "Replaces the current single enclosure; refused if the place carries "
        "several dated enclosures.",
    ),
    remove_parent: bool = Field(
        default=False, description="Clear the enclosure instead of setting one."
    ),
    name: str | None = Field(
        default=None,
        description="New place NAME (the short local name, e.g. 'Cedar Flat'). "
        "Omit to leave unchanged.",
    ),
    title: str | None = Field(
        default=None,
        description="New full TITLE (e.g. 'Cedar Flat, Brannock County, "
        "Ohio, USA') -- the string event-place resolution matches against.",
    ),
    latitude: str | None = Field(default=None, description="Latitude, e.g. '40.1532'."),
    longitude: str | None = Field(default=None, description="Longitude, e.g. '-82.4101'."),
    code: str | None = Field(default=None, description="Place code (postal etc.)."),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Edit a place's type, parent enclosure, name, title, or coordinates.

    This is the tool update_object_fields deliberately refuses to be: place_type
    and the enclosure are structural, so they get guard rails here -- the parent
    must already exist, setting it cannot create an enclosure cycle, and a place
    holding several dated enclosures (a territory-to-state succession) is
    refused rather than silently flattened. Aim for every place typed, every
    non-country place parented, and street addresses on events rather than in
    the place tree.
    """
    try:
        svc = await state.service_()
        return await svc.update_place(
            place,
            place_type=place_type,
            parent=parent,
            remove_parent=remove_parent,
            name=name,
            title=title,
            latitude=latitude,
            longitude=longitude,
            code=code,
            allow_new_type=allow_new_type,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ==========================================================================  #
# EDIT: citation plumbing
# ==========================================================================  #
@mcp.tool(annotations=ADDS)
async def cite_object(
    object_type: str = Field(
        description="person, family, event, place, media, source, citation, or name "
        "(one of a person's names: ref is the person, name says which)."
    ),
    ref: str = Field(description="Handle or gramps_id of the object to cite."),
    citation: CitationInput = Field(description="Citation to attach."),
    name: NameMatch | None = Field(
        default=None,
        description="With object_type='name': which of the person's names, e.g. "
        "{'surname': 'Bittner', 'type': 'Birth Name'} or {'primary': true}. Must "
        "match exactly one.",
    ),
) -> dict:
    """Attach a citation to any object that carries one -- not just events.

    cite_event covers facts; this covers the rest. The important case is the
    FAMILY, whose citation supports a claim no event makes: that these two
    people were a couple. Person-level citations are for evidence about the
    individual as a whole (an identity document) rather than about one dated
    fact -- prefer citing the specific event where one exists.

    A NAME is cited with object_type='name': "this record gives this spelling"
    is narrower than evidence about the person. To cite a parent-child link,
    use cite_child_link: that is a different claim and needs its own citation.
    """
    try:
        svc = await state.service_()
        return await svc.cite_object(object_type, ref, citation, name=name)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def cite_child_link(
    family: str = Field(description="Family handle or gramps_id (e.g. 'F0001')."),
    child: str = Field(description="The child's person handle or gramps_id."),
    citation: CitationInput = Field(
        description="Citation for the PARENTAGE claim. Create a new one (source "
        "+ page + confidence) rather than reusing a citation handle from "
        "elsewhere -- see the warning below."
    ),
) -> dict:
    """Cite the parent-child link itself, on the family's ChildRef.

    "This child belongs to these parents" is a DIFFERENT claim from "this child
    appears in this record", and it needs its own citation object. Reusing the
    child's existing citation handle makes the link inherit a confidence that
    was assigned to another claim entirely, so the link displays a confidence
    its evidence never earned.

    Note what a ChildRef citation asserts: BOTH sides of the link. A census
    naming only the mother does not document the father. Where only one parent
    is evidenced, cite that parent's relationship instead of implying both.
    """
    try:
        svc = await state.service_()
        return await svc.cite_child_link(family, child, citation)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=REMOVES)
async def uncite(
    object_type: str = Field(description="Type of the object to detach from."),
    ref: str = Field(description="Handle or gramps_id of that object."),
    citation: str = Field(description="Citation handle or gramps_id to remove."),
    delete_if_orphan: bool = Field(
        default=True,
        description="Delete the citation if nothing else references it after "
        "detaching. Leave True unless you are keeping it deliberately. A citation "
        "that holds the only link to a note or image is kept unless carry_to is given.",
    ),
    name: NameMatch | None = Field(
        default=None,
        description="With object_type='name' (ref is the person): which of the "
        "person's names to detach the citation from.",
    ),
    carry_to: str | None = Field(
        default=None,
        description="Citation (handle or gramps_id) to receive the notes and images "
        "only this citation holds, so it can be deleted without losing them.",
    ),
) -> dict:
    """Detach a citation from an object, deleting it if it is left orphaned.

    Detaching without deleting is how orphan citations accumulate: the fact the
    citation supported is gone, but the citation sits in the database still
    looking like evidence of something. Use this when a citation was attached to
    the wrong fact, or when a superseded bucket citation is replaced by the real
    record.
    """
    try:
        svc = await state.service_()
        return await svc.uncite(
            object_type,
            ref,
            citation,
            delete_if_orphan=delete_if_orphan,
            name=name,
            carry_to=carry_to,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ==========================================================================  #
# EDIT: structure
# ==========================================================================  #
@mcp.tool(annotations=REMOVES)
async def merge_objects(
    object_type: str = Field(
        description="person, family, event, place, source, citation, repository, media, or note."
    ),
    keep: str = Field(description="Handle or gramps_id of the object that SURVIVES."),
    drop: str = Field(description="Handle or gramps_id of the object absorbed into it."),
    dry_run: bool = Field(
        default=True,
        description="True (default) reports what would move without changing "
        "anything. Set False to apply.",
    ),
    enclosures: str = Field(
        default="auto",
        description="Places only: the survivor otherwise gets both places' parents. "
        "'auto' drops an undated parent that encloses another (a state beside its "
        "county) and refuses if two unrelated undated parents remain; or "
        "'keep_keeper', 'keep_drop', 'keep_both'. Dated enclosures are always kept.",
    ),
) -> dict:
    """Merge two objects that are the same thing. Dry-run by default.

    This uses Gramps' own server-side merge: every reference to `drop` is
    re-pointed at `keep` and the subordinate lists are unioned, in one
    transaction. Do NOT do this by hand: a manual merge that misses one of the
    lists the dropped object carries loses what was on it.

    Before merging, be sure they really are one thing. Two records OF the same
    event are two documents: an index entry and the register page it indexes
    stay separate, and merging them would turn two independent citations into
    one, silently weakening every fact that rested on both. Conversely, the same
    census page entered once per household member IS one document, and leaving
    the duplicates makes single-sourced facts look corroborated.

    Reversible: the merge is one transaction, so list_transactions +
    undo_transaction can back it out.
    """
    try:
        svc = await state.service_()
        return await svc.merge_objects(
            object_type, keep, drop, dry_run=dry_run, enclosures=enclosures
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=REMOVES)
async def detach_object(
    parent_type: str = Field(description="Type of the object holding the reference."),
    parent: str = Field(description="Its handle or gramps_id."),
    child_kind: str = Field(
        description="What to detach: event, media, note, tag (removes a tag), citation, "
        "child (a person from a family), person (a person_ref), repository, enclosure "
        "(a parent of a place), or -- on a person, to repair a link only the person "
        "holds (check_family_links) -- parent_family or family."
    ),
    child: str = Field(
        description="Handle or gramps_id of the thing to detach; a tag may be given by name."
    ),
    delete_if_orphan: bool = Field(
        default=False,
        description="Also delete the detached object if nothing else references "
        "it. Off by default -- detaching and deleting are different decisions.",
    ),
    call_number: str | None = Field(
        default=None,
        description="Repository only: detach just the link with this call number, "
        "when a source is held twice in one repository. Omit to detach every link.",
    ),
) -> dict:
    """Remove a reference from an object: an event from a person, an image from a
    source, a tag, a note, a child from a family.

    The reference is removed; the object itself survives unless
    delete_if_orphan is set AND nothing else points at it -- which is checked,
    because deleting something other facts still reference leaves dangling
    handles behind.
    """
    try:
        svc = await state.service_()
        return await svc.detach_object(
            parent_type,
            parent,
            child_kind,
            child,
            delete_if_orphan=delete_if_orphan,
            call_number=call_number,
        )
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_media(
    file_path: str = Field(
        description="Local path to the image, PDF, audio or video file to upload."
    ),
    description: str = Field(
        description="What the document IS -- archival identity, not the person "
        "it mentions ('1900 US census, Cedar Flat, Brannock Co., Ohio, ED 12 "
        "sheet 4A'). Files are stored under checksum names."
    ),
    dedup_by_checksum: bool = Field(
        default=True,
        description="Reuse an existing Media object if the identical file is "
        "already in the tree. Leave True.",
    ),
) -> dict:
    """Upload a document as a standalone Media object, reusing an identical file
    already in the tree.

    One image, one Media object -- then attach it wherever it belongs with
    attach_media(media_ref=...) and cite it once per fact it proves. Uploading
    the same photograph once per person it depicts is the duplicate pattern that
    later has to be unpicked by hand.
    """
    try:
        svc = await state.service_()
        return await svc.add_media(file_path, description, dedup_by_checksum=dedup_by_checksum)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def ocr_media(
    media: str = Field(description="Media handle or gramps_id."),
    lang: str = Field(
        default="eng", description="Tesseract language code ('eng', 'deu', 'swe', 'nor', ...)."
    ),
) -> dict:
    """Run OCR on a document image, server-side, to locate text within it.

    A finding aid, not evidence. OCR output is a machine's guess at the writing,
    and it is at its worst on exactly the handwritten records that matter most.
    Use it to find WHERE something appears in a long scan; read the image before
    citing what it says.
    """
    try:
        svc = await state.service_()
        return await svc.ocr_media(media, lang=lang)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


# ==========================================================================  #
# OPS: backup, change log, undo
# ==========================================================================  #
@mcp.tool(annotations=WRITES_LOCAL_FILE)
async def export_backup(
    dest_path: str | None = Field(
        default=None,
        description="A new file in an existing directory; an existing file is "
        "never replaced. Omit for a timestamped file in the cache directory.",
    ),
    export_format: str = Field(
        default="gramps",
        description="'gramps' (Gramps XML, lossless -- use this for a safety "
        "dump), 'ged', 'json', or 'csv'.",
    ),
) -> dict:
    """Write a full-tree export to disk. Take one before any bulk write.

    Cheap insurance: a few seconds and one file. A bulk write that goes wrong
    cannot always be undone transaction by transaction; a dump can be
    re-imported.
    """
    try:
        svc = await state.service_()
        return await svc.export_backup(dest_path, extension=export_format)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_transactions(
    limit: int = Field(default=20, ge=1, le=200, description="How many, newest first."),
) -> dict:
    """Recent writes to the tree: what changed, when, by which user.

    Each entry's transaction_id is what undo_transaction takes. Useful for
    "what did that bulk pass actually do?" and for finding the transaction to
    reverse when it did the wrong thing.
    """
    try:
        svc = await state.service_()
        return await svc.list_transactions(limit)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=REMOVES)
async def undo_transaction(
    transaction_id: int = Field(description="From list_transactions."),
    dry_run: bool = Field(
        default=True,
        description="True (default) only checks whether the undo is clean. Set "
        "False to actually undo.",
    ),
    force: bool = Field(
        default=False,
        description="Undo even when there are conflicts. This DISCARDS edits "
        "made to those objects after the transaction. Use deliberately.",
    ),
) -> dict:
    """Undo a past transaction, after checking whether it can be undone cleanly.

    A conflict means an object was edited again after this transaction; undoing
    anyway throws that later edit away. The conflict check is free and runs
    first, so the default tells you what you are dealing with before anything
    changes.
    """
    try:
        svc = await state.service_()
        return await svc.undo_transaction(transaction_id, dry_run=dry_run, force=force)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_reports() -> dict:
    """List the reports this Gramps instance can generate.

    Gramps ships a full report engine — Ahnentafel, descendant reports, family
    group sheets, kinship, fan and relationship charts, statistics, and an
    end-of-line report that lists exactly where research stops. Each entry
    names the option keys it accepts; read the defaults with
    get_report_options before overriding any.
    """
    try:
        svc = await state.service_()
        return await svc.list_reports()
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_report_options(
    report_id: str = Field(description="Report id, e.g. 'ancestor_report'."),
) -> dict:
    """Read one report's default options before running it.

    Reports take a full option dict, not a partial one, so the way to change
    a single setting is to read these defaults and override that key.
    """
    try:
        svc = await state.service_()
        return await svc.get_report_options(report_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=RUNS_ON_SERVER)
async def run_report(
    report_id: str = Field(description="Report id, from list_reports."),
    options: dict | None = Field(
        default=None,
        description="Overrides for the report's defaults, merged over them. "
        "Common keys: 'pid' (the central person's gramps_id), 'maxgen', "
        "'off' (output format), 'living_people'.",
    ),
    locale: str = Field(default="", description="Language code for the report output."),
    include_private: bool = _include_private(),
) -> dict:
    """Generate a report and return the file it produced.

    Living people and private records are left out (living_people 0,
    incl_private false) unless you pass those options or include_private;
    Gramps' own default includes both.

    Usually runs in the background, returning a task_id to poll with get_task.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "run_report"):
            return await svc.run_report(report_id, options, locale or None)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_filter_rules(
    namespace: str = Field(
        default="people",
        description="Plural namespace: people, families, events, places, "
        "sources, citations, repositories, media, notes.",
    ),
) -> dict:
    """List the filter rules Gramps offers in a namespace.

    This is the vocabulary that `query_records` and GrampsQL cannot reach:
    "is a descendant of", "has a common ancestor with", "matches another
    filter". Read it before building a custom filter with create_filter.
    """
    try:
        svc = await state.service_()
        return await svc.list_filter_rules(namespace)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_custom_filters(
    namespace: str = Field(default="", description="Restrict to one namespace. Omit for all."),
) -> dict:
    """List the custom filters already saved on this instance.

    A saved filter can be reused by name from `query_objects` and the
    timelines, so a complicated selection is defined once.
    """
    try:
        svc = await state.service_()
        return await svc.list_custom_filters(namespace or None)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def create_filter(
    namespace: str = Field(
        description="Singular, capitalised: Person, Family, Event, Place, "
        "Citation, Source, Repository, Media, Note."
    ),
    name: str = Field(description="Filter name, used to apply it later."),
    rules: list = Field(
        description="Rules, each {'name': <rule>, 'values': [...], "
        "'regex': false}. Rule names come from list_filter_rules.",
    ),
    function: str = Field(default="and", description="How rules combine: 'and', 'or', or 'one'."),
    invert: bool = Field(default=False, description="Return everything the rules do NOT match."),
    comment: str = Field(default="", description="Note on the filter's purpose."),
) -> dict:
    """Save a reusable custom filter built from Gramps' own rules.

    Worth doing for a selection you will run repeatedly — an audit scope, a
    branch of the tree — because the filter then has a name rather than being
    retyped each time.
    """
    try:
        svc = await state.service_()
        return await svc.create_filter(namespace, name, rules, function, invert, comment)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=REMOVES)
async def delete_filter(
    namespace: str = Field(description="Plural namespace, e.g. 'people'."),
    name: str = Field(description="Name of the filter to delete."),
) -> dict:
    """Delete a saved custom filter.

    Deletes the filter definition only. Nothing in the tree is touched.
    """
    try:
        svc = await state.service_()
        return await svc.delete_filter(namespace, name)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def consolidated_timeline(
    targets: list = Field(description="Handles or gramps_ids to merge into one timeline."),
    object_type: str = Field(default="person", description="Either 'person' or 'family'."),
    anchor: str = Field(
        default="",
        description="Handle or gramps_id of the central person, so ages are "
        "reported relative to them.",
    ),
    event_types: str = Field(
        default="",
        description="Comma-delimited event type names to include, e.g. "
        "'Birth,Death,Census'. Omit for all.",
    ),
    limit: int = Field(default=200, description="Maximum events."),
    include_private: bool = _include_private(),
) -> dict:
    """Merge several people or families into one chronological timeline.

    The way to see a household move together through censuses, or to check
    whether a family's events are mutually consistent. Carries the same
    citation count and confidence per event as get_timeline, with an
    uncited_count across the whole set.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "consolidated_timeline"):
            return await svc.consolidated_timeline(
                object_type,
                list(targets),
                anchor or None,
                event_types or None,
                limit,
            )
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_tasks(
    limit: int = Field(default=25, description="Maximum tasks to return."),
) -> dict:
    """List recent background jobs for this tree, newest first.

    Use when you have lost a task_id, or to see whether anything is still
    running before starting a write session.
    """
    try:
        svc = await state.service_()
        return await svc.list_tasks(limit)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_transaction(
    transaction_id: int = Field(description="Id from list_transactions."),
) -> dict:
    """Read one transaction in full, including the objects it changed.

    list_transactions summarises; this shows what actually moved. Read it
    before undoing anything.
    """
    try:
        svc = await state.service_()
        return await svc.get_transaction(transaction_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_record_history(
    object_type: str = Field(
        description="person, family, event, place, source, citation, repository, media, "
        "note, or tag."
    ),
    ref: str = Field(description="Handle or gramps_id. A deleted record by its handle."),
    limit: int = Field(default=20, ge=1, le=200, description="Most recent changes to return."),
) -> dict:
    """Who added, edited or deleted one record, and when, newest first.

    Each change names its transaction: get_transaction shows what it changed.
    Needs gramps-webapi 3.22 or later.
    """
    try:
        svc = await state.service_()
        return await svc.record_history(object_type, ref, limit)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_place(
    place: str = Field(description="Handle or gramps_id of the place."),
) -> dict:
    """Read one place: name, title, type, enclosure, coordinates and URLs.

    The `enclosed_by` handles are the jurisdictional chain. A place with none
    is orphaned in the hierarchy, which is usually a place that got minted
    from an event's place string rather than created deliberately.
    """
    try:
        svc = await state.service_()
        return await svc.get_place(place)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_citation(
    citation: str = Field(description="Handle or gramps_id of the citation."),
) -> dict:
    """Read one citation: page, confidence, date, and its source.

    `cited_by_count` is the useful part. Zero means nothing references this
    citation — it is orphan debris, and `uncite` should have deleted it.
    """
    try:
        svc = await state.service_()
        return await svc.get_citation(citation)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_note(
    note: str = Field(description="Handle or gramps_id of the note."),
) -> dict:
    """Read one note in full, with its type and what it is attached to.

    Notes hold the researcher's own reasoning — why a conflict was resolved
    one way, what a hard-to-read page actually said — so the text comes back
    whole rather than truncated.
    """
    try:
        svc = await state.service_()
        return await svc.get_note(note)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_media(
    media: str = Field(description="Handle or gramps_id of the media object."),
) -> dict:
    """Read one media object: path, mime type, checksum, description, date.

    `referenced_by_count` above one is usually correct — one image cited from
    every fact it proves. Several media objects sharing a checksum is the
    duplicate that `find_duplicates(kind='media_checksum')` hunts.
    """
    try:
        svc = await state.service_()
        return await svc.get_media(media)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_place(
    name: str = Field(description="The place's own name, e.g. 'Cedar Flat'."),
    place_type: str = Field(
        default="",
        description="Gramps place type: Town, City, County, State, Country, "
        "Parish, Farm, Building, and so on, or a custom type the tree already has.",
    ),
    parent: str = Field(
        default="",
        description="Handle or gramps_id of an existing enclosing place. Must "
        "already exist — it is never created for you.",
    ),
    title: str = Field(
        default="",
        description="Full display title, e.g. 'Cedar Flat, Brannock, Ohio, USA'. "
        "Defaults to the name. This is what event place matching compares "
        "against, so set it properly.",
    ),
    latitude: str = Field(default="", description="Latitude, decimal degrees."),
    longitude: str = Field(default="", description="Longitude, decimal degrees."),
    code: str = Field(default="", description="Postal or FIPS code."),
    allow_new_type: bool = _allow_new_type(),
) -> dict:
    """Create a place deliberately, with a type and a parent.

    Use this instead of letting a place appear as a side effect of naming one
    in an event. That route produces an untyped, unparented place whose title
    is the bare string you typed, which is how duplicate hierarchies start.
    """
    try:
        svc = await state.service_()
        return await svc.add_place(
            name,
            place_type or None,
            parent or None,
            title or None,
            latitude or None,
            longitude or None,
            code or None,
            allow_new_type,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_facts(
    person_filter: str = Field(
        default="",
        description="Narrow the set: 'Ancestors', 'Descendants', "
        "'DescendantFamilies' or 'CommonAncestor' of person, or a saved custom "
        "person filter's name. Omit for the whole tree.",
    ),
    person: str = Field(
        default="",
        description="Handle or gramps_id a built-in person_filter is anchored on.",
    ),
    rank: int = Field(default=1, description="Record-holders to return per statistic."),
    include_private: bool = _include_private(),
) -> dict:
    """Read the tree's record-holders: oldest at death, youngest parent, most children.

    Superlatives across a set of people, not statistics about one person. An
    implausible holder — a father at eight, a death at 130 — is usually a data
    error, which makes this a quick plausibility check. Living and private
    people are excluded unless include_private is set. Slow: the server
    computes it all per call.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "get_facts"):
            return await svc.get_facts(person_filter or None, person or None, rank)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_researcher() -> dict:
    """Read the researcher details recorded for this tree.

    These are embedded in every export, so they travel with any GEDCOM or
    Gramps XML you hand to someone else. Worth checking before sharing one.
    """
    try:
        svc = await state.service_()
        return await svc.get_researcher()
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def query_records(
    object_type: str = Field(
        description="Collection to query: person, family, event, place, "
        "source, citation, repository, media, note, or tag."
    ),
    select: list | None = Field(
        default=None,
        description="Columns to return. A plain column name, or "
        '{"json_path": [...], "as": "label"} to reach into the stored '
        "object. A path may cross a relationship: person->birth/death, "
        "family->father/mother, event->place. Omit for the default columns.",
    ),
    where: list | None = Field(
        default=None,
        description="Conditions combined with AND. Each is "
        '{"column": <name or json_path>, "op": <op>, "value": ...}. '
        "Operators: eq, ne, lt, lte, gt, gte, like, regex, contains, in. Use "
        '"value_column" instead of "value" to compare two columns. A list value '
        "is for 'in' only; 'contains' finds one value in a list field. A date's "
        "year is dateval[2], 0 when unknown.",
    ),
    where_expr: str | None = Field(
        default=None,
        description="An expression instead of `where`, e.g. \"surname == 'Smith'\".",
    ),
    event_type: str = Field(
        default="",
        description="Events only. Filter by type name such as 'Birth' or "
        "'Census'. Translated to the integer the tree stores, which is the "
        "only way event type is filterable at all.",
    ),
    order_by: list | None = Field(
        default=None,
        description='Sort keys, each {"column": ..., "direction": '
        '"asc" or "desc"}. json_path is not usable here.',
    ),
    limit: int = Field(default=50, description="Maximum rows (1-500)."),
    after: str = Field(
        default="",
        description="Cursor from a previous response's next_after, for paging past the first page.",
    ),
    include_private: bool = _include_private(),
) -> dict:
    """Query any collection server-side, with columns, filters and sorting.

    More capable than `query_objects` and the tool to reach for on an audit.
    It reads indexed columns, reaches arbitrary paths inside the stored object,
    and follows relationships — so "families where the mother died before the
    father" or "events whose place is in Ohio" are single queries.

    **It is the only way to filter events by type.** GrampsQL cannot: the word
    is shadowed, so `type = "Birth"` silently matches nothing. Pass `event_type`
    here instead.

    Returns rows plus a total count and a `next_after` cursor. Private records,
    living people and families with a living parent come back as redacted stubs.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "query_records"):
            return await svc.query_records(
                object_type,
                select=select,
                where=where,
                where_expr=where_expr,
                order_by=order_by,
                limit=limit,
                after=after or None,
                event_type=event_type or None,
            )
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def list_event_types() -> dict:
    """List the event type names this tree uses, with their stored integers.

    Useful before a `query_records` filter, and as a check on itself: an
    unexpected type name in the list is usually a typo that Gramps silently
    accepted as a new custom type.
    """
    try:
        svc = await state.service_()
        raw = await svc.client.type_map("event_types")
        return {
            "event_types": sorted(
                ({"name": label, "value": int(value)} for value, label in raw.items()),
                key=lambda e: e["name"],
            )
        }
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=ADDS)
async def add_dna_match(
    person: str = Field(
        description="Handle or gramps_id of the tested person, whose results list the match."
    ),
    match: str = Field(
        description="Handle or gramps_id of the matching person. Add them first if "
        "they are not in the tree."
    ),
    segments: str = Field(
        description="The shared segments as the testing company exports them: rows "
        "of chromosome, start, stop, centiMorgans, SNPs, comma- or tab-separated, "
        "with an optional side of M, P or U. A header row is tolerated."
    ),
    citation: CitationInput = Field(
        description="The test the match came from: source or source_title naming the "
        "company and kit, page for where the match is shown, confidence in the match "
        "itself. A new citation is always minted."
    ),
) -> dict:
    """Record a DNA match as evidence, cited to the test that found it.

    Stored as Gramps Web stores a match, so its interface and get_dna_matches
    read it back. Nothing is written unless the segments parse: unreadable data
    would otherwise record a match sharing no DNA.

    A match proves the two share DNA, not how they are related. Record any
    relationship separately, with its own evidence.
    """
    try:
        svc = await state.service_()
        return await svc.add_dna_match(person, match, segments, citation)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_dna_matches(
    person: str = Field(description="Handle or gramps_id of the tested person."),
    include_raw: bool = Field(
        default=False,
        description="Include the unparsed note text the segments came from.",
    ),
) -> dict:
    """List the DNA matches recorded against a person.

    Each match reports total shared centiMorgans and the largest single
    segment — the two figures a relationship estimate actually rests on —
    plus any common ancestor already identified.

    **DNA evidence works differently from documentary evidence.** A match
    proves a biological relationship exists; it does not say which one. Shared
    cM constrains the possibilities and rarely resolves them, and it says
    nothing about the paper trail. `unattributed_count` is the useful number:
    matches with no common ancestor identified are the open research.
    """
    try:
        svc = await state.service_()
        return await svc.get_dna_matches(person, include_raw)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_ydna(
    person: str = Field(description="Handle or gramps_id of the tested person."),
    include_raw: bool = Field(default=False, description="Include the raw SNP data string."),
) -> dict:
    """Report a person's Y-DNA haplogroup, from broadest clade to terminal.

    Y-DNA follows the direct paternal line only, so it speaks to one thread
    of a tree and is silent on every other. A shared terminal clade indicates
    a common paternal ancestor, usually far further back than any record
    reaches — it corroborates a surname line rather than proving a named
    link.

    `has_data` is false when the person has no Y-DNA recorded, which is the
    normal case.
    """
    try:
        svc = await state.service_()
        return await svc.get_ydna(person, include_raw)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def parse_dna_segments(
    data: str = Field(
        description="Raw shared-segment data, as a testing company exports "
        "it: rows of chromosome, start, stop, centiMorgans, SNPs, separated "
        "by commas or tabs, with an optional side of M, P or U. A header row "
        "is tolerated.",
    ),
) -> dict:
    """Parse pasted shared-segment data into structured segments and totals.

    Use this to check what a match file actually contains before recording
    it. Returns each segment plus the total and largest-segment centiMorgans.

    If nothing parses, `parsed` is false and the reason is given. That
    distinction matters: the server answers unreadable input with zero
    segments and a success status, which would otherwise read as "this person
    shares no DNA" rather than "I could not read that".
    """
    try:
        svc = await state.service_()
        return await svc.parse_dna_segments(data)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_relationship(
    person1: str = Field(description="Handle or gramps_id of the first person."),
    person2: str = Field(description="Handle or gramps_id of the second person."),
    all_paths: bool = Field(
        default=False,
        description="Report every relationship path, not just the closest. Use "
        "this when two people may be related more than one way.",
    ),
    depth: int | None = Field(
        default=None, description="Generations to search. Server default if omitted."
    ),
) -> dict:
    """Work out how two people in the tree are related.

    Returns the relationship in words plus the generation distance from each
    person to their common ancestor. `related` is false when no common
    ancestor was found within the search depth — which is a finding in itself
    if you expected one.
    """
    try:
        svc = await state.service_()
        return await svc.get_relationship(person1, person2, all_paths, depth)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def assess_living(
    person: str = Field(description="Handle or gramps_id of the person."),
    explain: bool = Field(
        default=False,
        description="Also return the estimated birth and death dates and which "
        "relative they were derived from.",
    ),
    max_age_probably_alive: int | None = Field(
        default=None, description="Age beyond which a person is presumed dead."
    ),
    average_generation_gap: int | None = Field(
        default=None, description="Years per generation used when estimating."
    ),
) -> dict:
    """Ask the server whether a person is probably still alive, and why.

    The server walks relatives to decide, so it handles people with no dates
    of their own — someone undated whose children died a century ago. Use it
    before publishing or sharing anything, and use `explain` when you want to
    see the reasoning rather than just the verdict.

    Note this is advisory. Bulk output is filtered by this server's own rule
    regardless of what this returns.
    """
    try:
        svc = await state.service_()
        return await svc.assess_living(
            person,
            explain=explain,
            max_age_probably_alive=max_age_probably_alive,
            average_generation_gap=average_generation_gap,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_timeline(
    target: str = Field(description="Handle or gramps_id of the person or family."),
    object_type: str = Field(default="person", description="Either 'person' or 'family'."),
    ancestors: int | None = Field(
        default=None,
        description="Generations of ancestors whose events to fold in.",
    ),
    offspring: int | None = Field(
        default=None,
        description="Generations of descendants whose events to fold in.",
    ),
    limit: int = Field(default=200, description="Maximum events to return."),
    include_private: bool = _include_private(),
) -> dict:
    """Build a chronological timeline of someone's life events.

    Each entry carries their age at the time, how many citations support the
    event, and the strongest confidence among them — so a timeline doubles as
    a readable audit of where the evidence thins out. `uncited_count` says how
    many events on it rest on nothing.
    """
    try:
        svc = await state.service_()
        with svc.privacy_lifted(include_private, "get_timeline"):
            return await svc.get_timeline(object_type, target, ancestors, offspring, limit)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def event_span(
    event1: str = Field(description="Handle or gramps_id of the first event."),
    event2: str = Field(description="Handle or gramps_id of the second event."),
    as_age: bool = Field(
        default=False,
        description="Phrase the result as an age rather than an interval.",
    ),
    precision: int | None = Field(
        default=None, description="How many units to include (years, months, days)."
    ),
) -> dict:
    """Measure the elapsed time between two events.

    The arithmetic behind most plausibility checks: age at marriage, years
    between a census and a death, how long a widow waited. Doing this by hand
    from two formatted date strings is where transcription errors hide.
    """
    try:
        svc = await state.service_()
        return await svc.event_span(event1, event2, as_age, precision)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=RUNS_ON_SERVER)
async def reindex_search(
    full: bool = Field(
        default=False,
        description="Rebuild from scratch rather than updating incrementally. "
        "Slower, and the right choice after a large import.",
    ),
) -> dict:
    """Rebuild the full-text search index.

    `search_text` reads a stored index, and nothing refreshes it after writes.
    Run this after a bulk import or a large editing session, or searches will
    quietly miss everything added since the last build. Returns a task_id to
    poll with get_task.
    """
    try:
        svc = await state.service_()
        return await svc.reindex_search(full)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def get_task(
    task_id: str = Field(
        description="Task id returned by whatever dispatched the work, e.g. "
        "the task_id from undo_transaction or verify_tree."
    ),
) -> dict:
    """Check whether a background job has finished, and whether it worked.

    Undo, verification, import and reindex are dispatched to a worker and
    answer before the work is done. Poll this until `finished` is true, then
    read `succeeded`. Submitting one of those operations and never checking
    leaves you assuming an outcome you have not seen.
    """
    try:
        svc = await state.service_()
        return await svc.get_task(task_id)
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


@mcp.tool(annotations=READS)
async def verify_tree(
    tree_id: str = Field(
        default="",
        description="Tree to check. Leave empty to use the tree these "
        "credentials are bound to, which is the usual case.",
    ),
    max_age_at_death: int | None = Field(
        default=None, description="Flag a death later than this age. Server default 90."
    ),
    min_age_to_marry: int | None = Field(
        default=None, description="Flag a marriage younger than this. Server default 17."
    ),
    max_age_to_marry: int | None = Field(
        default=None, description="Flag a marriage older than this. Server default 50."
    ),
    min_mother_age: int | None = Field(
        default=None, description="Flag a mother younger than this. Server default 17."
    ),
    max_mother_age: int | None = Field(
        default=None, description="Flag a mother older than this. Server default 48."
    ),
    min_father_age: int | None = Field(
        default=None, description="Flag a father younger than this. Server default 18."
    ),
    max_father_age: int | None = Field(
        default=None, description="Flag a father older than this. Server default 65."
    ),
    max_children_mother: int | None = Field(
        default=None, description="Flag a woman with more children than this. Default 12."
    ),
    max_children_father: int | None = Field(
        default=None, description="Flag a man with more children than this. Default 15."
    ),
    max_spouses: int | None = Field(
        default=None, description="Flag more spouses than this. Server default 3."
    ),
    max_husband_wife_age_gap: int | None = Field(
        default=None, description="Flag a wider spousal age gap. Server default 30."
    ),
    max_years_between_children: int | None = Field(
        default=None, description="Flag a longer gap between siblings. Default 8."
    ),
    max_child_birth_span: int | None = Field(
        default=None, description="Flag a longer span of one couple's births. Default 25."
    ),
    max_widowhood_years: int | None = Field(
        default=None, description="Flag a longer widowhood before remarriage. Default 30."
    ),
    estimate_age: bool = Field(
        default=False,
        description="Estimate missing or inexact dates when checking ages. "
        "Finds more, at the cost of guessing.",
    ),
    flag_invalid_dates: bool = Field(
        default=True, description="Report dates the parser cannot read."
    ),
) -> dict:
    """Run Gramps' own genealogical plausibility checks over the whole tree.

    This is a different audit from the citation ones. `list_unsourced_facts`
    asks whether a claim has evidence; this asks whether a claim is possible —
    a mother bearing a child at nine, a marriage lasting 120 years, a date that
    will not parse. A wrong date can be impeccably sourced, so these catch what
    a citation sweep cannot.

    Leave the thresholds alone on a first run; the server's defaults are the
    conventional ones. Tighten a specific bound when chasing a specific class
    of error.

    May run in the background, in which case a task_id comes back — poll it
    with get_task.
    """
    try:
        svc = await state.service_()
        return await svc.verify_tree(
            tree_id or None,
            oldage=max_age_at_death,
            yngmar=min_age_to_marry,
            oldmar=max_age_to_marry,
            yngmom=min_mother_age,
            oldmom=max_mother_age,
            yngdad=min_father_age,
            olddad=max_father_age,
            mxchildmom=max_children_mother,
            mxchilddad=max_children_father,
            wedder=max_spouses,
            hwdif=max_husband_wife_age_gap,
            cspace=max_years_between_children,
            cbspan=max_child_birth_span,
            lngwdw=max_widowhood_years,
            estimate_age=estimate_age or None,
            invdate=None if flag_invalid_dates else False,
        )
    except Exception as exc:  # noqa: BLE001 - surfaced as structured error
        return _error(exc)


# ==========================================================================  #
# REFERENCE LAYER (read-only, separate from the Gramps DB)
# ==========================================================================  #
@mcp.tool(annotations=READS)
async def consult_reference(
    name: str = Field(description="Name (or name substring) to look up."),
    approx_birth_year: int | None = Field(
        default=None, description="Approximate birth year to disambiguate (± a few years)."
    ),
    year_tolerance: int = Field(
        default=5, ge=0, le=50, description="Allowed birth-year difference when matching."
    ),
    include_private: bool = _include_private(),
) -> dict:
    """Consult the legacy GEDCOM reference layer for HINTS (never authoritative).

    Searches the configured Ancestry/FamilySearch exports and returns, per file,
    matching individuals and their claimed facts. Crucially, each fact is flagged
    whether the legacy tree attached a source, with the source text if present --
    so you can distinguish 'they cite an actual death certificate' from 'unsourced
    guess'. These are UNTRUSTED hints: use them to decide what real record to hunt
    for, then create the fact in the tree citing that record -- do not copy a hint
    in as a sourced fact. Probably-living people are withheld and counted.
    """
    try:
        if len(name.strip()) < 2:
            return {
                "error": "no_criteria",
                "message": "Pass at least two characters of a name. An empty "
                "name would return every person in every file.",
            }
        library = state.library_()
        if not library.labels:
            return {
                "error": "no_reference_files",
                "message": "No reference GEDCOMs configured. Add [[reference]] "
                "entries to gramps_mcp.toml.",
            }
        exposing = bool(state.config and state.config.expose_private)
        if include_private and not exposing:
            logger.info("privacy filter lifted for consult_reference at the caller's request")
        withhold = not (exposing or include_private)
        return {
            "query": {"name": name, "approx_birth_year": approx_birth_year},
            "results": library.consult(
                name, approx_birth_year, year_tolerance, withhold_living=withhold
            ),
        }
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


def _strip_schema_titles(node: Any) -> None:
    """Remove every ``title`` *keyword* from a JSON schema, in place.

    Pydantic derives a title for each field from its own name, so a property
    called ``source`` ships ``"title": "Source"`` and the argument wrapper
    ships ``"title": "<tool>Arguments"``. Neither tells a model anything the
    surrounding structure does not.

    The subtlety, and the reason this walks the structure rather than every
    dict it meets: under ``properties`` and ``$defs`` the keys are *names*,
    not schema keywords. A tool with a parameter called ``title`` -- NARA's
    record search has one -- would otherwise lose it entirely.

    Titles are documentation-only in JSON Schema, and validation runs against
    the pydantic models rather than the published copy, so dropping them
    changes nothing a caller can observe.
    """
    if not isinstance(node, dict):
        if isinstance(node, list):
            for value in node:
                _strip_schema_titles(value)
        return

    node.pop("title", None)
    for keyword, value in node.items():
        if keyword in ("properties", "$defs", "definitions", "patternProperties"):
            # Keys here are names. Descend into the values only.
            if isinstance(value, dict):
                for subschema in value.values():
                    _strip_schema_titles(subschema)
        else:
            _strip_schema_titles(value)


def compact_schemas() -> int:
    """Shrink the published tool schemas. Returns the characters saved.

    Every tool definition ships to the model on every session, before any
    work happens. Across this surface the schemas outweigh the descriptions
    three to one, and auto-generated titles are roughly a tenth of the whole
    block while carrying no information.

    Run once at import. Idempotent, so calling it again is harmless.
    """
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:  # pragma: no cover - guards a future mcp refactor
        return 0
    registered = getattr(manager, "_tools", {})
    before = sum(len(json.dumps(t.parameters)) for t in registered.values())
    for tool in registered.values():
        _strip_schema_titles(tool.parameters)
    after = sum(len(json.dumps(t.parameters)) for t in registered.values())
    return before - after


#: Characters trimmed from the published schemas at import.
SCHEMA_CHARS_SAVED = compact_schemas()


def _refusing_unknown(model: type[BaseModel], tool_name: str) -> type[BaseModel]:
    """Subclass a tool's argument model so it refuses names it does not define.

    The refusal lists what the tool does take, so a caller that guessed a name
    can correct itself in one step rather than guessing again.

    Parameters
    ----------
    model : type[BaseModel]
        The argument model the SDK built from the tool's signature.
    tool_name : str
        The registered tool name, which heads the refusal.

    Returns
    -------
    type[BaseModel]
        A subclass with the same name and fields that forbids extras.
    """
    # A parameter that shadows a BaseModel method is stored under a prefixed
    # field name with the real one as its alias; callers send the alias.
    accepted = sorted(f.alias or name for name, f in model.model_fields.items())

    def name_the_unknown(cls, data: Any) -> Any:
        if isinstance(data, dict):
            if unknown := sorted(set(data) - set(accepted)):
                # ValueError specifically: pydantic turns it into a
                # ValidationError, which the SDK reports as a ToolError with
                # this message intact. Other exception types are not
                # guaranteed to keep it.
                raise ValueError(
                    f"{tool_name} has no parameter "
                    f"{', '.join(repr(u) for u in unknown)}. It takes: "
                    f"{', '.join(accepted) or 'no parameters'}."
                )
        return data

    # Built with type() so the subclass keeps the parent's name, which is
    # what pydantic prints at the head of the refusal.
    return type(
        model.__name__,
        (model,),
        {
            "__module__": model.__module__,
            # Merged with the parent's config, not a replacement for it.
            "model_config": ConfigDict(extra="forbid"),
            "_name_the_unknown": model_validator(mode="before")(classmethod(name_the_unknown)),
        },
    )


def refuse_unknown_arguments() -> int:
    """Make every tool refuse a parameter it does not define. Returns the count.

    The SDK builds argument models with pydantic's default of *ignoring* extra
    fields, and the published schemas do not forbid them either. So a misnamed
    argument was accepted and silently dropped, and the call answered as if
    that filter had never been given -- a plausible-looking wrong answer
    rather than an error. Reported from real use: ``query_objects`` given
    ``query="1870 census"`` for sources and ``query="all"`` for places
    (the parameter is ``gql``) returned the first 200 objects unfiltered, and
    nothing flagged it. On a write tool the same defect silently drops part
    of what the caller meant to record.

    Also publishes ``additionalProperties: false``, so a client that validates
    against the schema can refuse before sending.

    Run once at import. Idempotent, so calling it again is harmless.
    """
    manager = getattr(mcp, "_tool_manager", None)
    if manager is None:  # pragma: no cover - guards a future mcp refactor
        return 0
    changed = 0
    for tool in getattr(manager, "_tools", {}).values():
        meta = tool.fn_metadata
        if meta.arg_model.model_config.get("extra") != "forbid":
            meta.arg_model = _refusing_unknown(meta.arg_model, tool.name)
            changed += 1
        tool.parameters["additionalProperties"] = False
    return changed


#: Tools made to refuse unknown parameters at import.
TOOLS_REFUSING_UNKNOWN = refuse_unknown_arguments()


def run() -> None:
    """Serve on the transport ``GRAMPS_MCP_TRANSPORT`` names.

    stdio, the default, is what a desktop client launches. ``http`` serves
    Streamable HTTP at ``/mcp`` on ``GRAMPS_MCP_HOST``:``GRAMPS_MCP_PORT``,
    with no authentication of its own -- the README says what to put in front
    of it.
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    # httpx logs every request URL at INFO, and a GrampsQL filter or a place
    # name travels in the query string. The log holds ids and handles only.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    transport, options = transport_settings()
    # MCPServer.run is synchronous -- it drives its own event loop. Wrapping
    # it in asyncio.run() passes None where a coroutine is expected and
    # raises ValueError once the server stops.
    mcp.run(transport, **options)


if __name__ == "__main__":
    run()
