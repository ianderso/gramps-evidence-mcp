"""High-level genealogy operations used by the MCP tools.

This sits between the thin HTTP client and the MCP tool functions. It enforces
the evidence model (a fact needs a citation), resolves human-friendly references
(handle *or* gramps_id), find-or-creates Places, applies privacy filtering to
bulk output, and shapes results into LLM-friendly dicts.

Nothing here logs record *contents*: only handles, ids, and operation names.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import copy
import difflib
import hashlib
import logging
import mimetypes
import re
from collections.abc import Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from . import mapping, ocr
from .client import (
    _CLASS_NAMES,
    ENDPOINTS,
    GQL_NOTES,
    FailedWriteError,
    GrampsApiError,
    GrampsWebClient,
    UnsupportedServerError,
)
from .config import Config
from .models import (
    ChildLink,
    CitationEdit,
    CitationInput,
    Confidence,
    DocType,
    EventInput,
    Gender,
    NameMatch,
    NameParts,
    OcrEngine,
    RepositoryLink,
    TaskPriority,
    TaskStatus,
)
from .privacy import assess, redacted_stub

logger = logging.getLogger("gramps_evidence_mcp.service")

_BIRTHLIKE = {"Birth", "Baptism", "Christening"}
_DEATHLIKE = {"Death", "Burial", "Cremation"}

#: Paths a structured query adds to its ``select`` so each row can be judged
#: for privacy, keyed by collection; any other collection is judged by its own
#: private flag. A family row can reach either parent's name through
#: ``father``/``mother``, so it is judged by both parents as well.
_PRIVACY_PATHS: dict[str, tuple[tuple[str, list[str]], ...]] = {
    "person": (
        ("_birth", ["birth", "date"]),
        ("_death", ["death", "date"]),
        ("_private", ["private"]),
    ),
    "family": (
        ("_private", ["private"]),
        *(
            (f"_{parent}_{key}", [parent, *path])
            for parent in ("father", "mother")
            for key, path in (
                ("handle", ["handle"]),
                ("birth", ["birth", "date"]),
                ("death", ["death", "date"]),
                ("private", ["private"]),
            )
        ),
    ),
}

#: What export_backup can write: Gramps XML (lossless) and three interchange
#: formats. The name becomes part of a URL and a file name, so it is checked.
_EXPORT_FORMATS = ("gramps", "ged", "json", "csv")

#: Transactions per request when the log is read for one record's history on
#: gramps-webapi 3.21: 2,000 took 1.7 s and 1 MB on a tree with 54,000
#: transactions (2026-10-06), as do the changes of one record on 3.22.
_HISTORY_PAGE = 2000

#: How far back the log is read for one record on 3.21 before stopping and
#: saying so: about 85 s at that rate.
_HISTORY_SCAN_LIMIT = 100_000

#: How many of an event's other participants are read for the attribute
#: names on their references to it.
_REF_NAME_SOURCES = 50

#: The ids a redacted stub is built from, selected under names the caller
#: cannot use: a caller's own column aliased ``gramps_id`` could otherwise
#: carry a living person's name into the stub that withholds them.
_IDENTITY_PATHS: tuple[tuple[str, list[str]], ...] = (
    ("_gid", ["gramps_id"]),
    ("_handle", ["handle"]),
)

#: The person columns the query engine accepts (docs/PITFALLS.md section 13).
#: Selected explicitly when a person query names none, so that the privacy
#: paths can travel with them: the default columns carry no dates, and
#: without dates nobody can be shown to be historical.
_PERSON_COLUMNS = (
    "gramps_id",
    "handle",
    "given_name",
    "surname",
    "gender",
    "birth_ref_index",
    "death_ref_index",
    "private",
    "change",
)

#: Report options that decide whether living people and private records
#: appear, with the values that leave them out. ``living_people`` 0 is
#: Gramps' "Not included"; its default, 99, includes them with all data.
_REPORT_PRIVACY_OPTIONS: dict[str, Any] = {"living_people": 0, "incl_private": False}

#: The built-in person filters ``GET /api/facts/`` accepts. Each is anchored
#: on one person, passed as ``handle``; any other name is a saved custom
#: filter, which takes no anchor.
FACT_PERSON_FILTERS = ("Ancestors", "Descendants", "DescendantFamilies", "CommonAncestor")


#: Set for the length of one tool call whose caller asked to see living people
#: and private records. Context-local, so one call's request cannot leak into
#: another running beside it -- see :meth:`GrampsService.privacy_lifted`.
_PRIVACY_LIFTED: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "privacy_lifted", default=False
)


class CitationRequiredError(ValueError):
    """Raised when a fact-recording call omits a citation and require_citation=True."""


class NotFoundError(ValueError):
    """Raised when a referenced object can't be resolved by handle or gramps_id."""


class AmbiguousPlaceError(ValueError):
    """Raised when a bare place name matches more than one existing place.

    Guessing between them — or minting yet another duplicate — is exactly the
    failure recorded as PITFALLS #10; the caller must disambiguate with a
    gramps_id or the full title.
    """


class MultipleEnclosuresError(ValueError):
    """Raised when update_place would flatten a multi-entry (dated) enclosure list."""


class InvalidCarryTargetError(ValueError):
    """Raised when carry_to cannot receive what a deletion would strand."""


class UnknownTypeError(ValueError):
    """Raised when a write names a type neither Gramps nor the tree has.

    Gramps would store it as a new custom type rather than refuse it, which
    is how a typo becomes part of the vocabulary for good.
    """


class GrampsService:
    """Genealogy operations over a :class:`GrampsWebClient`.

    Enforces the evidence model, resolves handle-or-gramps_id references,
    find-or-creates places, applies privacy filtering to bulk output, and
    shapes results for the tool layer.

    Parameters
    ----------
    client : GrampsWebClient
        Connected API client.
    config : Config
        Resolved server configuration.
    """

    def __init__(self, client: GrampsWebClient, config: Config):
        self.client = client
        self.config = config
        self._place_cache: dict[str, str] = {}  # lower(name) -> place handle
        self._event_types: dict[str, int] | None = None  # label -> stored int
        self._open_spans: bool | None = None  # server stores "from X" / "to X"
        self._transkribus_api: ocr.TranskribusClient | None = None  # made on first use
        self._ledger_lock = asyncio.Lock()  # one Transkribus ledger change at a time

    @property
    def exposing_private(self) -> bool:
        """bool: Whether bulk output shows living people and private records.

        True when the operator set ``expose_private``, or when the current
        tool call asked for them through :meth:`privacy_lifted`.
        """
        return self.config.expose_private or _PRIVACY_LIFTED.get()

    @contextlib.contextmanager
    def privacy_lifted(self, lifted: bool, tool: str) -> Iterator[None]:
        """Show living people and private records for one call, if asked.

        This is how the filter is lifted at the user's request without
        touching configuration: the tool passes its ``include_private``
        argument here and makes its call inside the block. A lift is logged
        by tool name, so the operator can see when one happened.

        Parameters
        ----------
        lifted : bool
            The caller's ``include_private`` argument.
        tool : str
            The tool's name, for the log.
        """
        if lifted and not self.config.expose_private:
            logger.info("privacy filter lifted for %s at the caller's request", tool)
        token = _PRIVACY_LIFTED.set(bool(lifted))
        try:
            yield
        finally:
            _PRIVACY_LIFTED.reset(token)

    # ------------------------------------------------------------------ #
    # reference resolution
    # ------------------------------------------------------------------ #
    async def _resolve(self, object_type: str, ref: str, **kwargs: Any) -> dict:
        """Fetch an object by handle, falling back to gramps_id.

        Parameters
        ----------
        object_type : str
            Gramps object type, e.g. ``"person"``.
        ref : str
            A handle or a gramps_id.
        **kwargs
            Passed to the client's fetch.

        Returns
        -------
        dict
            The object.

        Raises
        ------
        NotFoundError
            If neither lookup finds it.
        """
        try:
            return await self.client.get_object(object_type, ref, **kwargs)
        except GrampsApiError as exc:
            if exc.status != 404:
                raise
        obj = await self.client.get_by_gramps_id(object_type, ref, **kwargs)
        if obj is None:
            raise NotFoundError(f"No {object_type} found with handle or gramps_id '{ref}'.")
        return obj

    async def _resolve_handle(self, object_type: str, ref: str) -> str:
        """Resolve a handle-or-gramps_id reference to a handle."""
        obj = await self._resolve(object_type, ref, keys="handle,gramps_id")
        return obj["handle"]

    # ------------------------------------------------------------------ #
    # vocabularies and dates
    # ------------------------------------------------------------------ #
    async def _canonical_type(
        self,
        datatype: str,
        value: str,
        allow_new: bool = False,
        types: dict | None = None,
        custom_keys: tuple[str, ...] = (),
    ) -> str:
        """Spell a type name as the tree's vocabulary does, or refuse it.

        The server matches a type name exactly, and stores any other string as
        a new custom type (docs/PITFALLS.md section 26), so a typo or a stray
        capital becomes a permanent entry in the tree's vocabulary. The
        vocabulary is ``GET /api/types/``: Gramps' standard names under
        ``default`` and the tree's own under ``custom``, read on every call so
        a custom type created a moment ago is known.

        A name matches ignoring case, spacing and punctuation ("census",
        "E-Mail"), then through a short list of unambiguous synonyms
        (``_TYPE_SYNONYMS``: "Born" is Birth), then among the tree's custom
        names. Anything else is refused, with the closest names suggested,
        unless it is meant as a new custom type.

        Parameters
        ----------
        datatype : str
            The standard vocabulary, e.g. ``"event_types"``.
        value : str
            The name the caller gave.
        allow_new : bool, optional
            Accept a name in neither list, as a deliberate new custom type.
        types : dict, optional
            ``GET /api/types/`` already read in this call.
        custom_keys : tuple of str, optional
            The tree's custom lists to match against, when not the one named
            like ``datatype``: Gramps keeps attribute names apart by the kind
            of object carrying them. The first list's spelling wins.

        Returns
        -------
        str
            The name as the vocabulary spells it; ``value`` with its spacing
            tidied when it is a new custom type, or when the server lists no
            vocabulary at all.

        Raises
        ------
        UnknownTypeError
            If the name matches nothing and ``allow_new`` is False.
        """
        wanted = " ".join(str(value).split())
        types = types if types is not None else await self.client.types()
        default = [str(n).strip() for n in ((types.get("default") or {}).get(datatype)) or []]
        custom = list(
            dict.fromkeys(
                str(n).strip()
                for key in custom_keys or (datatype,)
                for n in ((types.get("custom") or {}).get(key)) or []
                if n
            )
        )
        if not (default or custom):
            return wanted
        by_key = {_type_key(n): n for n in default}
        synonyms = _TYPE_SYNONYMS.get(datatype, {})

        def standard(key: str) -> str | None:
            target = synonyms.get(key)
            return by_key.get(key) or (by_key.get(_type_key(target)) if target else None)

        key = _type_key(wanted)
        if found := standard(key):
            return found
        # The tree's own names, less any a standard one shadows: a custom
        # "census" or "Web Home Page" is an old accident, never the name to use.
        own = [n for n in custom if standard(_type_key(n)) is None]
        for name in own:
            if _type_key(name) == key:
                return name
        if allow_new:
            return wanted
        known = {_type_key(n): n for n in (*own, *default)}
        close = [known[k] for k in difflib.get_close_matches(key, list(known), n=3, cutoff=0.6)]
        label = _TYPE_LABELS.get(datatype) or datatype.removesuffix("_types").replace("_", " ")
        article = "an" if label[0] in "aeio" else "a"
        raise UnknownTypeError(
            f"{wanted!r} is not {article} {label} type in this tree."
            + (f" Did you mean {' or '.join(repr(c) for c in close)}?" if close else "")
            + f" Standard: {', '.join(n for n in default if n != 'Unknown') or 'none'}."
            + f" Custom: {', '.join(own) or 'none'}."
            + " Pass allow_new_type=True to create it as a new custom type."
        )

    async def _open_spans_supported(self) -> bool:
        """Whether the server's Gramps stores "from X" and "to X" dates.

        The modifiers arrived in Gramps 5.2 (``Date.MOD_FROM`` 7,
        ``Date.MOD_TO`` 8); gramps-webapi 2.x and 3.x run on 5.2 and 6.0.
        ``GET /api/metadata/`` names the version under ``gramps.version``.
        Read once per session. An unreadable answer counts as unsupported,
        which keeps the words as a text-only date rather than writing a
        modifier an older server cannot display; a failed read counts as
        unsupported for that date only, and is retried for the next.
        """
        if self._open_spans is None:
            try:
                meta = await self.client.metadata()
            except GrampsApiError:
                # Not cached: a passing failure must not decide the session.
                return False
            gramps = meta.get("gramps") if isinstance(meta, dict) else None
            version = str((gramps or {}).get("version") or "")
            parts = [int(p) for p in re.findall(r"\d+", version)[:2]]
            self._open_spans = len(parts) == 2 and tuple(parts) >= (5, 2)
        return self._open_spans

    async def _parse_date(self, text: str | None) -> dict:
        """Parse a date, keeping "from X" / "to X" as text where unsupported."""
        if mapping.is_open_span(text) and not await self._open_spans_supported():
            return mapping.parse_date(text, open_spans=False)
        return mapping.parse_date(text)

    # ------------------------------------------------------------------ #
    # citation enforcement
    # ------------------------------------------------------------------ #
    async def resolve_citation(self, cit: CitationInput) -> str:
        """Return a citation handle, creating the source, citation and note as needed.

        ``citation`` and ``source`` each accept a handle or a gramps_id;
        :meth:`_resolve_handle` tries the handle first and falls back, so a
        caller never has to say which form it holds.

        Parameters
        ----------
        cit : CitationInput
            An existing citation, an existing source, or enough to create both.

        Returns
        -------
        str
            Handle of the citation to attach.

        Raises
        ------
        CitationRequiredError
            If nothing in the input identifies or creates a source.
        """
        if cit.citation:
            return await self._resolve_handle("citation", cit.citation)

        # Need a source handle to hang a new citation on.
        if cit.source:
            source_handle = await self._resolve_handle("source", cit.source)
        elif cit.source_title:
            created = await self.client.create_object(
                "source",
                mapping.source_payload(cit.source_title, cit.source_author, cit.source_pubinfo),
            )
            source_handle = created["handle"]
            logger.info("created source %s", created.get("gramps_id"))
        else:  # pragma: no cover - guarded by the pydantic validator
            raise CitationRequiredError("CitationInput has no source to cite.")

        note_handles: list[str] = []
        if cit.note:
            note = await self.client.create_object(
                "note", {"_class": "Note", "text": {"string": cit.note}, "type": "Citation"}
            )
            note_handles.append(note["handle"])

        citation = await self.client.create_object(
            "citation",
            mapping.citation_payload(
                source_handle,
                cit.page,
                cit.confidence,
                await self._parse_date(cit.date),
                note_handles,
            ),
        )
        logger.info(
            "created citation %s (confidence=%s)", citation.get("gramps_id"), cit.confidence.value
        )
        return citation["handle"]

    async def _citation_list_for_fact(
        self, citation: CitationInput | None, require_citation: bool
    ) -> tuple[list[str], list[dict]]:
        """Return (citation_handles, extra_attributes) for a fact.

        Enforces the evidence model: a citation is required unless the caller
        explicitly passes require_citation=False, in which case the event is
        stamped with the configured UNSOURCED attribute so it's auditable.
        """
        if citation is not None:
            handle = await self.resolve_citation(citation)
            return [handle], []
        if require_citation:
            raise CitationRequiredError(
                "This fact has no citation. Provide a citation (existing handle/id "
                "or an inline source_title + page + confidence), or pass "
                "require_citation=False to record it as UNSOURCED for later audit."
            )
        return [], [mapping.unsourced_attribute(self.config.unsourced_attribute)]

    # ------------------------------------------------------------------ #
    # places
    # ------------------------------------------------------------------ #
    async def find_or_create_place(self, name: str | None) -> str | None:
        """Resolve a place reference, creating one only as a last resort.

        Resolution order:

        1. Handle or gramps_id, so ``"P0000"`` means that place.
        2. Exact title match, case-insensitive.
        3. Exact name match, case-insensitive, but only when unambiguous.
        4. Create. Only a string matching nothing becomes a new place.

        See ``docs/PITFALLS.md`` section 10.

        Parameters
        ----------
        name : str or None
            Handle, gramps_id, title or name. None returns None.

        Returns
        -------
        str or None
            The place's handle, or None for empty input.

        Raises
        ------
        AmbiguousPlaceError
            If several places share the given name, rather than guessing.
        """
        if not name or not name.strip():
            return None
        ref = name.strip()
        key = ref.lower()
        if key in self._place_cache:
            return self._place_cache[key]

        try:
            obj = await self._resolve("place", ref, keys="handle,gramps_id")
            self._place_cache[key] = obj["handle"]
            return obj["handle"]
        except NotFoundError:
            pass

        places = await self.client.list_objects("place", keys="handle,title,gramps_id,name")
        for place in places:
            if (place.get("title") or "").strip().lower() == key:
                self._place_cache[key] = place["handle"]
                return place["handle"]

        name_hits = [
            p for p in places if (((p.get("name") or {}).get("value")) or "").strip().lower() == key
        ]
        if len(name_hits) == 1:
            self._place_cache[key] = name_hits[0]["handle"]
            return name_hits[0]["handle"]
        if len(name_hits) > 1:
            candidates = ", ".join(
                sorted(
                    f"{p.get('gramps_id') or p['handle']} ('{p.get('title') or ''}')"
                    for p in name_hits
                )
            )
            raise AmbiguousPlaceError(
                f"{len(name_hits)} places are named '{ref}': {candidates}. "
                "Pass the gramps_id or the full title of the one you mean — "
                "guessing (or minting yet another) would make the duplication worse."
            )

        created = await self.client.create_object("place", mapping.place_payload(ref))
        self._place_cache[key] = created["handle"]
        logger.info("created place %s", created.get("gramps_id"))
        return created["handle"]

    async def update_place(
        self,
        ref: str,
        place_type: str | None = None,
        parent: str | None = None,
        remove_parent: bool = False,
        name: str | None = None,
        title: str | None = None,
        latitude: str | None = None,
        longitude: str | None = None,
        code: str | None = None,
        allow_new_type: bool = False,
    ) -> dict:
        """Edit a place's type, enclosure, name, title or coordinates.

        These are the fields ``update_object_fields`` refuses, because they
        need guard rails: the parent must already exist, since a typo'd parent
        silently minting a place is how duplicate hierarchies start; the
        enclosure must not form a cycle; and a place holding several dated
        enclosures is refused rather than flattened.

        Parameters
        ----------
        ref : str
            Handle or gramps_id of the place.
        place_type : str, optional
            Gramps place type.
        parent : str, optional
            Handle or gramps_id of an existing enclosing place.
        remove_parent : bool, optional
            Clear the enclosure. Mutually exclusive with ``parent``.
        name, title : str, optional
            New name or title.
        latitude, longitude, code : str, optional
            New coordinates or code.

        Returns
        -------
        dict
            Handle, gramps_id and a message, or an ``error`` key describing
            why the edit was refused.

        Raises
        ------
        MultipleEnclosuresError
            If the place holds several dated enclosures.
        """
        if parent and remove_parent:
            return {
                "error": "conflicting_arguments",
                "message": "Pass either parent or remove_parent, not both.",
            }
        if place_type is not None:
            place_type = await self._canonical_type("place_types", place_type, allow_new_type)

        parent_handle: str | None = None
        if parent:
            target = await self._resolve("place", ref, keys="handle,gramps_id")
            parent_obj = await self._resolve("place", parent)
            parent_handle = parent_obj["handle"]
            if parent_handle == target["handle"]:
                return {
                    "error": "enclosure_cycle",
                    "message": "A place cannot enclose itself.",
                }
            # Walk up from the proposed parent; the target must not be an
            # ancestor of it, or the enclosure graph becomes a cycle.
            seen: set[str] = set()
            frontier = [parent_obj]
            for _ in range(50):
                next_frontier = []
                for node in frontier:
                    for pref in node.get("placeref_list") or []:
                        up = pref.get("ref")
                        if not up or up in seen:
                            continue
                        if up == target["handle"]:
                            return {
                                "error": "enclosure_cycle",
                                "message": f"Setting that parent would create a cycle: "
                                f"{target.get('gramps_id') or ref} already encloses "
                                f"{parent_obj.get('gramps_id') or parent} (directly or "
                                "transitively).",
                            }
                        seen.add(up)
                        next_frontier.append(
                            await self._resolve("place", up, keys="handle,placeref_list")
                        )
                if not next_frontier:
                    break
                frontier = next_frontier

        def edit(obj: dict) -> str | bool:
            changed: list[str] = []
            if place_type is not None and obj.get("place_type") != place_type:
                obj["place_type"] = place_type
                changed.append(f"place_type={place_type}")
            if parent_handle or remove_parent:
                refs = obj.get("placeref_list") or []
                if len(refs) > 1:
                    raise MultipleEnclosuresError(
                        f"This place has {len(refs)} enclosure references "
                        "(dated enclosures like a territory→state succession). "
                        "Refusing to replace them wholesale — edit those in the "
                        "Gramps UI where the dates are visible."
                    )
                if remove_parent:
                    if refs:
                        obj["placeref_list"] = []
                        changed.append("parent removed")
                elif not refs or refs[0].get("ref") != parent_handle:
                    obj["placeref_list"] = [{"_class": "PlaceRef", "ref": parent_handle}]
                    changed.append("parent set")
            if name is not None:
                current = obj.get("name") or {}
                if current.get("value") != name:
                    current["value"] = name
                    obj["name"] = current
                    changed.append("name")
            for key_, val in (
                ("title", title),
                ("lat", latitude),
                ("long", longitude),
                ("code", code),
            ):
                if val is not None and obj.get(key_) != val:
                    obj[key_] = val
                    changed.append(key_)
            return ", ".join(changed) if changed else False

        return await self._mutate("place", ref, edit, label="updated")

    # ------------------------------------------------------------------ #
    # write: events
    # ------------------------------------------------------------------ #
    async def _resolve_event_type(self, ev: EventInput) -> None:
        """Spell an event's type as the tree does, refusing an unknown one unless meant.

        Done once per event, before anything is written: ``add_person`` checks
        a birth and a death together, so a refused death leaves no birth
        behind.
        """
        if not ev._type_resolved:
            ev.type = await self._canonical_type("event_types", ev.type, ev.allow_new_type)
            ev._type_resolved = True

    async def _create_event(self, ev: EventInput, require_citation: bool) -> tuple[str, bool]:
        """Create an Event object. Returns (event_handle, is_unsourced).

        The type is spelt as Gramps spells it, and a name matching nothing is
        refused unless ``ev.allow_new_type`` (:meth:`_resolve_event_type`):
        the server matches type names case-sensitively, so "birth" would
        otherwise be stored as a new custom type beside Birth. ``ev.type`` is
        updated to what was stored.
        """
        await self._resolve_event_type(ev)
        citation_handles, extra_attrs = await self._citation_list_for_fact(
            ev.citation, require_citation
        )
        place_handle = await self.find_or_create_place(ev.place)
        payload = mapping.event_payload(
            ev.type, await self._parse_date(ev.date), place_handle, ev.description, citation_handles
        )
        if extra_attrs:
            payload["attribute_list"] = extra_attrs
        created = await self.client.create_object("event", payload)
        logger.info(
            "created event %s type=%s unsourced=%s",
            created.get("gramps_id"),
            ev.type,
            not citation_handles,
        )
        return created["handle"], not citation_handles

    async def add_person(
        self,
        name: NameParts,
        gender: Gender,
        birth: EventInput | None = None,
        death: EventInput | None = None,
        require_citation: bool = True,
    ) -> dict:
        """Create a person, optionally with cited birth and death events.

        Parameters
        ----------
        name : NameParts
            The person's primary name.
        gender : Gender
            The person's gender.
        birth, death : EventInput, optional
            Events to create and attach. The type defaults to ``"Birth"`` and
            ``"Death"`` respectively.
        require_citation : bool, optional
            Fail rather than record an uncited event. When False the event is
            stamped with the unsourced attribute instead.

        Returns
        -------
        dict
            Handle, gramps_id and a message naming any unsourced facts.

        Raises
        ------
        CitationRequiredError
            If an event lacks a citation and ``require_citation`` is True.
        """
        event_refs: list[dict] = []
        birth_index = -1
        death_index = -1
        unsourced: list[str] = []

        # The indices are what the server computes on the person's first
        # update -- the first Birth and Death events -- so a birth given as,
        # say, a Baptism is kept as an event but never set as the birth only
        # to be unset by the next edit (docs/PITFALLS.md section 19).
        if birth is not None:
            birth.type = birth.type or "Birth"
        if death is not None:
            death.type = death.type or "Death"
        for event in (birth, death):  # both checked before either is written
            if event is not None:
                await self._resolve_event_type(event)
        if birth is not None:
            handle, is_uns = await self._create_event(birth, require_citation)
            if birth.type == "Birth":
                birth_index = len(event_refs)
            event_refs.append(mapping.event_ref(handle))
            if is_uns:
                unsourced.append("birth")
        if death is not None:
            handle, is_uns = await self._create_event(death, require_citation)
            if death.type == "Death":
                death_index = len(event_refs)
            event_refs.append(mapping.event_ref(handle))
            if is_uns:
                unsourced.append("death")

        payload = mapping.person_payload(name, gender)
        payload["event_ref_list"] = event_refs
        payload["birth_ref_index"] = birth_index
        payload["death_ref_index"] = death_index
        created = await self.client.create_object("person", payload)
        logger.info("created person %s", created.get("handle"))
        return {
            "handle": created["handle"],
            "gramps_id": created.get("gramps_id"),
            "object_type": "person",
            "unsourced_facts": unsourced,
            "message": _created_message("person", created, unsourced),
        }

    async def add_event_to_person(
        self, person_ref: str, ev: EventInput, require_citation: bool = True
    ) -> dict:
        """Create a cited event and attach it to a person.

        A birth-like or death-like event becomes the person's primary birth or
        death when they have none.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id of the person.
        ev : EventInput
            The event to create.
        require_citation : bool, optional
            Fail rather than record an uncited event.

        Returns
        -------
        dict
            The event's handle, gramps_id and a message.

        Raises
        ------
        CitationRequiredError
            If the event lacks a citation and ``require_citation`` is True.
        NotFoundError
            If the person cannot be resolved.
        """
        # Resolved first, so a bad reference fails before an event exists.
        person_handle = await self._resolve_handle("person", person_ref)
        handle, is_uns = await self._create_event(ev, require_citation)

        def edit(person: dict) -> str:
            _append_event_ref(person, mapping.event_ref(handle), ev.type)
            return f"{ev.type} event added"

        result = await self._mutate("person", person_handle, edit, label="added an event to")
        return _with_repairs(
            {
                "handle": result["handle"],
                "gramps_id": result.get("gramps_id"),
                "event_handle": handle,
                "object_type": "event",
                "unsourced": is_uns,
                "message": f"Added {ev.type} event to person {result.get('gramps_id')}"
                + (" (UNSOURCED)" if is_uns else ""),
            },
            result,
        )

    async def cite_event(self, event_ref: str, cit: CitationInput) -> dict:
        """Attach a citation to an existing event.

        Resolves the event by handle-or-gramps_id, resolves/creates the citation
        (enforcing the evidence model via resolve_citation), appends it to the
        event's citation_list (avoiding duplicates), and PUTs the event back.
        """
        event_handle = await self._resolve_handle("event", event_ref)
        citation_handle = await self.resolve_citation(cit)
        unsourced = self.config.unsourced_attribute

        def edit(event: dict) -> str | bool:
            changed = False
            citation_list = event.setdefault("citation_list", [])
            if citation_handle not in citation_list:
                citation_list.append(citation_handle)
                changed = True
            # The event now has a source, so drop any UNSOURCED audit tag on it.
            attrs = event.get("attribute_list") or []
            kept = [a for a in attrs if _type_string(a.get("type")) != unsourced]
            if len(kept) != len(attrs):
                event["attribute_list"] = kept
                changed = True
            return "citation attached" if changed else False

        result = await self._mutate("event", event_handle, edit, label="cited")
        logger.info("cited event %s with citation %s", result.get("gramps_id"), citation_handle)
        return _with_repairs(
            {
                "handle": result["handle"],
                "gramps_id": result.get("gramps_id"),
                "object_type": "event",
                "citation_handle": citation_handle,
                "message": f"Cited event {result.get('gramps_id') or result['handle']}"
                if result["changed"]
                else f"Event {result.get('gramps_id')} already carries that citation",
            },
            result,
        )

    async def update_event(
        self,
        event_ref: str,
        date: str | None = None,
        place: str | None = None,
        description: str | None = None,
        clear_place: bool = False,
        *,
        event_type: str | None = None,
        clear_date: bool = False,
        allow_new_type: bool = False,
    ) -> dict:
        """Edit an existing event in place: type, date, place, description.

        Only what is given changes. The event keeps its handle, gramps_id,
        citations, notes, attributes, media, tags and every reference to it,
        which is what replacing it with a new event of the right type loses.

        Parameters
        ----------
        event_ref : str
            Handle or gramps_id.
        date : str, optional
            New date, parsed by :func:`mapping.parse_date`.
        place : str, optional
            New place, resolved by :meth:`find_or_create_place`.
        description : str, optional
            New description.
        clear_place, clear_date : bool, optional
            Remove the place or the date. An empty ``place`` or ``date`` is
            refused rather than taken to mean either: an empty place string
            would otherwise reach place resolution, and an empty date would
            clear the date without being asked to.
        event_type : str, optional
            New type, matched against the tree's event types. When the change
            moves an event into or out of Birth or Death, gramps-webapi 3.21.1
            recomputes the birth and death of every person referencing it
            (``update_object`` in ``api/resources/util.py``).
        allow_new_type : bool, optional
            Accept a type the tree does not have yet, as a new custom type.

        Returns
        -------
        dict
            Handle, gramps_id and a message naming what changed, or an
            ``error`` key.

        Raises
        ------
        UnknownTypeError
            If ``event_type`` is not in the tree's vocabulary and
            ``allow_new_type`` is False.
        """
        if date is not None and clear_date:
            return _conflict("date", "clear_date")
        if place is not None and clear_place:
            return _conflict("place", "clear_place")
        for name, value, flag in (("date", date, "clear_date"), ("place", place, "clear_place")):
            if value is not None and not value.strip():
                return {
                    "error": "empty_value",
                    "message": f"An empty {name} is not a {name}. Pass {flag}=True to remove "
                    f"it, or omit {name} to leave it unchanged.",
                }
        # The event and the type are checked before the place, whose
        # resolution may create one: a refused edit must leave nothing behind.
        event_handle = await self._resolve_handle("event", event_ref)
        new_type = (
            await self._canonical_type("event_types", event_type, allow_new_type)
            if event_type is not None and event_type.strip()
            else None
        )
        new_date = await self._parse_date(date) if date is not None else None
        new_place = await self.find_or_create_place(place) if place is not None else None

        def edit(event: dict) -> str | bool:
            changed: list[str] = []
            if new_type is not None:
                old = _type_string(event.get("type"))
                if old != new_type:
                    event["type"] = new_type
                    changed.append(f"type {old} -> {new_type}")
            if new_date is not None and not _same_date(event.get("date"), new_date):
                event["date"] = new_date
                changed.append(f"date={_date_string(new_date)}")
            if clear_date and _date_string(event.get("date")):
                event["date"] = mapping.parse_date(None)
                changed.append("date cleared")
            if new_place is not None and event.get("place") != new_place:
                event["place"] = new_place
                changed.append("place")
            if clear_place and event.get("place"):
                event["place"] = ""
                changed.append("place cleared")
            if description is not None and (event.get("description") or "") != description:
                event["description"] = description
                changed.append("description")
            return ", ".join(changed) if changed else False

        return await self._mutate("event", event_handle, edit, label="updated")

    async def add_event_ref(
        self,
        person_ref: str,
        event_ref: str,
        role: str = "Primary",
        allow_new_type: bool = False,
        attributes: dict[str, str] | None = None,
    ) -> dict:
        """Add an existing event to a person, in a role.

        One census entry, burial or residence that several people took part
        in is one event, referenced by each of them; a copy per person would
        split its citations and let the copies drift apart. What is the
        person's own -- their line on a census sheet, their age -- goes on the
        reference as attributes (TOOL-REQUESTS #7).

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id of the person.
        event_ref : str
            Handle or gramps_id of the existing event.
        role : str, optional
            The person's role, matched against the tree's role types.
        attributes : dict, optional
            Attribute name to value, set on the reference. Names are matched
            by :meth:`_ref_attribute_names`.

        Returns
        -------
        dict
            Handle, gramps_id, the event and a message, or an ``error`` key
            when the person already references the event.

        Raises
        ------
        UnknownTypeError
            If the role is not in the tree's vocabulary, or an attribute name
            is close to a known one without being it.
        """
        event = await self._resolve("event", event_ref, keys="handle,gramps_id,type")
        role_name = await self._canonical_type("event_role_types", role, allow_new_type)
        names, new_names = await self._ref_attribute_names(
            event["handle"], list(attributes or {}), allow_new_type
        )
        event_type = _type_string(event.get("type"))
        event_label = event.get("gramps_id") or event["handle"]
        state = {"present": False}

        def edit(person: dict) -> str | bool:
            if any(r.get("ref") == event["handle"] for r in person.get("event_ref_list") or []):
                state["present"] = True
                return False
            ref = mapping.event_ref(event["handle"], role=role_name)
            ref["attribute_list"] = [
                {"_class": "Attribute", "type": names[given], "value": value}
                for given, value in (attributes or {}).items()
            ]
            _append_event_ref(person, ref, event_type)
            return f"{event_type} {event_label} added as {role_name}" + (
                f", with {', '.join(names[a] for a in attributes)}" if attributes else ""
            )

        result = await self._mutate("person", person_ref, edit, label="shared an event with")
        if state["present"]:
            return {
                "error": "already_referenced",
                "message": f"Person {result.get('gramps_id')} already references event "
                f"{event_label}; nothing was added. update_event_ref changes that "
                "reference's role or attributes.",
                **({"repaired": result["repaired"]} if result.get("repaired") else {}),
            }
        result.update({"event_handle": event["handle"], "event": event_label, "role": role_name})
        if attributes:
            result["attributes"] = {names[a]: v for a, v in attributes.items()}
        return _with_new_names(result, new_names)

    async def update_event_ref(
        self,
        person_ref: str,
        event_ref: str,
        role: str | None = None,
        attributes: dict[str, str | None] | None = None,
        allow_new_type: bool = False,
    ) -> dict:
        """Change a person's reference to an event in place: its role, its attributes.

        An attribute is set by name: the first of that name on the reference
        takes the value, keeping its citations, notes and privacy, and any
        further one of the same name is removed; a name the reference lacks
        is added. A value of None removes every attribute of that name.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id of the person.
        event_ref : str
            Handle or gramps_id of the event the person references.
        role : str, optional
            The new role, matched against the tree's role types.
        attributes : dict, optional
            Attribute name to its new value, or to None to remove it.

        Returns
        -------
        dict
            Handle, gramps_id and a message, or an ``error`` key.
        """
        if role is None and not attributes:
            return {"error": "nothing_to_do", "message": "Pass role, attributes, or both."}
        event = await self._resolve("event", event_ref, keys="handle,gramps_id")
        event_label = event.get("gramps_id") or event["handle"]
        role_name = (
            await self._canonical_type("event_role_types", role, allow_new_type)
            if role is not None
            else None
        )
        setting = [name for name, value in (attributes or {}).items() if value is not None]
        names, new_names = await self._ref_attribute_names(event["handle"], setting, allow_new_type)
        state = {"refs": 0}

        def edit(person: dict) -> str | bool:
            refs = [
                r for r in person.get("event_ref_list") or [] if r.get("ref") == event["handle"]
            ]
            state["refs"] = len(refs)
            if len(refs) != 1:
                return False
            ref, changed = refs[0], []
            if role_name is not None and _type_string(ref.get("role")) != role_name:
                changed.append(f"role {_type_string(ref.get('role')) or 'unset'} -> {role_name}")
                ref["role"] = role_name
            for given, value in (attributes or {}).items():
                name = names.get(given) or given
                attrs = ref.setdefault("attribute_list", [])
                same = [
                    a for a in attrs if _type_key(_type_string(a.get("type"))) == _type_key(name)
                ]
                if value is None:
                    if same:
                        ref["attribute_list"] = [a for a in attrs if not any(a is b for b in same)]
                        changed.append(f"{_type_string(same[0].get('type'))} removed")
                    continue
                if not same:
                    attrs.append({"_class": "Attribute", "type": name, "value": value})
                    changed.append(f"{name} added")
                    continue
                if same[0].get("value") != value:
                    same[0]["value"] = value
                    changed.append(f"{_type_string(same[0].get('type'))} set")
                if len(same) > 1:
                    extra = same[1:]
                    ref["attribute_list"] = [a for a in attrs if not any(a is b for b in extra)]
                    changed.append(f"{len(extra)} more {name} removed")
            return ", ".join(changed) if changed else False

        result = await self._mutate(
            "person", person_ref, edit, label="updated the event reference on"
        )
        if state["refs"] != 1:
            who = f"Person {result.get('gramps_id')}"
            return {
                "error": "not_referenced" if not state["refs"] else "ambiguous_reference",
                "message": (
                    f"{who} does not reference event {event_label}; add_event_ref adds a reference."
                    if not state["refs"]
                    else f"{who} references event {event_label} {state['refs']} times, so "
                    "which to change is unclear. detach_object(child_kind='event') removes "
                    "them all; add_event_ref then adds one back."
                )
                + " Nothing was changed.",
                **({"repaired": result["repaired"]} if result.get("repaired") else {}),
            }
        result.update({"event_handle": event["handle"], "event": event_label})
        return _with_new_names(result, new_names)

    async def _ref_attribute_names(
        self, event_handle: str, given: list[str], allow_new_type: bool
    ) -> tuple[dict[str, str], list[str]]:
        """Spell attribute names for a reference to this event.

        Matched like any type name (``docs/PITFALLS.md`` section 26): Gramps'
        standard attribute names, the tree's custom ones, and -- since Gramps
        keeps no list of the names used on event references (section 29) --
        the names on every other reference to the same event. A name matching
        none of them is accepted as new and reported; one close to a known
        name is refused as a likely typo unless ``allow_new_type``.

        Returns
        -------
        tuple
            The spelling to write for each given name, and the names new to
            all of those lists.
        """
        if not given:
            return {}, []
        types = await self.client.types()
        holder = await self.client.get_object(
            "event", event_handle, keys="handle,backlinks", backlinks=True
        )
        people = ((holder.get("backlinks") or {}).get("person") or [])[:_REF_NAME_SOURCES]
        used: list[str] = []
        if people:
            for row in await self.client.list_objects(
                "person", handles=list(people), keys="handle,event_ref_list"
            ):
                for ref in row.get("event_ref_list") or []:
                    if ref.get("ref") == event_handle:
                        used += [
                            _type_string(a.get("type")) for a in ref.get("attribute_list") or []
                        ]
        types = {**types, "custom": {**(types.get("custom") or {}), "_event_refs": used}}
        lists = (
            "_event_refs",
            *(f"{k}_attribute_types" for k in ("event", "person", "family", "media")),
        )
        known = {
            _type_key(n): n
            for n in [
                *((types.get("default") or {}).get("attribute_types") or []),
                *(n for key in lists for n in (types["custom"].get(key) or [])),
            ]
            if n
        }
        names: dict[str, str] = {}
        new: list[str] = []
        for name in given:
            spelt = await self._canonical_type(
                "attribute_types", name, True, types=types, custom_keys=lists
            )
            if _type_key(spelt) not in known:
                close = difflib.get_close_matches(_type_key(spelt), list(known), n=2, cutoff=0.8)
                if close and not allow_new_type:
                    raise UnknownTypeError(
                        f"{spelt!r} is not an attribute name on this event's references or in "
                        f"this tree. Did you mean {' or '.join(repr(known[c]) for c in close)}? "
                        "Pass allow_new_type=True to use it as written."
                    )
                new.append(spelt)
            names[name] = spelt
        return names, new

    async def delete_object(self, object_type: str, ref: str, carry_to: str | None = None) -> dict:
        """Delete an object by handle-or-gramps_id, without stranding evidence.

        gramps-webapi 3.21.1 deletes "the object and its references": every
        object pointing at it loses that reference, in the same transaction
        (``api/resources/delete.py``). What it holds is another matter. A
        note or image reachable only through it -- a citation's transcription,
        the page image -- is left attached to nothing and is lost in
        practice, so that is refused unless ``carry_to`` names another object
        of the same type to move them to first.

        A source is refused while citations point at it: the server deletes
        those citations with it, removing them from every fact they support.

        Parameters
        ----------
        object_type : str
            Gramps object type.
        ref : str
            Handle or gramps_id.
        carry_to : str, optional
            Handle or gramps_id of an object of the same type to receive the
            notes and media only this one holds.

        Returns
        -------
        dict
            Handle, a message, what was carried, or an ``error`` key.
        """
        obj = await self._resolve(object_type, ref)
        handle = obj["handle"]
        label = obj.get("gramps_id") or handle
        if object_type == "source":
            cited = await self.get_backlinks("source", handle)
            count = (cited["referenced_by"].get("citation") or {}).get("count", 0)
            if count:
                return {
                    "error": "source_has_citations",
                    "message": f"{count} citation(s) point at source {label}, and the server "
                    "deletes them with it, removing them from every fact they support. "
                    "Re-point them (update_citation source=...) or merge the source "
                    "(merge_objects) instead.",
                }
        outcome = await self._delete_keeping_evidence(object_type, obj, carry_to)
        if not outcome.pop("deleted"):
            return {"error": "would_orphan", "handle": handle, **outcome}
        logger.info("deleted %s %s", object_type, handle)
        suffix = outcome.pop("suffix", "")
        return {
            "handle": handle,
            "gramps_id": obj.get("gramps_id"),
            "object_type": object_type,
            **outcome,
            "message": f"Deleted {object_type} {label}{suffix}",
        }

    async def _delete_keeping_evidence(
        self, object_type: str, obj: dict, carry_to: str | None
    ) -> dict:
        """Delete an object unless that would strand a note or image.

        Returns
        -------
        dict
            ``deleted``; when not deleted, ``would_orphan`` and a ``message``;
            when deleted, ``carried`` if anything moved, the server's late
            error if it answered one, and ``suffix`` for the caller's message.
        """
        orphans = await self._would_orphan(object_type, obj)
        label = obj.get("gramps_id") or obj["handle"]
        if orphans and not carry_to:
            listing = ", ".join(
                f"{typ} {e['gramps_id'] or e['handle']}" for typ, es in orphans.items() for e in es
            )
            return {
                "deleted": False,
                "would_orphan": {
                    typ: [e["gramps_id"] or e["handle"] for e in es] for typ, es in orphans.items()
                },
                "message": f"Not deleted: {object_type} {label} is the only thing holding "
                f"{listing}, which would be left attached to nothing. Pass carry_to=<another "
                f"{object_type}> to move them there first, or attach them elsewhere yourself.",
            }
        out: dict[str, Any] = {"deleted": True, "suffix": ""}
        if orphans:
            out["carried"] = await self._carry(object_type, obj, carry_to, orphans)
            out["suffix"] += f"; moved {_carried_listing(out['carried'])} to it first"
        status = await self._delete(object_type, obj["handle"])
        if status:
            out["server_error_after_delete"] = status
            out["suffix"] += (
                f"; the server answered HTTP {status} after the delete had landed (re-read "
                "confirms it is gone)"
            )
        if object_type == "media":
            out["file_kept"] = True
            out["suffix"] += "; its file stays in the media directory (the server never removes it)"
        return out

    async def _would_orphan(self, object_type: str, obj: dict) -> dict[str, list[dict]]:
        """Notes and media that nothing but ``obj`` references.

        Collected wherever ``obj`` holds them, including inside its own
        references (a note on an event reference dies with the person).
        """
        held = {
            "note": _collect_held(obj, "note_list", by_ref=False),
            "media": _collect_held(obj, "media_list", by_ref=True),
        }
        out: dict[str, list[dict]] = {}
        for typ, handles in held.items():
            for handle in dict.fromkeys(handles):
                try:
                    target = await self.client.get_object(
                        typ, handle, keys="handle,gramps_id,backlinks", backlinks=True
                    )
                except GrampsApiError as exc:
                    if exc.status == 404:
                        continue
                    raise
                links = target.get("backlinks") or {}
                others = (
                    [h for hs in links.values() if isinstance(hs, list) for h in hs]
                    if isinstance(links, dict)
                    else []
                )
                if all(h == obj["handle"] for h in others):
                    out.setdefault(typ, []).append(
                        {"handle": handle, "gramps_id": target.get("gramps_id")}
                    )
        return out

    async def _carry(
        self, object_type: str, obj: dict, carry_to: str | None, orphans: dict[str, list[dict]]
    ) -> dict:
        """Move the notes and media only ``obj`` holds onto another object."""
        target = await self._resolve(object_type, carry_to or "", keys="handle,gramps_id")
        if target["handle"] == obj["handle"]:
            raise InvalidCarryTargetError("carry_to names the object being deleted.")
        if orphans.get("media") and object_type not in _MEDIA_HOLDERS:
            raise InvalidCarryTargetError(f"A {object_type} cannot hold media for carry_to.")
        if orphans.get("note") and object_type not in _NOTE_HOLDERS:
            raise InvalidCarryTargetError(f"A {object_type} cannot hold notes for carry_to.")
        notes = [e["handle"] for e in orphans.get("note", [])]
        moving = {e["handle"] for e in orphans.get("media", [])}
        media_refs = [dict(m) for m in obj.get("media_list") or [] if m.get("ref") in moving]

        def edit(other: dict) -> str | bool:
            note_list = other.setdefault("note_list", []) if notes else []
            for handle in notes:
                if handle not in note_list:
                    note_list.append(handle)
            media_list = other.setdefault("media_list", []) if media_refs else []
            for ref in media_refs:
                if not any(m.get("ref") == ref["ref"] for m in media_list):
                    media_list.append(ref)
            return "received notes and media" if notes or media_refs else False

        await self._mutate(object_type, target["handle"], edit, label="updated")
        return {
            "to": target.get("gramps_id") or target["handle"],
            **{
                typ: [e["gramps_id"] or e["handle"] for e in entries]
                for typ, entries in orphans.items()
            },
        }

    async def _delete(self, object_type: str, handle: str) -> int | None:
        """DELETE, then believe the tree rather than a late error.

        gramps-webapi 3.21.1 commits the delete before it updates its search
        indices, and an error there answers HTTP 500 for a delete that has
        landed. A caller trusting that would retry or think the tree
        unchanged. So on a 5xx the object is looked up again: if it is gone
        the delete is reported as done, with the status it came back with.

        Returns
        -------
        int or None
            The status of an error answered after a delete that landed.
        """
        try:
            await self.client.delete_object(object_type, handle)
            return None
        except GrampsApiError as exc:
            if exc.status < 500:
                raise
            try:
                await self.client.get_object(object_type, handle, keys="handle")
            except GrampsApiError as again:
                if again.status == 404:
                    logger.warning(
                        "%s %s deleted although the server answered %s",
                        object_type,
                        handle,
                        exc.status,
                    )
                    return exc.status
            raise exc

    async def add_family(
        self,
        father_ref: str | None,
        mother_ref: str | None,
        child_refs: list[str | ChildLink] | None,
        marriage: EventInput | None = None,
        relationship: str = "Married",
        require_citation: bool = True,
        allow_new_type: bool = False,
    ) -> dict:
        """Create a family linking parents and children.

        Parameters
        ----------
        father_ref, mother_ref : str or None
            Handle or gramps_id of an existing person.
        child_refs : list of str or ChildLink, or None
            Existing children: a handle or gramps_id is a birth child of both
            parents; a :class:`ChildLink` says how each is related.
        marriage : EventInput, optional
            Marriage event to create and attach; type defaults to
            ``"Marriage"``.
        relationship : str, optional
            Gramps family relationship type.
        require_citation : bool, optional
            Fail rather than record an uncited marriage event.

        Returns
        -------
        dict
            Handle, gramps_id and a message.

        Raises
        ------
        CitationRequiredError
            If the marriage lacks a citation and ``require_citation`` is True.
        NotFoundError
            If any referenced person cannot be resolved.
        """
        father_handle = await self._resolve_handle("person", father_ref) if father_ref else None
        mother_handle = await self._resolve_handle("person", mother_ref) if mother_ref else None
        vocabulary = await self.client.types()
        child_refs_out: list[dict] = []
        for entry in child_refs or []:
            link = entry if isinstance(entry, ChildLink) else ChildLink(person=entry)
            handle = await self._resolve_handle("person", link.person)
            if any(c["ref"] == handle for c in child_refs_out):
                continue
            rels = {
                key: await self._canonical_type(
                    "child_reference_types", value, allow_new_type, types=vocabulary
                )
                for key, value in (("frel", link.frel), ("mrel", link.mrel))
            }
            child_refs_out.append({"_class": "ChildRef", "ref": handle, **rels})

        event_refs: list[dict] = []
        unsourced = False
        relationship = await self._canonical_type(
            "family_relation_types", relationship, allow_new_type, types=vocabulary
        )
        if marriage is not None:
            marriage.type = marriage.type or "Marriage"
            await self._resolve_event_type(marriage)
            handle, unsourced = await self._create_event(marriage, require_citation)
            event_refs.append(mapping.event_ref(handle, role="Family"))

        payload: dict[str, Any] = {
            "_class": "Family",
            "type": relationship,
            "father_handle": father_handle,
            "mother_handle": mother_handle,
            "child_ref_list": child_refs_out,
            "event_ref_list": event_refs,
        }
        created = await self.client.create_object("family", payload)
        logger.info("created family %s", created.get("handle"))
        return {
            "handle": created["handle"],
            "gramps_id": created.get("gramps_id"),
            "object_type": "family",
            "unsourced_marriage": unsourced,
            "message": _created_message("family", created, ["marriage"] if unsourced else []),
        }

    # ------------------------------------------------------------------ #
    # write: standalone evidence objects
    # ------------------------------------------------------------------ #
    async def add_source(
        self,
        title: str,
        author: str | None,
        pubinfo: str | None,
        abbrev: str | None,
        repository_ref: str | None,
        call_number: str | None,
        media_type: str = "Unknown",
        allow_new_type: bool = False,
    ) -> dict:
        """Create a source, optionally held in a repository.

        Parameters
        ----------
        title : str
            Source title.
        author, pubinfo, abbrev : str or None
            Bibliographic fields.
        repository_ref : str or None
            Handle or gramps_id of a repository holding this source.
        call_number : str or None
            Shelf mark within that repository.
        media_type : str, optional
            The source's medium in that repository, e.g. ``"Book"``; the same
            field link_repository sets.

        Returns
        -------
        dict
            Handle, gramps_id and a message.
        """
        payload = mapping.source_payload(title, author, pubinfo)
        if abbrev:
            payload["abbrev"] = abbrev
        if repository_ref:
            media_type = await self._canonical_type(
                "source_media_types", media_type, allow_new_type
            )
            repo_handle = await self._resolve_handle("repository", repository_ref)
            payload["reporef_list"] = [_reporef(repo_handle, call_number, media_type)]
        created = await self.client.create_object("source", payload)
        return _write_result("source", created)

    async def add_citation(self, cit: CitationInput) -> dict:
        """Resolve or create a standalone citation.

        Parameters
        ----------
        cit : CitationInput
            An existing citation, an existing source to cite, or enough to
            create both.

        Returns
        -------
        dict
            Handle, gramps_id and a message.
        """
        handle = await self.resolve_citation(cit)
        obj = await self.client.get_object("citation", handle, keys="handle,gramps_id")
        return {
            "handle": handle,
            "gramps_id": obj.get("gramps_id"),
            "object_type": "citation",
            "message": f"Citation ready (handle {handle}).",
        }

    async def add_repository(
        self, name: str, repo_type: str, url: str | None, allow_new_type: bool = False
    ) -> dict:
        """Create a repository such as an archive, library or website.

        Parameters
        ----------
        name : str
            Repository name.
        repo_type : str
            Gramps repository type.
        url : str or None
            Home page, recorded as a Web Home URL.

        Returns
        -------
        dict
            Handle, gramps_id and a message.
        """
        types = await self.client.types()
        repo_type = await self._canonical_type(
            "repository_types", repo_type, allow_new_type, types=types
        )
        payload: dict[str, Any] = {"_class": "Repository", "name": name, "type": repo_type}
        if url:
            payload["urls"] = [{"_class": "Url", "path": url, "type": "Web Home"}]
        created = await self.client.create_object("repository", payload)
        return _write_result("repository", created)

    async def add_note(
        self,
        target_ref: str | None,
        target_type: str | None,
        text: str,
        note_type: str,
        allow_new_type: bool = False,
    ) -> dict:
        """Create a note, optionally attaching it to an object.

        The attach is re-read afterwards, because this write has been seen to
        report success without landing.

        A 5xx is never taken at its word (TOOL-REQUESTS #28). The client
        makes the note's handle, so a failed create is looked up: absent, the
        error says nothing was written. A failed attach that left the target
        as it was removes the new note again, so the call writes all or
        nothing. Neither the server nor the tool limits a note's length.

        Parameters
        ----------
        target_ref : str or None
            Handle or gramps_id to attach to. None creates a standalone note.
        target_type : str or None
            Object type of the target.
        text : str
            Note body.
        note_type : str
            Gramps note type.

        Returns
        -------
        dict
            Handle, gramps_id, what it attached to, and ``verified`` -- False
            when the attachment could not be demonstrated by re-reading.

        Raises
        ------
        FailedWriteError
            When the server answered a 5xx, saying what was written.
        """
        # Resolved first, so a bad target fails before an orphan note exists.
        target_handle = (
            await self._resolve_handle(target_type, target_ref)
            if target_ref and target_type
            else None
        )
        note_type = await self._canonical_type("note_types", note_type, allow_new_type)
        note = await self.client.create_object(
            "note", {"_class": "Note", "text": {"string": text}, "type": note_type}
        )
        note_handle = note["handle"]
        attached_to = None
        verified: bool | None = None
        if target_handle:

            def edit(obj: dict) -> str:
                obj.setdefault("note_list", []).append(note_handle)
                return "note attached"

            try:
                result = await self._mutate(target_type, target_handle, edit, label="noted")
            except FailedWriteError as exc:
                raise await self._unattached_note(note, target_type, exc) from exc
            attached_to = result.get("gramps_id")
            verified = await self._verify_in_list(
                target_type, target_handle, "note_list", note_handle
            )
        message = f"Created {note_type} note"
        if note.get("late_error"):
            message += (
                f" (the server answered HTTP {note['late_error']} after creating it; "
                "a re-read found it)"
            )
        if attached_to:
            message += (
                f", attached to {target_type} {attached_to}"
                if verified
                else f" but it did NOT attach to {target_type} {attached_to} -- "
                f"the note exists ({note.get('gramps_id')}) and is currently orphaned"
            )
        return {
            "handle": note_handle,
            "gramps_id": note.get("gramps_id"),
            "object_type": "note",
            "attached_to": attached_to if verified is not False else None,
            "verified": verified,
            "message": message,
        }

    async def _unattached_note(
        self, note: dict, target_type: str, exc: FailedWriteError
    ) -> FailedWriteError:
        """Account for a note created whose attach answered 5xx.

        When the target was left as it was, the note is removed again, so the
        call wrote nothing; when the target changed, the attach may have
        landed and the note is kept.
        """
        label = note.get("gramps_id") or note["handle"]
        if exc.written is False:
            try:
                await self.client.delete_object("note", note["handle"])
                what = f"the {target_type} was left as it was, and the new note was removed again"
            except GrampsApiError:
                logger.warning("could not remove unattached note %s", note["handle"])
                return FailedWriteError(
                    exc,
                    None,
                    f"note {label} was created but not attached, and could not be removed "
                    "again: attach it by retrying, or delete it with delete_object",
                )
            return FailedWriteError(exc, False, what)
        return FailedWriteError(
            exc,
            None,
            f"note {label} was created; whether it was attached is unknown, because the "
            f"{target_type} changed meanwhile",
        )

    async def attach_media(
        self,
        target_ref: str,
        target_type: str,
        file_path: str | None = None,
        description: str = "",
        media_ref: str | None = None,
    ) -> dict:
        """Attach an image or document to an object.

        Prefer ``media_ref`` when the same document supports several people:
        one image should be one Media object cited from each fact it proves,
        not uploaded once per person. Uploading a file whose md5 already exists
        reuses that Media object rather than storing the bytes twice.

        Parameters
        ----------
        target_ref : str
            Handle or gramps_id of the object to attach to.
        target_type : str
            That object's type.
        file_path : str, optional
            Path of a file to upload. Mutually exclusive with ``media_ref``.
        description : str, optional
            Description for a newly created Media object.
        media_ref : str, optional
            Handle or gramps_id of an existing Media object.

        Returns
        -------
        dict
            Handle, gramps_id and a message, or an ``error`` key when neither
            ``file_path`` nor ``media_ref`` was given.
        """
        if not (file_path or media_ref):
            return {
                "error": "no_media",
                "message": "Pass file_path to upload a file, or media_ref to "
                "attach a Media object already in the tree.",
            }

        notes: list[str] = []
        if media_ref:
            media = await self._resolve("media", media_ref, keys="handle,gramps_id,desc")
            media_handle = media["handle"]
            created = False
        else:
            uploaded = await self.add_media(file_path, description)  # type: ignore[arg-type]
            if "error" in uploaded:
                return uploaded
            media_handle = uploaded["handle"]
            media = {"handle": media_handle, "gramps_id": uploaded.get("gramps_id")}
            created = uploaded.get("created", True)
            notes = uploaded.get("notes", [])

        def edit(obj: dict) -> str | bool:
            media_list = obj.setdefault("media_list", [])
            if any(m.get("ref") == media_handle for m in media_list):
                return False
            media_list.append(
                {
                    "_class": "MediaRef",
                    "ref": media_handle,
                    "attribute_list": [],
                    "citation_list": [],
                    "note_list": [],
                }
            )
            return "media attached"

        try:
            result = await self._mutate(target_type, target_ref, edit, label="attached to")
        except (GrampsApiError, httpx.TransportError) as exc:
            if not created:
                raise
            return _unattached_upload(media, target_type, target_ref, exc)
        attached = result.get("gramps_id")
        verified = await self._verify_in_list(
            target_type, result["handle"], "media_list", media_handle
        )
        return {
            "handle": media_handle,
            "gramps_id": media.get("gramps_id"),
            "object_type": "media",
            "attached_to": attached,
            "media_created": created,
            "verified": verified,
            "message": (
                f"Attached media {media.get('gramps_id')} to {target_type} {attached}"
                if verified
                else f"WARNING: media {media.get('gramps_id')} does NOT appear on "
                f"{target_type} {attached} after the write. Nothing was attached."
            )
            + "".join(f"; {n}" for n in notes),
        }

    # ------------------------------------------------------------------ #
    # read
    # ------------------------------------------------------------------ #
    def _current_year(self) -> int:
        """Current UTC year, used as the reference point for privacy checks."""
        return datetime.now(UTC).year

    def _assess_person(self, profile: dict, private_flag: bool):
        """Assess a person's privacy status from their profile.

        Parameters
        ----------
        profile : dict
            The person's profile, carrying birth and death.
        private_flag : bool
            The Gramps private flag on the record.

        Returns
        -------
        LivingAssessment
            The verdict.
        """
        birth_year = _year_from_profile(profile.get("birth"))
        death_year = _year_from_profile(profile.get("death"))
        return assess(
            private_flag=private_flag,
            birth_year=birth_year,
            death_year=death_year,
            current_year=self._current_year(),
            # The profile carries an empty block when there is no death event.
            died=bool(profile.get("death")),
        )

    async def get_person(self, person_ref: str) -> dict:
        """Fetch full detail for one person.

        Direct lookups are allowed even for private or living records; only
        bulk output is filtered. See :mod:`gramps_evidence_mcp.privacy`.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            Name, gender, events with citation counts, families and media.
        """
        obj = await self._resolve("person", person_ref, extend="all", profile="all")
        return _format_person(obj, self.exposing_private)

    async def search_people(
        self, name: str, birth_year_min: int | None, birth_year_max: int | None
    ) -> list[dict]:
        """Search people by name substring and birth-year range.

        Privacy-filtered: restricted people appear as redacted stubs unless
        ``expose_private`` is set.

        Parameters
        ----------
        name : str
            Case-insensitive substring of the display name.
        birth_year_min, birth_year_max : int or None
            Inclusive bounds on birth year.

        Returns
        -------
        list of dict
            Matching people, or redacted stubs.
        """
        people = await self.client.list_objects("person", profile="self")
        needle = name.strip().lower()
        results: list[dict] = []
        for obj in people:
            profile = obj.get("profile", {})
            display = profile.get("name") or _name_from_person(obj)
            if needle and needle not in display.lower():
                continue
            birth_year = _year_from_profile(profile.get("birth"))
            if birth_year_min is not None and (birth_year is None or birth_year < birth_year_min):
                continue
            if birth_year_max is not None and (birth_year is None or birth_year > birth_year_max):
                continue
            verdict = self._assess_person(profile, obj.get("private", False))
            if verdict.restricted and not self.exposing_private:
                results.append(redacted_stub(obj.get("gramps_id"), obj.get("handle")))
                continue
            results.append(
                {
                    "handle": obj["handle"],
                    "gramps_id": obj.get("gramps_id"),
                    "name": display,
                    "gender": mapping.gender_label(obj.get("gender")),
                    "birth_year": birth_year,
                    "death_year": _year_from_profile(profile.get("death")),
                }
            )
        return results

    async def get_family(self, family_ref: str) -> dict:
        """Fetch one family: relationship, parents, children and event count.

        Parameters
        ----------
        family_ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            The formatted family.
        """
        obj = await self._resolve("family", family_ref, extend="all", profile="all")
        return _format_family(obj)

    async def get_ancestors(self, person_ref: str, generations: int) -> dict:
        """Walk up the tree from a person.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id of the root person.
        generations : int
            How many generations to climb.

        Returns
        -------
        dict
            Nested ancestors, privacy-filtered.
        """
        root = await self._resolve_handle("person", person_ref)
        return await self._walk(root, generations, direction="up")

    async def get_descendants(self, person_ref: str, generations: int) -> dict:
        """Walk down the tree from a person.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id of the root person.
        generations : int
            How many generations to descend.

        Returns
        -------
        dict
            Nested descendants, privacy-filtered.
        """
        root = await self._resolve_handle("person", person_ref)
        return await self._walk(root, generations, direction="down")

    async def _walk(self, handle: str, generations: int, direction: str) -> dict:
        """Walk the tree in one direction, filtering and cycle-guarding.

        Parameters
        ----------
        handle : str
            Handle of the root person.
        generations : int
            Depth limit.
        direction : {"up", "down"}
            Whether to follow parents or children.

        Returns
        -------
        dict
            The nested tree. Already-visited people are not expanded twice.
        """
        visited: set[str] = set()

        async def node(h: str, depth: int) -> dict | None:
            if h in visited or depth < 0:
                return None
            visited.add(h)
            person = await self.client.get_object("person", h, profile="self")
            profile = person.get("profile", {})
            verdict = self._assess_person(profile, person.get("private", False))
            if verdict.restricted and not self.exposing_private:
                return redacted_stub(person.get("gramps_id"), h)
            entry: dict[str, Any] = {
                "handle": h,
                "gramps_id": person.get("gramps_id"),
                "name": profile.get("name") or _name_from_person(person),
                "birth_year": _year_from_profile(profile.get("birth")),
                "death_year": _year_from_profile(profile.get("death")),
            }
            if depth == 0:
                return entry
            if direction == "up":
                entry["parents"] = await self._parents(person, depth)
            else:
                entry["children"] = await self._children(person, depth)
            return entry

        result = await node(handle, generations)
        return result or {}

    async def _parents(self, person: dict, depth: int) -> list[dict]:
        """Collect a person's parents from the families they are a child in."""
        out: list[dict] = []
        for fam_handle in person.get("parent_family_list", []):
            fam = await self.client.get_object(
                "family", fam_handle, keys="father_handle,mother_handle"
            )
            for parent_handle in (fam.get("father_handle"), fam.get("mother_handle")):
                if parent_handle:
                    child = await self._walk_child(parent_handle, depth - 1, "up")
                    if child:
                        out.append(child)
        return out

    async def _children(self, person: dict, depth: int) -> list[dict]:
        """Collect a person's children from the families they are a spouse in."""
        out: list[dict] = []
        for fam_handle in person.get("family_list", []):
            fam = await self.client.get_object("family", fam_handle, keys="child_ref_list")
            for child_ref in fam.get("child_ref_list", []):
                ref = child_ref.get("ref")
                if ref:
                    child = await self._walk_child(ref, depth - 1, "down")
                    if child:
                        out.append(child)
        return out

    async def _walk_child(self, handle: str, depth: int, direction: str) -> dict | None:
        """Recurse into one relative, returning None when nothing was found."""
        return await self._walk(handle, depth, direction) or None

    async def list_unsourced_facts(self, person_ref: str | None = None, limit: int = 500) -> dict:
        """Audit: events with no citation, or tagged with the UNSOURCED attribute.

        Two collection reads, not one per event. The original walked every
        person's event_ref_list issuing a GET per event -- over a thousand
        round-trips on a tree of a few hundred people, which timed out. The event
        table is fetched once and indexed by handle instead.

        Across the whole tree, a private or probably-living person's findings
        are withheld unless ``expose_private`` is set: the person appears once
        in ``withheld`` as a redacted stub, and their facts are counted in
        ``withheld_fact_count``. Naming the person lists them in full.
        """
        if person_ref:
            people = [
                await self._resolve(
                    "person",
                    person_ref,
                    keys="handle,gramps_id,primary_name,event_ref_list,private",
                )
            ]
        else:
            people = await self.client.list_objects(
                "person",
                keys="handle,gramps_id,primary_name,event_ref_list,private",
            )

        wanted = {
            er.get("ref")
            for person in people
            for er in person.get("event_ref_list") or []
            if er.get("ref")
        }
        events: dict[str, dict] = {}
        if wanted:
            # One request for the lot; ?handles= is capped by URL length, so for a
            # whole-tree audit just take the collection.
            rows = await self.client.list_objects(
                "event",
                handles=None if person_ref is None else list(wanted),
                keys="handle,gramps_id,type,date,citation_list,attribute_list,private",
            )
            if not person_ref:
                rows = self._drop_private(rows)
            events = {e["handle"]: e for e in rows}

        per_person: list[tuple[dict, list[dict]]] = []
        for person in people:
            name = _name_from_person(person)
            found = []
            for ev_ref in person.get("event_ref_list", []):
                event = events.get(ev_ref.get("ref"))
                if not event:
                    continue
                reason = _unsourced_reason(event, self.config.unsourced_attribute)
                if reason:
                    found.append(
                        {
                            "person": name,
                            "person_gramps_id": person.get("gramps_id"),
                            "event_type": _type_string(event.get("type")),
                            "event_gramps_id": event.get("gramps_id"),
                            "date": _date_string(event.get("date")),
                            "reason": reason,
                        }
                    )
            if found:
                per_person.append((person, found))

        restricted: set[str] = set()
        if not person_ref and not self.exposing_private:
            restricted = await self._restricted_people(p.get("handle") for p, _ in per_person)
        findings = [
            f for p, found in per_person if p.get("handle") not in restricted for f in found
        ]
        withheld = [(p, found) for p, found in per_person if p.get("handle") in restricted]
        return {
            "unsourced_count": len(findings),
            "facts": findings[:limit],
            "truncated": len(findings) > limit,
            "withheld_fact_count": sum(len(found) for _, found in withheld),
            "withheld": [
                redacted_stub(p.get("gramps_id"), p.get("handle")) for p, _ in withheld[:limit]
            ],
        }

    async def db_stats(self) -> dict:
        """Count every object type in the tree.

        Returns
        -------
        dict
            Counts keyed by object type.
        """
        counts = {}
        for object_type in (
            "person",
            "family",
            "event",
            "citation",
            "source",
            "repository",
            "place",
            "media",
            "note",
        ):
            counts[object_type] = await self.client.count(object_type)
        return {"counts": counts}

    # ------------------------------------------------------------------ #
    # write: tags, attributes, urls, privacy
    # ------------------------------------------------------------------ #
    async def tag_object(
        self, object_type: str, ref: str, tag_name: str, color: str | None = None
    ) -> dict:
        """Find-or-create a Tag by exact name and attach it to the target object."""
        tags = await self.client.list_objects("tag")
        tag_handle: str | None = None
        for tag in tags:
            if (tag.get("name") or "") == tag_name:
                tag_handle = tag["handle"]
                break
        if tag_handle is None:
            created = await self.client.create_object(
                "tag", {"_class": "Tag", "name": tag_name, "color": color or "#4444FF"}
            )
            tag_handle = created["handle"]
            logger.info("created tag %s", tag_handle)

        def edit(obj: dict) -> str | bool:
            tag_list = obj.setdefault("tag_list", [])
            if tag_handle in tag_list:
                return False
            tag_list.append(tag_handle)
            return f"tagged '{tag_name}'"

        result = await self._mutate(object_type, ref, edit, label="tagged")
        label = result.get("gramps_id") or result["handle"]
        return _with_repairs(
            {
                "object_type": object_type,
                "gramps_id": result.get("gramps_id"),
                "tag": tag_name,
                "tag_handle": tag_handle,
                "message": f"Tagged {object_type} {label} with '{tag_name}'"
                if result["changed"]
                else f"{object_type} {label} already carries '{tag_name}'",
            },
            result,
        )

    async def list_tags(self) -> list[dict]:
        """List every tag with its handle, name and colour.

        Returns
        -------
        list of dict
            One entry per tag.
        """
        tags = await self.client.list_objects("tag", keys="handle,name,color")
        return [
            {"handle": t.get("handle"), "name": t.get("name"), "color": t.get("color")}
            for t in tags
        ]

    async def add_attribute(
        self, object_type: str, ref: str, name: str, value: str, allow_new_type: bool = False
    ) -> dict:
        """Append an Attribute (or SrcAttribute for sources/citations) to an object."""
        source_like = object_type in {"source", "citation"}
        cls = "SrcAttribute" if source_like else "Attribute"
        # A name the tree already uses on another kind of object is no typo.
        kinds = ("person", "family", "event", "media")
        name = await self._canonical_type(
            "source_attribute_types" if source_like else "attribute_types",
            name,
            allow_new_type,
            custom_keys=("source_attribute_types",)
            if source_like
            else tuple(
                f"{kind}_attribute_types"
                for kind in sorted(kinds, key=lambda kind: kind != object_type)
            ),
        )

        def edit(obj: dict) -> str:
            obj.setdefault("attribute_list", []).append(
                {"_class": cls, "type": name, "value": value}
            )
            return f"attribute '{name}' added"

        result = await self._mutate(object_type, ref, edit, label="updated")
        return _with_repairs(
            {
                "object_type": object_type,
                "gramps_id": result.get("gramps_id"),
                "message": f"Added attribute '{name}' to {object_type} "
                f"{result.get('gramps_id') or result['handle']}",
            },
            result,
        )

    async def update_attribute(
        self,
        object_type: str,
        ref: str,
        name: str,
        value: str | None = None,
        match: str | None = None,
        remove: bool = False,
    ) -> dict:
        """Set or remove one attribute of an object in place (TOOL-REQUESTS #32).

        ``add_attribute`` only appends, and ``update_object_fields`` refuses
        ``attribute_list``, a structural list, so a value could be neither
        corrected nor taken out. The attribute is chosen by its name, and,
        where the name repeats, by ``match`` against its value. Several left
        are refused with the candidates, unless they are identical in every
        field, when any one is the same as another and the last is taken. A
        value set keeps the attribute's citations, notes and privacy; an
        attribute removed has its citations named, since nothing else may
        reference them.

        Parameters
        ----------
        object_type : {"person", "family", "event", "media", "source", "citation"}
            Type of the object carrying the attribute.
        ref : str
            Handle or gramps_id.
        name : str
            The attribute's name (type), matched ignoring case and punctuation.
        value : str, optional
            The new value. Not with ``remove``.
        match : str, optional
            Case-insensitive substring of the current value.
        remove : bool, optional
            Remove the attribute instead of setting its value.

        Returns
        -------
        dict
            Handle, gramps_id, the attribute as it now is (or was, removed),
            and a message; or an ``error`` key naming the candidates.
        """
        if object_type not in _ATTRIBUTE_HOLDERS:
            return {
                "error": "unsupported",
                "message": f"Attributes are carried by {', '.join(sorted(_ATTRIBUTE_HOLDERS))}, "
                f"not {object_type}. An event reference's are update_event_ref's.",
            }
        if remove == (value is not None):
            return {
                "error": "nothing_to_do",
                "message": "Pass value to set the attribute's value, or remove=True to "
                "remove it; not both.",
            }
        needle = (match or "").strip().lower()
        found: dict[str, Any] = {}

        def edit(obj: dict) -> str | bool:
            attrs = obj.get("attribute_list") or []
            named = [a for a in attrs if _type_key(_type_string(a.get("type"))) == _type_key(name)]
            hits = [a for a in named if needle in str(a.get("value") or "").lower()]
            found["listing"] = (
                "; ".join(
                    f"{_type_string(a.get('type'))}: {str(a.get('value') or '')[:60]}"
                    for a in (named or attrs)
                )
                or f"(no attributes on this {object_type})"
            )
            if not hits:
                found["error"] = "not_found"
                return False
            if any(a != hits[0] for a in hits[1:]):
                found["error"] = "ambiguous"
                found["count"] = len(hits)
                return False
            attr = hits[-1]
            found["before"] = copy.deepcopy(attr)
            label = _type_string(attr.get("type"))
            if remove:
                obj["attribute_list"] = [a for a in attrs if a is not attr]
                return f"attribute '{label}' removed"
            if attr.get("value") == value:
                return False
            attr["value"] = value
            return f"attribute '{label}' set"

        result = await self._mutate(object_type, ref, edit, label="updated")
        who = f"{object_type} {result.get('gramps_id') or result['handle']}"
        if "error" in found:
            wanted = f"'{name}'" + (f" with '{match}' in its value" if match else "")
            return _with_repairs(
                {
                    "error": found["error"],
                    "message": (
                        f"No attribute {wanted} on {who}."
                        if found["error"] == "not_found"
                        else f"{found['count']} attributes {wanted} on {who}: pass match, a "
                        "part of the value that only the one you mean has."
                    )
                    + f" Attributes: {found['listing']}. Nothing was changed.",
                },
                result,
            )
        before = found["before"]
        out = {
            "object_type": object_type,
            "handle": result["handle"],
            "gramps_id": result.get("gramps_id"),
            "changed": result.get("changed", True),
            "attribute": {
                "type": _type_string(before.get("type")),
                "value": before.get("value") if remove else value,
                "citation_count": len(before.get("citation_list") or []),
            },
            "message": result["message"],
        }
        if remove and before.get("citation_list"):
            rows = await self.client.list_objects(
                "citation", handles=before["citation_list"], keys="handle,gramps_id"
            )
            left = [r.get("gramps_id") or r["handle"] for r in rows]
            out["citations_left"] = left
            out["message"] += (
                f". Its citations stay in the tree: {', '.join(left)}; uncite or delete "
                "any that now supports nothing"
            )
        return out

    async def add_url(
        self,
        object_type: str,
        ref: str,
        url: str,
        description: str = "",
        url_type: str = "Web Home",
        allow_new_type: bool = False,
    ) -> dict:
        """Append a Url. Only person/place/repository carry a urls list."""
        if object_type not in {"person", "place", "repository"}:
            return {
                "error": "unsupported",
                "message": f"URLs are only supported on person, place, or "
                f"repository, not {object_type}. Use an attribute instead.",
            }

        url_type = await self._canonical_type("url_types", url_type, allow_new_type)

        def edit(obj: dict) -> str:
            obj.setdefault("urls", []).append(
                {"_class": "Url", "path": url, "desc": description, "type": url_type}
            )
            return "URL added"

        result = await self._mutate(object_type, ref, edit, label="updated")
        return _with_repairs(
            {
                "object_type": object_type,
                "gramps_id": result.get("gramps_id"),
                "message": f"Added URL to {object_type} "
                f"{result.get('gramps_id') or result['handle']}",
            },
            result,
        )

    async def update_url(
        self,
        object_type: str,
        ref: str,
        match: str,
        url: str | None = None,
        description: str | None = None,
        url_type: str | None = None,
        remove: bool = False,
        allow_new_type: bool = False,
    ) -> dict:
        """Edit or remove one existing URL entry.

        ``add_url`` can only append, so this is the way to correct a link filed
        under the wrong type.

        Parameters
        ----------
        object_type : {"person", "place", "repository"}
            Type of the object carrying the URL.
        ref : str
            Handle or gramps_id.
        match : str
            Case-insensitive substring tested against each entry's path and
            description. Must select exactly one entry.
        url, description, url_type : str, optional
            New values for the matched entry.
        remove : bool, optional
            Delete the matched entry instead of editing it.

        Returns
        -------
        dict
            Handle, gramps_id and a message, or an ``error`` key. Matching no
            entry or several reports the candidates rather than guessing.
        """
        if object_type not in {"person", "place", "repository"}:
            return {
                "error": "unsupported",
                "message": f"URLs are only supported on person, place, or "
                f"repository, not {object_type}.",
            }
        if not remove and url is None and description is None and url_type is None:
            return {
                "error": "nothing_to_do",
                "message": "Provide url, description, and/or url_type to change — or remove=True.",
            }

        needle = match.strip().lower()

        if url_type is not None:
            url_type = await self._canonical_type("url_types", url_type, allow_new_type)

        def edit(obj: dict) -> str | bool:
            urls = obj.get("urls") or []
            hits = [
                (i, u)
                for i, u in enumerate(urls)
                if needle in (u.get("path") or "").lower()
                or needle in (u.get("desc") or "").lower()
            ]
            listing = (
                "; ".join(
                    f"[{i}] {u.get('type') or '?'}: {u.get('path') or ''}"
                    for i, u in enumerate(urls)
                )
                or "(no URLs on this object)"
            )
            if not hits:
                raise NotFoundError(f"No URL entry matches '{match}'. Entries: {listing}")
            if len(hits) > 1:
                raise NotFoundError(
                    f"'{match}' matches {len(hits)} URL entries — be more "
                    f"specific. Entries: {listing}"
                )
            idx, entry = hits[0]
            if remove:
                removed = urls.pop(idx)
                obj["urls"] = urls
                return f"removed {removed.get('type') or '?'}: {removed.get('path')}"
            changed: list[str] = []
            for key_, val in (("path", url), ("desc", description), ("type", url_type)):
                if val is not None and entry.get(key_) != val:
                    entry[key_] = val
                    changed.append(key_)
            return ", ".join(changed) if changed else False

        return await self._mutate(object_type, ref, edit, label="updated")

    async def set_private(self, object_type: str, ref: str, private: bool = True) -> dict:
        """Set or clear the Gramps private flag on an object.

        Parameters
        ----------
        object_type : str
            Gramps object type.
        ref : str
            Handle or gramps_id.
        private : bool, optional
            The flag's new value.

        Returns
        -------
        dict
            Handle, gramps_id and a message.
        """
        flag = bool(private)

        def edit(obj: dict) -> str | bool:
            if bool(obj.get("private")) == flag:
                return False
            obj["private"] = flag
            return f"private={flag}"

        result = await self._mutate(object_type, ref, edit, label="updated")
        return _with_repairs(
            {
                "handle": result["handle"],
                "gramps_id": result.get("gramps_id"),
                "object_type": object_type,
                "private": flag,
                "changed": result["changed"],
                "message": f"Set {object_type} "
                f"{result.get('gramps_id') or result['handle']} private={flag}",
            },
            result,
        )

    # ------------------------------------------------------------------ #
    # write: sources, repositories, families
    # ------------------------------------------------------------------ #
    async def update_source(
        self,
        ref: str,
        title: str | None = None,
        author: str | None = None,
        pubinfo: str | None = None,
        abbrev: str | None = None,
    ) -> dict:
        """Edit an existing source's title/author/pubinfo/abbrev (only provided)."""

        def edit(source: dict) -> str | bool:
            changed = []
            for key, value in (
                ("title", title),
                ("author", author),
                ("pubinfo", pubinfo),
                ("abbrev", abbrev),
            ):
                if value is not None and (source.get(key) or "") != value:
                    source[key] = value
                    changed.append(key)
            return ", ".join(changed) if changed else False

        return await self._mutate("source", ref, edit, label="updated")

    async def link_repository(
        self,
        source_ref: str,
        repository_ref: str,
        call_number: str | None = None,
        media_type: str = "Unknown",
        allow_new_type: bool = False,
    ) -> dict:
        """Attach a RepoRef linking a repository to a source (dedup by repo handle)."""
        media_type = await self._canonical_type("source_media_types", media_type, allow_new_type)
        repo_handle = await self._resolve_handle("repository", repository_ref)

        def edit(source: dict) -> str | bool:
            reporef_list = source.setdefault("reporef_list", [])
            if any(r.get("ref") == repo_handle for r in reporef_list):
                return False
            reporef_list.append(_reporef(repo_handle, call_number, media_type))
            return "repository linked"

        result = await self._mutate("source", source_ref, edit, label="updated")
        label = result.get("gramps_id") or result["handle"]
        return _with_repairs(
            {
                "handle": result["handle"],
                "gramps_id": result.get("gramps_id"),
                "object_type": "source",
                "changed": result["changed"],
                "message": f"Linked repository to source {label}"
                if result["changed"]
                else f"Source {label} is already linked to that repository; nothing changed. "
                "To change the link, detach_object(child_kind='repository') and link again.",
            },
            result,
        )

    async def link_repositories(
        self, items: list[RepositoryLink], allow_new_type: bool = False
    ) -> dict:
        """Link many sources to repositories in one call.

        Each row is its own whole-object write, as with link_repository, so
        each is its own transaction: gramps-webapi gives every PUT one. A row
        that fails does not stop the rest, and each row says what happened.

        Returns
        -------
        dict
            Counts by outcome and one result per row: ``linked``,
            ``already_linked``, ``missing`` or ``error``.
        """
        rows = []
        for item in items:
            row: dict[str, Any] = {"source": item.source, "repository": item.repository}
            try:
                out = await self.link_repository(
                    item.source,
                    item.repository,
                    item.call_number,
                    item.media_type,
                    allow_new_type=allow_new_type,
                )
                row["status"] = "linked" if out.get("changed") else "already_linked"
            except NotFoundError as exc:
                row.update(status="missing", message=str(exc))
            except GrampsApiError as exc:
                row.update(status="error", message=exc.detail)
            except Exception as exc:  # noqa: BLE001 - one row must not end the sweep
                row.update(status="error", message=str(exc))
            rows.append(row)
        return _batch_result(rows)

    async def update_citations(self, items: list[CitationEdit]) -> dict:
        """Edit many citations' pages and confidences in one call.

        Each row goes through the same whole-object write as update_citation
        and is its own transaction. ``expect_page_prefix`` refuses a row whose
        page no longer starts as planned: a sweep planned from an earlier read
        must not overwrite an edit made since.

        Returns
        -------
        dict
            Counts by outcome and one result per row: ``applied``,
            ``unchanged``, ``drifted``, ``missing`` or ``error``.
        """
        rows = []
        for item in items:
            row: dict[str, Any] = {"citation": item.citation}
            if item.page is None and item.confidence is None:
                rows.append({**row, "status": "error", "message": "Nothing to set."})
                continue
            drift: dict[str, str] = {}

            def edit(cit: dict, item: CitationEdit = item, drift: dict = drift) -> str | bool:
                page = cit.get("page") or ""
                if item.expect_page_prefix is not None and not page.startswith(
                    item.expect_page_prefix
                ):
                    drift["page"] = page
                    return False
                changes = []
                if item.page is not None and page != item.page:
                    cit["page"] = item.page
                    changes.append("page")
                if item.confidence is not None:
                    value = mapping.confidence_to_int(item.confidence)
                    if cit.get("confidence") != value:
                        cit["confidence"] = value
                        changes.append(f"confidence={item.confidence.value}")
                return ", ".join(changes) if changes else False

            try:
                out = await self._mutate("citation", item.citation, edit, label="updated")
            except NotFoundError as exc:
                rows.append({**row, "status": "missing", "message": str(exc)})
                continue
            except GrampsApiError as exc:
                rows.append({**row, "status": "error", "message": exc.detail})
                continue
            except Exception as exc:  # noqa: BLE001 - one row must not end the sweep
                rows.append({**row, "status": "error", "message": str(exc)})
                continue
            if drift:
                row.update(status="drifted", live_page=drift["page"])
            else:
                row["status"] = "applied" if out.get("changed") else "unchanged"
            rows.append(row)
        return _batch_result(rows)

    async def add_event_to_family(
        self, family_ref: str, ev: EventInput, require_citation: bool = True
    ) -> dict:
        """Create a cited event and attach it to a family.

        Parameters
        ----------
        family_ref : str
            Handle or gramps_id of the family.
        ev : EventInput
            The event to create, attached with the Family role.
        require_citation : bool, optional
            Fail rather than record an uncited event.

        Returns
        -------
        dict
            The event's handle, gramps_id and a message.

        Raises
        ------
        CitationRequiredError
            If the event lacks a citation and ``require_citation`` is True.
        """
        # Resolved first, so a bad reference fails before an event exists.
        family_handle = await self._resolve_handle("family", family_ref)
        handle, is_uns = await self._create_event(ev, require_citation)

        def edit(family: dict) -> str:
            family.setdefault("event_ref_list", []).append(mapping.event_ref(handle, role="Family"))
            return f"{ev.type} event added"

        result = await self._mutate("family", family_handle, edit, label="added an event to")
        return _with_repairs(
            {
                "handle": result["handle"],
                "gramps_id": result.get("gramps_id"),
                "event_handle": handle,
                "object_type": "event",
                "unsourced": is_uns,
                "message": f"Added {ev.type} event to family {result.get('gramps_id')}"
                + (" (UNSOURCED)" if is_uns else ""),
            },
            result,
        )

    async def add_child_to_family(
        self,
        family_ref: str,
        child_ref: str,
        frel: str = "Birth",
        mrel: str = "Birth",
        allow_new_type: bool = False,
    ) -> dict:
        """Add an existing person as a child of an existing family.

        Parameters
        ----------
        family_ref : str
            Handle or gramps_id of the family.
        child_ref : str
            Handle or gramps_id of the child.
        frel, mrel : str, optional
            Relationship to the father and mother, e.g. ``"Birth"``,
            ``"Adopted"``.

        Returns
        -------
        dict
            Handle, gramps_id and a message. A child already in the family is
            left as it is; :meth:`update_child_ref` changes its relationship.
        """
        child_handle = await self._resolve_handle("person", child_ref)
        vocabulary = await self.client.types()
        frel = await self._canonical_type(
            "child_reference_types", frel, allow_new_type, types=vocabulary
        )
        mrel = await self._canonical_type(
            "child_reference_types", mrel, allow_new_type, types=vocabulary
        )

        def add_child(family: dict) -> str | bool:
            child_ref_list = family.setdefault("child_ref_list", [])
            if any(c.get("ref") == child_handle for c in child_ref_list):
                return False
            child_ref_list.append(
                {
                    "_class": "ChildRef",
                    "ref": child_handle,
                    "frel": frel,
                    "mrel": mrel,
                    "citation_list": [],
                    "note_list": [],
                }
            )
            return "child added"

        result = await self._mutate("family", family_ref, add_child, label="updated")
        family_handle = result["handle"]

        # The server links the child to the family when the family is written
        # (add_parent_family_handle); this covers a server that does not.
        def link_child(child: dict) -> str | bool:
            parent_family_list = child.setdefault("parent_family_list", [])
            if family_handle in parent_family_list:
                return False
            parent_family_list.append(family_handle)
            return "parent family linked"

        child_result = await self._mutate("person", child_handle, link_child, label="linked")
        label = result.get("gramps_id") or family_handle
        out = {
            "handle": family_handle,
            "gramps_id": result.get("gramps_id"),
            "child_handle": child_handle,
            "object_type": "family",
            "changed": result["changed"],
            "message": f"Added child to family {label}"
            if result["changed"]
            else f"Already a child of family {label}; nothing changed. To change the "
            "relationship to either parent, use update_child_ref.",
        }
        return _with_repairs(out, child_result)

    async def update_child_ref(
        self,
        family_ref: str,
        child_ref: str,
        frel: str | None = None,
        mrel: str | None = None,
        allow_new_type: bool = False,
    ) -> dict:
        """Change a child's relationship to the father or mother, in place.

        The ChildRef keeps its citations, notes, privacy and its position in
        the family's birth order -- all of which detaching the child and
        adding it back loses.

        Parameters
        ----------
        family_ref : str
            Handle or gramps_id of the family.
        child_ref : str
            Handle or gramps_id of the child.
        frel, mrel : str, optional
            Relationship to the father and to the mother, matched against the
            tree's child reference types: Birth, Adopted, Stepchild, Foster,
            Sponsored, None, Unknown, or a custom one the tree has.

        Returns
        -------
        dict
            Handle, gramps_id and a message, or an ``error`` key.
        """
        if frel is None and mrel is None:
            return {"error": "nothing_to_do", "message": "Pass frel, mrel, or both."}
        child_handle = await self._resolve_handle("person", child_ref)
        vocabulary = await self.client.types()
        wanted = {
            key: await self._canonical_type(
                "child_reference_types", value, allow_new_type, types=vocabulary
            )
            for key, value in (("frel", frel), ("mrel", mrel))
            if value is not None
        }
        found = {"hit": False}

        def edit(family: dict) -> str | bool:
            changed = []
            for cref in family.get("child_ref_list") or []:
                if cref.get("ref") != child_handle:
                    continue
                found["hit"] = True
                for key, value in wanted.items():
                    old = _type_string(cref.get(key))
                    if old != value:
                        cref[key] = value
                        changed.append(f"{key} {old or 'unset'} -> {value}")
            return ", ".join(dict.fromkeys(changed)) if changed else False

        result = await self._mutate("family", family_ref, edit, label="updated")
        if not found["hit"]:
            return {
                "error": "not_a_child",
                "message": f"Person {child_ref} is not a child of family {family_ref}; "
                "add_child_to_family adds one.",
            }
        result["child"] = child_ref
        return result

    async def set_family_parent(
        self,
        family_ref: str,
        role: str,
        person_ref: str | None = None,
        replace: bool = False,
    ) -> dict:
        """Set, replace or remove the father or mother of an existing family.

        Both sides of each link are kept. Writing the family makes the server
        take the family off the old parent and add it to the new one
        (``docs/PITFALLS.md`` section 15), with two defects handled here: it
        appends to the new parent's ``family_list`` without looking, so a
        family the person already listed would be listed twice; and it takes
        the family off the old parent with ``list.remove``, which refuses the
        write (400) when the old parent does not list it and leaves a second
        entry when they list it twice. So an old parent's missing link is
        restored first, and both people are written afterwards to hold the
        family exactly once, or not at all.

        Parameters
        ----------
        family_ref : str
            Handle or gramps_id of the family.
        role : str
            ``"father"`` or ``"mother"``.
        person_ref : str, optional
            The new parent. None removes the parent in that role.
        replace : bool, optional
            Put the person in place of a different parent the role already
            has; refused otherwise, naming that parent.

        Returns
        -------
        dict
            Handle, gramps_id, the parent before and after, and a message, or
            an ``error`` key.
        """
        key = f"{role}_handle"
        other = "mother" if role == "father" else "father"
        family = await self._resolve("family", family_ref)
        family_handle = family["handle"]
        label = family.get("gramps_id") or family_handle
        new = await self._resolve("person", person_ref) if person_ref else None
        new_handle = new["handle"] if new else ""
        old_handle = family.get(key) or ""
        old = None
        if old_handle:
            with contextlib.suppress(NotFoundError):
                old = await self._resolve("person", old_handle)
        old_label = (old or {}).get("gramps_id") or old_handle or None
        new_label = (new or {}).get("gramps_id") or new_handle or None
        if old_handle == new_handle:
            return {
                "handle": family_handle,
                "gramps_id": family.get("gramps_id"),
                "object_type": "family",
                "changed": False,
                "message": f"Family {label} already has "
                + (f"{new_label} as its {role}" if new_handle else f"no {role}")
                + "; nothing changed.",
            }
        if new_handle and new_handle == family.get(f"{other}_handle"):
            return {
                "error": "already_a_parent",
                "message": f"{new_label} is the {other} of family {label}; nothing changed.",
            }
        if new_handle and any(
            c.get("ref") == new_handle for c in family.get("child_ref_list") or []
        ):
            return {
                "error": "child_of_family",
                "message": f"{new_label} is a child of family {label}, so cannot be its "
                f"{role}; nothing changed.",
            }
        if old_handle and new_handle and not replace:
            return {
                "error": "parent_already_set",
                "message": f"Family {label} already has a {role}, {old_label} "
                f"({_name_from_person(old) if old else 'a person who no longer exists'}). "
                f"Pass replace=True to put {new_label} in their place, or set_family_parent "
                f"without a person to remove them; nothing changed.",
            }
        repairs: list[str] = []
        if old is not None and family_handle not in (old.get("family_list") or []):
            # The server's list.remove would refuse the family write.

            def restore(person: dict) -> str:
                person.setdefault("family_list", []).append(family_handle)
                return "link to the family restored, for the server to remove"

            await self._mutate("person", old_handle, restore, label="linked")
            repairs.append(f"restored {old_label}'s missing link, so the server could remove it")

        def edit(fam: dict) -> str | bool:
            if (fam.get(key) or "") != old_handle:
                raise _ConcurrentChange(f"the {role} of family {label} changed meanwhile")
            fam[key] = new_handle
            return f"{role} {old_label or 'none'} -> {new_label or 'none'}"

        result = await self._mutate("family", family_handle, edit, label="updated")
        if new_handle:

            def link(person: dict) -> str | bool:
                listed = person.setdefault("family_list", [])
                if family_handle in listed:
                    return False  # _normalize drops a second entry
                listed.append(family_handle)
                return "link to the family added"

            if (await self._mutate("person", new_handle, link, label="linked")).get("changed"):
                repairs.append(f"{new_label} now lists the family once")
        if old is not None:

            def unlink(person: dict) -> str | bool:
                links = person.get("family_list") or []
                kept = [f for f in links if f != family_handle]
                if len(kept) == len(links):
                    return False
                person["family_list"] = kept
                return "remaining link to the family removed"

            if (await self._mutate("person", old_handle, unlink, label="unlinked")).get("changed"):
                repairs.append(f"removed {old_label}'s remaining link to the family")
        out = {
            "handle": family_handle,
            "gramps_id": family.get("gramps_id"),
            "object_type": "family",
            "changed": True,
            "role": role,
            "parent": new_label,
            "previous": old_label,
            "message": result["message"] + (f" ({'; '.join(repairs)})" if repairs else ""),
        }
        gender = (new or {}).get("gender")
        if (role, gender) in (("father", 0), ("mother", 1)):
            out["warning"] = (
                f"{new_label} is recorded as {'female' if gender == 0 else 'male'}, and is "
                f"now this family's {role}."
            )
        return out

    async def move_child(
        self,
        child_ref: str,
        from_family_ref: str,
        to_family_ref: str,
        frel: str | None = None,
        mrel: str | None = None,
        allow_new_type: bool = False,
    ) -> dict:
        """Move a child from one family to another, keeping the link's evidence.

        The ChildRef moves whole: its citations, notes and privacy, and its
        relationships unless ``frel`` or ``mrel`` changes one. In the new
        family it goes into birth order, before the first sibling born after
        the child; an undated child goes last. The new family is written
        first, so a failure part-way leaves the child in both families rather
        than in neither. Then every link from the child to the old family is
        removed (a duplicated one too, ``docs/PITFALLS.md`` section 15), and
        the new family takes the old one's place in the child's own list,
        whose first entry Gramps reads as the main parents.

        Parameters
        ----------
        child_ref : str
            Handle or gramps_id of the child.
        from_family_ref, to_family_ref : str
            Handles or gramps_ids of the families.
        frel, mrel : str, optional
            A new relationship to the father or mother; omitted, kept.

        Returns
        -------
        dict
            The child, both families, the child's position and the new
            family's children in order, or an ``error`` key.
        """
        child = await self._resolve("person", child_ref)
        child_handle = child["handle"]
        child_label = child.get("gramps_id") or child_handle
        source = await self._resolve("family", from_family_ref)
        target = await self._resolve("family", to_family_ref)
        source_label = source.get("gramps_id") or source["handle"]
        target_label = target.get("gramps_id") or target["handle"]
        if source["handle"] == target["handle"]:
            return {
                "error": "same_family",
                "message": "from_family and to_family are the same family; update_child_ref "
                "changes a child's relationships in place.",
            }
        refs = [c for c in source.get("child_ref_list") or [] if c.get("ref") == child_handle]
        if not refs:
            return {
                "error": "not_a_child",
                "message": f"{child_label} is not a child of family {source_label}; "
                "add_child_to_family adds a child.",
            }
        if any(c.get("ref") == child_handle for c in target.get("child_ref_list") or []):
            return {
                "error": "already_a_child",
                "message": f"{child_label} is already a child of family {target_label}. "
                f"detach_object(parent_type='family', parent='{source_label}', "
                f"child_kind='child', child='{child_label}') takes them out of the other.",
            }
        if child_handle in (target.get("father_handle"), target.get("mother_handle")):
            return {
                "error": "parent_of_family",
                "message": f"{child_label} is a parent in family {target_label}, so cannot be "
                "its child.",
            }
        moved = copy.deepcopy(refs[0])
        for extra in refs[1:]:  # a child listed twice keeps the evidence of both
            for key in ("citation_list", "note_list"):
                held = moved.setdefault(key, [])
                held += [h for h in extra.get(key) or [] if h not in held]
        vocabulary = await self.client.types()
        for key, value in (("frel", frel), ("mrel", mrel)):
            if value is not None:
                moved[key] = await self._canonical_type(
                    "child_reference_types", value, allow_new_type, types=vocabulary
                )
        births = await self._birth_sortvals(
            [c.get("ref") for c in target.get("child_ref_list") or []] + [child_handle]
        )
        placed: dict[str, Any] = {}

        def add(fam: dict) -> str | bool:
            crefs = fam.setdefault("child_ref_list", [])
            if any(c.get("ref") == child_handle for c in crefs):
                return False
            index = _birth_position(crefs, births, child_handle)
            crefs.insert(index, moved)
            placed.update(position=index + 1, order=[c.get("ref") for c in crefs])
            return f"child {child_label} added, {index + 1} of {len(crefs)} in birth order"

        await self._mutate("family", target["handle"], add, label="updated")
        if not placed:
            raise _ConcurrentChange(f"{child_label} was added to {target_label} meanwhile")

        def remove(fam: dict) -> str | bool:
            crefs = fam.get("child_ref_list") or []
            kept = [c for c in crefs if c.get("ref") != child_handle]
            if len(kept) == len(crefs):
                return False
            fam["child_ref_list"] = kept
            return f"child {child_label} removed"

        try:
            await self._mutate("family", source["handle"], remove, label="updated")
        except Exception as exc:  # noqa: BLE001 - reported with the half that landed
            return {
                "error": "partly_moved",
                "message": f"{child_label} was added to family {target_label} with the link's "
                f"evidence, but could not be taken out of {source_label} ({exc}). Finish with "
                f"detach_object(parent_type='family', parent='{source_label}', "
                f"child_kind='child', child='{child_label}').",
            }
        before = child.get("parent_family_list") or []
        slot = before.index(source["handle"]) if source["handle"] in before else len(before)

        def settle(person: dict) -> str | bool:
            links = person.get("parent_family_list") or []
            others = [f for f in links if f not in (source["handle"], target["handle"])]
            wanted = others[:slot] + [target["handle"]] + others[slot:]
            if links == wanted:
                return False
            person["parent_family_list"] = wanted
            return "links to the families settled"

        settled = await self._mutate("person", child_handle, settle, label="linked")
        ids = {
            row["handle"]: row.get("gramps_id")
            for row in await self.client.list_objects(
                "person", handles=[h for h in placed["order"] if h], keys="handle,gramps_id"
            )
        }
        out = {
            "handle": child_handle,
            "gramps_id": child.get("gramps_id"),
            "object_type": "person",
            "changed": True,
            "from_family": source_label,
            "to_family": target_label,
            "frel": _type_string(moved.get("frel")),
            "mrel": _type_string(moved.get("mrel")),
            "kept": {
                "citations": len(moved.get("citation_list") or []),
                "notes": len(moved.get("note_list") or []),
            },
            "position": placed["position"],
            "children": [ids.get(h) or h for h in placed["order"]],
            "message": f"Moved {child_label} from family {source_label} to {target_label}, "
            f"{placed['position']} of {len(placed['order'])} in birth order, with the link's "
            f"{len(moved.get('citation_list') or [])} citation(s) and "
            f"{len(moved.get('note_list') or [])} note(s)",
        }
        if settled.get("changed"):
            out["message"] += f"; {child_label}'s own links settled"
        return _with_repairs(out, settled)

    async def _birth_sortvals(self, handles: list[str | None]) -> dict[str, int]:
        """Each person's birth date as a day number, 0 when undated, in one query."""
        wanted = list(dict.fromkeys(h for h in handles if h))
        rows, _, _ = await self.client.structured_query(
            "person",
            {
                "select": ["handle", {"json_path": ["birth", "date", "sortval"], "as": "sortval"}],
                "where": [{"column": "handle", "op": "in", "value": wanted}],
                "limit": len(wanted),
            },
        )
        return {row.get("handle"): row.get("sortval") or 0 for row in rows}

    async def check_family_links(self, limit: int = 200) -> dict:
        """Audit the links between people and families, in both directions.

        A person and a family each record their link -- ``family_list`` and
        ``parent_family_list`` on the person, ``father_handle``,
        ``mother_handle`` and ``child_ref_list`` on the family -- and the two
        sides can disagree. Two collection reads, whatever the tree's size.

        Finds a family listed twice by one person (repaired by any edit of
        the person), a child listed twice by one family, a link one side
        holds and the other lacks, and a link to an object that does not
        exist.

        Unless ``expose_private`` is set, a finding about a private or
        probably-living person is withheld and counted.

        Parameters
        ----------
        limit : int, optional
            Maximum findings to return.

        Returns
        -------
        dict
            ``problem_count``, ``by_kind``, the findings, each with a
            ``repair``, and ``withheld_count``.
        """
        people = await self.client.list_objects(
            "person", keys="handle,gramps_id,family_list,parent_family_list"
        )
        families = await self.client.list_objects(
            "family", keys="handle,gramps_id,father_handle,mother_handle,child_ref_list"
        )
        person_by = {p["handle"]: p for p in people}
        family_by = {f["handle"]: f for f in families}
        pid = {h: p.get("gramps_id") or h for h, p in person_by.items()}
        fid = {h: f.get("gramps_id") or h for h, f in family_by.items()}
        found: list[dict] = []

        def add(kind: str, person: str, family: str, repair: str, **extra: Any) -> None:
            found.append(
                {
                    "kind": kind,
                    "person": pid.get(person, person),
                    "family": fid.get(family, family),
                    **extra,
                    "repair": repair,
                    "_person": person,
                }
            )

        for handle, person in person_by.items():
            for key in ("family_list", "parent_family_list"):
                entries = person.get(key) or []
                for family in dict.fromkeys(entries):
                    if entries.count(family) > 1:
                        add(
                            f"duplicate_{key}",
                            handle,
                            family,
                            f"Any edit of the person removes it: update_person(person="
                            f"'{pid[handle]}') with nothing else.",
                            count=entries.count(family),
                        )
            for family in dict.fromkeys(person.get("family_list") or []):
                fam = family_by.get(family)
                if fam is None:
                    add(
                        "missing_family",
                        handle,
                        family,
                        f"detach_object(parent_type='person', parent='{pid[handle]}', "
                        f"child_kind='family', child='{family}')",
                        side="family_list",
                    )
                elif handle not in (fam.get("father_handle"), fam.get("mother_handle")):
                    add(
                        "one_sided_spouse_link",
                        handle,
                        family,
                        "The person lists the family as a spouse; the family names them as "
                        "neither parent. If the person is not a parent here, detach_object("
                        f"parent_type='person', parent='{pid[handle]}', child_kind='family', "
                        f"child='{fid[family]}').",
                    )
            for family in dict.fromkeys(person.get("parent_family_list") or []):
                fam = family_by.get(family)
                if fam is None:
                    add(
                        "missing_family",
                        handle,
                        family,
                        f"detach_object(parent_type='person', parent='{pid[handle]}', "
                        f"child_kind='parent_family', child='{family}')",
                        side="parent_family_list",
                    )
                elif not any(c.get("ref") == handle for c in fam.get("child_ref_list") or []):
                    add(
                        "one_sided_child_link",
                        handle,
                        family,
                        "The person lists the family as parents; the family does not list "
                        f"them as a child. If they are its child, add_child_to_family(family="
                        f"'{fid[family]}', child='{pid[handle]}'); if not, detach_object("
                        f"parent_type='person', parent='{pid[handle]}', "
                        f"child_kind='parent_family', child='{fid[family]}').",
                    )
        for handle, fam in family_by.items():
            for role in ("father_handle", "mother_handle"):
                parent = fam.get(role)
                if not parent:
                    continue
                if parent not in person_by:
                    add(
                        "missing_person",
                        parent,
                        handle,
                        "The family names a parent who does not exist; set the parent in "
                        "Gramps Web.",
                        side=role,
                    )
                elif handle not in (person_by[parent].get("family_list") or []):
                    add(
                        "one_sided_spouse_link",
                        parent,
                        handle,
                        "The family names this parent; the person does not list the family. "
                        "Open the family in Gramps Web and save it, or correct the parent "
                        "there.",
                    )
            children = [c.get("ref") for c in fam.get("child_ref_list") or []]
            for child in dict.fromkeys(children):
                if children.count(child) > 1:
                    add(
                        "duplicate_child_ref",
                        child,
                        handle,
                        "The family lists the child twice. Compare the two entries' "
                        "citations in get_object(family) before removing one in Gramps Web.",
                        count=children.count(child),
                    )
                if child not in person_by:
                    add(
                        "missing_person",
                        child,
                        handle,
                        f"detach_object(parent_type='family', parent='{fid[handle]}', "
                        f"child_kind='child', child='{child}')",
                        side="child_ref_list",
                    )
                elif handle not in (person_by[child].get("parent_family_list") or []):
                    add(
                        "one_sided_child_link",
                        child,
                        handle,
                        "The family lists the child; the person does not list the family. "
                        f"add_child_to_family(family='{fid[handle]}', child='{pid[child]}') "
                        "restores the person's side.",
                    )

        withheld: set[str] = set()
        if not self.exposing_private:
            withheld = await self._restricted_people(
                f["_person"] for f in found if f["_person"] in person_by
            )
        shown = [
            {k: v for k, v in f.items() if k != "_person"}
            for f in found
            if f["_person"] not in withheld
        ]
        by_kind: dict[str, int] = {}
        for f in shown:
            by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        return {
            "people_checked": len(people),
            "families_checked": len(families),
            "problem_count": len(shown),
            "by_kind": by_kind,
            "problems": shown[:limit],
            "truncated": len(shown) > limit,
            "withheld_count": len(found) - len(shown),
            "message": (f"{len(shown)} link problem(s)." if shown else "No link problems.")
            + (
                f" {len(found) - len(shown)} more about private or living people withheld."
                if len(found) > len(shown)
                else ""
            ),
        }

    async def add_alternate_name(
        self,
        person_ref: str,
        name: NameParts,
        name_type: str = "Also Known As",
        citation: CitationInput | None = None,
        allow_new_type: bool = False,
    ) -> dict:
        """Add a non-primary name to a person, optionally cited.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id.
        name : NameParts
            The alternate name.
        name_type : str, optional
            Gramps name type, e.g. ``"Married Name"``.
        citation : CitationInput, optional
            The record that gives this form of the name. Goes on the name
            itself, not the person: "this record spells it so" is a claim
            about the name.

        Returns
        -------
        dict
            Handle, gramps_id, the citation handle if one was attached, and a
            message.
        """
        person_handle = await self._resolve_handle("person", person_ref)
        name_type = await self._canonical_type("name_types", name_type, allow_new_type)
        citation_handle = await self.resolve_citation(citation) if citation else None
        name_dict = mapping.name_payload(name)
        name_dict["type"] = name_type
        if citation_handle:
            name_dict["citation_list"] = [citation_handle]

        def edit(person: dict) -> str:
            person.setdefault("alternate_names", []).append(name_dict)
            return f"{name_type} name added" + (", cited" if citation_handle else "")

        try:
            result = await self._mutate("person", person_handle, edit, label="updated")
        except Exception:
            await self._discard_minted_citation(citation, citation_handle)
            raise
        return _with_repairs(
            {
                "handle": result["handle"],
                "gramps_id": result.get("gramps_id"),
                "object_type": "person",
                "citation_handle": citation_handle,
                "message": f"Added alternate name to person "
                f"{result.get('gramps_id') or result['handle']}"
                + (" with its citation" if citation_handle else " (uncited)"),
            },
            result,
        )

    # ------------------------------------------------------------------ #
    # read: source, repository, event detail
    # ------------------------------------------------------------------ #
    async def get_source(self, ref: str) -> dict:
        """Fetch a source and its real citation count.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            Title, author, pubinfo, abbrev and ``citation_count``.
        """
        # backlinks=True or citation_count reads 0 for every source.
        # docs/PITFALLS.md section 2.
        try:
            source = await self._resolve("source", ref, extend="all", backlinks=True)
        except GrampsApiError:
            source = await self._resolve("source", ref, backlinks=True)
        backlinks = source.get("backlinks") or {}
        citation_count = 0
        if isinstance(backlinks, dict):
            citation_count = len(backlinks.get("citation") or [])
        return {
            "handle": source["handle"],
            "gramps_id": source.get("gramps_id"),
            "title": source.get("title"),
            "author": source.get("author"),
            "pubinfo": source.get("pubinfo"),
            "abbrev": source.get("abbrev"),
            "repositories": [
                {"ref": r.get("ref"), "call_number": r.get("call_number")}
                for r in source.get("reporef_list", [])
            ],
            "media_count": len(source.get("media_list") or []),
            "note_count": len(source.get("note_list") or []),
            "attributes": [
                {"type": _type_string(a.get("type")), "value": a.get("value")}
                for a in source.get("attribute_list", [])
            ],
            "citation_count": citation_count,
        }

    async def get_repository(self, ref: str) -> dict:
        """Fetch a repository and the sources it holds.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            Name, type, URLs and held sources.
        """
        # backlinks=True is required or source_count never appears at all.
        try:
            repo = await self._resolve("repository", ref, extend="all", backlinks=True)
        except GrampsApiError:
            repo = await self._resolve("repository", ref, backlinks=True)
        result: dict[str, Any] = {
            "handle": repo["handle"],
            "gramps_id": repo.get("gramps_id"),
            "name": repo.get("name"),
            "type": _type_string(repo.get("type")),
            "urls": [
                {"path": u.get("path"), "type": _type_string(u.get("type")), "desc": u.get("desc")}
                for u in repo.get("urls", [])
            ],
            "address_count": len(repo.get("address_list") or []),
        }
        backlinks = repo.get("backlinks") or {}
        if isinstance(backlinks, dict) and "source" in backlinks:
            result["source_count"] = len(backlinks.get("source") or [])
        return result

    async def get_event(self, ref: str) -> dict:
        """Fetch one event: type, date, place, description and citation count.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            The formatted event.
        """
        event = await self._resolve("event", ref)
        return {
            "handle": event["handle"],
            "gramps_id": event.get("gramps_id"),
            "type": _type_string(event.get("type")),
            "date": _date_string(event.get("date")),
            "place_handle": event.get("place") or None,
            "description": event.get("description"),
            "citation_count": len(event.get("citation_list") or []),
            "attributes": [
                {"type": _type_string(a.get("type")), "value": a.get("value")}
                for a in event.get("attribute_list", [])
            ],
        }

    async def get_place(self, ref: str) -> dict:
        """Fetch one place: name, title, type, enclosure and coordinates.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            The formatted place, including the handle of its enclosing place.
        """
        place = await self._resolve("place", ref, extend="all")
        enclosed_by = place.get("placeref_list") or []
        out = {
            "handle": place["handle"],
            "gramps_id": place.get("gramps_id"),
            "name": (place.get("name") or {}).get("value"),
            "title": place.get("title"),
            "type": _type_string(place.get("place_type")),
            "code": place.get("code") or None,
            "latitude": place.get("lat") or None,
            "longitude": place.get("long") or None,
            "enclosed_by": [p.get("ref") for p in enclosed_by if p.get("ref")],
            "urls": [
                {
                    "path": u.get("path"),
                    "type": _type_string(u.get("type")),
                    "description": u.get("desc"),
                }
                for u in place.get("urls", [])
            ],
            "citation_count": len(place.get("citation_list") or []),
        }
        if "type" in place:
            # Written by 1.0.x add_place and never read by Gramps.
            out["stray_type_key"] = _type_string(place.get("type"))
            out["note"] = (
                "This place carries a stray top-level 'type' key, which Gramps ignores. "
                "Any edit of the place removes it -- update_place(place=...) with no other "
                "argument does -- moving it into the type when the type is Unknown."
            )
        return out

    async def get_citation(self, ref: str) -> dict:
        """Fetch one citation: page, confidence, date and its source.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            The formatted citation, plus how many objects cite it -- a count
            of zero means it is orphan debris.
        """
        citation = await self._resolve("citation", ref, backlinks=True)
        backlinks = citation.get("backlinks") or {}
        cited_by = (
            sum(len(v or []) for v in backlinks.values()) if isinstance(backlinks, dict) else 0
        )
        return {
            "handle": citation["handle"],
            "gramps_id": citation.get("gramps_id"),
            "page": citation.get("page"),
            "confidence": mapping.confidence_label(citation.get("confidence")),
            "date": _date_string(citation.get("date")),
            "source_handle": citation.get("source_handle"),
            "cited_by_count": cited_by,
            "note_count": len(citation.get("note_list") or []),
        }

    async def get_note(self, ref: str) -> dict:
        """Fetch one note: its type and text.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            The formatted note. Text is returned in full; notes are the one
            place a researcher's own reasoning lives.
        """
        note = await self._resolve("note", ref, backlinks=True)
        text = note.get("text")
        if isinstance(text, dict):
            text = text.get("string")
        backlinks = note.get("backlinks") or {}
        return {
            "handle": note["handle"],
            "gramps_id": note.get("gramps_id"),
            "type": _type_string(note.get("type")),
            "text": text,
            "attached_to_count": sum(len(v or []) for v in backlinks.values())
            if isinstance(backlinks, dict)
            else 0,
        }

    async def get_media(self, ref: str) -> dict:
        """Fetch one media object: path, checksum, description and date.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.

        Returns
        -------
        dict
            The formatted media object, and how many objects reference it.
            One image cited from several facts is correct; several copies of
            one image is the duplicate `find_duplicates` looks for.
        """
        media = await self._resolve("media", ref, backlinks=True)
        backlinks = media.get("backlinks") or {}
        return {
            "handle": media["handle"],
            "gramps_id": media.get("gramps_id"),
            "description": media.get("desc"),
            "path": media.get("path"),
            "mime": media.get("mime"),
            "checksum": media.get("checksum"),
            "date": _date_string(media.get("date")),
            "referenced_by_count": sum(len(v or []) for v in backlinks.values())
            if isinstance(backlinks, dict)
            else 0,
            "citation_count": len(media.get("citation_list") or []),
        }

    async def add_place(
        self,
        name: str,
        place_type: str | None = None,
        parent: str | None = None,
        title: str | None = None,
        latitude: str | None = None,
        longitude: str | None = None,
        code: str | None = None,
        allow_new_type: bool = False,
    ) -> dict:
        """Create a place deliberately, typed and parented.

        Until now a place could only appear as a side effect of resolving an
        event's place string, which produces an untyped, unparented place with
        the bare name as its title. A place created here can be correct from
        the start.

        Parameters
        ----------
        name : str
            The place's own name, e.g. ``"Cedar Flat"``.
        place_type : str, optional
            Gramps place type, e.g. ``"Town"``, ``"County"``.
        parent : str, optional
            Handle or gramps_id of an existing enclosing place. Never
            find-or-created: a typo minting a parent is how duplicate
            hierarchies start.
        title : str, optional
            Full display title. Defaults to ``name``.
        latitude, longitude, code : str, optional
            Coordinates and postal or FIPS code.

        Returns
        -------
        dict
            Handle, gramps_id and a message.

        Raises
        ------
        NotFoundError
            If ``parent`` names a place that does not exist.
        """
        payload = mapping.place_payload(name)
        if title:
            payload["title"] = title
        if place_type:
            place_type = await self._canonical_type("place_types", place_type, allow_new_type)
            # Gramps' field is place_type. 1.0.x wrote "type", which the server
            # kept as a stray key while the place stayed Unknown.
            payload["place_type"] = place_type
        for key, value in (("lat", latitude), ("long", longitude), ("code", code)):
            if value:
                payload[key] = value
        if parent:
            parent_handle = await self._resolve_handle("place", parent)
            payload["placeref_list"] = [{"_class": "PlaceRef", "ref": parent_handle}]
        created = await self.client.create_object("place", payload)
        self._place_cache.pop((title or name).strip().lower(), None)
        return _write_result("place", created)

    async def get_facts(
        self,
        person_filter: str | None = None,
        person: str | None = None,
        rank: int = 1,
    ) -> dict:
        """Read the tree's record-holders: oldest at death, most children.

        The statistics are superlatives over a set of people, never figures
        about one person. ``person`` only anchors a built-in filter; the API
        answers an anchor on its own with a bare 422, so that combination is
        refused here with the reason instead.

        Unless ``expose_private`` is set, private records and living people
        are excluded by the server before it computes anything, using Gramps'
        own living test. Filtering afterwards would not work: a living person
        can hold any record, not only "youngest living".

        Parameters
        ----------
        person_filter : str, optional
            One of :data:`FACT_PERSON_FILTERS`, matched without regard to
            case, or the name of a saved custom person filter.
        person : str, optional
            Handle or gramps_id anchoring a built-in filter.
        rank : int
            Record-holders to return per statistic.

        Returns
        -------
        dict
            ``facts`` as the server reports them, unshaped -- the contents
            vary by version -- plus the scope and privacy mode applied, or an
            ``error`` key describing why the arguments were refused.
        """
        if rank < 1:
            return {"error": "invalid_rank", "message": "rank must be 1 or more."}
        params: dict[str, Any] = {"rank": rank}
        scope = "whole tree"
        person_filter = (person_filter or "").strip() or None
        if person_filter:
            builtin = {f.lower(): f for f in FACT_PERSON_FILTERS}
            name = builtin.get(person_filter.strip().lower(), person_filter.strip())
            params["person"] = name
            if name in FACT_PERSON_FILTERS:
                if not person:
                    return {
                        "error": "no_target",
                        "message": f"person_filter {name!r} is anchored on a "
                        "person: pass person as well.",
                    }
                params["handle"] = await self._resolve_handle("person", person)
                scope = f"{name} of {person}"
            elif person:
                return {
                    "error": "conflicting_arguments",
                    "message": "person anchors only the built-in filters "
                    f"({', '.join(FACT_PERSON_FILTERS)}). A custom filter "
                    "carries its own rules; omit person.",
                }
            else:
                scope = f"custom filter {name}"
        elif person:
            return {
                "error": "conflicting_arguments",
                "message": "get_facts reports record-holders across a set of "
                "people, not statistics about one person. Pass person_filter "
                "(e.g. 'Ancestors' or 'Descendants') to anchor the set on this "
                "person, or read the person with get_person or get_timeline.",
            }
        if not self.exposing_private:
            params["private"] = True
            params["living"] = "ExcludeAll"
        facts = await self.client.facts(**params)
        return {
            "scope": scope,
            "living_and_private": ("included" if self.exposing_private else "excluded"),
            "facts": facts,
        }

    async def get_researcher(self) -> dict:
        """Read the researcher details embedded in exports.

        Returns
        -------
        dict
            Name, address and contact details recorded for this tree.
        """
        return await self.client.researcher()

    # ------------------------------------------------------------------ #
    # safety spine
    # ------------------------------------------------------------------ #
    async def _mutate(self, object_type: str, ref: str, fn: Any, *, label: str = "updated") -> dict:
        """Read a whole object, mutate it, write it back.

        The only write path in this module. ``PUT`` replaces the record with
        exactly what is sent, so a payload built from a ``keys=`` fetch drops
        every unfetched field -- see ``docs/PITFALLS.md`` section 1. Fetching
        without ``keys`` makes that impossible to get wrong.

        Parameters
        ----------
        object_type : str
            Gramps object type.
        ref : str
            Handle or gramps_id.
        fn : callable
            Receives the full object and mutates it in place. Returning False
            means nothing changed, and the write is skipped rather than
            issuing a no-op PUT that still lands in the transaction log.
        label : str, optional
            Verb used in the returned message.

        Returns
        -------
        dict
            Handle, gramps_id and a message. ``repaired`` lists defects
            :func:`_normalize` removed on the way; a write that repairs one
            happens even when ``fn`` changed nothing, which is how any edit
            of an affected object -- or a bare one -- cleans it up.
        """
        obj = await self._resolve(object_type, ref)
        # profile/extended/backlinks are computed by the API on read, never
        # stored. Writing them back would persist a snapshot of derived data.
        for computed in ("profile", "extended", "backlinks"):
            obj.pop(computed, None)
        before = copy.deepcopy(obj)
        result = fn(obj)
        repaired = _normalize(object_type, obj)
        label_id = obj.get("gramps_id") or obj["handle"]
        if result is False and not repaired:
            return {
                "handle": obj["handle"],
                "gramps_id": obj.get("gramps_id"),
                "object_type": object_type,
                "changed": False,
                "message": f"No change needed on {object_type} {label_id}",
            }
        try:
            await self.client.update_object(object_type, obj["handle"], obj)
        except GrampsApiError as exc:
            if exc.status < 500:
                raise
            raise await self._failed_write(object_type, before, exc) from exc
        logger.info("%s %s %s", label, object_type, obj.get("gramps_id"))
        if result is False:
            message = f"Repaired {object_type} {label_id}: {'; '.join(repaired)}"
        else:
            message = f"{label.capitalize()} {object_type} {label_id}" + (
                f": {result}" if isinstance(result, str) else ""
            )
            if repaired:
                message += f" (also repaired: {'; '.join(repaired)})"
        out = {
            "handle": obj["handle"],
            "gramps_id": obj.get("gramps_id"),
            "object_type": object_type,
            "changed": True,
            "message": message,
        }
        if repaired:
            out["repaired"] = repaired
        return out

    async def _failed_write(self, object_type: str, before: dict, exc: GrampsApiError) -> Exception:
        """Say what a PUT answered with a 5xx did, from a fresh read.

        The record read again exactly as it was read for the write means
        nothing was written; anything else means the write may have landed --
        or another session wrote meanwhile (``docs/PITFALLS.md`` sections 6
        and 17).
        """
        label = f"{object_type} {before.get('gramps_id') or before['handle']}"
        try:
            again = await self.client.get_object(object_type, before["handle"])
        except GrampsApiError:
            return FailedWriteError(exc, None, f"{label} could not be read again afterwards")
        for computed in ("profile", "extended", "backlinks"):
            again.pop(computed, None)
        if again == before:
            return FailedWriteError(exc, False, f"{label} reads exactly as it did before")
        return FailedWriteError(exc, None, f"{label} has changed since it was read")

    async def _verify_in_list(
        self, object_type: str, handle: str, list_key: str, needle: str
    ) -> bool:
        """Re-read an object and confirm a handle really landed in one of its lists.

        add_note has been observed returning success while the note never
        attached. A write that reports success it cannot demonstrate is worse
        than a failure, so attach-style operations check afterwards.
        """
        fresh = await self.client.get_object(object_type, handle, keys=list_key)
        entries = fresh.get(list_key) or []
        return any((e.get("ref") if isinstance(e, dict) else e) == needle for e in entries)

    # ------------------------------------------------------------------ #
    # query + audit
    # ------------------------------------------------------------------ #
    async def query_objects(
        self,
        object_type: str,
        gql: str | None = None,
        gramps_ids: list[str] | None = None,
        handles: list[str] | None = None,
        keys: str | None = None,
        sort: str | None = None,
        limit: int = 200,
        page: int = 1,
        backlinks: bool = False,
    ) -> dict:
        """Filtered collection read -- the workhorse for audit passes.

        Filtering happens SERVER-side (see client.GQL_NOTES for the GrampsQL
        syntax and its traps). Pulling a whole collection and filtering it in
        Python is slow enough on a real tree to time out.

        Unless ``expose_private`` is set, a record flagged private comes back
        as a redacted stub, and so does a probably-living person, judged by
        :meth:`_restricted_people`. ``keys`` cannot switch this off: the
        fields the judgement needs are fetched whether or not they were asked
        for, and dropped again afterwards.
        """
        refusal, membership = _gql_list_checks(gql) if gql else (None, [])
        if refusal:
            return refusal
        filtering = not self.exposing_private
        added: list[str] = []
        if filtering and keys:
            asked = {k.strip() for k in keys.split(",")}
            added = [k for k in ("handle", "private") if k not in asked]
            if added:
                keys = ",".join([keys, *added])

        if gramps_ids:
            rows: list[dict] = []
            for gid in gramps_ids:
                obj = await self.client.get_by_gramps_id(object_type, gid, keys=keys)
                if obj:
                    rows.append(obj)
            total = len(rows)
            truncated = False
        else:
            rows = await self.client.list_objects(
                object_type,
                gql=gql,
                handles=handles,
                keys=keys,
                sort=sort,
                backlinks=backlinks,
            )
            total = len(rows)
            start = (max(page, 1) - 1) * limit
            rows = rows[start : start + limit]
            truncated = total > start + len(rows)

        redacted = 0
        if filtering:
            withheld = {r.get("handle") for r in rows if r.get("private")}
            if object_type == "person":
                withheld |= await self._restricted_people(
                    r.get("handle") for r in rows if r.get("handle") not in withheld
                )
            redacted = sum(1 for r in rows if r.get("handle") in withheld)
            rows = [
                redacted_stub(r.get("gramps_id"), r.get("handle"))
                if r.get("handle") in withheld
                else {k: v for k, v in r.items() if k not in added}
                for r in rows
            ]
        out = {
            "object_type": object_type,
            "query": gql,
            "total_matched": total,
            "returned": len(rows),
            "truncated": truncated,
            "results": rows,
            "redacted_count": redacted,
            "note": "Filtered server-side. " + GQL_NOTES if gql else None,
        }
        if membership and not total:
            # Nothing matched a test of whether a handle is listed: say what
            # was asked, since the usual intent was a search of the text.
            out["warning"] = " ".join(membership)
        return out

    async def _restricted_people(self, handles: Iterable[str | None]) -> set[str]:
        """Which of these people bulk output must withhold.

        Raw person objects carry no dates, so they are fetched here: one
        structured query per 500 people, selecting the same paths
        :meth:`query_records` judges its rows by, so both tools reach the same
        verdict. A handle the query does not return is withheld too, since
        nothing shows it to be historical.

        Parameters
        ----------
        handles : iterable of str
            Person handles. Blanks and repeats are ignored.

        Returns
        -------
        set of str
            The handles that are private or probably living.
        """
        wanted = list(dict.fromkeys(h for h in handles if h))
        restricted: set[str] = set()
        for start in range(0, len(wanted), 500):
            batch = wanted[start : start + 500]
            select = [
                "handle",
                *({"json_path": path, "as": key} for key, path in _PRIVACY_PATHS["person"]),
            ]
            rows, _, _ = await self.client.structured_query(
                "person",
                {
                    "select": select,
                    "where": [{"column": "handle", "op": "in", "value": batch}],
                    "limit": len(batch),
                },
            )
            judged = {row.get("handle"): row for row in rows}
            for handle in batch:
                row = judged.get(handle)
                if row is None or self._person_restricted(
                    row.get("_private"), row.get("_birth"), row.get("_death")
                ):
                    restricted.add(handle)
        return restricted

    def _person_restricted(self, private: Any, birth: Any, death: Any) -> bool:
        """Apply :func:`privacy.assess` to a private flag and two Date dicts.

        The query engine answers ``death.date`` with a Date dict whenever the
        person has a death event, dated or not, and with null when they have
        none -- so a dict is a recorded death even when it holds no year.
        """
        return assess(
            private_flag=bool(private),
            birth_year=mapping.year_from_date_dict(birth if isinstance(birth, dict) else None),
            death_year=mapping.year_from_date_dict(death if isinstance(death, dict) else None),
            current_year=self._current_year(),
            died=isinstance(death, dict),
        ).restricted

    async def get_backlinks(self, object_type: str, ref: str) -> dict:
        """What references this object, grouped by type and resolved to gramps_ids.

        This is how you answer "is this source actually cited?" -- a source has
        no citation_list, because citations point AT it. Reading citation_list on
        a source makes every source report zero citations.
        """
        # 'backlinks' must be in keys, or the filter nulls it and every
        # object looks unreferenced. docs/PITFALLS.md section 2.
        obj = await self._resolve(
            object_type, ref, keys="handle,gramps_id,backlinks", backlinks=True
        )
        raw = obj.get("backlinks") or {}
        grouped: dict[str, Any] = {}
        for namespace, handles in raw.items():
            singular = _NAMESPACE_TO_TYPE.get(namespace, namespace)
            if not handles:
                continue
            ids = await self.client.list_objects(
                singular, handles=list(handles), keys="handle,gramps_id"
            )
            grouped[singular] = {
                "count": len(handles),
                "gramps_ids": [i.get("gramps_id") for i in ids],
            }
        return {
            "object_type": object_type,
            "gramps_id": obj.get("gramps_id"),
            "handle": obj["handle"],
            "total_references": sum(g["count"] for g in grouped.values()),
            "referenced_by": grouped,
            "message": "Nothing references this object."
            if not grouped
            else f"{sum(g['count'] for g in grouped.values())} object(s) reference this.",
        }

    async def find_duplicates(self, kind: str, limit: int = 50) -> dict:
        """Find likely-duplicate objects. Reports only; it never merges.

        Parameters
        ----------
        kind : {"media_checksum", "source_title", "citation_page", \
"vital_events", "person_name"}
            Which duplicate class to look for:

            ``media_checksum``
                The same file uploaded once per person it depicts.
            ``source_title``
                The same document entered twice.
            ``citation_page``
                The same source and page cited more than once.
            ``vital_events``
                A person carrying two births.
            ``person_name``
                Same given name and surname, overlapping birth years.
        limit : int, optional
            Maximum groups to report.

        Returns
        -------
        dict
            The candidate groups. Merging remains a judgement call.
        """
        finders = {
            "media_checksum": self._dupes_media_checksum,
            "source_title": self._dupes_source_title,
            "citation_page": self._dupes_citation_page,
            "vital_events": self._dupes_vital_events,
            "person_name": self._dupes_person_name,
        }
        if kind not in finders:
            return {
                "error": "unknown_kind",
                "message": f"kind must be one of: {', '.join(finders)}",
            }
        groups = await finders[kind]()
        return {
            "kind": kind,
            "group_count": len(groups),
            "surplus_objects": sum(len(g["members"]) - 1 for g in groups),
            "groups": groups[:limit],
            "truncated": len(groups) > limit,
            "next_step": "Review each group, then merge_objects(keep, drop). "
            "The server's merge re-points every reference; do not hand-roll it.",
        }

    def _drop_private(self, rows: list[dict]) -> list[dict]:
        """Leave out records flagged private, unless ``expose_private`` is set."""
        if self.exposing_private:
            return rows
        return [r for r in rows if not r.get("private")]

    async def _dupes_media_checksum(self) -> list[dict]:
        """Find media objects sharing a checksum, i.e. the same file twice."""
        media = self._drop_private(
            await self.client.list_objects(
                "media", keys="handle,gramps_id,checksum,desc,path,private"
            )
        )
        buckets: dict[str, list[dict]] = {}
        for m in media:
            cs = m.get("checksum")
            if cs:
                buckets.setdefault(cs, []).append(m)
        return [
            {
                "key": cs,
                "members": [
                    {"gramps_id": m.get("gramps_id"), "desc": m.get("desc"), "path": m.get("path")}
                    for m in group
                ],
            }
            for cs, group in buckets.items()
            if len(group) > 1
        ]

    async def _dupes_source_title(self) -> list[dict]:
        """Find sources sharing a normalised title."""
        sources = self._drop_private(
            await self.client.list_objects("source", keys="handle,gramps_id,title,private")
        )
        buckets: dict[str, list[dict]] = {}
        for s in sources:
            key = re.sub(r"\W+", " ", (s.get("title") or "")).strip().lower()
            if key:
                buckets.setdefault(key, []).append(s)
        return [
            {
                "key": group[0].get("title"),
                "members": [
                    {"gramps_id": s.get("gramps_id"), "title": s.get("title")} for s in group
                ],
            }
            for group in buckets.values()
            if len(group) > 1
        ]

    async def _dupes_citation_page(self) -> list[dict]:
        """Find citations on the same source with the same page."""
        cits = self._drop_private(
            await self.client.list_objects(
                "citation", keys="handle,gramps_id,source_handle,page,confidence,private"
            )
        )
        buckets: dict[tuple, list[dict]] = {}
        for c in cits:
            key = (c.get("source_handle"), (c.get("page") or "").strip())
            if key[0] and key[1]:
                buckets.setdefault(key, []).append(c)
        out = []
        for (src, page), group in buckets.items():
            if len(group) < 2:
                continue
            confidences = {c.get("confidence") for c in group}
            out.append(
                {
                    "key": f"source {src[:8]}... page '{page[:60]}'",
                    "divergent_confidence": len(confidences) > 1,
                    "members": [
                        {
                            "gramps_id": c.get("gramps_id"),
                            "confidence": mapping.confidence_label(c.get("confidence")),
                        }
                        for c in group
                    ],
                }
            )
        # The same page graded differently in two places is the interesting case.
        out.sort(key=lambda g: not g["divergent_confidence"])
        return out

    async def _dupes_vital_events(self) -> list[dict]:
        """Find people carrying more than one birth-like or death-like event."""
        events = {
            e["handle"]: e
            for e in self._drop_private(
                await self.client.list_objects("event", keys="handle,gramps_id,type,date,private")
            )
        }
        people = await self.client.list_objects(
            "person", keys="handle,gramps_id,event_ref_list,private"
        )
        vital = {"Birth", "Death", "Burial", "Baptism", "Christening"}
        out = []
        for p in people:
            by_type: dict[str, list[dict]] = {}
            for er in p.get("event_ref_list") or []:
                ev = events.get(er.get("ref"))
                if not ev:
                    continue
                t = _type_string(ev.get("type"))
                if t in vital:
                    by_type.setdefault(t, []).append(ev)
            for t, group in by_type.items():
                if len(group) > 1:
                    out.append(
                        {
                            "_handle": p.get("handle"),
                            "key": f"{p.get('gramps_id')} has {len(group)} {t} events",
                            "members": [
                                {
                                    "gramps_id": e.get("gramps_id"),
                                    "date": _date_string(e.get("date")),
                                }
                                for e in group
                            ],
                        }
                    )
        withheld: set[str] = set()
        if not self.exposing_private:
            withheld = await self._restricted_people(g["_handle"] for g in out)
        return [
            {k: v for k, v in g.items() if k != "_handle"}
            for g in out
            if g["_handle"] not in withheld
        ]

    async def _dupes_person_name(self) -> list[dict]:
        """Find people sharing a normalised primary name."""
        people = await self.client.list_objects(
            "person", keys="handle,gramps_id,primary_name,private"
        )
        buckets: dict[str, list[dict]] = {}
        for p in people:
            name = p.get("primary_name") or {}
            surnames = name.get("surname_list") or []
            key = (
                f"{(name.get('first_name') or '').strip().lower()}|"
                f"{(surnames[0].get('surname') if surnames else '').strip().lower()}"
            )
            if key.strip("|"):
                buckets.setdefault(key, []).append(p)
        groups = {k: g for k, g in buckets.items() if len(g) > 1}
        if not self.exposing_private:
            # A living namesake is dropped, not stubbed: the group's key is
            # the shared name, so a stub would still say who it is.
            withheld = await self._restricted_people(
                p.get("handle") for g in groups.values() for p in g
            )
            groups = {
                k: kept
                for k, g in groups.items()
                if len(kept := [p for p in g if p.get("handle") not in withheld]) > 1
            }
        return [
            {
                "key": key.replace("|", " "),
                "members": [
                    {"gramps_id": p.get("gramps_id"), "name": _name_from_person(p)} for p in group
                ],
            }
            for key, group in groups.items()
        ]

    async def list_object_types(self) -> dict:
        """The tree's type vocabularies (event types, attribute types, ...).

        Worth a look before inventing a type string: Gramps accepts an unknown
        event type as a new CUSTOM type rather than rejecting it, so a typo
        becomes a permanent addition to the tree's vocabulary.
        """
        types = await self.client.types()
        custom = types.get("custom", {}) or {}
        return {
            "default": types.get("default", {}),
            "custom": {k: v for k, v in custom.items() if v},
            "warning": "A type name in neither list is refused unless allow_new_type "
            "is set; it then becomes a custom type, kept in the tree for good.",
        }

    async def get_object(
        self,
        object_type: str,
        ref: str,
        keys: str | None = None,
        extend: str | None = None,
    ) -> dict:
        """Read any object's raw record.

        The typed getters shape their output for reading; this returns the
        record as stored, which is what an edit needs.

        A direct lookup is not privacy-filtered; only bulk output is. See
        :mod:`gramps_evidence_mcp.privacy`.

        Parameters
        ----------
        object_type : str
            Gramps object type.
        ref : str
            Handle or gramps_id.
        keys : str, optional
            Restrict the response. Note that it also filters backlinks.
        extend : str, optional
            Expand referenced objects.

        Returns
        -------
        dict
            The raw record.
        """
        return await self._resolve(object_type, ref, keys=keys, extend=extend)

    # ------------------------------------------------------------------ #
    # typed updates
    # ------------------------------------------------------------------ #
    async def update_citation(
        self,
        ref: str,
        page: str | None = None,
        confidence: Confidence | None = None,
        date: str | None = None,
        source_ref: str | None = None,
    ) -> dict:
        """Edit a citation's locator, confidence, date, or target source.

        Re-pointing at a different source is the fix for a fact cited to a
        compiled bucket when the actual record is in the tree, and for
        splitting a container source into the documents it held.

        A citation carries one confidence, belonging to one claim. If the same
        page supports a second claim, make a second citation: re-grading this
        one changes what every other fact citing it asserts. See
        ``docs/PITFALLS.md`` section 4.

        Parameters
        ----------
        ref : str
            Handle or gramps_id of the citation.
        page : str, optional
            New locator.
        confidence : Confidence, optional
            New grading.
        date : str, optional
            New date.
        source_ref : str, optional
            Handle or gramps_id of a source to re-point at.

        Returns
        -------
        dict
            Handle, gramps_id and a message naming what changed.
        """
        source_handle = await self._resolve_handle("source", source_ref) if source_ref else None
        new_date = await self._parse_date(date) if date is not None else None

        def edit(cit: dict) -> str | bool:
            changes = []
            if page is not None and cit.get("page") != page:
                cit["page"] = page
                changes.append("page")
            if confidence is not None:
                value = mapping.confidence_to_int(confidence)
                if cit.get("confidence") != value:
                    cit["confidence"] = value
                    changes.append(f"confidence={confidence.value}")
            if new_date is not None and not _same_date(cit.get("date"), new_date):
                cit["date"] = new_date
                changes.append("date")
            if source_handle and cit.get("source_handle") != source_handle:
                cit["source_handle"] = source_handle
                changes.append("re-pointed to a different source")
            return ", ".join(changes) if changes else False

        return await self._mutate("citation", ref, edit, label="updated")

    async def update_media(
        self,
        ref: str,
        description: str | None = None,
        date: str | None = None,
        path: str | None = None,
    ) -> dict:
        """Edit a media object's description, date, or stored path.

        A media object with an empty ``desc`` is effectively anonymous: files are
        stored under checksum names, so nothing in the media list says what the
        document is.
        """
        new_date = await self._parse_date(date) if date is not None else None

        def edit(media: dict) -> str | bool:
            changes = []
            if description is not None and media.get("desc") != description:
                media["desc"] = description
                changes.append("description")
            if new_date is not None and not _same_date(media.get("date"), new_date):
                media["date"] = new_date
                changes.append("date")
            if path is not None and media.get("path") != path:
                media["path"] = path
                changes.append("path")
            return ", ".join(changes) if changes else False

        return await self._mutate("media", ref, edit, label="updated")

    async def update_person(
        self,
        ref: str,
        gender: Gender | None = None,
        name: NameParts | None = None,
        private: bool | None = None,
        keep_old_as_alternate: bool = True,
        reason: str | None = None,
    ) -> dict:
        """Edit a person's gender, primary name, or privacy flag.

        Replacing the primary name keeps the old one as an alternate by
        default: a name in the tree came from some record, and dropping it
        loses the link to whatever document used it.

        That does not hold for a data-entry error in how the name was split
        -- given "Joan", surname "M. Anderson" for Joan M. Anderson -- where
        the old form is in no document and an alternate would invent a
        variant. With ``keep_old_as_alternate`` False the primary name is
        corrected in place instead, keeping its citations, notes, type and
        date, and ``reason`` is recorded with the old form in a Research
        note on the person. gramps-webapi's PUT takes no transaction
        description, so the note is the record.

        Parameters
        ----------
        ref : str
            Handle or gramps_id.
        gender : Gender, optional
            New gender.
        name : NameParts, optional
            The new primary name. A name whose parts are unchanged is no
            change.
        private : bool, optional
            New privacy flag.
        keep_old_as_alternate : bool, optional
            Keep the replaced primary name as an Also Known As.
        reason : str, optional
            Why the old form is not kept. Required when
            ``keep_old_as_alternate`` is False.

        Returns
        -------
        dict
            Handle, gramps_id and a message, plus ``note_handle`` when a
            correction was recorded, or an ``error`` key.
        """
        if not keep_old_as_alternate and name is None:
            return {
                "error": "conflicting_arguments",
                "message": "keep_old_as_alternate only applies when name is given.",
            }
        if not keep_old_as_alternate and not (reason or "").strip():
            return {
                "error": "reason_required",
                "message": "Dropping the old primary name needs a reason, recorded in a "
                "note on the person: e.g. 'entered with the middle initial in the "
                "surname; no record uses that form'.",
            }
        new_name = mapping.name_payload(name) if name is not None else None
        correction: dict[str, Any] = {}
        if new_name is not None and not keep_old_as_alternate:
            current = await self._resolve("person", ref, keys="handle,gramps_id,primary_name")
            old = current.get("primary_name") or {}
            if _name_parts(old) != _name_parts(new_name):
                note = await self.client.create_object(
                    "note",
                    {
                        "_class": "Note",
                        "type": "Research",
                        "text": {
                            "_class": "StyledText",
                            "string": f"Primary name corrected from {_name_label(old)} to "
                            f"{_name_label({**old, **new_name})}; the old form was not kept "
                            f"as an alternate name. Reason: {reason.strip()}",
                        },
                    },
                )
                correction["note"] = note

        def edit(person: dict) -> str | bool:
            changes = []
            if gender is not None:
                value = mapping.gender_to_int(gender)
                if person.get("gender") != value:
                    person["gender"] = value
                    changes.append(f"gender={gender.value}")
            if new_name is not None:
                old = person.get("primary_name") or {}
                if _name_parts(old) != _name_parts(new_name):
                    if keep_old_as_alternate:
                        if old:
                            alt = dict(old)
                            alt["type"] = "Also Known As"
                            person.setdefault("alternate_names", []).append(alt)
                        person["primary_name"] = new_name
                        changes.append("name (previous kept as an alternate)")
                    else:
                        # The same name, re-split: what supports it still does.
                        kept = old.get("surname_list") or [{}]
                        surname = dict(next((s for s in kept if s.get("primary")), kept[0]))
                        surname.update(surname=name.surname, prefix=name.prefix or "", primary=True)
                        person["primary_name"] = {
                            **old,
                            **new_name,
                            "surname_list": [surname],
                        }
                        if correction.get("note"):
                            person.setdefault("note_list", []).append(correction["note"]["handle"])
                        changes.append("name corrected in place (reason recorded in a note)")
            if private is not None and person.get("private") != private:
                person["private"] = bool(private)
                changes.append(f"private={bool(private)}")
            return ", ".join(changes) if changes else False

        try:
            result = await self._mutate("person", ref, edit, label="updated")
        except Exception:
            if correction.get("note"):
                await self._discard_note(correction["note"]["handle"])
            raise
        if correction.get("note"):
            result["note_handle"] = correction["note"]["handle"]
            result["note"] = correction["note"].get("gramps_id")
        return result

    async def update_alternate_name(
        self,
        person_ref: str,
        match: NameMatch,
        given: str | None = None,
        surname: str | None = None,
        prefix: str | None = None,
        suffix: str | None = None,
        nickname: str | None = None,
        name_type: str | None = None,
        remove: bool = False,
        allow_new_type: bool = False,
    ) -> dict:
        """Correct, retype or remove one alternate name, in place.

        Edited in place, the name keeps its citations and notes. A name that
        carries citations or notes is not removed: that would orphan them,
        or delete the evidence for a form some record used. Uncite it first
        (``uncite`` with ``object_type="name"``) if the name really is wrong.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id.
        match : NameMatch
            Which alternate name. Must select exactly one, except that of
            several identical names, removal takes one.
        given, surname, prefix, suffix, nickname : str, optional
            New parts.
        name_type : str, optional
            New type, matched against the tree's name types.
        remove : bool, optional
            Remove the name instead.

        Returns
        -------
        dict
            Handle, gramps_id and a message, or an ``error`` key.
        """
        edits = {
            "first_name": given,
            "surname": surname,
            "prefix": prefix,
            "suffix": suffix,
            "nick": nickname,
        }
        if remove and (name_type is not None or any(v is not None for v in edits.values())):
            return {
                "error": "conflicting_arguments",
                "message": "Pass remove=True on its own, or the parts to change.",
            }
        if not remove and name_type is None and all(v is None for v in edits.values()):
            return {
                "error": "nothing_to_do",
                "message": "Pass the parts or name_type to change, or remove=True.",
            }
        if match.primary:
            return {
                "error": "not_an_alternate",
                "message": "This edits alternate names. The primary name is "
                "update_person(name=...).",
            }
        new_type = (
            await self._canonical_type("name_types", name_type, allow_new_type)
            if name_type is not None
            else None
        )
        held: dict[str, list[str]] = {}

        def edit(person: dict) -> str | bool:
            key, target = _pick_name(person, match, alternates_only=True, allow_identical=remove)
            label = _name_label(target)
            if remove:
                held.update(
                    {
                        k: list(target.get(k) or [])
                        for k in ("citation_list", "note_list")
                        if target.get(k)
                    }
                )
                if held:
                    return False
                person["alternate_names"].pop(key)
                return f"removed alternate name {label}"
            changed = []
            surnames = target.setdefault("surname_list", [])
            if not surnames:
                surnames.append({"_class": "Surname", "primary": True})
            primary = next((s for s in surnames if s.get("primary")), surnames[0])
            for field, value in edits.items():
                if value is None:
                    continue
                holder = primary if field in ("surname", "prefix") else target
                if (holder.get(field) or "") != value:
                    holder[field] = value
                    changed.append(field)
            if new_type is not None and _type_string(target.get("type")) != new_type:
                target["type"] = new_type
                changed.append(f"type={new_type}")
            if not changed:
                return False
            return f"{label} -> {_name_label(target)}"

        result = await self._mutate("person", person_ref, edit, label="updated")
        if held:
            return {
                "error": "name_is_cited",
                "message": f"Not removed: the name holds {_held_listing(held)}. Uncite "
                "them first (uncite with object_type='name') if the name is wrong, or "
                "correct it in place instead of removing it.",
            }
        return result

    # Scalars only. Structural lists have dedicated tools, because setting
    # one wholesale drops references.
    _UPDATABLE_FIELDS: dict[str, set[str]] = {
        "place": {"name", "title", "code", "lat", "long", "private"},
        "note": {"text", "type", "format", "private"},
        "family": {"type", "private"},
        "repository": {"name", "type", "private"},
        "source": {"title", "author", "pubinfo", "abbrev", "private"},
        "event": {"description", "private"},
        "citation": {"page", "confidence", "private"},
        "media": {"desc", "path", "mime", "private"},
        "person": {"gender", "private"},
        "tag": {"name", "color", "priority"},
    }

    async def update_object_fields(
        self, object_type: str, ref: str, fields: dict[str, Any], allow_new_type: bool = False
    ) -> dict:
        """Set scalar fields on any object -- the escape hatch for the rest.

        Only scalar fields are settable (see _UPDATABLE_FIELDS). Structural
        lists are refused on purpose: replacing one wholesale is exactly how
        references get dropped, and every list has a dedicated tool.
        """
        allowed = self._UPDATABLE_FIELDS.get(object_type)
        if allowed is None:
            return {
                "error": "unsupported_type",
                "message": f"No updatable scalar fields defined for {object_type}.",
            }
        rejected = sorted(set(fields) - allowed)
        if rejected:
            return {
                "error": "unsupported_fields",
                "message": f"Cannot set {rejected} on a {object_type} this way. "
                f"Settable: {sorted(allowed)}. Structural lists have their own "
                f"tools (cite_object, detach_object, tag_object, attach_media).",
            }
        vocabulary = _TYPE_FIELD_VOCABULARY.get(object_type)
        if vocabulary and isinstance(fields.get("type"), str):
            fields = {
                **fields,
                "type": await self._canonical_type(vocabulary, fields["type"], allow_new_type),
            }

        def edit(obj: dict) -> str | bool:
            changed = []
            for key, value in fields.items():
                if key == "text" and object_type == "note":
                    if (obj.get("text") or {}).get("string") == value:
                        continue
                    obj["text"] = {
                        "_class": "StyledText",
                        "string": value,
                        "tags": (obj.get("text") or {}).get("tags", []),
                    }
                    changed.append("text")
                    continue
                if obj.get(key) != value:
                    obj[key] = value
                    changed.append(key)
            return ", ".join(changed) if changed else False

        return await self._mutate(object_type, ref, edit, label="updated")

    # ------------------------------------------------------------------ #
    # citation plumbing
    # ------------------------------------------------------------------ #
    async def cite_object(
        self,
        object_type: str,
        ref: str,
        cit: CitationInput,
        name: NameMatch | None = None,
    ) -> dict:
        """Attach a citation to ANY object that carries a citation_list.

        cite_event covers facts; this covers everything else -- most importantly
        the FAMILY, whose citation supports a claim no event makes: that these
        two people were a couple. Also person-level, media, place, and
        name-level citations.

        A name is cited with ``object_type="name"``, ``ref`` the person and
        ``name`` saying which of their names: "this record gives this form of
        the name" is a claim about the name, narrower than one about the
        person. The person and the name are checked before an inline citation
        is created, so a name that does not match leaves nothing behind.
        """
        if object_type == "name":
            if name is None:
                return {
                    "error": "name_required",
                    "message": "object_type 'name' cites one of a person's names: pass "
                    "ref (the person) and name (which name, e.g. {'surname': 'Bittner', "
                    "'type': 'Birth Name'}).",
                }
            person = await self._resolve("person", ref)
            _pick_name(person, name, alternates_only=False)
            citation_handle = await self.resolve_citation(cit)
            picked: dict[str, Any] = {}

            def edit_name(obj: dict) -> str | bool:
                key, target = _pick_name(obj, name, alternates_only=False)
                picked.update(key=key, label=_name_label(target))
                citation_list = target.setdefault("citation_list", [])
                if citation_handle in citation_list:
                    return False
                citation_list.append(citation_handle)
                return f"citation attached to the name {picked['label']}"

            try:
                result = await self._mutate("person", person["handle"], edit_name, label="cited")
            except Exception:
                await self._discard_minted_citation(cit, citation_handle)
                raise
            result.update(citation_handle=citation_handle, name=picked["label"])
            if result.get("changed"):
                fresh = await self._resolve("person", person["handle"])
                _, landed = _pick_name(fresh, name, alternates_only=False)
                result["verified"] = citation_handle in (landed.get("citation_list") or [])
            return result

        if object_type not in _CITABLE_TYPES:
            return {
                "error": "unsupported_type",
                "message": f"{object_type} has no citation_list. Citable: "
                f"{', '.join(sorted(_CITABLE_TYPES | {'name'}))}.",
            }
        citation_handle = await self.resolve_citation(cit)

        def edit(obj: dict) -> str | bool:
            citation_list = obj.setdefault("citation_list", [])
            if citation_handle in citation_list:
                return False
            citation_list.append(citation_handle)
            return "citation attached"

        result = await self._mutate(object_type, ref, edit, label="cited")
        result["citation_handle"] = citation_handle
        if result.get("changed"):
            result["verified"] = await self._verify_in_list(
                object_type, result["handle"], "citation_list", citation_handle
            )
        return result

    async def cite_child_link(self, family_ref: str, child_ref: str, cit: CitationInput) -> dict:
        """Cite the parent-child link itself, on the family's ChildRef.

        This is a different claim from "the child appears in this record", so
        it always mints its own citation rather than reusing a handle. A reused
        handle would make the link inherit a confidence assigned to another
        claim -- see ``docs/PITFALLS.md`` section 4.

        A ChildRef citation asserts both sides of the link. A census naming
        only the mother does not document the father; cite that on the family's
        mother relation instead.

        Parameters
        ----------
        family_ref : str
            Handle or gramps_id of the family.
        child_ref : str
            Handle or gramps_id of the child.
        cit : CitationInput
            Evidence for the link. Always used to mint a new citation.

        Returns
        -------
        dict
            Handle, gramps_id and a message.
        """
        if cit.citation:
            return _reused_citation_refused("cite_child_link")
        citation_handle = await self.resolve_citation(cit)
        child_handle = await self._resolve_handle("person", child_ref)
        found = {"hit": False}

        def edit(family: dict) -> str | bool:
            for cref in family.get("child_ref_list") or []:
                if cref.get("ref") != child_handle:
                    continue
                found["hit"] = True
                citation_list = cref.setdefault("citation_list", [])
                if citation_handle in citation_list:
                    return False
                citation_list.append(citation_handle)
                return "child link cited"
            return False

        result = await self._mutate("family", family_ref, edit, label="cited")
        if not found["hit"]:
            return {
                "error": "not_a_child",
                "message": f"Person {child_ref} is not a child of family {family_ref}.",
            }
        result["citation_handle"] = citation_handle
        result["child"] = child_ref
        return result

    async def uncite(
        self,
        object_type: str,
        ref: str,
        citation_ref: str,
        delete_if_orphan: bool = True,
        *,
        name: NameMatch | None = None,
        carry_to: str | None = None,
    ) -> dict:
        """Detach a citation from an object, and delete it if nothing else uses it.

        Detaching without deleting is how orphan citations accumulate: the fact
        they supported is gone, but the citation stays in the database looking
        like evidence of something. The default cleans up after itself; pass
        delete_if_orphan=False to keep the object deliberately.

        "Nothing else uses it" counts what points at the citation, but the
        citation can also hold notes and images -- a transcription, the page
        image -- that are reachable through nothing else. Deleting it would
        strand them, so then it is kept unless ``carry_to`` names a citation
        to move them to first.

        A name's citation is detached with ``object_type="name"``, ``ref``
        the person and ``name`` choosing the name.
        """
        citation_handle = await self._resolve_handle("citation", citation_ref)
        if object_type == "name" and name is None:
            return {
                "error": "name_required",
                "message": "object_type 'name' uncites one of a person's names: pass ref "
                "(the person) and name (which name).",
            }

        detached = {"done": False}

        def edit(obj: dict) -> str | bool:
            holder = obj
            if object_type == "name":
                _, holder = _pick_name(obj, name, alternates_only=False)
            citation_list = holder.get("citation_list") or []
            if citation_handle not in citation_list:
                return False
            holder["citation_list"] = [c for c in citation_list if c != citation_handle]
            detached["done"] = True
            return "citation detached"

        target_type = "person" if object_type == "name" else object_type
        result = await self._mutate(target_type, ref, edit, label="uncited")
        result["citation"] = citation_ref
        if not detached["done"]:
            result["message"] = f"Citation {citation_ref} was not attached to that {object_type}."
            return result

        remaining = await self.get_backlinks("citation", citation_handle)
        result["remaining_references"] = remaining["total_references"]
        if remaining["total_references"]:
            return result
        if not delete_if_orphan:
            result["citation_deleted"] = False
            result["message"] += (
                "; WARNING: this citation is now an orphan -- nothing "
                "references it. Delete it or re-attach it."
            )
            return result
        citation = await self._resolve("citation", citation_handle)
        outcome = await self._delete_keeping_evidence("citation", citation, carry_to)
        result["citation_deleted"] = outcome.pop("deleted")
        if result["citation_deleted"]:
            result.update({k: v for k, v in outcome.items() if k != "suffix"})
            result["message"] += "; citation was orphaned and has been deleted" + outcome.get(
                "suffix", ""
            )
        else:
            result["would_orphan"] = outcome["would_orphan"]
            result["message"] += (
                f"; the citation was KEPT although nothing cites it any more. "
                f"{outcome['message']} Pass carry_to=<citation> to move them and delete it."
            )
        return result

    # ------------------------------------------------------------------ #
    # structure editing
    # ------------------------------------------------------------------ #
    async def merge_objects(
        self,
        object_type: str,
        keep_ref: str,
        drop_ref: str,
        dry_run: bool = True,
        enclosures: str = "auto",
    ) -> dict:
        """Merge two objects using the server's native merge.

        The server re-points every reference and unions the subordinate lists
        inside one transaction. Hand-rolled merges drop data -- see
        ``docs/PITFALLS.md`` section 8.

        Whether to merge is a judgement call. A duplicate source pair makes a
        single-sourced fact look corroborated, so merging corrects the record;
        but an index entry and the register page it points to are two
        documents and must not be merged.

        Parameters
        ----------
        object_type : str
            Gramps object type. Must be one of the mergeable types.
        keep_ref : str
            Handle or gramps_id of the survivor.
        drop_ref : str
            Handle or gramps_id of the object merged away.
        dry_run : bool, optional
            Report what the merge would carry across without performing it.
        enclosures : {"auto", "keep_keeper", "keep_drop", "keep_both"}, optional
            Places only. The server gives the survivor every enclosure of both
            places, and two undated parents make the hierarchy ambiguous --
            Gramps reads a second enclosure as a dated alternative, and the
            first silently drives the title. ``auto`` drops an undated parent
            that encloses another undated parent (a state beside its own
            county), and refuses, before merging, if two unrelated undated
            parents would remain. The others keep the survivor's, the dropped
            place's, or both lists. Dated enclosures are kept in every case.

        Returns
        -------
        dict
            What the dropped object carries, and the outcome. An unsupported
            type returns an ``error`` key.
        """
        if object_type not in _MERGEABLE_TYPES:
            return {
                "error": "unsupported_type",
                "message": f"Cannot merge {object_type}. Mergeable: "
                f"{', '.join(sorted(_MERGEABLE_TYPES))}.",
            }
        keep = await self._resolve(object_type, keep_ref)
        drop = await self._resolve(object_type, drop_ref)
        if keep["handle"] == drop["handle"]:
            return {"error": "same_object", "message": "keep and drop are the same object."}

        drop_links = await self.get_backlinks(object_type, drop["handle"])
        plan = {
            "object_type": object_type,
            "keep": {"gramps_id": keep.get("gramps_id"), "label": _object_label(object_type, keep)},
            "drop": {"gramps_id": drop.get("gramps_id"), "label": _object_label(object_type, drop)},
            "references_moving_to_keep": drop_links["total_references"],
            "referenced_by": drop_links["referenced_by"],
            "drop_carries": {
                key: len(drop.get(key) or [])
                for key in (
                    "media_list",
                    "note_list",
                    "citation_list",
                    "tag_list",
                    "attribute_list",
                    "reporef_list",
                    "urls",
                )
                if drop.get(key)
            },
        }
        also, refusal = await self._merge_side_effects(object_type, keep, drop)
        if also:
            plan["also_merges"] = also
        if refusal:
            plan["refused"] = refusal
        final_refs: list[dict] | None = None
        if object_type == "place":
            if enclosures not in _ENCLOSURE_CHOICES:
                return {
                    "error": "unsupported_choice",
                    "message": f"enclosures must be one of: {', '.join(_ENCLOSURE_CHOICES)}.",
                }
            final_refs, report = await self._merged_enclosures(keep, drop, enclosures)
            plan["enclosures"] = report
            if report.get("ambiguous") and not dry_run:
                return {
                    "error": "ambiguous_enclosures",
                    **plan,
                    "message": "Not merged: the survivor would be enclosed by "
                    f"{', '.join(report['result'])}, none of which encloses another. "
                    "Choose with enclosures='keep_keeper', 'keep_drop' or 'keep_both'.",
                }
        if dry_run:
            plan["dry_run"] = True
            plan["message"] = (
                f"DRY RUN. Would merge {drop.get('gramps_id')} into "
                f"{keep.get('gramps_id')}, moving {drop_links['total_references']} "
                f"reference(s). Re-run with dry_run=False to apply."
            )
            if also:
                plan["message"] += " It would also merge " + _describe_cascade(also) + "."
            if refusal:
                plan["message"] += f" It would be refused: {refusal}"
            if (plan.get("enclosures") or {}).get("ambiguous"):
                plan["message"] += (
                    " It would be refused as it stands: two unrelated undated parents "
                    "would remain; choose with enclosures."
                )
            return plan

        if refusal:
            return {"error": "merge_refused", **plan, "message": f"Not merged: {refusal}"}
        await self.client.merge(object_type, keep["handle"], drop["handle"])
        plan["dry_run"] = False
        plan["message"] = (
            f"Merged {object_type} {drop.get('gramps_id')} into "
            f"{keep.get('gramps_id')}. The merge is one transaction -- "
            f"list_transactions + undo_transaction can reverse it."
        )
        if also:
            plan["message"] += " It also merged " + _describe_cascade(also) + "."
        if final_refs is not None:
            wanted = [_placeref_key(r) for r in final_refs]

            def settle(place: dict) -> str | bool:
                current = place.get("placeref_list") or []
                if [_placeref_key(r) for r in current] == wanted:
                    return False
                by_key = {_placeref_key(r): r for r in current}
                place["placeref_list"] = [by_key.get(_placeref_key(r), r) for r in final_refs]
                return "enclosures settled"

            settled = await self._mutate("place", keep["handle"], settle, label="updated")
            if settled.get("changed"):
                plan["message"] += (
                    " The survivor's enclosures were then set to "
                    f"{', '.join(plan['enclosures']['result']) or 'none'} (a separate "
                    "transaction)."
                )
        logger.info(
            "merged %s %s into %s", object_type, drop.get("gramps_id"), keep.get("gramps_id")
        )
        return plan

    async def _merge_side_effects(
        self, object_type: str, keep: dict, drop: dict
    ) -> tuple[list[dict], str | None]:
        """What else Gramps' merge would merge, and whether it refuses (PITFALLS 25).

        A person merge (``MergePersonQuery``, with the ``family_merger`` the
        server uses by default) then merges the first two of the survivor's
        families that have the same parents -- a family each record had with
        the same spouse. It refuses spouses, and a parent and their child.
        A family merge (``MergeFamilyQuery``) keeps the survivor's father and
        mother and merges a different one into each.

        Returns
        -------
        tuple
            The merges that would follow, each ``{"family"|"person": <kept id>,
            "absorbs": <dropped id>, "because": ...}``, and the reason Gramps
            would refuse, or None.
        """
        also: list[dict] = []
        if object_type == "person":
            refusal = _person_merge_refusal(keep, drop)
            if refusal:
                return also, refusal
            keep_fams = list(keep.get("family_list") or [])
            drop_fams = list(drop.get("family_list") or [])
            seen: dict[tuple, str | None] = {}
            for handle in keep_fams + [h for h in drop_fams if h not in keep_fams]:
                family = await self.client.get_object("family", handle)
                parents = tuple(
                    keep["handle"] if h == drop["handle"] else h
                    for h in (family.get("father_handle"), family.get("mother_handle"))
                )
                if parents in seen and handle in drop_fams:
                    also.append(
                        {
                            "family": seen[parents],
                            "absorbs": family.get("gramps_id"),
                            "because": "the two families would have the same parents",
                        }
                    )
                    break
                seen.setdefault(parents, family.get("gramps_id"))
        elif object_type == "family":
            for role in ("father", "mother"):
                mine, theirs = keep.get(f"{role}_handle"), drop.get(f"{role}_handle")
                if mine and theirs and mine != theirs:
                    kept = await self._resolve("person", mine)
                    dropped = await self._resolve("person", theirs)
                    refusal = _person_merge_refusal(kept, dropped)
                    if refusal:
                        return also, (
                            f"merging the families would merge their {role}s, "
                            f"{kept.get('gramps_id')} and {dropped.get('gramps_id')}, and "
                            + refusal[0].lower()
                            + refusal[1:]
                        )
                    also.append(
                        {
                            "person": kept.get("gramps_id"),
                            "absorbs": dropped.get("gramps_id"),
                            "because": f"the families have different {role}s",
                        }
                    )
        return also, None

    async def _merged_enclosures(
        self, keep: dict, drop: dict, choice: str
    ) -> tuple[list[dict], dict]:
        """The survivor's enclosures after a place merge, and a report of them.

        gramps-webapi merges with Gramps' ``Place.merge``, whose
        ``_merge_placeref_list`` appends every PlaceRef of the dropped place
        not equal to one the survivor has (Gramps 6.0).
        """
        keep_refs = list(keep.get("placeref_list") or [])
        drop_refs = list(drop.get("placeref_list") or [])
        union = keep_refs + [
            r
            for r in drop_refs
            if _placeref_key(r) not in {_placeref_key(k) for k in keep_refs}
            and r.get("ref") != keep["handle"]
        ]
        pruned: list[dict] = []
        if choice == "keep_keeper":
            final = keep_refs
        elif choice == "keep_drop":
            final = [r for r in drop_refs if r.get("ref") != keep["handle"]]
            final += [r for r in keep_refs if _date_string(r.get("date"))]
        elif choice == "keep_both":
            final = union
        else:
            undated = [r for r in union if not _date_string(r.get("date"))]
            ancestors = {
                r["ref"]: await self._place_ancestors(r["ref"]) for r in undated if r.get("ref")
            }
            coarser = {
                r["ref"]
                for r in undated
                for other in undated
                if other is not r and r.get("ref") in ancestors.get(other.get("ref"), set())
            }
            pruned = [
                r for r in union if r.get("ref") in coarser and not _date_string(r.get("date"))
            ]
            final = [r for r in union if r not in pruned]
        ids = await self._place_ids([r.get("ref") for r in keep_refs + drop_refs if r.get("ref")])

        def named(refs: list[dict]) -> list[str]:
            return [
                ids.get(r.get("ref"), r.get("ref"))
                + (f" ({_date_string(r.get('date'))})" if _date_string(r.get("date")) else "")
                for r in refs
            ]

        undated_left = [r for r in final if not _date_string(r.get("date"))]
        report = {
            "keeper": named(keep_refs),
            "dropped_place": named(drop_refs),
            "result": named(final),
            "ambiguous": choice == "auto" and len(undated_left) > 1,
        }
        if pruned:
            report["pruned_as_coarser"] = named(pruned)
        return final, report

    async def _place_ancestors(self, handle: str) -> set[str]:
        """Every place enclosing this one, through any enclosure, dated or not."""
        seen: set[str] = set()
        frontier = [handle]
        for _ in range(50):
            next_frontier = []
            for current in frontier:
                try:
                    place = await self.client.get_object(
                        "place", current, keys="handle,placeref_list"
                    )
                except GrampsApiError:
                    continue
                for ref in place.get("placeref_list") or []:
                    up = ref.get("ref")
                    if up and up not in seen:
                        seen.add(up)
                        next_frontier.append(up)
            if not next_frontier:
                break
            frontier = next_frontier
        return seen

    async def _place_ids(self, handles: list[str]) -> dict[str, str]:
        """gramps_ids for place handles, for a readable report."""
        wanted = list(dict.fromkeys(handles))
        if not wanted:
            return {}
        rows = await self.client.list_objects("place", handles=wanted, keys="handle,gramps_id")
        return {r["handle"]: r.get("gramps_id") or r["handle"] for r in rows}

    async def detach_object(
        self,
        parent_type: str,
        parent_ref: str,
        child_kind: str,
        child_ref: str,
        delete_if_orphan: bool = False,
        *,
        call_number: str | None = None,
    ) -> dict:
        """Remove a reference from an object's list (event, media, note, tag, child).

        The reference goes; the referenced object survives unless
        delete_if_orphan is set AND nothing else points at it. Deleting an
        object that other facts still reference leaves dangling handles, so the
        check is not optional; nor is the one for notes and images that only
        the deleted object held, which keeps it instead.

        A tag may be named rather than given by handle. A repository link can
        be narrowed to one ``call_number``, for a source held twice in the
        same repository. ``enclosure`` takes a parent off a place, which is
        how an extra undated parent left by a merge comes off. Detaching a
        child also removes every remaining link from the child to the family:
        gramps-webapi removes only the first of two, which left a one-sided
        link.
        """
        spec = _DETACH_SPECS.get(child_kind)
        if spec is None:
            return {
                "error": "unknown_kind",
                "message": f"child_kind must be one of: {', '.join(_DETACH_SPECS)}.",
            }
        if call_number is not None and child_kind != "repository":
            return {
                "error": "conflicting_arguments",
                "message": "call_number narrows a repository link; it means nothing for "
                f"child_kind {child_kind!r}.",
            }
        list_key, ref_type, by_ref = spec
        if child_kind == "enclosure" and (parent_type != "place" or delete_if_orphan):
            return {
                "error": "conflicting_arguments",
                "message": "child_kind 'enclosure' removes a parent from a place, and never "
                "deletes the parent: pass parent_type='place' and no delete_if_orphan.",
            }
        if child_kind == "tag":
            child_handle = await self._resolve_tag(child_ref)
        elif child_kind in ("parent_family", "family"):
            if delete_if_orphan:
                return {
                    "error": "conflicting_arguments",
                    "message": "Removing a person's link never deletes the family.",
                }
            if parent_type != "person":
                return {
                    "error": "conflicting_arguments",
                    "message": f"child_kind {child_kind!r} removes a link from a person; "
                    "to take a child out of a family, use child_kind='child' on the family.",
                }
            refusal, child_handle = await self._one_sided_family(parent_ref, child_ref, child_kind)
            if refusal:
                return refusal
        elif child_kind == "child":
            try:
                child_handle = await self._resolve_handle("person", child_ref)
            except NotFoundError:
                # A ChildRef to a person who no longer exists is detached by
                # the handle it holds.
                child_handle = child_ref
        else:
            child_handle = await self._resolve_handle(ref_type, child_ref)

        def matches(entry: Any) -> bool:
            if not by_ref:
                return entry == child_handle
            if entry.get("ref") != child_handle:
                return False
            return call_number is None or (entry.get("call_number") or "") == call_number

        detached = {"done": False}

        def edit(obj: dict) -> str | bool:
            entries = obj.get(list_key) or []
            kept = [e for e in entries if not matches(e)]
            if len(kept) == len(entries):
                return False
            obj[list_key] = kept
            detached["done"] = True
            return f"detached {child_kind}" + (
                f" ({len(entries) - len(kept)} links)" if len(entries) - len(kept) > 1 else ""
            )

        result = await self._mutate(parent_type, parent_ref, edit, label="detached from")
        result["detached"] = child_ref
        if not detached["done"]:
            result["message"] = f"{child_kind} {child_ref} was not attached to that {parent_type}."
            return result

        if child_kind == "child" and parent_type == "family":
            family_handle = result["handle"]

            def unlink(person: dict) -> str | bool:
                links = person.get("parent_family_list") or []
                kept = [f for f in links if f != family_handle]
                if len(kept) == len(links):
                    return False
                person["parent_family_list"] = kept
                return "link to the family removed"

            try:
                unlinked = await self._mutate("person", child_handle, unlink, label="unlinked")
            except NotFoundError:
                unlinked = {}
            if unlinked.get("changed"):
                result["message"] += "; the child's remaining link back to the family removed"

        if delete_if_orphan:
            remaining = await self.get_backlinks(ref_type, child_handle)
            result["remaining_references"] = remaining["total_references"]
            if remaining["total_references"]:
                result["deleted"] = False
                result["message"] += (
                    f"; kept the {ref_type}: {remaining['total_references']} other "
                    f"object(s) still reference it"
                )
                return result
            target = await self._resolve(ref_type, child_handle)
            outcome = await self._delete_keeping_evidence(ref_type, target, None)
            result["deleted"] = outcome.pop("deleted")
            if result["deleted"]:
                result.update({k: v for k, v in outcome.items() if k != "suffix"})
                result["message"] += f"; orphaned {ref_type} deleted" + outcome.get("suffix", "")
            else:
                result["would_orphan"] = outcome["would_orphan"]
                result["message"] += (
                    f"; kept the {ref_type}: {outcome['message']} Delete it with "
                    "delete_object(carry_to=...) once they have a home."
                )
        return result

    async def _one_sided_family(
        self, person_ref: str, family_ref: str, child_kind: str
    ) -> tuple[dict | None, str]:
        """Allow removing a person's link to a family only when it is one-sided.

        Returns
        -------
        tuple
            A refusal (or None) and the family handle to detach. A family that
            no longer exists is detached by the handle the person holds.
        """
        try:
            family = await self._resolve("family", family_ref)
        except NotFoundError:
            return None, family_ref
        person_handle = await self._resolve_handle("person", person_ref)
        if child_kind == "parent_family":
            held = any(c.get("ref") == person_handle for c in family.get("child_ref_list") or [])
            how = "detach_object(parent_type='family', child_kind='child')"
        else:
            held = person_handle in (family.get("father_handle"), family.get("mother_handle"))
            how = "set_family_parent without a person removes a parent"
        if held:
            return (
                {
                    "error": "two_sided_link",
                    "message": f"Family {family.get('gramps_id')} still holds its side of this "
                    "link, so removing only the person's side would leave it one-sided. "
                    f"Take the person out of the family instead: {how}.",
                },
                family["handle"],
            )
        return None, family["handle"]

    async def _resolve_tag(self, ref: str) -> str:
        """A tag's handle, from its handle or its exact name."""
        try:
            return (await self.client.get_object("tag", ref, keys="handle"))["handle"]
        except GrampsApiError as exc:
            if exc.status != 404:
                raise
        for tag in await self.client.list_objects("tag", keys="handle,name"):
            if (tag.get("name") or "") == ref:
                return tag["handle"]
        raise NotFoundError(f"No tag with handle or name '{ref}'.")

    async def add_media(
        self, file_path: str, description: str, dedup_by_checksum: bool = True
    ) -> dict:
        """Upload a file as a standalone Media object, reusing an existing one
        if the identical file is already in the tree.

        One image, one Media object. The same photograph uploaded once per
        person it depicts produces duplicates that are tedious to unpick later,
        so the default checks the md5 first; a reused object without a
        description is given this one.

        The file goes in the request that creates the object
        (:meth:`GrampsWebClient.create_media`), so the object holds the whole
        file or does not exist; one the server stored with another checksum
        or mime type is removed again (TOOL-REQUESTS #31). The description
        takes a second write, the create's body being the file.
        """
        path = Path(file_path).expanduser()
        if not path.exists():
            raise NotFoundError(f"Media file not found: {path}")
        mime = _media_mime(path)
        if mime is None:
            return {
                "error": "unsupported_media_type",
                "message": f"{path.name} is not an image, PDF, audio or video "
                "file, so it is not uploaded. Only those are genealogy media; "
                "anything else on disk -- a key, a .env -- stays there.",
            }
        content = path.read_bytes()
        checksum = hashlib.md5(content).hexdigest()  # noqa: S324 - matches Gramps' own

        if dedup_by_checksum:
            existing = await self.client.list_objects(
                "media", gql=f'checksum = "{checksum}"', keys="handle,gramps_id,desc"
            )
            if existing:
                hit = existing[0]
                # A retry after an upload whose description never landed.
                if description and not hit.get("desc"):
                    await self.update_media(hit["handle"], description=description)
                    hit["desc"] = description
                return {
                    "handle": hit["handle"],
                    "gramps_id": hit.get("gramps_id"),
                    "object_type": "media",
                    "created": False,
                    "checksum": checksum,
                    "message": f"This exact file is already in the tree as "
                    f"{hit.get('gramps_id')} ('{hit.get('desc') or 'no description'}'). "
                    f"Reusing it instead of uploading a duplicate.",
                }

        media = await self.client.create_media(content, mime)
        label = media.get("gramps_id") or media["handle"]
        stored = await self.client.get_object(
            "media", media["handle"], keys="handle,gramps_id,checksum,mime"
        )
        if (stored.get("checksum"), stored.get("mime")) != (checksum, mime):
            return await self._unwanted_media(label, media["handle"], path.name, stored)
        notes = []
        if media.get("late_error"):
            notes.append(
                f"the upload's request failed ({media['late_error']}) after the server had "
                "stored it; a look-up by checksum found it"
            )
        if description:
            try:
                await self.update_media(media["handle"], description=description)
            except (GrampsApiError, httpx.TransportError) as exc:
                logger.warning("media %s: description not set (%s)", label, type(exc).__name__)
                notes.append(
                    f"its description could not be set ({type(exc).__name__}): "
                    f"update_media(ref='{label}', description=...)"
                )
        return {
            "handle": media["handle"],
            "gramps_id": media.get("gramps_id"),
            "object_type": "media",
            "created": True,
            "checksum": checksum,
            **({"notes": notes} if notes else {}),
            "message": f"Uploaded {path.name} as media {label}"
            + "".join(f"; {n}" for n in notes)
            + ". Attach it with attach_media(media_ref=...).",
        }

    async def _unwanted_media(self, label: str, handle: str, name: str, stored: dict) -> dict:
        """Remove a new media object that does not hold the file sent.

        It is junk -- a request body, say, where the image should be -- that
        nothing references yet (TOOL-REQUESTS #31).
        """
        held = f"checksum {stored.get('checksum')}, mime {stored.get('mime')}"
        try:
            await self.client.delete_object("media", handle)
        except (GrampsApiError, httpx.TransportError):
            return {
                "error": "upload_mismatch",
                "written": True,
                "message": f"The server stored media {label}, which does not hold {name} "
                f"({held}), and it could not be removed again: delete it with "
                f"delete_object('media', '{label}'). Nothing was attached.",
            }
        return {
            "error": "upload_mismatch",
            "written": False,
            "message": f"The server stored a media object that does not hold {name} "
            f"({held}), so it was removed again. Nothing was attached; retrying is safe.",
        }

    # ------------------------------------------------------------------ #
    # ocr_media: the router
    # ------------------------------------------------------------------ #
    async def ocr_media(
        self,
        ref: str,
        lang: str = "eng",
        output_format: str = "string",
        doc_type: DocType | str = DocType.print,
        engine: OcrEngine | str = OcrEngine.auto,
        second_witness: bool = False,
        spend_credits: bool = False,
        store: bool = False,
        page: int = 1,
        region: list[float] | None = None,
    ) -> dict:
        """Read the text of a document image with the engine that suits it.

        The route is chosen by what the document is and its language
        (:mod:`gramps_evidence_mcp.ocr` gives the evidence for each):

        ========================  ==============================================
        print                     Text the media already carries -- a
                                  Transcript note, a PDF's text layer -- or the
                                  Library of Congress's or the Internet
                                  Archive's OCR for a page its sources name;
                                  otherwise Gramps Web's Tesseract.
        hand, English             The image, returned for the calling model to
                                  transcribe diplomatically; Transkribus as a
                                  second witness when asked.
        hand, German              Transkribus only. Never a vision read, even
                                  when asked.
        hand, Norwegian / other   Transkribus with the image for a cross-check;
                                  without Transkribus, the image and a warning.
        table                     Not read: FamilySearch's index.
        volume                    FamilySearch Full-Text Search, then
                                  Transkribus page by page.
        ========================  ==============================================

        ``engine`` other than ``auto`` asks for one reader outright.

        A transcript is never the evidence; the citation stays on the image.
        ``store`` keeps a machine reading as a Transcript note on the media
        object -- with Transkribus' PAGE XML in a second note, since
        Transkribus deletes it a day after the job -- through ``_mutate()``.

        Parameters
        ----------
        ref : str
            Handle or gramps_id of the media object.
        lang : str, optional
            Tesseract-style code: ``eng``, ``deu``, ``nor``; ``+`` joins several.
        output_format : str, optional
            Tesseract's output format, for the Tesseract route only.
        doc_type, engine : str, optional
            What the document is, and which reader to use.
        second_witness : bool, optional
            Also run Transkribus beside a vision read, to compare.
        spend_credits : bool, optional
            Consent to Transkribus credits for this call, past any budget.
        store : bool, optional
            Keep a machine reading on the media object as a Transcript note.
        page : int, optional
            Page of a PDF, from 1.
        region : list of float, optional
            ``[x1, y1, x2, y2]`` in percent: the part of the page to return.

        Returns
        -------
        dict
            The text and its ``provenance`` (``engine``, ``model``, ``date``),
            or the image under ``_image_jpeg`` with an ``instruction``, or
            guidance; ``error`` when no route can read the media.
        """
        doc_type = DocType(doc_type)
        engine = OcrEngine(engine)
        if refusal := ocr.region_refusal(region):
            return {"error": "bad_region", "message": refusal}
        if page < 1:
            return {"error": "bad_page", "message": "page counts from 1."}
        media = await self._resolve("media", ref, backlinks=True)
        doc = _OcrDocument(self, media, page)
        base = ocr.base_language(lang)
        route = doc_type.value
        if doc_type is DocType.hand:
            route = {"eng": "hand_english", "deu": "hand_german", "nor": "hand_norwegian"}.get(
                base, "hand_other"
            )
        result: dict[str, Any] = {
            "media": media.get("gramps_id"),
            "handle": media["handle"],
            "description": media.get("desc"),
            "mime": media.get("mime"),
            "lang": lang,
            "doc_type": doc_type.value,
            "route": route,
        }
        try:
            if doc.is_pdf:
                result["page"] = page
                result["pages"] = (await doc.pdf()).pages
            transcripts = await doc.transcripts()
            if transcripts:
                result["existing_transcripts"] = [
                    {k: v for k, v in t.items() if k != "text"} for t in transcripts
                ]
            await self._ocr_route(
                doc,
                result,
                transcripts,
                engine,
                route,
                lang,
                base,
                output_format,
                second_witness,
                spend_credits,
                store,
                region,
            )
        except ocr.ImageError as exc:
            return {
                **result,
                "error": "unreadable_media",
                "message": f"Media {doc.gid} cannot be read: {exc}.",
            }

        if "error" in result:
            return result
        if store:
            result["stored"] = await self._store_reading(doc, result)
        result.pop("_page_xml", None)
        result["caveat"] = (
            "A machine reading is a finding aid, never the evidence: the citation stays "
            "on the image. Read the image before citing anything any engine says."
        )
        logger.info(
            "ocr_media %s route=%s engine=%s", media.get("gramps_id"), route, result.get("engine")
        )
        return result

    async def _ocr_route(
        self,
        doc: _OcrDocument,
        result: dict,
        transcripts: list[dict],
        engine: OcrEngine,
        route: str,
        lang: str,
        base: str,
        output_format: str,
        second_witness: bool,
        spend_credits: bool,
        store: bool,
        region: list[float] | None,
    ) -> None:
        """Send the media down the route ``ocr_media`` chose, filling ``result``."""
        if engine is OcrEngine.existing:
            await self._ocr_existing(doc, result, transcripts)
        elif engine is OcrEngine.tesseract:
            await self._ocr_tesseract(doc, result, lang, output_format, route)
        elif engine is OcrEngine.vision:
            if base == "deu" and route != "print":
                result.update(_GERMAN_VISION_REFUSAL)
                return
            await self._ocr_vision(doc, result, region, base, second_witness, spend_credits)
        elif engine is OcrEngine.transkribus:
            await self._ocr_transkribus_only(doc, result, base, spend_credits, store)
        elif route == "print":
            if not await self._ocr_existing(doc, result, transcripts, quiet=True):
                await self._ocr_tesseract(doc, result, lang, output_format, route)
        elif route == "hand_english":
            await self._ocr_vision(doc, result, region, base, second_witness, spend_credits)
        elif route == "hand_german":
            await self._ocr_transkribus_only(doc, result, base, spend_credits, store)
        elif route in ("hand_norwegian", "hand_other"):
            await self._ocr_transkribus_with_image(doc, result, region, base, spend_credits, store)
        elif route == "table":
            result["engine"] = None
            result["text"] = None
            result["guidance"] = ocr.TABLE_GUIDANCE
            if base != "deu":
                result["guidance"] += " " + ocr.TABLE_LOOK
        else:  # volume
            result["engine"] = None
            result["text"] = None
            result["guidance"] = ocr.VOLUME_GUIDANCE.format(
                credits=ocr.CREDITS_PER_PAGE, eur=ocr.EUR_PER_PAGE
            )
            if not self.config.transkribus_configured:
                result["guidance"] += " (Transkribus is not configured on this server.)"

    async def _ocr_existing(
        self, doc: _OcrDocument, result: dict, transcripts: list[dict], *, quiet: bool = False
    ) -> bool:
        """Text the media already carries, or that an archive its sources name holds.

        In order: a Transcript note on the media (for a PDF of several pages,
        only one this tool stored for this page), the PDF's own text layer,
        and the Library of Congress's or the Internet Archive's OCR for a page
        a URL names. Returns whether any was found; when ``quiet`` is false,
        finding none is reported in ``result``.
        """
        layer = await doc.pdf()
        single = layer is None or layer.pages <= 1
        usable = [t for t in transcripts if single or t.get("page") == doc.page]
        if usable:
            chosen = usable[0]
            result.update(
                engine="existing",
                text=chosen["text"],
                provenance=chosen.get("provenance")
                or {"engine": "transcript_note", "model": None, "date": chosen.get("changed")},
                source_note=chosen["note"],
            )
            return True
        if layer is not None and layer.text and len(layer.text) < _PDF_LAYER_MIN_CHARS:
            result.setdefault("warnings", []).append(
                f"The PDF's text layer holds only {len(layer.text)} characters, beginning "
                f"{layer.text[:60]!r}: too little to be the page's text, so it is not used. "
                "A website's download often carries just a caption."
            )
        if layer is not None:
            if len(layer.text) >= _PDF_LAYER_MIN_CHARS:
                result.update(
                    engine="existing",
                    text=layer.text,
                    provenance={
                        "engine": "pdf_text_layer",
                        "model": "the PDF's own text layer",
                        "date": ocr.today(),
                    },
                )
                return True
        # An archive URL names one page; a PDF of several cannot say which is it.
        urls = [] if not single else await self._archive_urls(doc.media)
        looked: list[dict] = []
        if urls:
            async with ocr.archive_client() as http:
                for url in urls[:_ARCHIVE_URL_LIMIT]:
                    lookup = await ocr.archive_text(http, url, ocr.today())
                    if lookup.found:
                        found = lookup.found
                        result.update(
                            engine="existing", text=found.text, provenance=found.provenance
                        )
                        if found.note:
                            result.setdefault("warnings", []).append(found.note)
                        if looked:
                            result["looked_at"] = looked
                        return True
                    looked.append({"url": url, "reason": lookup.reason})
        if looked:
            result["looked_at"] = looked
        if not quiet:
            result.update(
                engine=None,
                text=None,
                message="No Transcript note is attached to this media object"
                + (", its PDF has no text layer of a page's length" if layer else "")
                + ", and no Library of Congress or Internet Archive page named in its "
                "sources has OCR text. Read it with engine='tesseract' (print in an image "
                "file), engine='vision' or engine='transkribus'.",
            )
        return False

    async def _archive_urls(self, media: dict) -> list[str]:
        """Library of Congress and Internet Archive URLs the media and its sources name.

        Looked for in the media's description, path and attributes; in every
        source holding the media (its attributes, title and publication
        information); and in every citation holding it (its page and
        attributes) and that citation's source.
        """
        texts: list[str] = [media.get("desc") or "", media.get("path") or ""]
        texts += [str(a.get("value") or "") for a in media.get("attribute_list") or []]
        backlinks = media.get("backlinks") or {}
        source_handles = list(backlinks.get("source") or [])
        for handle in backlinks.get("citation") or []:
            with contextlib.suppress(GrampsApiError):
                citation = await self.client.get_object("citation", handle)
                texts.append(citation.get("page") or "")
                texts += [str(a.get("value") or "") for a in citation.get("attribute_list") or []]
                if citation.get("source_handle"):
                    source_handles.append(citation["source_handle"])
        for handle in dict.fromkeys(source_handles):
            with contextlib.suppress(GrampsApiError):
                source = await self.client.get_object("source", handle)
                texts += [str(a.get("value") or "") for a in source.get("attribute_list") or []]
                texts += [source.get("pubinfo") or "", source.get("title") or ""]
        urls = [u for u in ocr.find_urls(texts) if ocr.archive_kind(u)]
        # A URL naming a page comes before one naming a whole item.
        return sorted(urls, key=lambda u: 0 if ("sp=" in u or "/page/" in u or "seq-" in u) else 1)

    async def _ocr_tesseract(
        self, doc: _OcrDocument, result: dict, lang: str, output_format: str, route: str
    ) -> None:
        """Gramps Web's Tesseract, which reads an image file's print."""
        if not (doc.mime or "").startswith("image/"):
            result.update(
                error="tesseract_cannot_read",
                message=f"Gramps Web's Tesseract reads image files only, and this is "
                f"{doc.mime or 'not an image'}: it would answer with nothing. "
                + (
                    "This PDF has no usable text layer either. Read the page with "
                    "engine='vision', or engine='transkribus'."
                    if doc.is_pdf
                    else "Nothing here can read it."
                ),
            )
            return
        tess_lang = ocr.tesseract_language(lang)
        meta = await self.client.metadata()
        server = meta.get("server") if isinstance(meta, dict) else None
        if isinstance(server, dict) and "ocr" in server:
            if not server.get("ocr"):
                result.update(
                    error="tesseract_unavailable",
                    message="This Gramps Web server has no Tesseract (its metadata says "
                    "ocr: false). Read the page with engine='vision'.",
                )
                return
            installed = set(server.get("ocr_languages") or [])
            missing = [p for p in tess_lang.split("+") if installed and p not in installed]
            if missing:
                result.update(
                    error="language_not_installed",
                    message=f"Tesseract on this server has no {', '.join(missing)} model. "
                    f"It has: {', '.join(sorted(installed))}.",
                )
                return
        try:
            text = await self.client.ocr_media(
                doc.handle, lang=tess_lang, output_format=output_format
            )
        except GrampsApiError as exc:
            if exc.status == 501:
                result.update(
                    error="tesseract_unavailable",
                    message=f"Gramps Web cannot run OCR: {exc.detail}. Read the page "
                    "with engine='vision'.",
                )
                return
            raise
        result.update(
            engine="tesseract",
            text=text,
            provenance={
                "engine": "tesseract",
                "model": "Tesseract, through Gramps Web",
                "lang": tess_lang,
                "date": ocr.today(),
            },
        )
        if route != "print":
            result.setdefault("warnings", []).append(
                "Tesseract reads print. On handwriting its output is mostly noise."
            )
        elif isinstance(text, str) and not text.strip():
            result.setdefault("warnings", []).append(
                "Tesseract found no text. If the page is handwritten, call again with "
                "doc_type='hand'."
            )

    async def _ocr_vision(
        self,
        doc: _OcrDocument,
        result: dict,
        region: list[float] | None,
        base: str,
        second_witness: bool,
        spend_credits: bool,
    ) -> None:
        """Return the image for the calling model to transcribe diplomatically."""
        if not await self._attach_image(doc, result, region):
            return
        result.update(engine="vision", text=None, instruction=ocr.DIPLOMATIC_INSTRUCTION)
        result["after_reading"] = ocr.KEEPING_A_READING
        if base != "eng" and result.get("doc_type") == DocType.hand.value:
            name = ocr.LANGUAGE_NAMES.get(base, base)
            rate = ocr.CER_VISION_NORWEGIAN if base == "nor" else None
            result.setdefault("warnings", []).append(
                f"A vision read of {name} handwriting is error-prone: "
                + (
                    f"the best model measured {rate} character error on Norwegian "
                    "(METATR 2026), about one character in ten. "
                    if rate
                    else "no published benchmark measures it for this language; on "
                    "Norwegian the best model is wrong on one character in ten. "
                )
                + "Check every name, date and number letter by letter against the image."
            )
        if second_witness:
            witness = await self._transkribus(doc, base, spend_credits, store=False)
            if witness.get("text") is not None:
                result["witnesses"] = [
                    {
                        "engine": "transkribus",
                        "text": witness["text"],
                        "provenance": witness["provenance"],
                    }
                ]
                result["instruction"] += " " + ocr.COMPARE_WITNESSES
            result["transkribus"] = witness["transkribus"]
            if witness.get("credits"):
                result["credits"] = witness["credits"]

    async def _ocr_transkribus_only(
        self, doc: _OcrDocument, result: dict, base: str, spend_credits: bool, store: bool
    ) -> None:
        """Transkribus, and nothing else: German handwriting, or when asked for."""
        reading = await self._transkribus(doc, base, spend_credits, store)
        result["transkribus"] = reading["transkribus"]
        if reading.get("credits"):
            result["credits"] = reading["credits"]
        if reading.get("text") is not None:
            result.update(engine="transkribus", text=reading["text"])
            result["provenance"] = reading["provenance"]
            if reading.get("xml"):
                result["_page_xml"] = reading["xml"]
            result.setdefault("warnings", []).append(
                f"{reading['provenance']['model']}: {reading['covers']}. Check every name, "
                "date and number against the image."
            )
            return
        status = reading["transkribus"]
        if status.get("status") == "RUNNING":
            result.update(engine="transkribus", text=None)
            return
        german = result["route"] == "hand_german"
        code = {
            "not_configured": "transkribus_required",
            "needs_consent": "spend_not_approved",
            "private": "private_media",
            "no_model": "no_transkribus_model",
        }.get(status.get("reason_code", ""), "transkribus")
        message = status.get("message", "Transkribus did not read the page.")
        if german:
            message = (
                "German handwriting (Kurrent, Sütterlin) is read here by Transkribus only: "
                f"vision models measure {ocr.CER_VISION_GERMAN} character error on historical "
                "German (METATR 2026, READ-2016), so a vision read is not offered as a "
                f"fallback. {message} Meanwhile FamilySearch's index may hold the entry "
                "(get_records_on_image)."
            )
        result.update(error=code, message=message)

    async def _ocr_transkribus_with_image(
        self,
        doc: _OcrDocument,
        result: dict,
        region: list[float] | None,
        base: str,
        spend_credits: bool,
        store: bool,
    ) -> None:
        """Transkribus' reading with the image to check it; the image alone without it."""
        reading = await self._transkribus(doc, base, spend_credits, store)
        result["transkribus"] = reading["transkribus"]
        if reading.get("credits"):
            result["credits"] = reading["credits"]
        if reading.get("text") is None:
            await self._ocr_vision(doc, result, region, base, False, spend_credits)
            if "error" not in result:
                result["warnings"].append(
                    "Transkribus did not read this page: "
                    + reading["transkribus"].get("message", "")
                )
            return
        if not await self._attach_image(doc, result, region):
            return
        result.update(engine="transkribus", text=reading["text"])
        result["provenance"] = reading["provenance"]
        if reading.get("xml"):
            result["_page_xml"] = reading["xml"]
        result["instruction"] = (
            "Check the Transkribus reading against the image. Read the image yourself, "
            "diplomatically as below, and list every name, date and number where your "
            "reading and Transkribus' differ; settle each from the image or mark it [?]. "
            + ocr.DIPLOMATIC_INSTRUCTION
        )

    async def _attach_image(
        self, doc: _OcrDocument, result: dict, region: list[float] | None
    ) -> bool:
        """Put the page image, scaled for a vision model, under ``_image_jpeg``."""
        try:
            img, how = await doc.page_pil()
            original = img.size
            jpeg, size = ocr.vision_jpeg(ocr.crop_region(img, region))
        except ocr.ImageError as exc:
            result.update(
                error="not_an_image",
                message=f"Media {doc.gid} could not be read as an image: {exc}.",
            )
            return False
        result["_image_jpeg"] = jpeg
        result["image"] = {
            "media": doc.gid,
            "page": doc.page if doc.is_pdf else None,
            "region": region,
            "sent": list(size),
            "original": list(original),
            "from": how,
        }
        result.setdefault("warnings", [])
        if region is None and max(original) > max(size) * 1.5:
            result["warnings"].append(
                f"The page is {original[0]}x{original[1]} and was scaled to "
                f"{size[0]}x{size[1]}. Where the writing is too small to read, call again "
                "with region=[x1, y1, x2, y2] (percent) to see that part at full detail."
            )
        return True

    async def _transkribus(
        self, doc: _OcrDocument, base: str, spend_credits: bool, store: bool
    ) -> dict:
        """Run, or reuse, a Transkribus job on the page, within the caller's consent.

        A page is sent only when the caller passed ``spend_credits`` or the
        month's page budget has room; a job submitted for the same file, page
        and model within the day Transkribus keeps results is fetched again
        rather than paid for again. A private media object is never sent.

        Returns
        -------
        dict
            ``transkribus`` (what happened), and when it read the page
            ``text``, ``provenance``, ``covers``, ``credits`` and, if asked
            to store, ``xml``.
        """
        model = ocr.transkribus_model(base)
        cfg = self.config
        if not cfg.transkribus_configured:
            return {
                "transkribus": {
                    "ran": False,
                    "reason_code": "not_configured",
                    "message": "Transkribus is not configured on this server: set "
                    "GRAMPS_MCP_TRANSKRIBUS_USERNAME and GRAMPS_MCP_TRANSKRIBUS_PASSWORD "
                    "(a Scholar account or above).",
                }
            }
        if doc.media.get("private"):
            return {
                "transkribus": {
                    "ran": False,
                    "reason_code": "private",
                    "message": f"Media {doc.gid} is private, and a private record is never "
                    "sent to a third party.",
                }
            }
        if model is None:
            return {
                "transkribus": {
                    "ran": False,
                    "reason_code": "no_model",
                    "message": f"No Transkribus model here covers {base!r}: German and "
                    "Norwegian have their own, and Text Titan II covers "
                    f"{', '.join(sorted(ocr.TEXT_TITAN_LANGUAGES))}. No page was sent.",
                }
            }
        now = datetime.now(UTC)
        path = cfg.transkribus_ledger
        key = f"{doc.handle}:{doc.media.get('checksum') or ''}:{doc.page}:{model.id}"
        async with self._ledger_lock:
            ledger = ocr.Ledger.load(path)
            job, used = ledger.job(key, now), ledger.pages_in(now)
        credits: dict[str, Any] = {
            "pages_sent": 0,
            "estimated_credits": 0.0,
            "pages_this_month": used,
            "monthly_page_budget": cfg.transkribus_page_budget,
            "basis": f"about {ocr.CREDITS_PER_PAGE} credits (EUR {ocr.EUR_PER_PAGE}) a page: "
            "the app's 1 credit a page, at the API's half rate. Transkribus reports no "
            "charge; the account shows the balance.",
        }

        def not_run(code: str, message: str, **extra: Any) -> dict:
            return {
                "transkribus": {"ran": False, "reason_code": code, "message": message, **extra},
                "credits": credits,
            }

        def no_consent(used: int) -> dict:
            budget = (
                f"This month's budget of {cfg.transkribus_page_budget} pages is used up "
                f"({used} sent)."
                if cfg.transkribus_page_budget
                else "No monthly page budget is set (GRAMPS_MCP_TRANSKRIBUS_PAGE_BUDGET), "
                "so every page needs consent."
            )
            return not_run(
                "needs_consent",
                f"Reading this page with Transkribus ({model.name}) costs about "
                f"{ocr.CREDITS_PER_PAGE} credits (EUR {ocr.EUR_PER_PAGE}). {budget} Call "
                "again with spend_credits=true to pay for it.",
            )

        client = self._transkribus_client()
        state: dict | None = None
        process_id: Any = None
        try:
            if job is not None:
                process_id = job["process_id"]
                try:
                    state = await client.wait(process_id)
                    credits["reused_job"] = process_id
                except ocr.TranskribusError as exc:
                    if exc.status != 404:
                        raise
                    # Transkribus no longer has it: forget it, and read afresh.
                    await self._ledger_update(path, lambda ledger: ledger.forget(key))
            if state is None:
                if not (spend_credits or used < cfg.transkribus_page_budget):
                    return no_consent(used)
                data, mime, _how = await doc.page_image()
                image = ocr.transkribus_image(data, mime)
                # Counted before it is sent, under a lock: a page the ledger
                # cannot record is never sent, and two calls cannot both take
                # the budget's last page.
                async with self._ledger_lock:
                    ledger = ocr.Ledger.load(path)
                    used = ledger.pages_in(now)
                    if not (spend_credits or used < cfg.transkribus_page_budget):
                        return no_consent(used)
                    try:
                        ledger.reserve(key, model.id, now)
                    except OSError as exc:
                        return not_run(
                            "ledger",
                            f"The page count could not be written to {path} "
                            f"({type(exc).__name__}), so no page was sent: a page that is "
                            "not counted could be paid for again.",
                        )
                try:
                    submitted = await client.submit(image, model.id)
                except ocr.TranskribusError:
                    # Refused, so not charged: give the page back.
                    await self._ledger_update(path, lambda ledger: ledger.release(key, now))
                    raise
                process_id = submitted["processId"]
                await self._ledger_update(path, lambda ledger: ledger.assign(key, process_id))
                credits.update(
                    pages_sent=1, estimated_credits=ocr.CREDITS_PER_PAGE, pages_this_month=used + 1
                )
                logger.info("transkribus job %s submitted for media %s", process_id, doc.gid)
                state = await client.wait(process_id)
        except ocr.TranskribusError as exc:
            message = exc.message
            if exc.status == 429:
                message = (
                    f"Transkribus refused the job: {exc.message} The account's credits "
                    "are used up, or a usage limit applies."
                )
            return not_run("api", message, status_code=exc.status)
        except ocr.ImageError as exc:
            return not_run("image", f"The page could not be prepared for Transkribus: {exc}.")
        except (httpx.HTTPError, ValueError, KeyError) as exc:
            detail = f": {exc}" if str(exc) else ""
            return not_run(
                "network",
                f"Transkribus could not be reached, or answered in a way it should not "
                f"({type(exc).__name__}{detail}).",
            )
        status = str(state.get("status") or "").upper()
        info = {"ran": True, "status": status, "process_id": process_id, "model": model.id}
        if status != "FINISHED":
            if status in ("FAILED", "CANCELLED"):
                # Not kept: a failed job must not stand in for a fresh one.
                await self._ledger_update(path, lambda ledger: ledger.forget(key))
                info["message"] = f"Transkribus job {process_id} ended {status}."
            else:
                info["status"] = "RUNNING"
                info["message"] = (
                    f"Transkribus job {process_id} is still {status or 'running'}. Call "
                    "ocr_media again with the same arguments: the job is remembered for a "
                    "day and is not paid for twice."
                )
            return {"transkribus": info, "credits": credits}
        provenance = {
            "engine": "transkribus",
            "model": model.name,
            "model_id": model.id,
            "date": ocr.today(now),
            "process_id": process_id,
        }
        out = {
            "transkribus": info,
            "text": ocr.content_text(state.get("content")),
            "provenance": provenance,
            "covers": model.covers,
            "credits": credits,
        }
        if store:
            try:
                out["xml"] = await client.page_xml(process_id)
            except (ocr.TranskribusError, httpx.HTTPError) as exc:
                info["xml_message"] = f"The PAGE XML could not be fetched: {exc}"
        return out

    async def _ledger_update(self, path: Path, change: Any) -> None:
        """Apply one change to the Transkribus ledger, read afresh, under its lock.

        A failure to write after a page was sent is logged, not raised: the
        reading it paid for still reaches the caller.
        """
        async with self._ledger_lock:
            try:
                change(ocr.Ledger.load(path))
            except OSError:
                logger.warning("the Transkribus ledger could not be written")

    def _transkribus_client(self) -> ocr.TranskribusClient:
        """The Transkribus client, made on first use and kept for its token."""
        if self._transkribus_api is None:
            cfg = self.config
            self._transkribus_api = ocr.TranskribusClient(
                cfg.transkribus_username or "",
                cfg.transkribus_password or "",
                cfg.transkribus_api_url,
            )
        return self._transkribus_api

    async def _store_reading(self, doc: _OcrDocument, result: dict) -> dict:
        """Keep a machine reading on the media object as a Transcript note.

        One note holds the text under a header line naming the engine, model
        and date; a second, preformatted, holds Transkribus' PAGE XML, which
        Transkribus deletes a day after the job. Both are attached in one
        write through ``_mutate()``, and the attachment is re-read. A private
        media object's notes are private. A reading the media already holds
        is not stored twice.
        """
        xml = result.pop("_page_xml", None)
        provenance = result.get("provenance") or {}
        text = result.get("text")
        if result.get("engine") == "vision":
            return {
                "stored": False,
                "message": "Nothing to store: the reading is yours to make. "
                + ocr.KEEPING_A_READING,
            }
        if not isinstance(text, str) or not text.strip():
            return {"stored": False, "message": "No machine reading to store."}
        if provenance.get("engine") == "transcript_note" or result.get("source_note"):
            return {"stored": False, "message": "The reading is already a note on the media."}
        for existing in await doc.transcripts():
            if existing["text"].strip() == text.strip():
                return {
                    "stored": False,
                    "note": existing["note"],
                    "message": f"This reading is already stored, in note {existing['note']}.",
                }
        note_type = await self._canonical_type("note_types", "Transcript")
        private = bool(doc.media.get("private"))
        stored_provenance = {**provenance}
        if doc.is_pdf:
            stored_provenance["page_of"] = (doc.page, doc.pages)
        body = ocr.header(stored_provenance) + "\n\n" + text.strip()
        payloads = [
            {
                "_class": "Note",
                "text": {"string": body},
                "type": note_type,
                "format": 0,
                "private": private,
            }
        ]
        if xml:
            payloads.append(
                {
                    "_class": "Note",
                    "text": {"string": ocr.xml_with_provenance(xml, provenance)},
                    "type": note_type,
                    "format": 1,
                    "private": private,
                }
            )
        notes = [await self.client.create_object("note", p) for p in payloads]
        handles = [n["handle"] for n in notes]

        def edit(obj: dict) -> str:
            obj.setdefault("note_list", []).extend(handles)
            return f"{len(handles)} transcript note{'s' * (len(handles) > 1)} attached"

        await self._mutate("media", doc.handle, edit, label="transcribed")
        verified = all(
            [await self._verify_in_list("media", doc.handle, "note_list", h) for h in handles]
        )
        return {
            "stored": True,
            "notes": [n.get("gramps_id") for n in notes],
            "verified": verified,
            "message": (
                f"Stored the reading as note {notes[0].get('gramps_id')}"
                + (f" and its PAGE XML as {notes[1].get('gramps_id')}" if xml else "")
                + f" on media {doc.gid}."
                if verified
                else "The notes were created but did NOT attach to the media: re-read it."
            ),
        }

    # ------------------------------------------------------------------ #
    # ops: backup, change log, undo
    # ------------------------------------------------------------------ #
    async def export_backup(self, dest_path: str | None = None, extension: str = "gramps") -> dict:
        """Write a full-tree export to disk. Take one before any bulk write.

        'gramps' (Gramps XML) is the lossless format and the right choice for a
        safety dump; ged/json/csv exist for interchange. A dump costs seconds.

        The file is created, never replaced: the path comes from the model,
        and an export written over an existing file -- an earlier backup, or
        anything else on disk -- would destroy it. A path in a directory that
        does not exist is refused rather than created.
        """
        if extension not in _EXPORT_FORMATS:
            return {
                "error": "unsupported_format",
                "message": f"export_format must be one of: {', '.join(_EXPORT_FORMATS)}.",
            }
        if dest_path:
            target = Path(dest_path).expanduser()
            if not target.parent.is_dir():
                return {
                    "error": "no_such_directory",
                    "message": f"{target.parent} is not a directory. Pass a path "
                    "in an existing directory, or omit dest_path.",
                }
        else:
            stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
            target = self.config.cache_dir / "backups" / f"tree-{stamp}.{extension}"
            target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            return {
                "error": "file_exists",
                "message": f"{target} already exists and is left untouched. Choose a new name.",
            }
        data = await self.client.export_file(extension, compress=False)
        try:
            with target.open("xb") as fh:
                fh.write(data)
        except FileExistsError:
            return {
                "error": "file_exists",
                "message": f"{target} appeared while the export ran and is left "
                "untouched. Choose a new name.",
            }
        logger.info("wrote backup %s (%d bytes)", target, len(data))
        return {
            "path": str(target),
            "bytes": len(data),
            "format": extension,
            "message": f"Wrote a {len(data):,}-byte {extension} export to {target}.",
        }

    async def list_transactions(self, limit: int = 20) -> dict:
        """Recent writes, newest first: who changed what, and in which transaction.

        The transaction id is what undo_transaction takes.
        """
        txns = await self.client.transactions(page=1, pagesize=limit, sort="-id")
        rows = []
        for t in txns:
            conn = t.get("connection") or {}
            user = (conn.get("user") or {}).get("name")
            rows.append(
                {
                    "transaction_id": t.get("id"),
                    "description": t.get("description"),
                    "user": user,
                    "timestamp": _iso_from_epoch(t.get("timestamp")),
                    "change_count": len(t.get("changes") or []),
                    "is_undo": t.get("undo", False),
                }
            )
        return {"count": len(rows), "transactions": rows}

    async def record_history(
        self, object_type: str, ref: str, limit: int = 20, field: str | None = None
    ) -> dict:
        """Who added, edited or deleted one record, and when, newest first.

        gramps-webapi 3.22 serves one record's changes. 3.21 does not, but
        its transaction log names the record each change touched, so there
        the log is read instead, newest first, until the record's creation
        (TOOL-REQUESTS #29): slow on a large log, and the result says it was
        used. A record deleted and later restored by an undo is read back to
        the restore.

        Parameters
        ----------
        object_type : str
            Gramps object type.
        ref : str
            Handle or gramps_id. A deleted record has no gramps_id to look up,
            so its history is reached by handle.
        limit : int, optional
            The most recent changes to return.
        field : str, optional
            A field of the stored record, dotted for a nested one
            (``parent_family_list``, ``primary_name.first_name``): only the
            changes that changed its value are reported, each with the value
            before and after.

        Returns
        -------
        dict
            The record, its total number of changes, and the latest ones,
            each with its transaction, user and time.

        Raises
        ------
        NotFoundError
            If the record neither exists nor has any history.
        """
        if object_type not in ENDPOINTS:
            return {
                "error": "unsupported_type",
                "message": f"Unknown object type {object_type!r}. "
                f"Known: {', '.join(sorted(ENDPOINTS))}.",
            }
        path = _field_path(field) if field else None
        if field and not path:
            return {
                "error": "unsupported_field",
                "message": f"{field!r} is not a field path: name a field of the stored "
                "record, dotted for a nested one, e.g. 'parent_family_list' or "
                "'primary_name.first_name'.",
            }
        try:
            obj = await self._resolve(object_type, ref)
            handle, gramps_id = obj["handle"], obj.get("gramps_id")
        except NotFoundError:
            handle, gramps_id = ref, None
        from_log: dict | None = None
        try:
            if path is None:
                changes, total = await self.client.object_history(
                    object_type, handle, pagesize=max(1, min(limit, 200))
                )
            else:
                # A change no transaction covers is a write rolled back at its
                # commit (docs/PITFALLS.md sections 6 and 30): it did not happen.
                changes = [
                    c
                    for c in await self._all_object_changes(object_type, handle)
                    if c.get("transaction_id") is not None
                ]
                total = len(changes)
        except UnsupportedServerError:
            changes, from_log = await self._changes_from_log(
                object_type, handle, with_states=path is not None
            )
            total = len(changes)
        if gramps_id is None and not changes:
            raise NotFoundError(
                f"No {object_type} '{ref}' exists, and no history is recorded under that "
                "handle. A deleted record's history is found by its handle, not its gramps_id."
            )
        kinds = {0: "added", 1: "edited", 2: "deleted"}
        rows = []
        for c in changes:
            row = {
                "change": kinds.get(c.get("trans_type"), str(c.get("trans_type"))),
                "transaction_id": c.get("transaction_id"),
                "user": ((c.get("connection") or {}).get("user") or {}).get("name"),
                "timestamp": _iso_from_epoch(c.get("timestamp")),
            }
            if path is not None:
                before = _value_at(c.get("old_data"), path)
                after = _value_at(c.get("new_data"), path)
                if before == after or (_blank(before) and _blank(after)):
                    continue
                row.update({"before": before, "after": after})
            rows.append(row)
        found = len(rows)
        rows = rows[: max(1, min(limit, 200))]
        label = gramps_id or handle
        out = {
            "object_type": object_type,
            "gramps_id": gramps_id,
            "handle": handle,
            "deleted": gramps_id is None,
            "total_changes": total,
            "returned": len(rows),
            "changes": rows,
        }
        message = f"{total} change(s) to {object_type} {label}" + (
            ", which has since been deleted" if gramps_id is None else ""
        )
        if unrecorded := sum(1 for row in rows if row["transaction_id"] is None):
            message += (
                f"; {unrecorded} shown with no transaction was a write rolled back at its "
                "commit, and changed nothing"
            )
        if path is not None:
            out.update({"field": field, "field_changes": found})
            message += (
                f"; {found} of them changed {field}, each shown with its value before and after"
                if found
                else f"; none changed {field}. A field the record has never had matches "
                "nothing: get_object shows the record's fields"
            )
        out["read_from"] = "record history" if from_log is None else "transaction log"
        if from_log is not None:
            out["log_complete"] = from_log["complete"]
            message += (
                ". Read from the whole transaction log, newest first"
                + (
                    ", back to the record's creation"
                    if from_log["created"]
                    else " to its start"
                    if from_log["complete"]
                    else f", stopping after {from_log['scanned']} transactions; older "
                    "changes were not read"
                )
                + ": gramps-webapi 3.21 has no per-record history (3.22 has), so this is "
                "slow on a large log"
            )
        out["message"] = message + ". get_transaction shows what a transaction changed."
        return out

    async def _all_object_changes(self, object_type: str, handle: str) -> list[dict]:
        """Every change to one record, newest first, with its states before and after."""
        changes: list[dict] = []
        page = 1
        while True:
            batch, total = await self.client.object_history(
                object_type, handle, page=page, pagesize=_HISTORY_PAGE, old=True, new=True
            )
            changes += batch
            if not batch or len(changes) >= total:
                return changes
            page += 1

    async def _changes_from_log(
        self, object_type: str, handle: str, *, with_states: bool = False
    ) -> tuple[list[dict], dict]:
        """One record's changes found in the whole transaction log, newest first.

        For gramps-webapi 3.21, which has no per-record history. Pages through
        ``/api/transactions/history/`` with the ``before_id`` cursor, which a
        write meanwhile cannot shift, keeping each change whose class and
        ``obj_handle`` are the record's; a reference change (class ``7``)
        names the record too, and is skipped. Stops at the record's creation
        -- an add not made by an undo -- or after
        :data:`_HISTORY_SCAN_LIMIT` transactions.

        Returns
        -------
        tuple
            The changes, shaped as 3.22's per-record history serves them,
            and how the log was read: ``complete``, ``created``, ``scanned``.
        """
        cls = _CLASS_NAMES[object_type]
        found: list[dict] = []
        scanned, before_id, created = 0, None, False
        while True:
            page = await self.client.transactions(
                page=1, pagesize=_HISTORY_PAGE, sort="-id", before_id=before_id
            )
            for txn in page:
                hits = [
                    change
                    for change in txn.get("changes") or []
                    if change.get("obj_class") == cls and change.get("obj_handle") == handle
                ]
                for change in sorted(hits, key=lambda c: c.get("id") or 0, reverse=True):
                    found.append(
                        {
                            **change,
                            "transaction_id": txn.get("id"),
                            "connection": txn.get("connection"),
                        }
                    )
                    undo = str(txn.get("description") or "").lstrip("_").lower()
                    created = created or (
                        change.get("trans_type") == 0 and not undo.startswith("undo")
                    )
            scanned += len(page)
            complete = created or len(page) < _HISTORY_PAGE
            if complete or scanned >= _HISTORY_SCAN_LIMIT:
                break
            before_id = page[-1].get("id")
        if with_states:
            for change in found:
                txn = await self.client.transaction(change["transaction_id"], old=True, new=True)
                full = next(
                    (
                        c
                        for c in txn.get("changes") or []
                        if c.get("obj_class") == cls
                        and c.get("obj_handle") == handle
                        and c.get("id") == change.get("id")
                    ),
                    {},
                )
                change["old_data"] = full.get("old_data")
                change["new_data"] = full.get("new_data")
        return found, {"complete": complete, "created": created, "scanned": scanned}

    async def undo_transaction(
        self,
        transaction_id: int,
        dry_run: bool = True,
        force: bool = False,
        message: str | None = None,
    ) -> dict:
        """Undo a past transaction. Checks for conflicts first; dry-run by default.

        A later edit to the same object makes an undo conflict -- undoing anyway
        (force) discards that later edit. The check is free and runs first.
        """
        check = await self.client.undo_check(transaction_id)
        clean = check.get("can_undo_without_force", False)
        summary = {
            "transaction_id": transaction_id,
            "can_undo_cleanly": clean,
            "conflicts": check.get("conflicts", []),
            "total_changes": check.get("total_changes"),
        }
        if dry_run:
            summary["dry_run"] = True
            summary["message"] = (
                f"DRY RUN. Transaction {transaction_id} "
                + (
                    "can be undone cleanly."
                    if clean
                    else f"has {check.get('conflicts_count', 0)} conflict(s); undoing "
                    f"would discard later edits to those objects."
                )
                + " Re-run with dry_run=False to apply."
            )
            return summary
        if not clean and not force:
            summary["message"] = (
                f"Refused: transaction {transaction_id} has "
                f"{check.get('conflicts_count', 0)} conflict(s). Objects were "
                f"edited after this transaction, and undoing would discard those "
                f"edits. Pass force=True only if that is what you intend."
            )
            return summary
        result = await self.client.undo(transaction_id, force=force, message=message)
        summary["dry_run"] = False
        summary["result"] = result
        task_id = _task_id_from(result)
        summary["task_id"] = task_id
        summary["message"] = f"Undo of transaction {transaction_id} submitted." + (
            f" It runs in the background; poll get_job('{task_id}')." if task_id else ""
        )
        logger.info("undid transaction %s (force=%s)", transaction_id, force)
        return summary

    # ------------------------------------------------------------------ #
    # reports
    # ------------------------------------------------------------------ #
    async def list_reports(self) -> dict:
        """List the reports this instance can generate.

        Returns
        -------
        dict
            One entry per report with its id, name, description and the option
            keys it accepts. The option values are defaults, and a caller
            overrides only what it needs.
        """
        raw = await self.client.reports()
        reports = [
            {
                "id": r.get("id"),
                "name": r.get("name"),
                "description": (r.get("description") or "").strip(),
                "options": sorted((r.get("options_dict") or {}).keys()),
            }
            for r in (raw if isinstance(raw, list) else [raw])
        ]
        return {"report_count": len(reports), "reports": reports}

    async def get_report_options(self, report_id: str) -> dict:
        """Read one report's default options.

        Parameters
        ----------
        report_id : str
            The report's id, from :meth:`list_reports`.

        Returns
        -------
        dict
            The report's metadata and its full default option dict.
        """
        raw = await self.client.reports(report_id)
        entry = raw[0] if isinstance(raw, list) and raw else raw
        return {
            "id": entry.get("id"),
            "name": entry.get("name"),
            "description": (entry.get("description") or "").strip(),
            "report_modes": entry.get("report_modes"),
            "default_options": entry.get("options_dict") or {},
        }

    async def run_report(
        self,
        report_id: str,
        options: dict | None = None,
        locale: str | None = None,
    ) -> dict:
        """Generate a report.

        Parameters
        ----------
        report_id : str
            The report to run.
        options : dict, optional
            Overrides for the report's defaults. Merged over them rather than
            replacing, because a partial option dict makes most reports fail.
        locale : str, optional
            Language for the output.

        Returns
        -------
        dict
            The filename produced, a ``task_id`` to poll when the server ran
            it in the background, and the privacy options applied.

        Notes
        -----
        Gramps' own defaults put living people in a report with all their
        data (``living_people`` 99) and include private records. Unless
        ``expose_private`` is set, a report that has those options leaves
        both out instead (``living_people`` 0, "Not included"), and any value
        the caller passes still wins.
        """
        overrides = dict(options or {})
        defaults: dict[str, Any] = {}
        if overrides or not self.exposing_private:
            defaults = (await self.get_report_options(report_id)).get("default_options") or {}
        private_safe: dict[str, Any] = {}
        if not self.exposing_private:
            for key, value in _REPORT_PRIVACY_OPTIONS.items():
                if key in defaults and key not in overrides:
                    private_safe[key] = value
        merged: dict[str, Any] = {}
        if overrides or private_safe:
            merged = {**defaults, **private_safe, **overrides}
        raw = await self.client.run_report(report_id, merged or None, locale)
        task_id = _task_id_from(raw)
        return {
            "report_id": report_id,
            "task_id": task_id,
            "privacy_options": {k: merged[k] for k in _REPORT_PRIVACY_OPTIONS if k in merged},
            "file_name": raw.get("file_name") or raw.get("filename"),
            "message": (
                f"Report submitted; poll get_job('{task_id}')." if task_id else "Report generated."
            ),
            "raw": raw,
        }

    # ------------------------------------------------------------------ #
    # custom filters
    # ------------------------------------------------------------------ #
    async def list_filter_rules(self, namespace: str) -> dict:
        """List the filter rules available in a namespace.

        Gramps' rule vocabulary expresses conditions no query language here
        reaches -- "is a descendant of", "has a common ancestor with",
        "matches the results of another filter".

        Parameters
        ----------
        namespace : str
            Plural namespace, e.g. ``"people"`` or ``"events"``.

        Returns
        -------
        dict
            The rules, each with its name, category, description and the
            argument labels it expects.
        """
        raw = await self.client.filters(namespace)
        rules = raw.get("rules") or []
        return {
            "namespace": namespace,
            "rule_count": len(rules),
            "rules": [
                {
                    "rule": r.get("rule"),
                    "name": r.get("name"),
                    "category": r.get("category"),
                    "description": (r.get("description") or "").strip(),
                    "arguments": r.get("labels") or [],
                }
                for r in rules
            ],
        }

    async def list_custom_filters(self, namespace: str | None = None) -> dict:
        """List custom filters already defined on this instance.

        Parameters
        ----------
        namespace : str, optional
            Restrict to one namespace. Omit for all of them.

        Returns
        -------
        dict
            Filters keyed by namespace, each with its name and rules.
        """
        raw = await self.client.filters(namespace)
        if namespace:
            return {namespace: raw.get("filters") or []}
        return {
            ns: (block or {}).get("filters") or []
            for ns, block in raw.items()
            if isinstance(block, dict)
        }

    async def create_filter(
        self,
        namespace: str,
        name: str,
        rules: list[dict],
        function: str = "and",
        invert: bool = False,
        comment: str = "",
    ) -> dict:
        """Create a reusable custom filter.

        Parameters
        ----------
        namespace : str
            Gramps namespace the filter applies to, e.g. ``"Person"``.
        name : str
            Filter name. Reused by ``query_objects`` and the timelines.
        rules : list of dict
            Each ``{"name": <rule>, "values": [...], "regex": bool}``. The
            available rule names come from :meth:`list_filter_rules`.
        function : {"and", "or", "one"}, optional
            How the rules combine.
        invert : bool, optional
            Return everything the rules do not match.
        comment : str, optional
            A note on what the filter is for.

        Returns
        -------
        dict
            The created filter, or an error envelope from the server.
        """
        body = {
            "name": name,
            "namespace": namespace,
            "function": function,
            "invert": invert,
            "rules": rules,
        }
        if comment:
            body["comment"] = comment
        result = await self.client.create_filter(namespace.lower() + "s", body)
        logger.info("created a custom filter in %s", namespace)
        return {"namespace": namespace, "name": name, "result": result}

    async def delete_filter(self, namespace: str, name: str) -> dict:
        """Delete a custom filter by name."""
        await self.client.delete_filter(namespace, name)
        logger.info("deleted a custom filter from %s", namespace)
        return {"namespace": namespace, "name": name, "deleted": True}

    # ------------------------------------------------------------------ #
    # consolidated timelines, task list, one transaction
    # ------------------------------------------------------------------ #
    async def consolidated_timeline(
        self,
        object_type: str,
        refs: list[str],
        anchor: str | None = None,
        event_types: str | None = None,
        limit: int = 200,
    ) -> dict:
        """Build one timeline spanning several people or families.

        Parameters
        ----------
        object_type : {"person", "family"}
            What the references name.
        refs : list of str
            Handles or gramps_ids to include.
        anchor : str, optional
            Handle or gramps_id of the central person, so ages are reported
            relative to them.
        event_types : str, optional
            Comma-delimited event type names to include.
        limit : int, optional
            Maximum events.

        Returns
        -------
        dict
            The merged events, with a count of how many carry no citation.
        """
        if object_type not in {"person", "family"}:
            return {
                "error": "unsupported_type",
                "message": "Consolidated timelines exist for person and family.",
            }
        handles = [await self._resolve_handle(object_type, r) for r in refs]
        anchor_handle = await self._resolve_handle(object_type, anchor) if anchor else None
        kind = "people" if object_type == "person" else "families"
        named = set(handles) | {anchor_handle} if object_type == "person" else set()
        # An anchor brings a generation of its relatives with it, as in
        # get_timeline; only the people named are kept, so paging waits.
        relatives = object_type == "person" and anchor_handle is not None
        options: dict[str, Any] = {"pagesize": limit, "page": 1} if not relatives else {}
        if object_type == "person":
            options["omit_anchor"] = 0
        raw = await self.client.consolidated_timeline(
            kind,
            handles=",".join(handles),
            anchor=anchor_handle,
            events=event_types,
            ratings="1",
            **options,
        )
        if relatives:
            raw = [e for e in raw if _timeline_person(e) in named][:limit]
        events, withheld = await self._timeline_events(raw, named)
        return {
            "object_type": object_type,
            "included": len(handles),
            "event_count": len(events),
            "uncited_count": sum(1 for e in events if not e["citations"]),
            "withheld_count": withheld,
            "events": events,
        }

    async def list_jobs(self, limit: int = 25) -> dict:
        """List recent background jobs for this tree.

        gramps-webapi calls them tasks (``GET /api/tasks/``); they are not the
        research tasks of :meth:`list_research_tasks`.

        Parameters
        ----------
        limit : int, optional
            Maximum jobs to return.

        Returns
        -------
        dict
            ``job_count`` and ``jobs``, newest first, each with its state.
        """
        raw = await self.client.task_list(limit=limit)
        return {"job_count": len(raw), "jobs": raw}

    async def get_transaction(self, transaction_id: int) -> dict:
        """Read one transaction from the change log in full.

        Parameters
        ----------
        transaction_id : int
            Id from :meth:`list_transactions`.

        Returns
        -------
        dict
            The transaction, including the objects it changed.
        """
        return await self.client.transaction(transaction_id)

    # ------------------------------------------------------------------ #
    # structured query
    # ------------------------------------------------------------------ #
    async def event_type_value(self, name: str) -> int | None:
        """Translate an event type name to the integer the tree stores.

        Gramps keeps a built-in type as an integer, leaving the ``string``
        member empty, so a filter has to compare integers. The map comes from
        the server rather than a hard-coded table, and is cached per session.

        Parameters
        ----------
        name : str
            Event type label, e.g. ``"Birth"``. Case-insensitive.

        Returns
        -------
        int or None
            The stored integer, or None if the server does not know the name.
        """
        if self._event_types is None:
            raw = await self.client.type_map("event_types")
            self._event_types = {
                str(label).strip().lower(): int(value) for value, label in raw.items()
            }
        return self._event_types.get(name.strip().lower())

    async def query_records(
        self,
        object_type: str,
        select: list | None = None,
        where: list | None = None,
        where_expr: str | None = None,
        order_by: list | None = None,
        limit: int = 50,
        after: str | None = None,
        event_type: str | None = None,
    ) -> dict:
        """Run a structured query and shape the result.

        Parameters
        ----------
        object_type : str
            Collection to query.
        select : list, optional
            Columns or ``{"json_path": [...], "as": "..."}`` entries.
        where : list, optional
            Conditions, combined with AND.
        where_expr : str, optional
            An expression, as an alternative to ``where``.
        order_by : list, optional
            Sort keys, each ``{"column": ..., "direction": "asc"|"desc"}``.
        limit : int, optional
            Maximum rows.
        after : str, optional
            Cursor from a previous response's ``next_after``.
        event_type : str, optional
            Convenience filter for events by type name, translated to the
            stored integer and added to ``where``.

        Returns
        -------
        dict
            ``rows``, ``returned``, ``total`` and ``next_after``. People are
            privacy-filtered unless ``expose_private`` is set.
        """
        if object_type not in ENDPOINTS:
            return {
                "error": "unsupported_type",
                "message": f"Unknown object type {object_type!r}. "
                f"Known: {', '.join(sorted(ENDPOINTS))}.",
            }

        trap = _query_trap(object_type, select, where, order_by)
        if trap:
            return trap

        conditions = list(where or [])
        if event_type:
            if object_type != "event":
                return {
                    "error": "unsupported_filter",
                    "message": "event_type only applies to the event collection.",
                }
            value = await self.event_type_value(event_type)
            if value is None:
                return {
                    "error": "unknown_event_type",
                    "message": f"The tree has no event type named {event_type!r}.",
                }
            conditions.append(
                {"column": {"json_path": ["type", "value"]}, "op": "eq", "value": value}
            )

        body: dict[str, Any] = {"limit": max(1, min(limit, 500)), "count": True}
        # Privacy filtering needs dates. Without them every person row would be
        # withheld for want of proof that they are historical, which makes a
        # person query useless. Fetch them, then drop them from the output if
        # the caller did not ask for them. The default columns carry no dates,
        # so a person query that names none selects them explicitly.
        filtering = not self.exposing_private
        reserved = [
            e["as"]
            for e in select or []
            if isinstance(e, dict) and str(e.get("as") or "").startswith("_")
        ]
        if reserved:
            return {
                "error": "reserved_alias",
                "message": f"Aliases starting with '_' are reserved: {', '.join(reserved)}. "
                "Choose another name.",
            }
        added_keys: set[str] = set()
        if filtering and object_type == "person" and not select:
            select = list(_PERSON_COLUMNS)
        if filtering and select:
            select = list(select)
            paths = _IDENTITY_PATHS + _PRIVACY_PATHS.get(object_type, (("_private", ["private"]),))
            for key, path in paths:
                select.append({"json_path": path, "as": key})
                added_keys.add(key)
        if select:
            body["select"] = select
        if conditions:
            body["where"] = conditions
        if where_expr:
            body["where_expr"] = where_expr
        if order_by:
            body["order_by"] = order_by
        if after:
            body["after"] = after

        rows, total, cursor = await self.client.structured_query(object_type, body)

        redacted = 0
        if filtering:
            rows = [self._redact_row(r, added_keys, object_type) for r in rows]
            redacted = sum(1 for r in rows if r.get("redacted") is True)

        return {
            "object_type": object_type,
            "returned": len(rows),
            "total": total,
            "next_after": cursor,
            "redacted_count": redacted,
            "rows": rows,
        }

    def _redact_row(self, row: dict, added_keys: set[str], object_type: str = "person") -> dict:
        """Withhold a query row about a private record or a living person.

        A structured query can select any path, so a row could carry a name
        and a birth date. This is bulk output, so it is filtered: a person by
        their own dates, a family by both parents' as well as its own flag,
        anything else by its private flag. Only the paths this service added
        are read, so a column the caller aliased ``_death`` or ``death`` cannot
        stand in for the real death date.

        Parameters
        ----------
        row : dict
            One result row.
        added_keys : set of str
            Keys added to the select purely to judge privacy; they are
            stripped before the row is returned. Empty when the caller named
            no columns and the defaults came back.
        object_type : str
            The collection the row came from.

        Returns
        -------
        dict
            The row, or a redacted stub in its place.
        """
        if not added_keys:
            # Default columns: they carry the record's own private flag and,
            # for a family, handles rather than names.
            withhold = bool(row.get("private"))
        elif object_type == "person":
            withhold = self._person_restricted(
                row.get("_private"), row.get("_birth"), row.get("_death")
            )
        else:
            withhold = bool(row.get("_private"))
            if object_type == "family":
                withhold = withhold or any(
                    row.get(f"_{parent}_handle")
                    and self._person_restricted(
                        row.get(f"_{parent}_private"),
                        row.get(f"_{parent}_birth"),
                        row.get(f"_{parent}_death"),
                    )
                    for parent in ("father", "mother")
                )
        if withhold:
            if added_keys:
                return redacted_stub(row.get("_gid"), row.get("_handle"))
            return redacted_stub(row.get("gramps_id"), row.get("handle"))
        return {k: v for k, v in row.items() if k not in added_keys}

    # ------------------------------------------------------------------ #
    # DNA
    # ------------------------------------------------------------------ #
    async def get_dna_matches(self, person: str, include_raw: bool = False) -> dict:
        """Report the DNA matches recorded against a person.

        Each match is summarised with the two numbers a genealogist actually
        reasons from -- total shared centiMorgans and the largest single
        segment -- and flagged when no common ancestor has been identified,
        which is the open research question a match represents.

        Parameters
        ----------
        person : str
            Handle or gramps_id.
        include_raw : bool, optional
            Include the unparsed note strings the segments came from.

        Returns
        -------
        dict
            The matches, a count, and how many have no common ancestor
            identified.
        """
        handle = await self._resolve_handle("person", person)
        raw = await self.client.dna_matches(handle, raw=include_raw)
        matches = [_dna_match(entry, include_raw) for entry in raw]
        return {
            "person": person,
            "match_count": len(matches),
            "unattributed_count": sum(1 for m in matches if not m["common_ancestors"]),
            "matches": matches,
        }

    async def add_dna_match(
        self, person_ref: str, match_ref: str, segments: str, cit: CitationInput
    ) -> dict:
        """Record a DNA match as evidence, cited to the test that found it.

        Stored the way Gramps Web stores a match, so its interface and
        :meth:`get_dna_matches` read it back: an association on the tested
        person, pointing at the match, with relationship ``DNA``. The segment
        data goes in a note on the association. The citation on it is the
        evidence: its source is the test -- the company and the kit -- its
        page says where the match was read, and its confidence grades the
        match, not any relationship inferred from it.

        Nothing is written until the segments parse. The server answers
        unreadable input with no segments rather than an error, and recorded
        as-is that would be a match sharing no DNA.

        Parameters
        ----------
        person_ref : str
            Handle or gramps_id of the tested person.
        match_ref : str
            Handle or gramps_id of the matching person.
        segments : str
            Shared-segment rows as the testing company exports them.
        cit : CitationInput
            The test. A new citation is always minted for the match.

        Returns
        -------
        dict
            The association's handles and the segment totals, or an ``error``
            key saying why nothing was written.
        """
        if cit.citation:
            return _reused_citation_refused("add_dna_match")
        person_handle = await self._resolve_handle("person", person_ref)
        match_handle = await self._resolve_handle("person", match_ref)
        if person_handle == match_handle:
            return {
                "error": "same_person",
                "message": "A person cannot be their own DNA match.",
            }
        parsed = [_dna_segment(seg) for seg in await self.client.parse_dna_match(segments)]
        if not parsed:
            return {
                "error": "unparsed_segments",
                "message": "No segments could be read from that data, so nothing was "
                "recorded. Paste the rows the testing company exports -- chromosome, "
                "start, stop, centiMorgans, SNPs -- and check them with "
                "parse_dna_segments first.",
            }
        person = await self._resolve("person", person_handle)
        if any(
            ref.get("ref") == match_handle and ref.get("rel") == "DNA"
            for ref in person.get("person_ref_list") or []
        ):
            return {
                "error": "match_exists",
                "message": f"A DNA match between {person_ref} and {match_ref} is already "
                "recorded and was left as it is. Another company's report of the same "
                "match is the same shared DNA: recording its segments too would count "
                "them twice.",
            }

        citation_handle = await self.resolve_citation(cit)
        note = await self.client.create_object(
            "note", {"_class": "Note", "text": {"_class": "StyledText", "string": segments}}
        )

        def edit(obj: dict) -> str | bool:
            refs = obj.setdefault("person_ref_list", [])
            if any(r.get("ref") == match_handle and r.get("rel") == "DNA" for r in refs):
                return False
            refs.append(
                {
                    "_class": "PersonRef",
                    "ref": match_handle,
                    "rel": "DNA",
                    "note_list": [note["handle"]],
                    "citation_list": [citation_handle],
                    "private": False,
                }
            )
            return "DNA match recorded"

        try:
            result = await self._mutate("person", person_handle, edit, label="recorded a match on")
        except Exception:
            await self._discard_new(note["handle"], citation_handle)
            raise
        if not result.get("changed"):
            # Another writer recorded the same match between the check and the
            # write; the new note and citation would be debris.
            await self._discard_new(note["handle"], citation_handle)
            return {
                "error": "match_exists",
                "message": "The same DNA match was recorded by another writer meanwhile.",
            }
        result.update(
            {
                "match": match_ref,
                "citation_handle": citation_handle,
                "note_handle": note["handle"],
                "segment_count": len(parsed),
                "total_cM": round(sum(seg["cM"] or 0 for seg in parsed), 2),
                "largest_segment_cM": max((seg["cM"] or 0) for seg in parsed),
            }
        )
        return result

    async def _discard_minted_citation(
        self, cit: CitationInput | None, citation_handle: str | None
    ) -> None:
        """Delete what :meth:`resolve_citation` created for a write that failed.

        Only what it minted: a reused citation, or one on an existing source,
        leaves the source alone, and a reused citation is not touched at all.
        """
        if cit is None or not citation_handle or cit.citation:
            return
        try:
            minted = await self.client.get_object("citation", citation_handle)
            doomed = [("citation", citation_handle)]
            doomed += [("note", h) for h in minted.get("note_list") or []]
            if not cit.source and cit.source_title:
                doomed.append(("source", minted.get("source_handle")))
            for object_type, handle in doomed:
                if handle:
                    await self.client.delete_object(object_type, handle)
        except GrampsApiError:
            logger.warning("could not remove unused citation %s", citation_handle)

    async def _discard_note(self, note_handle: str) -> None:
        """Delete a note minted for a write that did not land."""
        try:
            await self.client.delete_object("note", note_handle)
        except GrampsApiError:
            logger.warning("could not remove unused note %s", note_handle)

    async def _discard_new(self, note_handle: str, citation_handle: str) -> None:
        """Delete a note and citation minted for a write that did not land."""
        for object_type, handle in (("note", note_handle), ("citation", citation_handle)):
            try:
                await self.client.delete_object(object_type, handle)
            except GrampsApiError:
                logger.warning("could not remove unused %s %s", object_type, handle)

    async def get_ydna(self, person: str, include_raw: bool = False) -> dict:
        """Report a person's Y-DNA haplogroup assignment.

        Parameters
        ----------
        person : str
            Handle or gramps_id.
        include_raw : bool, optional
            Include the raw SNP data string.

        Returns
        -------
        dict
            The clade lineage from broadest to most specific, the terminal
            clade, and the YFull tree version. ``has_data`` is False when
            nothing is recorded.
        """
        handle = await self._resolve_handle("person", person)
        raw = await self.client.ydna(handle, raw=include_raw)
        lineage = [
            {
                "name": c.get("name") or c.get("clade"),
                "snps": c.get("snps") or c.get("SNPs"),
            }
            for c in (raw.get("clade_lineage") or [])
            if isinstance(c, dict)
        ]
        out = {
            "person": person,
            "has_data": bool(lineage),
            "terminal_clade": lineage[-1]["name"] if lineage else None,
            "clade_lineage": lineage,
            "tree_version": raw.get("tree_version"),
        }
        if include_raw and raw.get("raw_data"):
            out["raw_data"] = raw["raw_data"]
        return out

    async def parse_dna_segments(self, data: str) -> dict:
        """Parse raw shared-segment data into structured segments.

        Parameters
        ----------
        data : str
            Segment data as a testing company exports it.

        Returns
        -------
        dict
            The segments, their count, and the totals. When nothing parsed,
            ``parsed`` is False with an explanation -- the server answers
            unparseable input with an empty list and HTTP 200, which would
            otherwise read as "no shared segments".
        """
        segments = await self.client.parse_dna_match(data)
        shaped = [_dna_segment(seg) for seg in segments]
        if not shaped:
            return {
                "parsed": False,
                "segment_count": 0,
                "segments": [],
                "message": (
                    "Nothing parsed from that input. The server accepts "
                    "comma- or tab-separated rows of chromosome, start, "
                    "stop, centiMorgans, SNPs, with an optional side of M, P "
                    "or U, and tolerates a header row. It reports an "
                    "unreadable string as zero segments rather than as an "
                    "error, so this is a parse failure, not an absence of "
                    "shared DNA."
                ),
            }
        return {
            "parsed": True,
            "segment_count": len(shaped),
            "total_cM": round(sum(s["cM"] or 0 for s in shaped), 2),
            "largest_segment_cM": max((s["cM"] or 0) for s in shaped),
            "chromosomes": sorted({s["chromosome"] for s in shaped if s["chromosome"]}),
            "segments": shaped,
        }

    # ------------------------------------------------------------------ #
    # derived views
    # ------------------------------------------------------------------ #
    async def get_relationship(
        self,
        person1: str,
        person2: str,
        all_paths: bool = False,
        depth: int | None = None,
    ) -> dict:
        """Describe how two people are related.

        Parameters
        ----------
        person1, person2 : str
            Handle or gramps_id of each person.
        all_paths : bool, optional
            Report every path rather than the most direct one, which matters
            in an endogamous tree where two people are related twice over.
        depth : int, optional
            Generations to search.

        Returns
        -------
        dict
            The relationship wording and the generation distance from each
            person to their common ancestor. ``related`` is False when no
            common ancestor was found within ``depth``.
        """
        h1 = await self._resolve_handle("person", person1)
        h2 = await self._resolve_handle("person", person2)
        raw = await self.client.relationship(h1, h2, all_paths=all_paths, depth=depth)
        if isinstance(raw, list):
            return {
                "person1": person1,
                "person2": person2,
                "paths": [_relationship_entry(r) for r in raw],
                "path_count": len(raw),
            }
        entry = _relationship_entry(raw)
        return {"person1": person1, "person2": person2, **entry}

    async def assess_living(self, person: str, explain: bool = False, **options: Any) -> dict:
        """Ask the server whether a person is estimated to be alive.

        The server walks relatives to reach a verdict, so it sees cases the
        local heuristic in :mod:`gramps_evidence_mcp.privacy` cannot -- a
        person with no dates whose children died a century ago. The local
        filter still governs what bulk output reveals; this is a second
        opinion and an explanation of it.

        Parameters
        ----------
        person : str
            Handle or gramps_id.
        explain : bool, optional
            Also fetch the estimated birth and death dates and the reasoning.
        **options
            ``average_generation_gap``, ``max_age_probably_alive``,
            ``max_sibling_age_difference``.

        Returns
        -------
        dict
            ``living``, and when ``explain`` is set the estimated dates and
            the relative the estimate was drawn from.
        """
        handle = await self._resolve_handle("person", person)
        verdict = await self.client.living(handle, **options)
        out: dict[str, Any] = {
            "person": person,
            "living": bool(verdict.get("living")),
        }
        if explain:
            dates = await self.client.living_dates(handle, **options)
            out["estimated_birth"] = dates.get("birth") or None
            out["estimated_death"] = dates.get("death") or None
            out["explanation"] = dates.get("explain") or None
            other = dates.get("other")
            if isinstance(other, dict):
                out["derived_from"] = other.get("gramps_id") or other.get("handle")
        return out

    async def get_timeline(
        self,
        object_type: str,
        ref: str,
        ancestors: int | None = None,
        offspring: int | None = None,
        limit: int = 200,
    ) -> dict:
        """Build a chronological event timeline for a person or family.

        Each entry carries the anchor person's age at the event, how many
        citations support it and the strongest confidence among them -- which
        makes a timeline a readable audit of where the evidence thins out.

        gramps-webapi counts citations only when asked for ``ratings``, and
        folds in a generation of relatives each way whether asked or not: its
        ``ancestors`` and ``offspring`` start at 1 (TOOL-REQUESTS #30). So
        ``ratings`` is always asked for, and without ``ancestors`` or
        ``offspring`` only the person's own events are kept -- theirs, and
        their families'. A family's timeline is its members' events; the
        server takes no ``ancestors`` or ``offspring`` for one.

        Parameters
        ----------
        object_type : {"person", "family"}
            Whose timeline to build.
        ref : str
            Handle or gramps_id of the anchor.
        ancestors : int, optional
            Generations of ancestors whose events to fold in.
        offspring : int, optional
            Generations of descendants whose events to fold in.
        limit : int, optional
            Maximum events to return.

        Returns
        -------
        dict
            The events, a count, and how many of them carry no citation.
        """
        if object_type not in {"person", "family"}:
            return {
                "error": "unsupported_type",
                "message": "Timelines exist for person and family only.",
            }
        relatives = bool(ancestors or offspring)
        if object_type == "family" and relatives:
            return {
                "error": "unsupported_option",
                "message": "ancestors and offspring apply to a person's timeline. A "
                "family's timeline is its members' events; pass a member as a person "
                "to fold in their relatives.",
            }
        handle = await self._resolve_handle(object_type, ref)
        options: dict[str, Any] = {"ratings": 1}
        if object_type == "person":
            # The anchor's own entries carry their name and id only with this.
            options.update(ancestors=ancestors or None, offspring=offspring or None, omit_anchor=0)
        own_only = object_type == "person" and not relatives
        if not own_only:
            options.update(pagesize=limit, page=1)
        raw = await self.client.timeline(object_type, handle, **options)
        if own_only:
            raw = [e for e in raw if _timeline_person(e) == handle][:limit]
        named = {handle} if object_type == "person" else set()
        events, withheld = await self._timeline_events(raw, named)
        return {
            "anchor": ref,
            "event_count": len(events),
            "uncited_count": sum(1 for e in events if not e["citations"]),
            "withheld_count": withheld,
            "events": events,
        }

    async def _timeline_events(self, raw: list[dict], named: set[str]) -> tuple[list[dict], int]:
        """Shape timeline entries, withholding other people's private lives.

        The people in ``named`` were asked for by id and are shown in full, as
        get_person shows them. Anyone else a timeline folds in -- relatives,
        the members of a family -- is bulk output: unless ``expose_private``
        is set, their events are left out when they are private or probably
        living, and counted.

        Returns
        -------
        tuple of (list of dict, int)
            The shaped entries kept, and how many were withheld.
        """
        if self.exposing_private:
            return [_timeline_entry(e) for e in raw], 0
        others = {_timeline_person(e) for e in raw} - named - {None}
        restricted = await self._restricted_people(others) if others else set()
        kept = [_timeline_entry(e) for e in raw if _timeline_person(e) not in restricted]
        return kept, len(raw) - len(kept)

    async def event_span(
        self,
        event1: str,
        event2: str,
        as_age: bool = False,
        precision: int | None = None,
    ) -> dict:
        """Report the elapsed time between two events.

        Parameters
        ----------
        event1, event2 : str
            Handle or gramps_id of each event.
        as_age : bool, optional
            Phrase the result as an age rather than an interval.
        precision : int, optional
            How many units to include.

        Returns
        -------
        dict
            The two events and a human-readable span.
        """
        h1 = await self._resolve_handle("event", event1)
        h2 = await self._resolve_handle("event", event2)
        raw = await self.client.event_span(h1, h2, as_age=as_age or None, precision=precision)
        return {"event1": event1, "event2": event2, "span": raw.get("span")}

    async def reindex_search(self, full: bool = False) -> dict:
        """Rebuild the full-text index behind Gramps Web's search.

        gramps-webapi updates the index after each write, as a background task
        (``update_search_indices_from_transaction``); a rebuild is for after a
        large import, or an index that has fallen behind.

        Parameters
        ----------
        full : bool, optional
            Rebuild from scratch rather than updating incrementally.

        Returns
        -------
        dict
            A ``task_id`` to poll with :meth:`get_job`, when the server
            dispatched the work.
        """
        raw = await self.client.reindex_search(full=full)
        task_id = _task_id_from(raw)
        return {
            "full": full,
            "task_id": task_id,
            "message": (
                f"Reindex submitted; poll get_job('{task_id}')."
                if task_id
                else "Reindex submitted."
            ),
        }

    # ------------------------------------------------------------------ #
    # trees, tasks, verification
    # ------------------------------------------------------------------ #
    async def resolve_tree_id(self, tree_id: str | None = None) -> str:
        """Determine which tree to operate on.

        On gramps-webapi the tree is bound to the authenticated account, so
        there is normally exactly one. An explicit id is honoured; otherwise
        the single reachable tree is used, and an ambiguous choice raises
        rather than picking one.

        Parameters
        ----------
        tree_id : str, optional
            Tree id to use as given.

        Returns
        -------
        str
            The resolved tree id.

        Raises
        ------
        NotFoundError
            If no tree is reachable, or several are and none was named.
        """
        if tree_id:
            return tree_id
        trees = await self.client.trees()
        if not trees:
            raise NotFoundError("No tree is reachable with these credentials.")
        if len(trees) > 1:
            names = ", ".join(f"{t.get('id')} ({t.get('name')})" for t in trees if t.get("id"))
            raise NotFoundError(f"Several trees are reachable; name one explicitly: {names}")
        return trees[0]["id"]

    async def get_job(self, task_id: str) -> dict:
        """Report a background job's state.

        Undo, import, reindex and verification are dispatched to a worker;
        the dispatching call returns before the work is done. Without this,
        an undo can be submitted with no way to learn whether it landed.

        Parameters
        ----------
        task_id : str
            The task id returned by whichever call dispatched it.

        Returns
        -------
        dict
            ``task_id``, ``state``, ``finished``, ``succeeded``, ``info``,
            the result if any, and the dispatch timestamp.
        """
        raw = await self.client.task(task_id)
        state = (raw.get("state") or "").upper()
        finished = state in _TERMINAL_TASK_STATES
        return {
            "task_id": raw.get("task_id") or task_id,
            "name": raw.get("name"),
            "state": state or None,
            "finished": finished,
            "succeeded": state == "SUCCESS" if finished else None,
            "info": raw.get("info"),
            "result": raw.get("result_object") or raw.get("result"),
            "created_at": raw.get("created_at"),
        }

    async def verify_tree(self, tree_id: str | None = None, **thresholds: Any) -> dict:
        """Run Gramps' own genealogical verification checks.

        These are plausibility bounds rather than citation checks: a mother
        bearing a child at 9, a marriage lasting 120 years, a date that does
        not parse. They catch transcription slips that a citation audit will
        not, because a wrong date can be perfectly well sourced.

        Parameters
        ----------
        tree_id : str, optional
            Tree to check. Resolved automatically when omitted.
        **thresholds
            Bounds such as ``oldage``, ``yngmom``, ``mxchilddad``. Omitted
            values use the server's defaults.

        Returns
        -------
        dict
            ``findings`` with a ``finding_count``, or ``task_id`` when the
            server ran the check in the background -- poll it with
            :meth:`get_job`.
        """
        resolved = await self.resolve_tree_id(tree_id)
        raw = await self.client.verify(resolved, **thresholds)

        task_id = _task_id_from(raw)
        if task_id:
            return {
                "tree_id": resolved,
                "task_id": task_id,
                "message": (
                    f"Verification is running in the background. Poll it with get_job('{task_id}')."
                ),
            }

        findings = raw if isinstance(raw, list) else (raw or {}).get("findings") or []
        return {
            "tree_id": resolved,
            "finding_count": len(findings),
            "findings": findings,
        }

    # ------------------------------------------------------------------ #
    # research tasks: Gramps Web's Tasks view
    # ------------------------------------------------------------------ #
    async def _tag_handles(
        self, names: Iterable[str], *, create: bool, color: str | None = "#4444FF"
    ) -> tuple[dict[str, str], list[str]]:
        """Handles of tags by exact name, creating the missing ones when asked.

        Parameters
        ----------
        names : iterable of str
            Tag names, matched exactly, as tag_object matches them.
        create : bool
            Create a tag that does not exist yet.
        color : str or None, optional
            Colour of a created tag, tag_object's default; None leaves the
            server's, as Gramps Web does when it creates the ToDo tag.

        Returns
        -------
        (dict, list)
            Name to handle for every name found or created, and the names
            created.
        """
        existing = {
            (tag.get("name") or ""): tag["handle"]
            for tag in await self.client.list_objects("tag", keys="handle,name")
        }
        found: dict[str, str] = {}
        created: list[str] = []
        for name in dict.fromkeys(names):
            if name in existing:
                found[name] = existing[name]
            elif create:
                payload: dict[str, Any] = {"_class": "Tag", "name": name}
                if color:
                    payload["color"] = color
                tag = await self.client.create_object("tag", payload)
                found[name] = existing[name] = tag["handle"]
                created.append(name)
                logger.info("created tag %s", tag["handle"])
        return found, created

    async def _task_attribute_names(
        self, attributes: dict[str, str | None] | None, allow_new_type: bool
    ) -> list[tuple[str, str | None]] | dict:
        """A task's further attributes, each name spelt as the tree spells it.

        Status and Priority are refused here: they have parameters of their
        own, and Gramps Web reads them by those exact names. Returns the
        refusal instead of the list when one is named.
        """
        given = dict(attributes or {})
        reserved = [n for n in given if _type_key(n) in _RESERVED_TASK_KEYS]
        if reserved:
            return {
                "error": "conflicting_arguments",
                "message": f"Pass {', '.join(reserved)} as status or priority, not in "
                "attributes: Gramps Web reads them by those exact names.",
            }
        types = await self.client.types() if given else {}
        return [
            (
                await self._canonical_type(
                    "source_attribute_types",
                    name,
                    # Removing an attribute creates no type, so any name will do.
                    allow_new_type or value is None,
                    types=types,
                ),
                value,
            )
            for name, value in given.items()
        ]

    async def add_research_task(
        self,
        title: str,
        description: str = "",
        priority: TaskPriority = TaskPriority.medium,
        tags: list[str] | None = None,
        attributes: dict[str, str] | None = None,
        private: bool = False,
        allow_new_type: bool = False,
    ) -> dict:
        """Create a research task exactly as Gramps Web's New Task form does.

        A task is a Source tagged ``ToDo``, with a ``Priority`` and a
        ``Status`` source attribute (``"5"`` and ``"Open"`` to begin with)
        and, when there is a description, a first note of type To Do carrying
        the same tags (``GrampsjsViewNewTask.js`` in the Gramps Web frontend).
        The ToDo tag is created, as the form creates it, the first time.

        Parameters
        ----------
        title : str
            What to do, one line.
        description : str, optional
            The task's description, stored as its first note.
        priority : TaskPriority, optional
            high, medium or low: stored as "1", "5" or "9".
        tags : list of str, optional
            Tags beside ToDo, found or created by exact name.
        attributes : dict, optional
            Further source attributes, name to value, after Priority and
            Status.
        private : bool, optional
            Mark the task, and its description note, private. Gramps Web's
            form marks only the source; the note follows it here so that a
            private task's description is not left public.
        allow_new_type : bool, optional
            Accept an attribute name the tree has never used.

        Returns
        -------
        dict
            The task's handle, gramps_id, status, priority, tags and note.
        """
        title = title.strip()
        if not title:
            return {"error": "title_required", "message": "A task needs a title."}
        named = await self._task_attribute_names(attributes, allow_new_type)
        if isinstance(named, dict):
            return named
        extra = list(dict.fromkeys(t for t in (tags or []) if t and t != TASK_TAG))
        todo, created_tags = await self._tag_handles([TASK_TAG], create=True, color=None)
        more, created_more = await self._tag_handles(extra, create=True)
        tag_list = [todo[TASK_TAG], *(more[t] for t in extra)]
        priority = TaskPriority(priority)
        payload: dict[str, Any] = {
            "_class": "Source",
            "title": title,
            "attribute_list": [
                _src_attribute(TASK_PRIORITY, _PRIORITY_VALUES[priority]),
                _src_attribute(TASK_STATUS, TaskStatus.open.value),
                *(_src_attribute(name, value or "") for name, value in named),
            ],
            "tag_list": tag_list,
            "private": private,
        }
        note = None
        if description.strip():
            note = await self.client.create_object(
                "note", _task_note(description, tag_list, private)
            )
            payload["note_list"] = [note["handle"]]
        try:
            created = await self.client.create_object("source", payload)
        except Exception:
            if note:
                await self._discard_note(note["handle"])
            raise
        logger.info("created research task %s", created.get("gramps_id"))
        out = {
            "handle": created["handle"],
            "gramps_id": created.get("gramps_id"),
            "object_type": "source",
            "title": title,
            "status": TaskStatus.open.value,
            "priority": priority.value,
            "tags": [TASK_TAG, *extra],
            "description_note": note.get("gramps_id") if note else None,
            "message": f"Created research task {created.get('gramps_id')} "
            f"(Open, {priority.value} priority); it shows in Gramps Web's Tasks view.",
        }
        if created_tags or created_more:
            out["created_tags"] = [*created_tags, *created_more]
        return out

    async def list_research_tasks(
        self,
        status: list[TaskStatus] | None = None,
        tag: str | None = None,
        attributes: dict[str, str] | None = None,
    ) -> dict:
        """List research tasks as Gramps Web's Tasks view lists them.

        Every Source tagged ToDo (the view's ``HasTag`` rule), in the view's
        order: Open, In Progress, Blocked, any other status, Done, and the
        newest id first within each. A task's status and priority are its
        first attribute of that name, the one Gramps Web reads.

        Parameters
        ----------
        status : list of TaskStatus, optional
            Only tasks in these states.
        tag : str, optional
            Only tasks that also carry this tag.
        attributes : dict, optional
            Only tasks with these attribute values, names and values matched
            ignoring case; an empty value matches any value.

        Returns
        -------
        dict
            ``task_count``, ``by_status``, ``tasks`` and ``redacted_count``.
            A private task is a redacted stub unless private records are
            shown, and a private description note is left out the same way.
        """
        names = {
            t["handle"]: t.get("name") or ""
            for t in await self.client.list_objects("tag", keys="handle,name")
        }
        if TASK_TAG not in names.values():
            return {
                "task_count": 0,
                "by_status": {},
                "tasks": [],
                "redacted_count": 0,
                "message": f"No research tasks: the tree has no {TASK_TAG} tag yet. "
                "add_research_task creates both.",
            }
        if tag is not None and tag not in names.values():
            raise NotFoundError(f"No tag named {tag!r}; list_tags lists them.")
        rows = await self.client.list_objects(
            "source",
            rules={"rules": [{"name": "HasTag", "values": [TASK_TAG]}]},
            keys="handle,gramps_id,title,attribute_list,tag_list,note_list,private",
        )
        wanted = {TaskStatus(s).value for s in status or []}
        wanted_attrs = [
            (_type_key(n), (v or "").strip().casefold()) for n, v in (attributes or {}).items()
        ]

        def keep(row: dict) -> bool:
            attrs = row.get("attribute_list") or []
            if wanted and _task_attribute(attrs, TASK_STATUS) not in wanted:
                return False
            if tag is not None and tag not in {names.get(h) for h in row.get("tag_list") or []}:
                return False
            return all(
                any(
                    _type_key(_type_string(a.get("type"))) == name
                    and (not value or str(a.get("value") or "").strip().casefold() == value)
                    for a in attrs
                )
                for name, value in wanted_attrs
            )

        rows = [r for r in rows if keep(r)]
        rows.sort(key=lambda r: r.get("gramps_id") or "", reverse=True)
        rows.sort(
            key=lambda r: _STATUS_ORDER.get(_task_attribute(r["attribute_list"], TASK_STATUS), 4)
        )

        shown = self.exposing_private
        visible = [r for r in rows if shown or not r.get("private")]
        firsts = [r["note_list"][0] for r in visible if r.get("note_list")]
        notes = (
            {
                n["handle"]: n
                for n in await self.client.list_objects(
                    "note", handles=firsts, keys="handle,gramps_id,text,private"
                )
            }
            if firsts
            else {}
        )
        tasks: list[dict] = []
        counts: dict[str, int] = {}
        for row in rows:
            if not shown and row.get("private"):
                tasks.append(redacted_stub(row.get("gramps_id"), row.get("handle")))
                continue
            entry = _task_entry(row, names, notes, shown)
            tasks.append(entry)
            counts[entry["status"] or "(none)"] = counts.get(entry["status"] or "(none)", 0) + 1
        return {
            "task_count": len(tasks),
            "by_status": counts,
            "tasks": tasks,
            "redacted_count": len(rows) - len(visible),
        }

    async def update_research_task(
        self,
        task: str,
        status: TaskStatus | None = None,
        priority: TaskPriority | None = None,
        attributes: dict[str, str | None] | None = None,
        note_append: str | None = None,
        allow_new_type: bool = False,
        *,
        private: bool | None = None,
        private_note: bool | None = None,
    ) -> dict:
        """Change a research task's status, priority, privacy or attributes, or add to its note.

        An attribute is replaced, never appended beside the old one: the first
        of that name takes the new value, as Gramps Web's task page sets it,
        and any further one of that name is removed, so the value Gramps Web
        reads is the value set. ``note_append`` adds a paragraph to the task's
        first note, the description Gramps Web shows, creating that note (type
        To Do) when the task has none.

        A private task's description note is private too: a note this call
        creates or edits on a private task is made private, and so is the
        note of a task this call makes private. Making a task public never
        makes its note public; that takes ``private_note=False``, and the
        result says when a private note was left private.

        Parameters
        ----------
        task : str
            Handle or gramps_id of the task's Source.
        status : TaskStatus, optional
            The new status.
        priority : TaskPriority, optional
            The new priority.
        attributes : dict, optional
            Further attributes to set, name to value; a null value removes
            every attribute of that name.
        note_append : str, optional
            Text to add, as a new paragraph, to the task's description note.
        allow_new_type : bool, optional
            Accept an attribute name the tree has never used.
        private : bool, optional
            Make the task private (True) or public (False).
        private_note : bool, optional
            Set the description note's private flag explicitly.

        Returns
        -------
        dict
            The task's status, priority and privacy after the call, and what
            changed.
        """
        appended = (note_append or "").strip()
        if (
            status is None
            and priority is None
            and not attributes
            and not appended
            and private is None
            and private_note is None
        ):
            return {
                "error": "nothing_to_do",
                "message": "Pass status, priority, attributes, note_append, private or "
                "private_note.",
            }
        named = await self._task_attribute_names(attributes, allow_new_type)
        if isinstance(named, dict):
            return named
        source = await self._resolve(
            "source", task, keys="handle,gramps_id,tag_list,note_list,private"
        )
        todo = (await self._tag_handles([TASK_TAG], create=False))[0].get(TASK_TAG)
        if not todo or todo not in (source.get("tag_list") or []):
            return {
                "error": "not_a_task",
                "message": f"Source {source.get('gramps_id')} is not a research task: it "
                f"does not carry the {TASK_TAG} tag. list_research_tasks lists the tasks.",
            }
        settings: list[tuple[str, str | None]] = []
        if status is not None:
            settings.append((TASK_STATUS, TaskStatus(status).value))
        if priority is not None:
            settings.append((TASK_PRIORITY, _PRIORITY_VALUES[TaskPriority(priority)]))
        settings += named

        task_private = bool(source.get("private")) if private is None else bool(private)
        # The note's flag: the caller's; else private, with a private task;
        # else left as it is -- never made public by default.
        note_flag = private_note if private_note is not None else (True if task_private else None)
        notes = source.get("note_list") or []
        changes: list[str] = []
        notices: list[str] = []
        noted: str | None = None
        note_private: bool | None = None

        # The note first: if the task's own write then fails, the note has
        # been made more private, never less, unless the caller asked so.
        if notes and (appended or note_flag is not None or private is False):
            said: list[str] = []
            state: dict[str, Any] = {}

            def edit_note(note: dict) -> str | bool:
                if appended:
                    text = note.setdefault("text", {"string": "", "tags": []})
                    current = text.get("string") or ""
                    text["string"] = f"{current}\n\n{appended}" if current.strip() else appended
                    said.append("paragraph added")
                if note_flag is not None and bool(note.get("private")) != note_flag:
                    note["private"] = note_flag
                    said.append("made private" if note_flag else "made public")
                state["private"] = bool(note.get("private"))
                return "; ".join(said) if said else False

            done = await self._mutate("note", notes[0], edit_note, label="updated")
            noted, note_private = done["gramps_id"], state.get("private")
            if said:
                changes.append(f"description note {noted}: {', '.join(said)}")
            if private is False and private_note is None and note_private:
                notices.append(
                    f"Description note {noted} stays private; pass private_note=false to "
                    "make it public."
                )
        elif private_note is not None and not notes and not appended:
            notices.append("The task has no description note to mark.")

        new_note = None
        if appended and not notes:
            note_private = bool(note_flag)
            new_note = await self.client.create_object(
                "note", _task_note(appended, source.get("tag_list") or [], note_private)
            )
            noted = new_note.get("gramps_id")
        after: dict[str, Any] = {}

        def edit(obj: dict) -> str | bool:
            attrs = obj.setdefault("attribute_list", [])
            for name, value in settings:
                changes.extend(_set_task_attribute(attrs, name, value))
            if private is not None and bool(obj.get("private")) != private:
                obj["private"] = private
                changes.append("task made private" if private else "task made public")
            if new_note:
                obj.setdefault("note_list", []).append(new_note["handle"])
                changes.append(
                    f"description note {noted} added" + (", private" if note_private else "")
                )
            after["status"] = _task_attribute(attrs, TASK_STATUS)
            after["priority"] = _priority_label(_task_attribute(attrs, TASK_PRIORITY))
            after["private"] = bool(obj.get("private"))
            return "; ".join(changes) if changes else False

        try:
            result = await self._mutate("source", source["handle"], edit, label="updated task")
        except Exception:
            if new_note:
                await self._discard_note(new_note["handle"])
            raise
        label = result.get("gramps_id") or result["handle"]
        message = (
            f"Task {label}: {'; '.join(changes)}"
            if changes
            else f"Task {label} already says that; nothing changed."
        )
        out = {
            "handle": result["handle"],
            "gramps_id": result.get("gramps_id"),
            "object_type": "source",
            "status": after.get("status"),
            "priority": after.get("priority"),
            "private": after.get("private"),
            "changed": bool(changes),
            "changes": changes,
            "description_note": noted,
            "description_note_private": note_private,
            "message": " ".join([message, *notices]),
        }
        if notices:
            out["notices"] = notices
        return _with_repairs(out, result)


#: A research task as Gramps Web keeps one: a Source carrying this tag,
TASK_TAG = "ToDo"
#: with its status and priority in source attributes of these names,
TASK_STATUS = "Status"
TASK_PRIORITY = "Priority"
#: and its description in a first note of this type.
TASK_NOTE_TYPE = "To Do"

#: Priority as Gramps Web stores it (``GrampsjsViewNewTask.js``).
_PRIORITY_VALUES = {TaskPriority.high: "1", TaskPriority.medium: "5", TaskPriority.low: "9"}

#: The Tasks view's order (``GrampsjsViewTasks.js``); any other status sorts 4th.
_STATUS_ORDER = {"Open": 1, "In Progress": 2, "Blocked": 3, "Done": 5}

#: Attribute names a task's ``attributes`` may not set, as ``_type_key`` spells them.
_RESERVED_TASK_KEYS = {"status", "priority"}

#: How much of a description list_research_tasks shows; get_note reads all of it.
_DESCRIPTION_CHARS = 400


def _src_attribute(name: str, value: str) -> dict:
    """A source attribute in the shape Gramps Web's task form writes."""
    return {"_class": "SrcAttribute", "type": name, "value": value}


def _task_note(text: str, tag_list: list[str], private: bool) -> dict:
    """A task's description note: type To Do, carrying the task's tags."""
    return {
        "_class": "Note",
        "text": {"_class": "StyledText", "string": text, "tags": []},
        "type": TASK_NOTE_TYPE,
        "tag_list": list(tag_list),
        "private": private,
    }


def _task_attribute(attrs: list[dict] | None, name: str) -> str | None:
    """The value of the first attribute of this exact name, the one Gramps Web reads."""
    for attr in attrs or []:
        if _type_string(attr.get("type")) == name:
            return attr.get("value")
    return None


def _priority_label(value: str | None) -> str | None:
    """high, medium or low, read from a stored priority as Gramps Web reads it."""
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return "high" if number < 5 else "medium" if number == 5 else "low"


def _set_task_attribute(attrs: list[dict], name: str, value: str | None) -> list[str]:
    """Set one attribute in place, leaving one of that name; None removes it.

    The first attribute of the name takes the value and keeps its place,
    privacy, citations and notes, as Gramps Web's task page edits it; any
    further one of the name is removed. Edits ``attrs`` in place, and says
    what changed.
    """
    at = [i for i, a in enumerate(attrs) if _type_string(a.get("type")) == name]
    if value is None:
        for i in reversed(at):
            del attrs[i]
        return [f"{name} removed"] if at else []
    if not at:
        attrs.append(_src_attribute(name, value))
        return [f"{name} set to {value!r}"]
    first = attrs[at[0]]
    old = first.get("value")
    first["value"] = value
    for i in reversed(at[1:]):
        del attrs[i]
    said = [f"{name} {old!r} -> {value!r}"] if old != value else []
    if len(at) > 1:
        said.append(f"{len(at) - 1} more {name} attribute(s) removed")
    return said


def _task_entry(row: dict, tag_names: dict[str, str], notes: dict[str, dict], shown: bool) -> dict:
    """One task as list_research_tasks shows it."""
    attrs = row.get("attribute_list") or []
    others: dict[str, Any] = {}
    for attr in attrs:
        name = _type_string(attr.get("type"))
        if name not in (TASK_STATUS, TASK_PRIORITY):
            others.setdefault(name, attr.get("value"))
    first = (row.get("note_list") or [None])[0]
    note = notes.get(first) if first else None
    text = None
    if note and (shown or not note.get("private")):
        text = ((note.get("text") or {}).get("string") or "").strip()
        if len(text) > _DESCRIPTION_CHARS:
            text = text[:_DESCRIPTION_CHARS].rstrip() + " ..."
    return {
        "handle": row.get("handle"),
        "gramps_id": row.get("gramps_id"),
        "title": row.get("title"),
        "status": _task_attribute(attrs, TASK_STATUS),
        "priority": _priority_label(_task_attribute(attrs, TASK_PRIORITY)),
        "tags": [
            tag_names.get(h, h) for h in row.get("tag_list") or [] if tag_names.get(h) != TASK_TAG
        ],
        "attributes": others,
        "description": text or None,
        "description_note": note.get("gramps_id") if note else None,
        "private": bool(row.get("private")),
    }


#: Side codes Gramps records on a shared segment.
_DNA_SIDES = {"M": "maternal", "P": "paternal", "U": "unknown"}


def _dna_segment(raw: dict) -> dict:
    """Shape one shared chromosome segment."""
    side = str(raw.get("side") or "U").upper()
    return {
        "chromosome": raw.get("chromosome"),
        "start": raw.get("start"),
        "stop": raw.get("stop"),
        "cM": raw.get("cM"),
        "snps": raw.get("SNPs"),
        "side": _DNA_SIDES.get(side, "unknown"),
        "comment": raw.get("comment") or None,
    }


def _dna_match(raw: dict, include_raw: bool) -> dict:
    """Shape one DNA match, with the totals a relationship estimate rests on.

    Total shared centiMorgans and the largest segment are the two figures
    that constrain a relationship; the server reports segments but not the
    sums, so they are computed here.
    """
    segments = [_dna_segment(s) for s in raw.get("segments") or []]
    ancestors = [
        {"handle": h, "name": _profile_name(p)}
        for h, p in zip(
            raw.get("ancestor_handles") or [],
            raw.get("ancestor_profiles") or [],
            strict=False,
        )
    ] or [{"handle": h, "name": None} for h in raw.get("ancestor_handles") or []]
    out = {
        "handle": raw.get("handle"),
        "estimated_relationship": raw.get("relation") or None,
        "segment_count": len(segments),
        "total_cM": round(sum(s["cM"] or 0 for s in segments), 2),
        "largest_segment_cM": max((s["cM"] or 0) for s in segments) if segments else 0,
        "common_ancestors": ancestors,
        "segments": segments,
    }
    if include_raw and raw.get("raw_data"):
        out["raw_data"] = raw["raw_data"]
    return out


def _profile_name(profile: Any) -> str | None:
    """Pull a display name out of an ancestor profile of uncertain shape."""
    if isinstance(profile, dict):
        return profile.get("name") or profile.get("gramps_id")
    if isinstance(profile, list) and profile:
        return _profile_name(profile[0])
    return None


def _relationship_entry(raw: dict) -> dict:
    """Shape one relationship result, flagging when no common ancestor exists."""
    d1 = raw.get("distance_common_origin", -1)
    d2 = raw.get("distance_common_other", -1)
    return {
        "relationship": raw.get("relationship_string") or None,
        "generations_to_common_ancestor": None if d1 == -1 else d1,
        "generations_from_common_ancestor": None if d2 == -1 else d2,
        "related": bool(raw.get("relationship_string")) and d1 != -1,
    }


#: Media types an upload may carry. The file path comes from the model, so
#: this is what keeps a prompt-injected call from copying an arbitrary local
#: file -- a private key, a credentials file -- into the tree.
_MEDIA_MIME_PREFIXES = ("image/", "audio/", "video/")
_MEDIA_MIME_TYPES = {"application/pdf"}
#: Common in archives and phone photos, and unknown to some platforms' tables.
_MEDIA_SUFFIXES = {".heic": "image/heic", ".heif": "image/heif", ".jp2": "image/jp2"}


def _media_mime(path: Path) -> str | None:
    """The file's media type if it is one an upload may carry, else None."""
    mime = mimetypes.guess_type(path.name)[0] or _MEDIA_SUFFIXES.get(path.suffix.lower())
    if mime and (mime.startswith(_MEDIA_MIME_PREFIXES) or mime in _MEDIA_MIME_TYPES):
        return mime
    return None


def _reused_citation_refused(tool: str) -> dict:
    """The refusal for a claim that must mint its own citation.

    A citation carries one confidence, so attaching an existing one to a new
    claim silently re-grades one of the two -- docs/PITFALLS.md section 4.
    """
    return {
        "error": "citation_reuse_refused",
        "message": f"{tool} records a claim of its own and mints its own citation. "
        "Pass source (or source_title) with page and confidence instead of an "
        "existing citation.",
    }


def _unattached_upload(media: dict, target_type: str, target: str, exc: Exception) -> dict:
    """Say that a file was uploaded and its attach failed, naming the object.

    The upload is kept: the file is on the server whatever happens, since the
    server never removes one (``docs/PITFALLS.md`` section 16), and a retry
    with the same file finds the object by checksum.
    """
    label = media.get("gramps_id") or media["handle"]
    written = exc.written if isinstance(exc, FailedWriteError) else None
    if isinstance(exc, GrampsApiError) and exc.status < 500:
        written = False
    attach = (
        "it was not attached"
        if written is False
        else f"whether it was attached is unknown: read {target_type} {target} before retrying"
    )
    return {
        "error": "not_attached",
        "handle": media["handle"],
        "gramps_id": media.get("gramps_id"),
        "media_created": True,
        "written": written,
        "message": f"The file was uploaded as media {label}, but attaching it to "
        f"{target_type} {target} failed ({exc.args[0] if exc.args else type(exc).__name__}); "
        f"{attach}. Retry with media_ref='{label}', or with the same file, which finds it.",
    }


def _timeline_person(raw: dict) -> str | None:
    """Handle of the person a timeline entry is about, if it names one."""
    person = raw.get("person")
    return person.get("handle") if isinstance(person, dict) else None


def _timeline_entry(raw: dict) -> dict:
    """Shape one timeline event, keeping the evidence columns.

    ``person`` says whose event it is: their id and name, how they are
    related to the anchor (``self`` for the anchor's own events and their
    families'), and their role in it. The server's ``label`` is the event
    type, never a name. ``place`` is the place's title and id only: the
    server's place profile carries every alternate name of the place and of
    each place enclosing it, which made thirty events 163,649 characters
    (TOOL-REQUESTS #30); get_place has the rest.
    """
    person = raw.get("person") or {}
    place = raw.get("place") or {}
    parts = (person.get(k) for k in ("name_given", "name_surname", "name_suffix"))
    name = " ".join(part for part in parts if part)
    title = place.get("display_name") or place.get("name")
    return {
        "gramps_id": raw.get("gramps_id"),
        "type": _type_string(raw.get("type")),
        "date": raw.get("date") or None,
        "place": {"title": title, "gramps_id": place.get("gramps_id")} if place else None,
        "description": raw.get("description") or None,
        "age": raw.get("age") or None,
        "person": {
            "gramps_id": person.get("gramps_id"),
            "name": name or None,
            "relationship": person.get("relationship") or None,
            "role": raw.get("role") or None,
        },
        "citations": raw.get("citations") or 0,
        "confidence": raw.get("confidence"),
    }


#: Celery states that mean a task will not change again.
_TERMINAL_TASK_STATES = {"SUCCESS", "FAILURE", "REVOKED"}


# --------------------------------------------------------------------------- #
# ocr_media
# --------------------------------------------------------------------------- #
#: A PDF text layer shorter than this is not a page's text. A website's
#: download often carries only a caption -- a Newspapers.com clipping's held
#: its URL and title, 212 characters, on a live tree on 2026-10-06 -- while
#: a page of print runs to thousands.
_PDF_LAYER_MIN_CHARS = 300
#: Archive URLs tried for existing text, page URLs first: each costs requests.
_ARCHIVE_URL_LIMIT = 4
#: Long edge, in pixels, Gramps Web is asked to render a file Pillow cannot
#: open (a PDF page with no embedded scan, a HEIC photograph) at.
_RENDER_SIZE = 3000

_GERMAN_VISION_REFUSAL = {
    "error": "vision_refused",
    "message": "German handwriting is not read by vision here, even when asked: vision "
    f"models measure {ocr.CER_VISION_GERMAN} character error on historical German "
    "(METATR 2026, READ-2016), half the characters, names among them. Use "
    "engine='transkribus' (or auto), or a person who reads Kurrent; FamilySearch's "
    "index may hold the entry (get_records_on_image).",
}


class _OcrDocument:
    """One media object being read: its file, fetched once, its page, its notes.

    Parameters
    ----------
    service : GrampsService
        The service whose client fetches the file and notes.
    media : dict
        The whole media object, with its backlinks.
    page : int
        The page wanted, from 1; only a PDF has more than one.
    """

    def __init__(self, service: GrampsService, media: dict, page: int):
        self.service = service
        self.media = media
        self.page = page
        self.handle: str = media["handle"]
        self.gid: str = media.get("gramps_id") or media["handle"]
        self.mime: str = (media.get("mime") or "").lower()
        self._file: tuple[bytes, str] | None = None
        self._pdf: ocr.PdfPage | None = None
        self._pdf_read = False
        self._pil: tuple[Any, str] | None = None
        self._transcripts: list[dict] | None = None

    @property
    def is_pdf(self) -> bool:
        """bool: Whether the media file is a PDF."""
        return self.mime == "application/pdf"

    @property
    def pages(self) -> int:
        """int: The PDF's page count once read; 1 for anything else."""
        return self._pdf.pages if self._pdf else 1

    async def file(self) -> tuple[bytes, str]:
        """The media file and the type it was served as, downloaded once."""
        if self._file is None:
            self._file = await self.service.client.media_file(self.handle)
        return self._file

    async def pdf(self) -> ocr.PdfPage | None:
        """The PDF's page: its text layer and scan. None for anything not a PDF.

        Raises
        ------
        ocr.ImageError
            When the PDF cannot be read or has no such page.
        """
        if not self.is_pdf:
            return None
        if not self._pdf_read:
            data, _served = await self.file()
            self._pdf = ocr.pdf_page(data, self.page)
            self._pdf_read = True
        return self._pdf

    async def page_image(self) -> tuple[bytes, str, str]:
        """The page as image bytes: ``(bytes, mime, where it came from)``.

        An image file is itself; a PDF's page is the scan embedded in it. What
        Pillow cannot open -- a PDF page with no scan, a HEIC photograph --
        Gramps Web renders, as AVIF, though only a PDF's first page.

        Raises
        ------
        ocr.ImageError
            For a page that does not exist or a file that is no image.
        """
        if self.mime.startswith("image/"):
            if self.page != 1:
                raise ocr.ImageError("an image file has one page; page is for a PDF")
            data, served = await self.file()
            try:
                # Opened here to prove it is an image; kept, so it is opened once.
                self._pil = (ocr.open_image(data), "the media file")
                return data, served or self.mime, "the media file"
            except ocr.ImageError:
                pass
            return await self._rendered("the media file, rendered by Gramps Web")
        if self.is_pdf:
            pdf = await self.pdf()
            if pdf and pdf.image:
                return (
                    pdf.image,
                    pdf.image_mime or "image/jpeg",
                    (f"the scan embedded in page {self.page} of the PDF"),
                )
            if self.page == 1:
                return await self._rendered("page 1 of the PDF, rendered by Gramps Web")
            raise ocr.ImageError(
                f"page {self.page} of the PDF holds no scanned image, and Gramps Web "
                "renders only a PDF's first page"
            )
        raise ocr.ImageError(f"{self.mime or 'its file'} is neither an image nor a PDF")

    async def _rendered(self, how: str) -> tuple[bytes, str, str]:
        data = await self.service.client.media_thumbnail(self.handle, _RENDER_SIZE)
        return data, "image/avif", how

    async def page_pil(self) -> tuple[Any, str]:
        """The page as a Pillow image, opened once, and where it came from."""
        if self._pil is None:
            data, _mime, how = await self.page_image()
        if self._pil is None:
            self._pil = (ocr.open_image(data), how)
        return self._pil

    async def transcripts(self) -> list[dict]:
        """The Transcript notes on the media that hold a reading (not layout XML)."""
        if self._transcripts is None:
            found = []
            for handle in self.media.get("note_list") or []:
                try:
                    note = await self.service.client.get_object("note", handle)
                except GrampsApiError:
                    continue
                kind = note.get("type")
                kind = kind.get("string") if isinstance(kind, dict) else kind
                text = ((note.get("text") or {}).get("string")) or ""
                if kind != "Transcript" or not text.strip() or ocr.is_layout_xml(text):
                    continue
                provenance = ocr.parse_header(text)
                changed = note.get("change")
                found.append(
                    {
                        "note": note.get("gramps_id"),
                        "chars": len(text),
                        "provenance": provenance,
                        "page": (provenance or {}).get("page"),
                        "changed": ocr.today(datetime.fromtimestamp(changed, UTC))
                        if isinstance(changed, int | float) and changed
                        else None,
                        "text": ocr.body_of(text),
                    }
                )
            self._transcripts = found
        return self._transcripts


def _task_id_from(payload: Any) -> str | None:
    """Pull a task id out of a response that dispatched background work."""
    if not isinstance(payload, dict):
        return None
    task = payload.get("task")
    if isinstance(task, dict) and task.get("id"):
        return str(task["id"])
    if payload.get("task_id"):
        return str(payload["task_id"])
    return None


# --------------------------------------------------------------------------- #
# vocabularies
# --------------------------------------------------------------------------- #
# Both spellings are mapped: backlinks come back keyed by singular type name,
# not the plural REST namespace. docs/PITFALLS.md section 3.
_NAMESPACE_TO_TYPE = {
    "people": "person",
    "families": "family",
    "events": "event",
    "places": "place",
    "sources": "source",
    "citations": "citation",
    "repositories": "repository",
    "media": "media",
    "notes": "note",
    "tags": "tag",
}

# Objects that carry a citation_list. Note families are here: a family citation
# supports a claim no event makes -- that these two people were a couple.
_CITABLE_TYPES = {"person", "family", "event", "place", "media", "citation", "source"}

# Types with a server-side merge endpoint (verified against gramps-webapi 3.20.1).
_MERGEABLE_TYPES = {
    "person",
    "family",
    "event",
    "place",
    "source",
    "citation",
    "repository",
    "media",
    "note",
}

# child_kind -> (list field, referenced object type, entries are {"ref": ...} dicts)
# tag_list and note_list hold bare handles; the rest hold ref dicts.
_DETACH_SPECS: dict[str, tuple[str, str | None, bool]] = {
    "event": ("event_ref_list", "event", True),
    "media": ("media_list", "media", True),
    "note": ("note_list", "note", False),
    "tag": ("tag_list", "tag", False),
    "citation": ("citation_list", "citation", False),
    "child": ("child_ref_list", "person", True),
    "person": ("person_ref_list", "person", True),
    "repository": ("reporef_list", "repository", True),
    # A person's side of a link only; refused while the family still holds
    # the other side, since removing one side is how a one-sided link starts.
    "parent_family": ("parent_family_list", "family", False),
    "family": ("family_list", "family", False),
    # A place's parent. update_place refuses a place with several, so this is
    # how an extra one -- left by a merge -- comes off.
    "enclosure": ("placeref_list", "place", True),
}


def _object_label(object_type: str, obj: dict) -> str:
    """A short human label, so a merge plan says which two things it means."""
    if object_type == "person":
        return _name_from_person(obj)
    for key in ("title", "name", "desc", "page", "description"):
        value = obj.get(key)
        if isinstance(value, str) and value.strip():
            return value[:120]
    if object_type == "event":
        return f"{_type_string(obj.get('type'))} {_date_string(obj.get('date')) or ''}".strip()
    return obj.get("gramps_id") or obj.get("handle", "")


_FIELD_STEP = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\d+")


def _field_path(field: str) -> list[str | int] | None:
    """Split a dotted field path, ``primary_name.surname_list.0.surname``; None if malformed."""
    steps = str(field).strip().split(".")
    if not all(_FIELD_STEP.fullmatch(step) for step in steps):
        return None
    return [int(step) if step.isdigit() else step for step in steps]


def _value_at(data: Any, path: list[str | int]) -> Any:
    """The value at a field path of a stored record, without ``_class`` keys.

    None when the record is absent (an add has no state before it, a delete
    none after) or the path leads nowhere.
    """
    node = data if isinstance(data, dict) and data else None
    for step in path:
        if isinstance(step, int) and isinstance(node, list) and -len(node) <= step < len(node):
            node = node[step]
        elif isinstance(step, str) and isinstance(node, dict):
            node = node.get(step)
        else:
            return None
    return _unclassed(node)


def _unclassed(value: Any) -> Any:
    """A stored value with Gramps' ``_class`` keys removed, at any depth."""
    if isinstance(value, dict):
        return {k: _unclassed(v) for k, v in value.items() if k != "_class"}
    if isinstance(value, list):
        return [_unclassed(v) for v in value]
    return value


def _blank(value: Any) -> bool:
    """No value: absent, or an empty string, list or object."""
    return value is None or value == "" or value == [] or value == {}


def _iso_from_epoch(value: Any) -> str | None:
    """Format a Unix timestamp as an ISO-8601 UTC string, or None."""
    if not isinstance(value, int | float):
        return None
    return datetime.fromtimestamp(value, UTC).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# write normalization
# --------------------------------------------------------------------------- #
def _normalize(object_type: str, obj: dict) -> list[str]:
    """Remove defects no write should carry forward, in place.

    Run by :meth:`GrampsService._mutate` on every object it writes, so an
    affected object is repaired by its next edit and no edit can re-create
    the defect.

    - A person listing the same family twice in ``family_list`` or
      ``parent_family_list``. gramps-webapi 3.21.1 appends a family to its
      new father's or mother's ``family_list`` without checking for it, and
      removes a child's link with ``list.remove``, which takes only the first
      of two -- so a duplicate survives a detach as a one-sided link. The
      lists hold bare handles, so dropping a repeat loses nothing; the order
      is kept.
    - A place carrying a top-level ``type`` key. Gramps calls the field
      ``place_type``; 1.0.x ``add_place`` wrote ``type``, which the server
      keeps without reading. Moved into ``place_type`` when that is unset,
      dropped otherwise.
    - A date carrying ``year``. The server adds it to every date it serves
      but it is no field of a Gramps date: written back, it is stored, and
      from then on served in place of the year the date holds, however the
      date changes (``docs/PITFALLS.md`` section 24). Dropped from every
      date; reported only when it disagreed with the date.

    Parameters
    ----------
    object_type : str
        Gramps object type.
    obj : dict
        The whole object, edited in place.

    Returns
    -------
    list of str
        What was repaired, for the result message. Empty when nothing was.
    """
    repaired: list[str] = []
    if object_type == "person":
        for key in ("family_list", "parent_family_list"):
            entries = obj.get(key) or []
            unique = list(dict.fromkeys(entries))
            if len(unique) < len(entries):
                obj[key] = unique
                dropped = len(entries) - len(unique)
                repaired.append(
                    f"removed {dropped} duplicate {key} entr{'y' if dropped == 1 else 'ies'}"
                )
    elif object_type == "place" and "type" in obj:
        stray = obj.pop("type")
        shown = stray if isinstance(stray, str) else _type_string(stray)
        if stray and not _is_unknown_type(stray) and _is_unknown_type(obj.get("place_type")):
            obj["place_type"] = stray
            repaired.append(f"moved the stray 'type' key ({shown}) into place_type")
        else:
            repaired.append(f"dropped a stray 'type' key ({shown or 'empty'})")
    stale = _drop_date_years(obj)
    if stale:
        repaired.append(
            f"dropped {stale} stale date year{'' if stale == 1 else 's'} "
            "the server would have shown instead of the date's own"
        )
    return repaired


def _query_trap(
    object_type: str, select: list | None, where: list | None, order_by: list | None
) -> dict | None:
    """Refuse the structured-query forms the server answers wrongly, saying what works.

    Each was verified against gramps-webapi 3.21.1, 3.22.3 and 3.23.1 and is in
    ``docs/PITFALLS.md``:

    - a date's ``year`` (sections 13 and 24): never stored by the server, so
      3.21 matches nothing on it -- or only records some client wrote a
      served year back to -- and 3.22 refuses it. The year is ``dateval[2]``.
    - ``type`` as a plain column (section 12): the stored type is
      ``{"string": "", "value": <number>}``; 3.21 refuses the column and
      3.22 compares the whole object, so nothing ever matches.
    - a list as the value of any operator but ``in`` (section 13): the
      server answers HTTP 500.

    Returns
    -------
    dict or None
        An error envelope, or None when the query has none of these.
    """
    clauses = [c for c in [*(where or []), *(order_by or [])] if isinstance(c, dict)]
    paths = [e["json_path"] for e in select or [] if isinstance(e, dict) and "json_path" in e]
    for clause in clauses:
        for key in ("column", "value_column"):
            column = clause.get(key)
            if isinstance(column, dict) and "json_path" in column:
                paths.append(column["json_path"])
    for path in paths:
        path = list(path) if isinstance(path, (list, tuple)) else []
        for i in range(1, len(path)):
            if path[i] == "year" and path[i - 1] == "date":
                fixed = [*path[:i], "dateval", 2]
                return {
                    "error": "date_year",
                    "message": f"{path} reads a year the server does not store, so it matches "
                    "nothing, or only the records some client wrote a served year back to. "
                    f"The year is {fixed}; it is 0 when unknown, so add a condition that "
                    "it is greater than 0 to leave undated records out.",
                }
    for clause in clauses:
        if clause.get("column") == "type":
            hint = (
                "Pass event_type='Birth' (or any event type's name) instead."
                if object_type == "event"
                else 'Compare {"json_path": ["type", "value"]} with the type\'s number, '
                'or {"json_path": ["type", "string"]} with a custom type\'s name.'
            )
            return {
                "error": "type_column",
                "message": 'A type is stored as {"string": "", "value": <number>}, so '
                f"'type' as a plain column matches nothing. {hint}",
            }
    for clause in where or []:
        if (
            isinstance(clause, dict)
            and isinstance(clause.get("value"), list)
            and clause.get("op", "eq") != "in"
        ):
            return {
                "error": "list_comparison",
                "message": f"Op {clause.get('op', 'eq')!r} with a list value makes the server "
                "fail with HTTP 500; only 'in' takes a list. To find a handle in a list field "
                "use op 'contains' with that one value. The engine cannot test a list for "
                "emptiness: for uncited facts use list_unsourced_facts, for what cites or "
                "holds an object use get_backlinks.",
            }
    return None


#: List fields whose items are objects, each with the path to the item field
#: a search usually means. GrampsQL compares such a list as a whole: ``~``
#: asks whether the value is one of the items, which a string never is, so it
#: matches nothing, and ``!~`` everything (docs/PITFALLS.md section 7).
_GQL_OBJECT_LISTS = {
    "address_list": "address_list.any.city",
    "alt_loc": "alt_loc.any.city",
    "alt_names": "alt_names.any.value",
    "alternate_names": "alternate_names.any.first_name",
    "attribute_list": "attribute_list.any.value",
    "child_ref_list": "child_ref_list.any.ref",
    "event_ref_list": "event_ref_list.any.ref.get_event.description",
    "lds_ord_list": "lds_ord_list.any.temple",
    "media_list": "media_list.any.ref.get_media.desc",
    "person_ref_list": "person_ref_list.any.rel",
    "placeref_list": "placeref_list.any.ref",
    "reporef_list": "reporef_list.any.call_number",
    "surname_list": "surname_list.any.surname",
    "urls": "urls.any.path",
}

#: List fields of handles, each with the path to the text of what they point
#: at. ``~ "<handle>"`` asks whether one object is listed, and is meant; any
#: other comparison, or ``~`` with text, matches nothing.
_GQL_HANDLE_LISTS = {
    "citation_list": "citation_list.any.get_citation.page",
    "family_list": "family_list.any.get_family.gramps_id",
    "note_list": "note_list.any.get_note.text.string",
    "parent_family_list": "parent_family_list.any.get_family.gramps_id",
    "tag_list": "tag_list.any.get_tag.name",
}

#: One condition of a GrampsQL query: ``lhs op rhs``. Quoted strings are
#: matched first and skipped, so text inside a value is never read as a field.
_GQL_CONDITION = re.compile(
    r"\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*'"
    r"|(?P<lhs>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[\d+\])*)\s*"
    r"(?P<op>!=|<=|>=|!~|=|<|>|~)\s*(?P<rhs>\"[^\"]*\"|'[^']*'|[\w.-]+)?"
)

#: What a handle can look like: no spaces, dots, slashes or other punctuation.
_HANDLE_LIKE = re.compile(r"[A-Za-z0-9_]+")


def _gql_list_checks(gql: str) -> tuple[dict | None, list[str]]:
    """Refuse a GrampsQL comparison of a whole list that can never be true.

    ``urls ~ "http"`` reads as a search of the URLs and returns zero rows with
    HTTP 200 (TOOL-REQUESTS #26); the server compares the list itself. A list
    of objects is searched through its items, ``urls.any.path ~ "http"``, and
    a list of handles through what they point at,
    ``note_list.any.get_note.text.string ~ "x"``. A test for one handle in a
    list of handles, ``tag_list ~ "<handle>"``, is allowed, and explained
    when it matches nothing.

    Returns
    -------
    (dict or None, list of str)
        An error envelope for the first comparison that cannot match, and for
        each handle test allowed through, what it asks.
    """
    membership: list[str] = []
    for found in _GQL_CONDITION.finditer(gql):
        lhs, op = found.group("lhs"), found.group("op")
        if not lhs:
            continue
        rhs = (found.group("rhs") or "").strip("\"'")
        parts = lhs.split(".")
        if parts[-1] in ("any", "all") and len(parts) > 1:
            parts = parts[:-1]
        field = parts[-1]
        prefix = ".".join(parts[:-1])

        def at(path: str, prefix: str = prefix) -> str:
            return f"{prefix}.{path}" if prefix else path

        condition = " ".join(found.group(0).split())
        if field in _GQL_OBJECT_LISTS:
            outcome = "every record" if op.startswith("!") else "nothing"
            return {
                "error": "gql_list_field",
                "message": f"The condition {condition} compares the list {field} as a "
                f"whole, and its items are objects, so it matches {outcome}. Search the "
                f'items instead: {at(_GQL_OBJECT_LISTS[field])} ~ "{rhs}" matches when '
                f"any item does (.all. when every item does), and {at(field)}.length > 0 "
                "when there are any.",
            }, []
        if field in _GQL_HANDLE_LISTS:
            follow = at(_GQL_HANDLE_LISTS[field])
            if op in ("~", "!~") and _HANDLE_LIKE.fullmatch(rhs):
                membership.append(
                    f"The condition {condition} asks whether the handle {rhs!r} is in "
                    f'{field}. To search what {field} points at: {follow} ~ "{rhs}".'
                )
                continue
            return {
                "error": "gql_list_field",
                "message": f"The condition {condition} cannot match as meant: {field} is "
                f"a list of handles, and the one comparison it takes is "
                f'{at(field)} ~ "<handle>", whether that object is listed. To search '
                f'what it points at: {follow} ~ "{rhs}"; {at(field)}.length > 0 when '
                "there are any.",
            }, []
    return None, membership


def _person_merge_refusal(keep: dict, drop: dict) -> str | None:
    """Why Gramps' MergePersonQuery would refuse to merge two people, or None."""
    keep_fams, drop_fams = set(keep.get("family_list") or []), set(drop.get("family_list") or [])
    if keep_fams & drop_fams:
        return "Gramps does not merge spouses. Detach one from the family they share first."
    if keep_fams & set(drop.get("parent_family_list") or []) or drop_fams & set(
        keep.get("parent_family_list") or []
    ):
        return (
            "Gramps does not merge a parent with their own child. Detach the child from "
            "the family first."
        )
    return None


#: The vocabulary of the ``type`` field update_object_fields may set.
_TYPE_FIELD_VOCABULARY = {
    "note": "note_types",
    "family": "family_relation_types",
    "repository": "repository_types",
}


#: How a refusal names a vocabulary, where its key does not read well.
_TYPE_LABELS = {
    "url_types": "URL",
    "source_media_types": "source medium",
    "family_relation_types": "family relationship",
    "child_reference_types": "child relationship",
}


def _type_key(name: str) -> str:
    """A type name as matching compares it: lower case, letters and digits only."""
    return re.sub(r"[^0-9a-z]", "", str(name).lower())


#: Names that mean one standard type unambiguously, per vocabulary, keyed as
#: :func:`_type_key` spells them. A near-miss is never here: "Censsus" is
#: refused with a suggestion, not corrected. Every target is a standard name
#: (gramps-webapi 3.21.1, Gramps 6.0); a test holds the table to that.
_TYPE_SYNONYMS: dict[str, dict[str, str]] = {
    "event_types": {
        "born": "Birth",
        "died": "Death",
        "deceased": "Death",
        "buried": "Burial",
        "interment": "Burial",
        "interred": "Burial",
        "baptized": "Baptism",
        "baptised": "Baptism",
        "christened": "Christening",
        "married": "Marriage",
        "wedding": "Marriage",
        "divorced": "Divorce",
        "cremated": "Cremation",
        "emigrated": "Emigration",
        "immigrated": "Immigration",
        "naturalized": "Naturalization",
        "naturalised": "Naturalization",
        "naturalisation": "Naturalization",
        "graduated": "Graduation",
        "ordained": "Ordination",
        "engaged": "Engagement",
        "retired": "Retirement",
        "confirmed": "Confirmation",
        "military": "Military Service",
        "adoption": "Adopted",
        "stillborn": "Stillbirth",
        "banns": "Marriage Banns",
        "marriagelicence": "Marriage License",
        "batmitzvah": "Bas Mitzvah",
        "resided": "Residence",
    },
    "event_role_types": {
        "godfather": "Godparent",
        "godmother": "Godparent",
        "officiant": "Officiator",
        "neighbour": "Neighbor",
        "principal": "Primary",
    },
    "name_types": {
        "aka": "Also Known As",
        "alias": "Also Known As",
        "maiden": "Birth Name",
        "maidenname": "Birth Name",
        "birth": "Birth Name",
        "married": "Married Name",
    },
    "child_reference_types": {
        "biological": "Birth",
        "natural": "Birth",
        "adoptive": "Adopted",
        "adoption": "Adopted",
        "step": "Stepchild",
        "stepson": "Stepchild",
        "stepdaughter": "Stepchild",
        "fostered": "Foster",
    },
    "family_relation_types": {"marriage": "Married"},
    "place_types": {"neighbourhood": "Neighborhood"},
    "note_types": {"transcription": "Transcript", "html": "Html code"},
    "repository_types": {"archives": "Archive", "graveyard": "Cemetery"},
    "source_media_types": {
        "photograph": "Photo",
        "microfilm": "Film",
        "microfiche": "Fiche",
        "headstone": "Tombstone",
        "gravestone": "Tombstone",
        "digital": "Electronic",
        "online": "Electronic",
    },
    "url_types": {
        "webhomepage": "Web Home",
        "homepage": "Web Home",
        "website": "Web Home",
        "webpage": "Web Home",
        "search": "Web Search",
    },
    "attribute_types": {
        "ssn": "Social Security Number",
        "idnumber": "Identification Number",
        "nationalid": "Identification Number",
        "profession": "Occupation",
        "nick": "Nickname",
    },
}


def _describe_cascade(also: list[dict]) -> str:
    """'family F0002 into F0001 (the two families would have the same parents)'."""
    parts = []
    for entry in also:
        kind = "family" if "family" in entry else "person"
        parts.append(f"{kind} {entry['absorbs']} into {entry[kind]} ({entry['because']})")
    return "; ".join(parts)


def _drop_date_years(node: Any) -> int:
    """Remove ``year`` from every date in an object; count those that were stale."""
    stale = 0
    if isinstance(node, dict):
        dateval = node.get("dateval")
        if "year" in node and isinstance(dateval, list):
            year = node.pop("year")
            if len(dateval) >= 3 and year != dateval[2]:
                stale += 1
        for value in node.values():
            stale += _drop_date_years(value)
    elif isinstance(node, list):
        for value in node:
            stale += _drop_date_years(value)
    return stale


def _append_event_ref(person: dict, ref: dict, event_type: str | None) -> None:
    """Append an event reference, as the primary birth or death if there is none.

    gramps-webapi 3.21.1 recomputes ``birth_ref_index`` and
    ``death_ref_index`` on every person write, from Birth and Death events
    held in the Primary role, so what is set here is what a reader sees only
    until the server's own rule applies; it is set for servers and readers
    that do not recompute.
    """
    refs = person.setdefault("event_ref_list", [])
    refs.append(ref)
    if (_type_string(ref.get("role")) or "Primary") != "Primary":
        return
    index = len(refs) - 1
    if event_type in _BIRTHLIKE and person.get("birth_ref_index", -1) < 0:
        person["birth_ref_index"] = index
    if event_type in _DEATHLIKE and person.get("death_ref_index", -1) < 0:
        person["death_ref_index"] = index


# --------------------------------------------------------------------------- #
# names
# --------------------------------------------------------------------------- #
def _fold(text: Any) -> str:
    """Compare-form of a name part: case-folded, whitespace collapsed."""
    return " ".join(str(text or "").split()).casefold()


def _surname_of(name: dict) -> str:
    """The surname a name is filed under: the primary one, else the first."""
    surnames = name.get("surname_list") or []
    primary = next((s for s in surnames if s.get("primary")), surnames[0] if surnames else {})
    return primary.get("surname") or ""


def _name_label(name: dict) -> str:
    """``Type 'Given / Surname'``: the split between the parts stays visible."""
    return (
        f"{_type_string(name.get('type')) or 'name'} "
        f"'{name.get('first_name') or ''} / {_surname_of(name)}'"
    )


def _name_parts(name: dict) -> tuple:
    """What NameParts sets on a name, for telling a real change from a no-op."""
    surnames = name.get("surname_list") or [{}]
    return (
        name.get("first_name") or "",
        tuple((s.get("surname") or "", s.get("prefix") or "") for s in surnames),
        name.get("suffix") or "",
        name.get("title") or "",
        name.get("call") or "",
        name.get("nick") or "",
    )


def _names_listing(person: dict) -> str:
    """Every name a person carries, keyed as NameMatch addresses them."""
    lines = [f"primary: {_name_label(person.get('primary_name') or {})}"]
    lines += [f"[{i}] {_name_label(n)}" for i, n in enumerate(person.get("alternate_names") or [])]
    return "; ".join(lines)


def _pick_name(
    person: dict, match: NameMatch, *, alternates_only: bool, allow_identical: bool = False
) -> tuple[str | int, dict]:
    """Select exactly one of a person's names.

    Parameters
    ----------
    person : dict
        The whole person.
    match : NameMatch
        What to look for. Every field given must match.
    alternates_only : bool
        Leave the primary name out of the search.
    allow_identical : bool, optional
        When several names match and every one is identical, take the last
        rather than refuse: removing either of two identical names has the
        same result.

    Returns
    -------
    tuple
        ``("primary", name)`` or ``(index, name)``, the name being the dict
        inside ``person``, so editing it edits the person.

    Raises
    ------
    NotFoundError
        If no name or several names match, listing what the person carries.
    """
    candidates: list[tuple[str | int, dict]] = []
    if not alternates_only and match.primary is not False and match.index is None:
        candidates.append(("primary", person.setdefault("primary_name", {})))
    if match.primary is not True:
        for i, name in enumerate(person.get("alternate_names") or []):
            if match.index is None or match.index == i:
                candidates.append((i, name))
    hits = [
        (key, name)
        for key, name in candidates
        if (match.given is None or _fold(match.given) == _fold(name.get("first_name")))
        and (match.surname is None or _fold(match.surname) == _fold(_surname_of(name)))
        and (match.type is None or _fold(match.type) == _fold(_type_string(name.get("type"))))
    ]
    if len(hits) == 1:
        return hits[0]
    if hits and allow_identical and all(n == hits[0][1] for _, n in hits):
        return hits[-1]
    who = person.get("gramps_id") or person.get("handle")
    if not hits:
        raise NotFoundError(f"No name of {who} matches. Names: {_names_listing(person)}")
    raise NotFoundError(
        f"{len(hits)} names of {who} match; add type or index to choose one. "
        f"Names: {_names_listing(person)}"
    )


#: How a place merge settles the survivor's enclosures.
_ENCLOSURE_CHOICES = ("auto", "keep_keeper", "keep_drop", "keep_both")


def _placeref_key(ref: dict) -> tuple:
    """A PlaceRef's identity for a merge: where, and when."""
    return (ref.get("ref"), _date_string(ref.get("date")))


#: Object types with a media_list, and with a note_list.
_MEDIA_HOLDERS = {"person", "family", "event", "place", "source", "citation"}
#: Objects with an ``attribute_list``: a SrcAttribute on a source or citation,
#: an Attribute on the rest.
_ATTRIBUTE_HOLDERS = {"person", "family", "event", "media", "source", "citation"}
_NOTE_HOLDERS = _MEDIA_HOLDERS | {"media", "repository"}


def _collect_held(node: Any, key: str, *, by_ref: bool) -> list[str]:
    """Every handle held under ``key`` anywhere in an object, in order."""
    found: list[str] = []
    if isinstance(node, dict):
        for k, value in node.items():
            if k == key and isinstance(value, list):
                found += [
                    (v.get("ref") if by_ref and isinstance(v, dict) else v)
                    for v in value
                    if (v.get("ref") if by_ref and isinstance(v, dict) else v)
                ]
            elif isinstance(value, dict | list):
                found += _collect_held(value, key, by_ref=by_ref)
    elif isinstance(node, list):
        for value in node:
            found += _collect_held(value, key, by_ref=by_ref)
    return found


def _carried_listing(carried: dict) -> str:
    """``note N0001, media O0002``: what a carry moved."""
    return ", ".join(
        f"{typ} {', '.join(ids)}" for typ, ids in carried.items() if typ != "to" and ids
    )


def _held_listing(held: dict[str, list[str]]) -> str:
    """Describe what a name or object holds, e.g. ``2 citation(s), 1 note(s)``."""
    words = {"citation_list": "citation(s)", "note_list": "note(s)", "media_list": "media"}
    return ", ".join(f"{len(v)} {words.get(k, k)}" for k, v in held.items())


def _reporef(repo_handle: str, call_number: str | None, media_type: str) -> dict:
    """A RepoRef in the shape both add_source and link_repository write."""
    return {
        "_class": "RepoRef",
        "ref": repo_handle,
        "call_number": call_number or "",
        "media_type": media_type or "Unknown",
        "note_list": [],
        "private": False,
    }


def _batch_result(rows: list[dict]) -> dict:
    """Counts by outcome, then every row, for a batch tool."""
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {
        "rows_given": len(rows),
        "outcomes": counts,
        "rows": rows,
        "message": ", ".join(f"{n} {status}" for status, n in counts.items()) or "No rows.",
    }


def _conflict(value: str, flag: str) -> dict:
    """The refusal for a value passed together with the flag that clears it."""
    return {
        "error": "conflicting_arguments",
        "message": f"Pass either {value} or {flag}, not both.",
    }


def _same_date(old: Any, new: dict) -> bool:
    """Whether a stored Date dict already says what a parsed one says."""
    if not isinstance(old, dict):
        return False
    keys = ("calendar", "modifier", "quality", "text")
    return all((old.get(k) or 0) == (new.get(k) or 0) for k in keys) and list(
        old.get("dateval") or []
    ) == list(new.get("dateval") or [])


def _is_unknown_type(value: Any) -> bool:
    """Whether a Gramps type value, string or dict, is unset or Unknown."""
    if value is None or value == "":
        return True
    if isinstance(value, dict):
        return not value.get("string") and value.get("value") in (None, -1)
    return str(value).strip().lower() == "unknown"


class _ConcurrentChange(RuntimeError):
    """Another session changed what a multi-step write had read."""


def _birth_position(crefs: list[dict], births: dict[str, int], child: str) -> int:
    """Where a child goes among siblings: before the first born after it; last if undated."""
    mine = births.get(child) or 0
    if not mine:
        return len(crefs)
    for index, cref in enumerate(crefs):
        theirs = births.get(cref.get("ref")) or 0
        if theirs > mine:
            return index
    return len(crefs)


def _with_new_names(result: dict, new_names: list[str]) -> dict:
    """Say which attribute names a reference write used for the first time."""
    if new_names:
        result["new_attribute_names"] = new_names
        result["message"] += (
            f" ({', '.join(repr(n) for n in new_names)} used nowhere else on this event or "
            "in the tree: check the spelling)"
        )
    return result


def _with_repairs(out: dict, result: dict) -> dict:
    """Carry what :meth:`GrampsService._mutate` repaired into a shaped result."""
    if result.get("repaired"):
        out["repaired"] = result["repaired"]
        out["message"] += f" (also repaired: {'; '.join(result['repaired'])})"
    return out


# --------------------------------------------------------------------------- #
# formatting helpers
# --------------------------------------------------------------------------- #
def _write_result(object_type: str, created: dict) -> dict:
    """Shape a create response as a :class:`~gramps_evidence_mcp.models.WriteResult`."""
    return {
        "handle": created["handle"],
        "gramps_id": created.get("gramps_id"),
        "object_type": object_type,
        "message": _created_message(object_type, created, []),
    }


def _created_message(object_type: str, created: dict, unsourced: list[str]) -> str:
    """Build the human-readable message for a create, naming unsourced facts."""
    msg = f"Created {object_type} {created.get('gramps_id') or created.get('handle')}"
    if unsourced:
        msg += f" (UNSOURCED: {', '.join(unsourced)})"
    return msg


def _type_string(t: Any) -> str:
    """Render a Gramps type, which may be a dict or a bare string, as a string."""
    if isinstance(t, dict):
        return t.get("string") or str(t.get("value", ""))
    return str(t) if t is not None else ""


def _date_string(date: Any) -> str | None:
    """Render a Gramps ``Date`` dict as Gramps displays it, in English.

    Delegates to :func:`mapping.date_display`, which keeps the modifier, the
    quality and both ends of a range or span: ``"between 1882 and 1883"``,
    never ``"1882"``.

    Returns
    -------
    str or None
        The formatted date, or None if there is nothing to show.
    """
    return mapping.date_display(date) if isinstance(date, dict) else None


def _year_from_profile(entry: Any) -> int | None:
    """Profiles express birth/death as a dict with a display 'date' string."""
    if not entry:
        return None
    if isinstance(entry, dict):
        for key in ("year",):
            if entry.get(key):
                return int(entry[key])
        date = entry.get("date")
        if date:
            found = re.findall(r"\d{3,4}", str(date))
            if found:
                return int(found[-1])
    elif isinstance(entry, str):
        found = re.findall(r"\d{3,4}", entry)
        if found:
            return int(found[-1])
    return None


def _name_from_person(person: dict) -> str:
    """Render a person's primary name as ``"Given Surname"``, else ``"(unknown)"``."""
    name = person.get("primary_name") or {}
    given = name.get("first_name", "")
    surnames = name.get("surname_list") or []
    surname = surnames[0].get("surname", "") if surnames else ""
    return " ".join(p for p in (given, surname) if p) or "(unknown)"


def _unsourced_reason(event: dict, unsourced_attr: str) -> str | None:
    """Explain why an event counts as unsourced.

    Parameters
    ----------
    event : dict
        The event object.
    unsourced_attr : str
        Attribute name marking a deliberate escape-hatch write.

    Returns
    -------
    str or None
        ``"tagged-unsourced"``, ``"no-citation"``, or None if it is cited.
    """
    # An escape-hatch event has both the tag and an empty citation_list; the
    # tag is the more informative answer, so it is checked first.
    for attr in event.get("attribute_list", []):
        if _type_string(attr.get("type")) == unsourced_attr:
            return "tagged-unsourced"
    if not event.get("citation_list"):
        return "no-citation"
    return None


def _format_person(obj: dict, expose_private: bool) -> dict:
    """Shape a person object for tool output.

    Parameters
    ----------
    obj : dict
        Person as returned by the API, with ``extended`` and ``profile``.
    expose_private : bool
        Include private records when True.

    Returns
    -------
    dict
        Name, gender, events with citation counts, families and media.
    """
    extended = obj.get("extended", {}) or {}
    profile = obj.get("profile", {}) or {}
    events = []
    for ev_ref, ev in zip(
        obj.get("event_ref_list", []), extended.get("events", []) or [], strict=False
    ):
        events.append(
            {
                "type": _type_string(ev.get("type")),
                "date": _date_string(ev.get("date")),
                "role": _type_string(ev_ref.get("role")),
                "citation_count": len(ev.get("citation_list") or []),
                "gramps_id": ev.get("gramps_id"),
            }
        )
    return {
        "handle": obj["handle"],
        "gramps_id": obj.get("gramps_id"),
        "name": profile.get("name") or _name_from_person(obj),
        "primary_name": _name_entry(obj.get("primary_name") or {}),
        "alternate_names": [
            {"index": i, **_name_entry(n)} for i, n in enumerate(obj.get("alternate_names") or [])
        ],
        "gender": mapping.gender_label(obj.get("gender")),
        "private": obj.get("private", False),
        "events": events,
        "citation_count": len(obj.get("citation_list") or []),
        "family_handles": obj.get("family_list", []),
        "parent_family_handles": obj.get("parent_family_list", []),
        "media_count": len(obj.get("media_list") or []),
    }


def _name_entry(name: dict) -> dict:
    """One name as get_person shows it: the parts NameMatch selects on."""
    return {
        "type": _type_string(name.get("type")) or None,
        "given": name.get("first_name") or "",
        "surname": _surname_of(name),
        "citation_count": len(name.get("citation_list") or []),
    }


def _format_family(obj: dict) -> dict:
    """Shape a family object for tool output: relationship, parents, children."""
    return {
        "handle": obj["handle"],
        "gramps_id": obj.get("gramps_id"),
        "relationship": _type_string(obj.get("type")),
        "father_handle": obj.get("father_handle"),
        "mother_handle": obj.get("mother_handle"),
        "child_handles": [c.get("ref") for c in obj.get("child_ref_list", [])],
        "event_count": len(obj.get("event_ref_list") or []),
    }
