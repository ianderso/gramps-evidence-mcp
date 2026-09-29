"""Read-only reference layer over legacy GEDCOM exports.

Independent of the Gramps Web database. Lets a caller consult untrusted legacy
trees as hints while building a cited tree, reporting for every claimed fact
whether the legacy tree cited a source and, if so, the source text. Nothing
here is written back to Gramps.

The tokenizer is pure-Python GEDCOM 5.5.1. Ancestry's dialect adds custom
underscore tags such as ``_APID`` and ``_TREE``; these parse as ordinary tags
and are carried along without special-casing.

Files are parsed on first consultation and cached on disk as JSON, keyed by a
fingerprint of the source file's path, size and mtime.

File content is treated as data, never as instructions.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .privacy import assess

logger = logging.getLogger("gramps_evidence_mcp.gedcom")

# Bump when the cache JSON schema below changes, to invalidate stale caches.
_CACHE_VERSION = 6


def apid_to_ancestry_url(apid: str) -> str | None:
    """Turn an Ancestry ``_APID`` token into a record URL.

    The result is a lead to the underlying record, not proof in itself.

    Parameters
    ----------
    apid : str
        An ``_APID`` token, e.g. ``"1,61903::1234567"`` for database 61903,
        record 1234567.

    Returns
    -------
    str or None
        The Ancestry record-view URL, or None if the token is unparseable.
    """
    if not apid or "::" not in apid:
        return None
    left, _, record_id = apid.partition("::")
    db_id = left.split(",")[-1].strip()
    record_id = record_id.strip()
    if not db_id or not record_id:
        return None
    return f"https://www.ancestry.com/discoveryui-content/view/{record_id}:{db_id}"


# --------------------------------------------------------------------------- #
# Low-level tokenizer: GEDCOM line -> nested record tree
# --------------------------------------------------------------------------- #
@dataclass
class GedLine:
    """One parsed GEDCOM line plus its descendant lines.

    Attributes
    ----------
    level : int
        GEDCOM nesting level.
    tag : str
        The line's tag, e.g. ``"INDI"``.
    xref : str or None
        Record id with delimiters stripped, e.g. ``"I1"`` from ``"@I1@"``.
    value : str or None
        Trailing text, which may itself be a pointer.
    pointer : str or None
        Dereferenced pointer id when ``value`` was one.
    children : list of GedLine
        Lines nested below this one.
    """

    level: int
    tag: str
    xref: str | None = None
    value: str | None = None
    pointer: str | None = None
    children: list[GedLine] = field(default_factory=list)

    def first(self, tag: str) -> GedLine | None:
        """Return the first child with ``tag``, or None."""
        for c in self.children:
            if c.tag == tag:
                return c
        return None

    def all(self, tag: str) -> list[GedLine]:
        """Return every child with ``tag``."""
        return [c for c in self.children if c.tag == tag]


_XREF = "@"


def _strip_xref(token: str) -> str | None:
    """Strip the ``@`` delimiters from an xref token, or return None."""
    if len(token) >= 2 and token.startswith(_XREF) and token.endswith(_XREF):
        return token[1:-1]
    return None


def _parse_lines(text: str) -> list[GedLine]:
    """Tokenize GEDCOM text into a forest of level-0 records.

    Parameters
    ----------
    text : str
        The GEDCOM file's contents.

    Returns
    -------
    list of GedLine
        Level-0 records, each carrying its descendants.
    """
    roots: list[GedLine] = []
    stack: list[GedLine] = []  # stack[i] is the open line at level i

    for raw in text.splitlines():
        line = raw.rstrip("\r\n")
        if not line.strip():
            continue
        # Format: LEVEL [@XREF@] TAG [VALUE]
        parts = line.lstrip().split(" ", 1)
        if not parts[0].isdigit():
            # CONT/CONC continuation of a malformed line, or junk; skip safely.
            continue
        level = int(parts[0])
        rest = parts[1] if len(parts) > 1 else ""

        xref: str | None = None
        maybe_xref = rest.split(" ", 1)
        if maybe_xref and (x := _strip_xref(maybe_xref[0])):
            xref = x
            rest = maybe_xref[1] if len(maybe_xref) > 1 else ""

        tag_value = rest.split(" ", 1)
        tag = tag_value[0].upper() if tag_value else ""
        value = tag_value[1] if len(tag_value) > 1 else None
        pointer = _strip_xref(value) if value else None

        node = GedLine(level=level, tag=tag, xref=xref, value=value, pointer=pointer)

        # Handle CONT (new line) / CONC (concatenation) by folding into parent.
        if tag in ("CONT", "CONC") and stack:
            parent = stack[-1] if level > stack[-1].level else _find_parent(stack, level)
            if parent is not None:
                sep = "\n" if tag == "CONT" else ""
                parent.value = (parent.value or "") + sep + (value or "")
                continue

        if level == 0:
            roots.append(node)
            stack = [node]
        else:
            parent = _find_parent(stack, level)
            if parent is None:
                # Malformed; attach to last root as best effort.
                if roots:
                    roots[-1].children.append(node)
                continue
            parent.children.append(node)
            del stack[level:]
            stack.append(node)
    return roots


def _find_parent(stack: list[GedLine], level: int) -> GedLine | None:
    """Find the open line one level above ``level``."""
    for node in reversed(stack):
        if node.level == level - 1:
            return node
    return None


# --------------------------------------------------------------------------- #
# Structured, cache-friendly extraction
# --------------------------------------------------------------------------- #
@dataclass
class RefFact:
    """A single claimed fact about an individual from a legacy tree.

    Attributes
    ----------
    kind : str
        Fact type, e.g. ``"Birth"`` or ``"Residence"``.
    date, place, detail : str or None
        As claimed by the legacy tree.
    has_source : bool
        Whether the legacy tree attached any citation at all.
    source_text : list of str
        Resolved source description lines.
    apid : list of str
        Ancestry ``_APID`` tokens.
    media : list of str
        ``OBJE``/``FILE`` references.
    record_urls : list of str
        Direct ``WWW``/``URL`` record links.
    transcription : list of str
        Record text from ``DATA/TEXT`` or ``TEXT``.
    """

    kind: str
    date: str | None = None
    place: str | None = None
    detail: str | None = None
    has_source: bool = False
    source_text: list[str] = field(default_factory=list)
    apid: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    record_urls: list[str] = field(default_factory=list)
    transcription: list[str] = field(default_factory=list)

    @property
    def ancestry_urls(self) -> list[str]:
        """list of str: Record URLs derived from :attr:`apid`."""
        urls = [apid_to_ancestry_url(a) for a in self.apid]
        return [u for u in urls if u]

    @property
    def all_urls(self) -> list[str]:
        """list of str: Record links, direct ones first, deduplicated."""
        seen: list[str] = []
        for u in self.record_urls + self.ancestry_urls:
            if u and u not in seen:
                seen.append(u)
        return seen


@dataclass
class RefPerson:
    """One individual as the legacy tree describes them.

    Attributes
    ----------
    xref : str
        The record's GEDCOM id.
    name : str
        Display name, ``"Given Surname"``.
    given, surname, sex : str or None
        Name parts and sex as recorded.
    birth_year, death_year : int or None
        Years extracted from the corresponding events.
    died : bool
        A death event is recorded, whether or not it carries a year.
    facts : list of RefFact
        Every claimed fact, including family events.
    apid : list of str
        Person-level Ancestry ``_APID`` tokens.
    media : list of str
        Person-level ``OBJE``/``FILE`` references.
    notes : list of str
        Biographical ``NOTE`` text.
    """

    xref: str
    name: str
    given: str | None = None
    surname: str | None = None
    sex: str | None = None
    birth_year: int | None = None
    death_year: int | None = None
    died: bool = False
    facts: list[RefFact] = field(default_factory=list)
    apid: list[str] = field(default_factory=list)
    media: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def ancestry_urls(self) -> list[str]:
        """list of str: Record URLs derived from :attr:`apid`."""
        urls = [apid_to_ancestry_url(a) for a in self.apid]
        return [u for u in urls if u]


# Event GEDCOM tags we surface, mapped to friendly names.
_EVENT_TAGS = {
    "BIRT": "Birth",
    "CHR": "Christening",
    "BAPM": "Baptism",
    "DEAT": "Death",
    "BURI": "Burial",
    "MARR": "Marriage",
    "DIV": "Divorce",
    "RESI": "Residence",
    "OCCU": "Occupation",
    "CENS": "Census",
    "IMMI": "Immigration",
    "EMIG": "Emigration",
    "NATU": "Naturalization",
    "PROB": "Probate",
    "WILL": "Will",
    "GRAD": "Graduation",
    "RETI": "Retirement",
    "RELI": "Religion",
    "NATI": "Nationality",
    "PROP": "Property",
    "CREM": "Cremation",
    "EVEN": "Event",
}


def _year_from_date(date: str | None) -> int | None:
    """Extract a year from a GEDCOM date string.

    Takes the last 3-4 digit run, which handles ``"ABT 1900"``,
    ``"12 JAN 1899"`` and ``"1900/1901"``.
    """
    if not date:
        return None
    import re

    matches = re.findall(r"\d{3,4}", date)
    if matches:
        try:
            return int(matches[-1])
        except ValueError:
            return None
    return None


class _SourceResolver:
    """Resolves inline and pointer SOUR references to human-readable text."""

    def __init__(self, sources: dict[str, GedLine], repos: dict[str, GedLine]):
        """Index top-level ``SOUR`` and ``REPO`` records by xref."""
        self._sources = sources
        self._repos = repos

    def text_for(self, sour: GedLine) -> list[str]:
        """Render one citation as human-readable lines.

        Parameters
        ----------
        sour : GedLine
            A ``SOUR`` sub-record, either a pointer to a top-level source or an
            inline description.

        Returns
        -------
        list of str
            Labelled lines: title, author, publication, page, record text.
        """
        out: list[str] = []
        if sour.pointer and sour.pointer in self._sources:
            rec = self._sources[sour.pointer]
            for tag, label in (
                ("TITL", "Title"),
                ("AUTH", "Author"),
                ("PUBL", "Publication"),
                ("ABBR", "Abbrev"),
            ):
                node = rec.first(tag)
                if node and node.value:
                    out.append(f"{label}: {node.value}")
            note = rec.first("NOTE")
            if note and note.value:
                out.append(f"Note: {note.value}")
        elif sour.value and not sour.pointer:
            out.append(sour.value)  # inline free-text description
        # PAGE and DATA/TEXT hang under the citing SOUR, not the source record.
        page = sour.first("PAGE")
        if page and page.value:
            out.append(f"Page: {page.value}")
        data = sour.first("DATA")
        if data:
            txt = data.first("TEXT")
            if txt and txt.value:
                out.append(f"Text: {txt.value}")
        txt = sour.first("TEXT")
        if txt and txt.value:
            out.append(f"Text: {txt.value}")
        return out


def _collect_media(node: GedLine) -> list[str]:
    """Collect OBJE/FILE media references hanging under a line."""
    out: list[str] = []
    for obje in node.all("OBJE"):
        file_node = obje.first("FILE")
        if file_node and file_node.value:
            out.append(file_node.value)
        elif obje.value:
            out.append(obje.value)
    return out


def _collect_urls_text(node: GedLine, fact: RefFact) -> None:
    """Add record URLs and transcribed text from ``node`` into ``fact``.

    Reads ``WWW``/``URL`` links and ``DATA/TEXT`` or ``TEXT`` content hanging
    under a citation or source node. Mutates ``fact`` in place, skipping
    duplicates.
    """
    for www in node.all("WWW") + node.all("URL"):
        if www.value and www.value not in fact.record_urls:
            fact.record_urls.append(www.value)
    data = node.first("DATA")
    text_nodes = list(node.all("TEXT"))
    if data:
        text_nodes += data.all("TEXT")
        for www in data.all("WWW") + data.all("URL"):
            if www.value and www.value not in fact.record_urls:
                fact.record_urls.append(www.value)
    for txt in text_nodes:
        if txt.value and txt.value not in fact.transcription:
            fact.transcription.append(txt.value)


def _collect_sources(event: GedLine, resolver: _SourceResolver, fact: RefFact) -> None:
    """Populate ``fact``'s citation fields from ``event``'s ``SOUR`` children.

    Gathers source text, ``_APID`` tokens from both the citation and the
    resolved source record, media and URLs. Mutates ``fact`` in place.
    """
    for sour in event.all("SOUR"):
        fact.has_source = True
        fact.source_text.extend(resolver.text_for(sour))
        _collect_urls_text(sour, fact)
        for apid in sour.all("_APID"):  # on the citation
            if apid.value:
                fact.apid.append(apid.value)
        # ...and on the resolved source record, with its media and urls.
        if sour.pointer and sour.pointer in resolver._sources:
            rec = resolver._sources[sour.pointer]
            for apid in rec.all("_APID"):
                if apid.value and apid.value not in fact.apid:
                    fact.apid.append(apid.value)
            fact.media.extend(_collect_media(rec))
            _collect_urls_text(rec, fact)
        fact.media.extend(_collect_media(sour))
    fact.media.extend(_collect_media(event))


def _extract_people(roots: list[GedLine]) -> list[RefPerson]:
    """Build a :class:`RefPerson` for every ``INDI`` record in ``roots``."""
    sources = {r.xref: r for r in roots if r.tag == "SOUR" and r.xref}
    repos = {r.xref: r for r in roots if r.tag == "REPO" and r.xref}
    families = {r.xref: r for r in roots if r.tag == "FAM" and r.xref}
    resolver = _SourceResolver(sources, repos)

    people: list[RefPerson] = []
    for rec in roots:
        if rec.tag != "INDI" or not rec.xref:
            continue
        person = _extract_one_person(rec, resolver)
        _attach_family_events(person, rec, families, resolver)
        people.append(person)
    return people


def _extract_one_person(rec: GedLine, resolver: _SourceResolver) -> RefPerson:
    """Build a :class:`RefPerson` from one ``INDI`` record and its events."""
    name_node = rec.first("NAME")
    given = surname = None
    display = ""
    if name_node:
        if g := name_node.first("GIVN"):
            given = g.value
        if s := name_node.first("SURN"):
            surname = s.value
        if name_node.value:
            # "John /Smith/" form.
            display = name_node.value.replace("/", " ").strip()
            display = " ".join(display.split())
            if given is None or surname is None:
                parts = name_node.value.split("/")
                if given is None and parts[0].strip():
                    given = parts[0].strip()
                if surname is None and len(parts) > 1 and parts[1].strip():
                    surname = parts[1].strip()
    if not display:
        display = " ".join(p for p in (given, surname) if p)

    sex_node = rec.first("SEX")
    person = RefPerson(
        xref=rec.xref or "",
        name=display or "(unknown)",
        given=given,
        surname=surname,
        sex=sex_node.value if sex_node else None,
        apid=[a.value for a in rec.all("_APID") if a.value],
        media=_collect_media(rec),
        notes=[n.value for n in rec.all("NOTE") if n.value and not n.pointer],
    )

    for ev in rec.children:
        if ev.tag not in _EVENT_TAGS:
            continue
        fact = _event_to_fact(ev, resolver)
        person.facts.append(fact)
        if ev.tag == "BIRT" and person.birth_year is None:
            person.birth_year = _year_from_date(fact.date)
        elif ev.tag in ("CHR", "BAPM") and person.birth_year is None:
            person.birth_year = _year_from_date(fact.date)
        elif ev.tag == "DEAT" and person.death_year is None:
            person.death_year = _year_from_date(fact.date)
            person.died = True
    return person


def _event_to_fact(ev: GedLine, resolver: _SourceResolver) -> RefFact:
    """Convert one event line into a :class:`RefFact` with its citations."""
    date_node = ev.first("DATE")
    place_node = ev.first("PLAC")
    # A custom EVEN carries its real kind in a TYPE subtag ("Military Service"...).
    kind = _EVENT_TAGS.get(ev.tag, ev.tag.title())
    if ev.tag == "EVEN":
        type_node = ev.first("TYPE")
        if type_node and type_node.value:
            kind = type_node.value
    fact = RefFact(
        kind=kind,
        date=date_node.value if date_node else None,
        place=place_node.value if place_node else None,
        detail=ev.value if ev.value and not ev.pointer else None,
    )
    _collect_sources(ev, resolver, fact)
    # Ancestry sometimes hangs SOUR under DATE/PLAC too.
    for sub in (date_node, place_node):
        if sub:
            _collect_sources(sub, resolver, fact)
    return fact


def _attach_family_events(
    person: RefPerson,
    rec: GedLine,
    families: dict[str, GedLine],
    resolver: _SourceResolver,
) -> None:
    """Pull MARR/DIV events from families this person is a spouse in."""
    for fams in rec.all("FAMS"):
        fam = families.get(fams.pointer or "")
        if not fam:
            continue
        for ev in fam.children:
            if ev.tag not in ("MARR", "DIV"):
                continue
            person.facts.append(_event_to_fact(ev, resolver))


# --------------------------------------------------------------------------- #
# Public API: lazy, disk-cached reference files
# --------------------------------------------------------------------------- #
def _person_to_dict(p: RefPerson) -> dict:
    """Serialize a :class:`RefPerson` for the on-disk cache."""
    return {
        "xref": p.xref,
        "name": p.name,
        "given": p.given,
        "surname": p.surname,
        "sex": p.sex,
        "birth_year": p.birth_year,
        "death_year": p.death_year,
        "died": p.died,
        "apid": p.apid,
        "media": p.media,
        "notes": p.notes,
        "facts": [
            {
                "kind": f.kind,
                "date": f.date,
                "place": f.place,
                "detail": f.detail,
                "has_source": f.has_source,
                "source_text": f.source_text,
                "apid": f.apid,
                "media": f.media,
                "record_urls": f.record_urls,
                "transcription": f.transcription,
            }
            for f in p.facts
        ],
    }


def _person_from_dict(d: dict) -> RefPerson:
    """Rebuild a :class:`RefPerson` from its cached form."""
    p = RefPerson(
        xref=d["xref"],
        name=d["name"],
        given=d.get("given"),
        surname=d.get("surname"),
        sex=d.get("sex"),
        birth_year=d.get("birth_year"),
        death_year=d.get("death_year"),
        died=d.get("died", False),
        apid=d.get("apid", []),
        media=d.get("media", []),
        notes=d.get("notes", []),
    )
    p.facts = [
        RefFact(
            kind=f["kind"],
            date=f.get("date"),
            place=f.get("place"),
            detail=f.get("detail"),
            has_source=f.get("has_source", False),
            source_text=f.get("source_text", []),
            apid=f.get("apid", []),
            media=f.get("media", []),
            record_urls=f.get("record_urls", []),
            transcription=f.get("transcription", []),
        )
        for f in d.get("facts", [])
    ]
    return p


def _fingerprint(path: Path) -> str:
    """Hash a file's path, size, mtime and cache version into a cache key."""
    st = path.stat()
    raw = f"{path.resolve()}|{st.st_size}|{int(st.st_mtime)}|v{_CACHE_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


class ReferenceFile:
    """A single lazily-parsed, disk-cached legacy GEDCOM.

    Parameters
    ----------
    path : Path
        The GEDCOM file.
    label : str
        Short name reported with every answer from this file.
    trust : str
        Provenance note travelling with every answer from this file.
    cache_dir : Path
        Directory holding parsed caches.
    """

    def __init__(self, path: Path, label: str, trust: str, cache_dir: Path):
        self.path = path
        self.label = label
        self.trust = trust
        self._cache_dir = cache_dir
        self._people: list[RefPerson] | None = None

    def _cache_path(self) -> Path:
        """Path of this file's parsed cache, keyed by :func:`_fingerprint`."""
        return self._cache_dir / f"{self.path.stem}.{_fingerprint(self.path)}.json"

    def _load(self) -> list[RefPerson]:
        """Return the parsed people, reading the cache or parsing on first call.

        Returns
        -------
        list of RefPerson
            Every individual in the file.

        Raises
        ------
        FileNotFoundError
            If the configured GEDCOM is missing.
        """
        if self._people is not None:
            return self._people
        if not self.path.exists():
            raise FileNotFoundError(f"Reference GEDCOM not found: {self.path}")

        cache = self._cache_path()
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                self._people = [_person_from_dict(d) for d in data["people"]]
                logger.info(
                    "reference '%s' loaded from cache (%d people)", self.label, len(self._people)
                )
                return self._people
            except (json.JSONDecodeError, KeyError, OSError):
                logger.warning("reference '%s' cache unreadable; reparsing", self.label)

        text = self.path.read_text(encoding="utf-8-sig", errors="replace")
        roots = _parse_lines(text)
        people = _extract_people(roots)
        self._people = people
        try:
            self._cache_dir.mkdir(parents=True, exist_ok=True)
            # Clear stale caches for this stem so the dir doesn't grow unbounded.
            for old in self._cache_dir.glob(f"{self.path.stem}.*.json"):
                if old != cache:
                    old.unlink(missing_ok=True)
            cache.write_text(
                json.dumps({"people": [_person_to_dict(p) for p in people]}),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("could not write reference cache for '%s'", self.label)
        logger.info("reference '%s' parsed (%d people)", self.label, len(people))
        return people

    def search(
        self, name: str, approx_birth_year: int | None, year_tolerance: int = 5
    ) -> Iterator[RefPerson]:
        """Yield individuals matching a name and optional birth year.

        Parameters
        ----------
        name : str
            Case-insensitive substring, matched against the display name and
            against the given name and surname separately.
        approx_birth_year : int or None
            Birth year to match. People with no recorded birth year are not
            excluded by it.
        year_tolerance : int, optional
            Permitted difference from ``approx_birth_year``.

        Yields
        ------
        RefPerson
            Each matching individual.
        """
        needle = name.lower().strip()
        for person in self._load():
            if needle and needle not in person.name.lower():
                fields = " ".join(x for x in (person.given, person.surname) if x).lower()
                if needle not in fields:
                    continue
            if approx_birth_year is not None and person.birth_year is not None:
                if abs(person.birth_year - approx_birth_year) > year_tolerance:
                    continue
            yield person


def _match_dict(person: RefPerson) -> dict:
    """Render one matched individual as the dict ``consult`` returns.

    Includes Ancestry ``_APID`` pointers as both tokens and URLs, plus media
    references -- the metadata worth mining when chasing the underlying record.
    """
    return {
        "name": person.name,
        "sex": person.sex,
        "birth_year": person.birth_year,
        "death_year": person.death_year,
        "apid": person.apid,
        "ancestry_urls": person.ancestry_urls,
        "media": person.media,
        "notes": person.notes,
        "facts": [
            {
                "kind": f.kind,
                "date": f.date,
                "place": f.place,
                "detail": f.detail,
                "has_source": f.has_source,
                "source_text": f.source_text,
                "apid": f.apid,
                "record_urls": f.record_urls,
                "record_links": f.all_urls,
                "transcription": f.transcription,
                "ancestry_urls": f.ancestry_urls,
                "media": f.media,
            }
            for f in person.facts
        ],
    }


class ReferenceLibrary:
    """All configured legacy GEDCOMs, consulted together. Read-only.

    Parameters
    ----------
    files : list of ReferenceFile
        The configured reference files.
    """

    def __init__(self, files: list[ReferenceFile]):
        self._files = files

    @classmethod
    def from_config(cls, reference_files, cache_dir: Path) -> ReferenceLibrary:
        """Build a library from ``config.reference_files``.

        Parameters
        ----------
        reference_files : list of ReferenceFileConfig
            Configured entries.
        cache_dir : Path
            Directory for parsed caches.

        Returns
        -------
        ReferenceLibrary
            A library over those files.
        """
        return cls(
            [ReferenceFile(rf.path, rf.label, rf.trust, cache_dir) for rf in reference_files]
        )

    @property
    def labels(self) -> list[str]:
        """list of str: The configured files' labels."""
        return [f.label for f in self._files]

    def consult(
        self,
        name: str,
        approx_birth_year: int | None,
        year_tolerance: int = 5,
        *,
        withhold_living: bool = False,
        current_year: int | None = None,
    ) -> list[dict]:
        """Search every configured file for an individual.

        Parameters
        ----------
        name : str
            Case-insensitive substring of the name.
        approx_birth_year : int or None
            Birth year to match.
        year_tolerance : int, optional
            Permitted difference from ``approx_birth_year``.
        withhold_living : bool, optional
            Leave out probably-living people, by the rule in
            :func:`privacy.assess`, and count them instead. Exports from
            Ancestry and similar sites do not privatize anyone, so the file
            itself offers no protection.
        current_year : int, optional
            Year to judge living against. Defaults to this year.

        Returns
        -------
        list of dict
            One entry per configured file, carrying its label, trust note,
            path, matches and ``withheld_count``. Each matched fact is flagged
            sourced or unsourced with any source text. A missing or
            unreadable file becomes an ``error`` key on its entry rather than
            an exception.
        """
        year = current_year or datetime.now().year
        results: list[dict] = []
        for ref in self._files:
            entry: dict = {
                "label": ref.label,
                "trust": ref.trust,
                "path": str(ref.path),
                "matches": [],
                "withheld_count": 0,
            }
            try:
                for person in ref.search(name, approx_birth_year, year_tolerance):
                    if (
                        withhold_living
                        and assess(
                            private_flag=False,
                            birth_year=person.birth_year,
                            death_year=person.death_year,
                            died=person.died,
                            current_year=year,
                        ).restricted
                    ):
                        # Not even a stub: the only handle on a GEDCOM person
                        # is the name that was searched for.
                        entry["withheld_count"] += 1
                        continue
                    entry["matches"].append(_match_dict(person))
            except FileNotFoundError as exc:
                entry["error"] = str(exc)
            results.append(entry)
        return results
