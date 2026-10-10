"""Translation between the tool-facing models and the Gramps wire JSON.

Pure functions: they build and parse dicts and perform no I/O. Orchestration
lives in :mod:`gramps_evidence_mcp.service`.

Type fields such as event ``type`` are sent as plain English strings, which
gramps-webapi coerces to internal Gramps type dicts. Dates are structured, so
:func:`parse_date` builds a full Gramps ``Date`` dict, falling back to a
text-only Date rather than dropping an unparseable string.
"""

from __future__ import annotations

import re
from typing import Any

from .models import Confidence, Gender, NameParts

# ---- enum <-> int -----------------------------------------------------------
_GENDER_TO_INT = {Gender.female: 0, Gender.male: 1, Gender.unknown: 2}
_INT_TO_GENDER = {0: "female", 1: "male", 2: "unknown", 3: "other"}

_CONFIDENCE_TO_INT = {
    Confidence.very_low: 0,
    Confidence.low: 1,
    Confidence.normal: 2,
    Confidence.high: 3,
    Confidence.very_high: 4,
}
_INT_TO_CONFIDENCE = {v: k.value for k, v in _CONFIDENCE_TO_INT.items()}


def gender_to_int(g: Gender) -> int:
    """Convert a :class:`~gramps_evidence_mcp.models.Gender` to its Gramps int."""
    return _GENDER_TO_INT[g]


def gender_label(value: int | None) -> str:
    """Convert a Gramps gender int to its label, defaulting to ``"unknown"``."""
    return _INT_TO_GENDER.get(value if value is not None else 2, "unknown")


def confidence_to_int(c: Confidence) -> int:
    """Convert a :class:`~gramps_evidence_mcp.models.Confidence` to 0-4."""
    return _CONFIDENCE_TO_INT[c]


def confidence_label(value: int | None) -> str:
    """Convert a Gramps confidence int to its label, defaulting to ``"normal"``."""
    return _INT_TO_CONFIDENCE.get(value if value is not None else 2, "normal")


# ---- Date parsing -----------------------------------------------------------
# Gramps Date constants (see gramps.gen.lib.date). MOD_FROM and MOD_TO arrived
# in Gramps 5.2: an open-ended "from 1880" or "to 1890". gramps-webapi 3.21.1
# requires 4 dateval entries for them and 8 for a range or span.
CAL_GREGORIAN = 0
(
    MOD_NONE,
    MOD_BEFORE,
    MOD_AFTER,
    MOD_ABOUT,
    MOD_RANGE,
    MOD_SPAN,
    MOD_TEXTONLY,
    MOD_FROM,
    MOD_TO,
) = range(9)
QUAL_NONE, QUAL_ESTIMATED, QUAL_CALCULATED = 0, 1, 2

_MONTHS = {
    m: i
    for i, m in enumerate(
        ["", "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
    )
    if m
}
_MONTHS_FULL = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

_MODIFIER_WORDS = {
    "bef": MOD_BEFORE,
    "before": MOD_BEFORE,
    "aft": MOD_AFTER,
    "after": MOD_AFTER,
    "abt": MOD_ABOUT,
    "about": MOD_ABOUT,
    "circa": MOD_ABOUT,
    "ca": MOD_ABOUT,
    "c": MOD_ABOUT,
    "from": MOD_FROM,
    "to": MOD_TO,
}


def _empty_date() -> dict:
    """Build a Gramps ``Date`` dict with every field at its zero value."""
    return {
        "_class": "Date",
        "calendar": CAL_GREGORIAN,
        "modifier": MOD_NONE,
        "quality": QUAL_NONE,
        "dateval": [0, 0, 0, False],
        "text": "",
        "sortval": 0,
        "newyear": 0,
    }


def _text_date(text: str) -> dict:
    """Build a text-only Gramps ``Date`` preserving ``text`` verbatim."""
    d = _empty_date()
    d["modifier"] = MOD_TEXTONLY
    d["text"] = text
    return d


_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{1,2})(?:-(\d{1,2}))?\s*$")


def _parse_ymd(token_str: str) -> tuple[int, int, int] | None:
    """Parse one date fragment into day, month and year.

    Parameters
    ----------
    token_str : str
        A single date, e.g. ``"12 Jan 1899"``, ``"Jan 1899"``, ``"1899"`` or
        ``"1899-01-12"``.

    Returns
    -------
    tuple of int or None
        ``(day, month, year)``, zero for absent parts, or None if unparseable.
    """
    # ISO year-first is unambiguous, so it precedes the day/month heuristic.
    iso = _ISO_RE.match(token_str)
    if iso:
        year = int(iso.group(1))
        month = int(iso.group(2))
        day = int(iso.group(3)) if iso.group(3) else 0
        return day, month, year

    day = month = year = 0
    tokens = re.split(r"[\s./-]+", token_str.strip())
    for tok in tokens:
        low = tok.lower().strip(".,")
        if not low:
            continue
        if low in _MONTHS:
            month = _MONTHS[low]
        elif low in _MONTHS_FULL:
            month = _MONTHS_FULL[low]
        elif low.isdigit():
            n = int(low)
            if n > 31 or (year == 0 and len(low) == 4):
                year = n
            elif day == 0 and n <= 31 and month == 0 and n > 12 and year == 0:
                # ambiguous; if >12 must be a day
                day = n
            elif day == 0 and n <= 31:
                day = n
            else:
                year = n
    if year == 0 and day == 0 and month == 0:
        return None
    return day, month, year


def parse_date(text: str | None, *, open_spans: bool = True) -> dict:
    """Build a Gramps ``Date`` dict from free text.

    Recognises exact dates (``"12 Jan 1899"``, ``"Jan 1899"``, ``"1899"``,
    ``"1899-01-12"``), qualifiers (``"ABT 1900"``, ``"BEF 1950"``,
    ``"AFT 1850"``), estimated or calculated quality (``"EST 1900"``,
    ``"CAL 1900"``), and the two kinds of interval Gramps distinguishes:

    - a range, ``"BET 1898 AND 1901"``: it happened once, somewhere in it;
    - a span, ``"FROM 1864 TO 1865"``: it lasted the whole of it.

    A lone ``"FROM 1880"`` or ``"TO 1890"`` is a span open at one end.

    Parameters
    ----------
    text : str or None
        The date as written. None or empty yields an empty Date.
    open_spans : bool, optional
        Whether the server stores a lone "from" or "to" (Gramps 5.2 and
        later). When False such a date is kept as text, never as a plain
        date with the word dropped.

    Returns
    -------
    dict
        A Gramps ``Date`` dict. Unparseable input becomes a text-only Date
        carrying the original string, so nothing is silently dropped.
    """
    if not text or not text.strip():
        return _empty_date()
    raw = text.strip()
    work = raw.lower()

    quality = QUAL_NONE
    if work.startswith(("est ", "estimated ")):
        quality = QUAL_ESTIMATED
        work = work.split(" ", 1)[1].strip()
    elif work.startswith(("cal ", "calculated ")):
        quality = QUAL_CALCULATED
        work = work.split(" ", 1)[1].strip()

    for pattern, compound in (
        (r"(?:bet|between)\.?\s+(.*?)\s+and\s+(.*)", MOD_RANGE),
        (r"from\s+(.*?)\s+to\s+(.*)", MOD_SPAN),
    ):
        m = re.fullmatch(pattern, work)
        if m:
            start = _parse_ymd(m.group(1))
            stop = _parse_ymd(m.group(2))
            if start and stop:
                d = _empty_date()
                d["modifier"] = compound
                d["quality"] = quality
                d["dateval"] = [
                    start[0],
                    start[1],
                    start[2],
                    False,
                    stop[0],
                    stop[1],
                    stop[2],
                    False,
                ]
                return d
            return _text_date(raw)

    modifier = MOD_NONE
    first = work.split(" ", 1)[0].strip(".")
    if first in _MODIFIER_WORDS:
        modifier = _MODIFIER_WORDS[first]
        work = work[len(first) :].strip().lstrip(".").strip()
        if modifier in (MOD_FROM, MOD_TO) and not open_spans:
            return _text_date(raw)

    parsed = _parse_ymd(work)
    if parsed is None:
        return _text_date(raw)
    day, month, year = parsed
    d = _empty_date()
    d["modifier"] = modifier
    d["quality"] = quality
    d["dateval"] = [day, month, year, False]
    return d


def is_open_span(text: str | None) -> bool:
    """Whether ``text`` parses as a span open at one end ("from X", "to X")."""
    return parse_date(text)["modifier"] in (MOD_FROM, MOD_TO)


#: Gramps' English date displayer, with the ISO date format.
_MODIFIER_TEXT = {
    MOD_BEFORE: "before ",
    MOD_AFTER: "after ",
    MOD_ABOUT: "about ",
    MOD_FROM: "from ",
    MOD_TO: "to ",
}
_QUALITY_TEXT = {QUAL_ESTIMATED: "estimated ", QUAL_CALCULATED: "calculated "}
_CALENDARS = {
    1: "Julian",
    2: "Hebrew",
    3: "French Republican",
    4: "Persian",
    5: "Islamic",
    6: "Swedish",
}


def _iso(day: Any, month: Any, year: Any) -> str:
    """One date in Gramps' ISO display format: ``1899``, ``1899-01``, ``1899-01-12``."""
    out = f"{int(year):04d}" if year else "????"
    if month:
        out += f"-{int(month):02d}"
        if day:
            out += f"-{int(day):02d}"
    elif day:
        out += f"-??-{int(day):02d}"
    return out


def date_display(date: dict | None) -> str | None:
    """Render a Gramps ``Date`` dict as Gramps displays it, in English.

    Every part of the date that carries meaning is shown: the modifier
    (``"about 1900"``), the quality (``"estimated 1900"``), both ends of a
    range or span (``"between 1882 and 1883"``, ``"from 1864-05-04 to
    1864-09-16"``), and a calendar other than Gregorian. Showing only the
    first year of a range would state a precision the tree does not hold.

    Parameters
    ----------
    date : dict or None
        A Gramps ``Date`` dict.

    Returns
    -------
    str or None
        The rendering, or None for an empty date.
    """
    if not isinstance(date, dict):
        return None
    modifier = date.get("modifier") or MOD_NONE
    text = (date.get("text") or "").strip()
    dateval = date.get("dateval") or []
    if modifier == MOD_TEXTONLY:
        return text or None
    if not isinstance(dateval, list) or len(dateval) < 3 or not any(dateval[:3]):
        return text or None
    start = _iso(*dateval[:3])
    if modifier in (MOD_RANGE, MOD_SPAN) and len(dateval) >= 7:
        stop = _iso(*dateval[4:7])
        body = f"between {start} and {stop}" if modifier == MOD_RANGE else f"from {start} to {stop}"
    else:
        body = _MODIFIER_TEXT.get(modifier, "") + start
    out = _QUALITY_TEXT.get(date.get("quality") or QUAL_NONE, "") + body
    calendar = _CALENDARS.get(date.get("calendar") or CAL_GREGORIAN)
    return f"{out} ({calendar})" if calendar else out


def year_from_date_dict(date: dict | None) -> int | None:
    """Extract a year from a Gramps ``Date`` dict.

    Used by privacy assessment and search, where an approximate year is more
    useful than none.

    Parameters
    ----------
    date : dict or None
        A Gramps ``Date`` dict.

    Returns
    -------
    int or None
        The structured year, else the last 3-4 digit run in the date text,
        else None.
    """
    if not date:
        return None
    dateval = date.get("dateval")
    if isinstance(dateval, list) and len(dateval) >= 3 and dateval[2]:
        return int(dateval[2])
    text = date.get("text")
    if text:
        found = re.findall(r"\d{3,4}", text)
        if found:
            return int(found[-1])
    return None


def age_between(birth: dict | None, at: dict | None) -> str | None:
    """Whole years from a birth date to another date, as an age: ``"34 years"``.

    ``"about 34 years"`` when either date is less than a known day -- a year or
    month alone, a modifier such as "before", an estimated or calculated date,
    a range or span (its start is used). A rough answer, made in Python from
    the stored dates where the server's own would need a request per event.

    Parameters
    ----------
    birth, at : dict or None
        Gramps ``Date`` dicts.

    Returns
    -------
    str or None
        The age; None for a text-only date, one with no year, or a date
        before the birth.
    """

    def parts(date: dict | None) -> tuple[int, int, int, bool] | None:
        if not date or date.get("modifier") == MOD_TEXTONLY:
            return None
        dateval = date.get("dateval") or []
        if len(dateval) < 3 or not dateval[2]:
            return None
        day, month, year = (int(v or 0) for v in dateval[:3])
        exact = bool(day and month) and not date.get("modifier") and not date.get("quality")
        return year, month, day, exact

    born, then = parts(birth), parts(at)
    if not born or not then:
        return None
    years = then[0] - born[0]
    if born[1] and then[1] and (then[1], then[2]) < (born[1], born[2]):
        years -= 1
    if years < 0:
        return None
    age = f"{years} year{'' if years == 1 else 's'}"
    return age if born[3] and then[3] else f"about {age}"


# ---- payload builders -------------------------------------------------------
def name_payload(name: NameParts) -> dict:
    """Build a Gramps ``Name`` dict for use as a person's primary name.

    Parameters
    ----------
    name : NameParts
        The name components.

    Returns
    -------
    dict
        A Gramps ``Name`` dict with a single primary surname.
    """
    surname: dict[str, Any] = {"surname": name.surname, "primary": True}
    if name.prefix:
        surname["prefix"] = name.prefix
    return {
        "_class": "Name",
        "first_name": name.given,
        "surname_list": [surname],
        "suffix": name.suffix,
        "title": name.title,
        "call": name.call,
        "nick": name.nick,
    }


def person_payload(name: NameParts, gender: Gender) -> dict:
    """Build a minimal Gramps ``Person`` payload.

    Parameters
    ----------
    name : NameParts
        The person's primary name.
    gender : Gender
        The person's gender.

    Returns
    -------
    dict
        A ``Person`` dict with empty reference lists. The server assigns the
        handle and gramps_id.
    """
    return {
        "_class": "Person",
        "primary_name": name_payload(name),
        "gender": gender_to_int(gender),
        "event_ref_list": [],
        "family_list": [],
        "parent_family_list": [],
        "citation_list": [],
    }


def event_payload(
    event_type: str,
    date: str | dict | None,
    place_handle: str | None,
    description: str | None,
    citation_handles: list[str],
) -> dict:
    """Build a Gramps ``Event`` payload.

    Parameters
    ----------
    event_type : str
        Plain English event type, e.g. ``"Birth"``. Coerced server-side.
    date : str, dict or None
        Date as free text, parsed by :func:`parse_date`, or a Date dict
        already parsed.
    place_handle : str or None
        Handle of an existing place. Omitted from the payload when None.
    description : str or None
        Free-text description. Omitted when None.
    citation_handles : list of str
        Citations supporting the event.

    Returns
    -------
    dict
        A Gramps ``Event`` dict.
    """
    payload: dict[str, Any] = {
        "_class": "Event",
        "type": event_type,  # string; server coerces to EventType
        "date": date if isinstance(date, dict) else parse_date(date),
        "citation_list": list(citation_handles),
    }
    if place_handle:
        payload["place"] = place_handle
    if description:
        payload["description"] = description
    return payload


def event_ref(event_handle: str, role: str = "Primary") -> dict:
    """Build a Gramps ``EventRef`` pointing at ``event_handle`` with ``role``."""
    return {
        "_class": "EventRef",
        "ref": event_handle,
        "role": role,
        "attribute_list": [],
        "note_list": [],
    }


def citation_payload(
    source_handle: str,
    page: str,
    confidence: Confidence,
    date: str | dict | None,
    note_handles: list[str] | None = None,
) -> dict:
    """Build a Gramps ``Citation`` payload.

    Parameters
    ----------
    source_handle : str
        Handle of the source being cited.
    page : str
        Where in the source the fact appears.
    confidence : Confidence
        How strongly the source supports the fact.
    date : str, dict or None
        Date the source was recorded or accessed, as text or a parsed Date.
    note_handles : list of str, optional
        Notes to attach.

    Returns
    -------
    dict
        A Gramps ``Citation`` dict.
    """
    return {
        "_class": "Citation",
        "source_handle": source_handle,
        "page": page,
        "confidence": confidence_to_int(confidence),
        "date": date if isinstance(date, dict) else parse_date(date),
        "note_list": note_handles or [],
    }


def source_payload(title: str, author: str | None, pubinfo: str | None) -> dict:
    """Build a Gramps ``Source`` payload; None author or pubinfo becomes ``""``."""
    return {
        "_class": "Source",
        "title": title,
        "author": author or "",
        "pubinfo": pubinfo or "",
    }


def unsourced_attribute(attr_name: str) -> dict:
    """Build the attribute stamped on an event recorded without a citation.

    Parameters
    ----------
    attr_name : str
        Attribute name, from ``config.unsourced_attribute``.

    Returns
    -------
    dict
        A Gramps ``Attribute`` dict with value ``"true"``.
    """
    return {"_class": "Attribute", "type": attr_name, "value": "true"}


def place_payload(name: str) -> dict:
    """Build a bare Gramps ``Place`` payload whose title and name are ``name``."""
    return {
        "_class": "Place",
        "name": {"_class": "PlaceName", "value": name},
        "title": name,
    }
