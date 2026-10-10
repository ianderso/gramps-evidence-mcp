"""Test fixtures: an in-memory fake gramps-webapi served via respx.

Rather than mock each HTTP call, we stand up a tiny stateful fake that mimics
the parts of gramps-webapi the service uses: JWT login, object create (assigns
handle + gramps_id, returns a change-record array), get-by-handle,
get-by-gramps_id, list, and count. This lets tests exercise the *real*
GrampsWebClient + GrampsService orchestration end-to-end.
"""

from __future__ import annotations

import copy
import hashlib
import io
import ipaddress
import itertools
import json
import mimetypes
import operator
import os
import re
import socket
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from gramps_evidence_mcp.client import ENDPOINTS, GrampsWebClient
from gramps_evidence_mcp.config import Config
from gramps_evidence_mcp.service import GrampsService

_SEG_TO_TYPE = {seg: typ for typ, seg in ENDPOINTS.items()}
_GID_PREFIX = {
    "person": "I",
    "family": "F",
    "event": "E",
    "source": "S",
    "citation": "C",
    "repository": "R",
    "place": "P",
    "media": "O",
    "note": "N",
    "tag": "T",
}


#: What gramps-webapi stores for a field a request leaves out, by class, and
#: which class each nested field holds. Recorded from a real server by
#: tests/live/capture_defaults.py; tests/live/test_contract_live.py checks it
#: against one.
_SERVER_SHAPES = json.loads(
    (Path(__file__).parent / "fixtures" / "server_defaults.json").read_text()
)


def _complete(cls: str | None, value: Any) -> Any:
    """Store an object as the server does: every field its class has, no ``_class``.

    The server builds a Gramps object from what it is sent, so a field the
    request leaves out is stored with its default, inside nested objects too,
    and a key the class does not have is kept as sent -- through 3.22; from
    3.23 :func:`_refuse_unknown_keys` refuses it first (docs/PITFALLS.md
    section 18). A date's sort value is recomputed whatever was sent.
    """
    if not isinstance(value, dict):
        return value
    cls = value.get("_class") or cls
    out = copy.deepcopy(_SERVER_SHAPES["defaults"].get(cls or "", {}))
    nested = _SERVER_SHAPES["nested"].get(cls or "", {})
    for key, item in value.items():
        if key == "_class":
            continue
        kind = nested.get(key)
        if isinstance(kind, list) and isinstance(item, list):
            item = [_complete(kind[0], entry) for entry in item]
        elif isinstance(kind, str):
            item = _complete(kind, item)
        out[key] = item
    if cls == "Date":
        out["sortval"] = _sortval(out)
    return out


def _admit(cls: str, payload: dict) -> httpx.Response | None:
    """Refuse a null the server refuses; store what it stores for the rest.

    The server validates a write against Gramps' schema: most top-level
    fields sent as null are refused with 400, a family's parent handles are
    stored as "", a date as the empty date. Edits ``payload`` in place;
    returns the refusal, or None.
    """
    for key, value in list(payload.items()):
        if value is not None:
            continue
        rule = _SERVER_SHAPES["nulls"].get(cls, {}).get(key)
        if rule == "refused":
            return httpx.Response(
                400,
                json={
                    "code": 400,
                    "message": f"Error while processing object: $.{key}: None is not of "
                    "the type the schema requires",
                },
            )
        if rule is not None:
            payload[key] = copy.deepcopy(rule)
    return None


#: Classes whose instances name another object by ``ref``.
_REF_CLASSES = {"ChildRef", "EventRef", "MediaRef", "PersonRef", "PlaceRef", "RepoRef"}


def _class_keys(cls: str) -> set[str] | None:
    """The keys a class has, from the recorded defaults; None if not recorded."""
    defaults = _SERVER_SHAPES["defaults"].get(cls)
    if defaults is None:
        return None
    keys = set(defaults) | {"_class"}
    if cls in _CLASS_TO_TYPE:
        keys |= {"handle", "change"} | (set() if cls == "Tag" else {"gramps_id"})
    if cls in _REF_CLASSES:
        keys.add("ref")
    return keys


def _refuse_unknown_keys(cls: str | None, value: Any, path: str = "$") -> str | None:
    """What gramps-webapi 3.23 and later check before storing a write.

    A date's ``year``, served but never stored, is dropped (the only key the
    server computes); any other key the object's class lacks, at any depth,
    is refused, naming where it is: ``$.name: unknown PlaceName keys: 'x'``
    (``_validate_keys`` in its ``api/resources/util.py``). Edits ``value``
    in place; returns the refusal, or None. A class the defaults do not
    record is not checked.
    """
    if isinstance(value, list):
        for i, item in enumerate(value):
            if refusal := _refuse_unknown_keys(cls, item, f"{path}[{i}]"):
                return refusal
        return None
    if not isinstance(value, dict):
        return None
    cls = value.get("_class") or cls
    if cls == "Date":
        value.pop("year", None)
    known = _class_keys(cls) if cls else None
    if known is not None:
        unknown = sorted(key for key in value if key not in known)
        if unknown:
            return f"{path}: unknown {cls} keys: {', '.join(repr(k) for k in unknown)}"
    nested = _SERVER_SHAPES["nested"].get(cls or "", {})
    for key, item in value.items():
        kind = nested.get(key)
        if refusal := _refuse_unknown_keys(
            kind[0] if isinstance(kind, list) else kind, item, f"{path}.{key}"
        ):
            return refusal
    return None


def _sortval(date: dict) -> int:
    """Gramps' sort value for a Gregorian date: the day number of its start.

    A zero day or month counts as the first (``Date._zero_adjust_ymd``); a
    date with no year, month or day sorts as 0. Other calendars keep what was
    sent, which no test relies on.
    """
    dateval = date.get("dateval") or []
    if len(dateval) < 3 or not any(dateval[:3]):
        return 0
    if date.get("calendar", 0) != 0:
        return date.get("sortval", 0)
    day, month, year = (int(v or 0) for v in dateval[:3])
    year, month, day = year or 1, max(month, 1), max(day, 1)
    # gramps.gen.lib.gcalendar.gregorian_sdn
    year += 4801 if year < 0 else 4800
    if month > 2:
        month -= 3
    else:
        month += 9
        year -= 1
    return (
        ((year // 100) * 146097) // 4
        + ((year % 100) * 1461) // 4
        + (month * 153 + 2) // 5
        + day
        - 32045
    )


def _sort_key(value: Any) -> tuple:
    """Order as SQL does: missing values first, then by value."""
    return (value is not None, value if value is not None else 0)


def _served(value: Any) -> Any:
    """An object as the server serves it: no ``_class``, and a ``year`` on every date.

    The year is added only where none is stored, so a year a client wrote
    back is served even after the date changes (docs/PITFALLS.md section 24).
    """
    if isinstance(value, dict):
        out = {k: _served(v) for k, v in value.items() if k != "_class"}
        dateval = value.get("dateval")
        if isinstance(dateval, list) and "year" not in out:
            out["year"] = dateval[2] if len(dateval) >= 3 else 0
        return out
    if isinstance(value, list):
        return [_served(v) for v in value]
    return value


class FakeGramps:
    def __init__(self) -> None:
        self.store: dict[str, dict[str, dict]] = {t: {} for t in ENDPOINTS}
        self._handles = itertools.count(1)
        self._gids: dict[str, itertools.count] = {t: itertools.count(1) for t in ENDPOINTS}
        self.requests: list[tuple[str, str, Any]] = []  # (method, type, payload)
        self.write_forbidden = False  # simulate read-only/locked DB (HTTP 403)
        self.merges: list[tuple[str, str, str]] = []  # (type, keep, drop)
        #: The undo log's per-object changes, as GET .../history/objects/ reads it.
        self.history: list[dict] = []
        self._history_txn = 0
        self.undos: list[int] = []
        self.transactions: list[dict] = []
        #: PUTs that sent fewer fields than the stored object carried. The real
        #: API replaces the whole record, so such a write destroys data --
        #: docs/PITFALLS.md section 1. Recorded rather than rejected so a test
        #: can assert the whole surface stays clean.
        self.partial_writes: list[tuple[str, str, list[str]]] = []
        #: Trees this account can reach. Several makes tree resolution
        #: ambiguous, which the service must refuse rather than guess.
        self.trees: list[dict] = [{"id": "tree1", "name": "My Tree"}]
        #: task_id -> the status document GET /api/tasks/{id} returns.
        self.tasks: dict[str, dict] = {}
        #: Findings the next verification run reports.
        self.verify_findings: list[dict] = []
        #: When set, verification dispatches to the background instead.
        self.verify_task_id: str | None = None
        self.verify_params: dict[str, str] = {}
        #: (handle1, handle2) -> the Relationship document to return.
        self.relationships: dict[tuple[str, str], Any] = {}
        #: handle -> {"living": bool} and the estimated-dates document.
        self.living: dict[str, bool] = {}
        self.living_dates: dict[str, dict] = {}
        #: handle -> timeline event profiles, as the server makes them with
        #: ratings on and the anchor not omitted (see :func:`timeline_row`).
        self.timelines: dict[str, list[dict]] = {}
        self.timeline_params: dict[str, str] = {}
        #: (handle1, handle2) -> span wording.
        self.spans: dict[tuple[str, str], str] = {}
        self.reindex_calls: list[dict[str, str]] = []
        #: Bodies posted to a /query/ endpoint, newest last.
        self.query_bodies: list[tuple[str, dict]] = []
        #: object type -> rows the next structured query returns.
        self.query_rows: dict[str, list[dict]] = {}
        self.query_cursor: Any = None
        #: What GET /api/facts/ returns: one entry per statistic.
        self.facts: list[dict] = [
            {
                "key": "person_oldestdied",
                "description": "Oldest person at death",
                "objects": [
                    {
                        "object": "Person",
                        "gramps_id": "I0001",
                        "handle": "h1",
                        "name": "Sample Person",
                        "value": "95 years",
                    }
                ],
            },
        ]
        #: Query parameters of every GET /api/facts/, newest last.
        self.facts_params: list[dict[str, str]] = []
        #: Report descriptors the instance offers.
        self.reports: list[dict] = [
            {
                "id": "ancestor_report",
                "name": "Ahnentafel Report",
                "description": "Produces a textual ancestral report",
                "report_modes": ["standard"],
                "options_dict": {"pid": "", "maxgen": 10, "living_people": 99, "off": "print"},
            }
        ]
        #: Query params each run_report call was made with.
        self.report_runs: list[tuple[str, dict[str, str]]] = []
        self.report_task_id: str | None = "report1"
        #: namespace -> {"filters": [...], "rules": [...]}
        self.filters: dict[str, dict] = {
            "people": {
                "filters": [],
                "rules": [
                    {
                        "rule": "IsDescendantOf",
                        "name": "Descendants of <person>",
                        "category": "Descendant filters",
                        "description": "Matches all descendants",
                        "labels": ["ID:", "Inclusive:"],
                    },
                ],
            },
        }
        self.created_filters: list[tuple[str, dict]] = []
        self.deleted_filters: list[tuple[str, str]] = []
        #: Consolidated timeline rows and the params they were asked for.
        self.consolidated: list[dict] = []
        self.consolidated_params: dict[str, str] = {}
        self.task_list: list[dict] = []
        #: handle -> DNA match documents the API would return.
        self.dna_matches: dict[str, list[dict]] = {}
        #: handle -> Y-DNA document.
        self.ydna: dict[str, dict] = {}
        #: Segments the parser returns next, and the strings it was given.
        #: What the segment parser returns. None parses the input the way
        #: the server does; a list answers every input with that list.
        self.parsed_segments: list[dict] | None = None
        self.parser_inputs: list[str] = []
        self.researcher: dict = {"name": "", "email": ""}
        #: What GET /api/metadata/ reports. gramps-webapi 3.21.1 runs on
        #: Gramps 6.0, which stores "from X" and "to X" dates.
        self.metadata: dict = {
            "gramps": {"version": "6.0.4"},
            "gramps_webapi": {"version": "3.21.1"},
            # What 3.21.1 reports with Tesseract installed (seen 2026-10-06).
            "server": {
                "ocr": True,
                "ocr_languages": ["dan", "deu", "eng", "fra", "lat", "nld", "nor", "swe"],
                "task_queue": False,
            },
        }
        #: handle -> (bytes, Content-Type) of each media file uploaded.
        self.files: dict[str, tuple[bytes, str]] = {}
        #: What Tesseract reads off any image, and the query of each OCR request.
        self.ocr_text = "OCRED TEXT"
        self.ocr_requests: list[dict[str, str]] = []
        #: When set, OCR answers 202 and a task, as a server with a task queue does.
        self.ocr_queued = False
        #: When set, OCR fails 501 with this message, as it does without Tesseract.
        self.ocr_unavailable: str | None = None
        #: Sizes each thumbnail was asked for, by media handle.
        self.thumbnails: list[tuple[str, int]] = []
        #: The respx router the fixtures serve the fake through; a test adds
        #: routes to it to answer for another host (Transkribus, an archive).
        self.router: Any = None
        #: The tree's custom type names, served by GET /api/types/ under
        #: "custom" beside the standard names recorded from a real server.
        #: Gramps adds a name when it stores an object carrying it and never
        #: takes one off, so these only grow (_learn_custom_types). Widowhood
        #: stands for a custom type the tree already had.
        self.custom_types: dict[str, set[str]] = {key: set() for key in _CUSTOM_TYPE_STANDARD}
        self.custom_types["event_types"].add("Widowhood")
        #: When set, DELETE commits and then answers this status, as
        #: gramps-webapi 3.21.1 does when its search-index step fails after
        #: the transaction has landed.
        self.delete_error_after_commit: int | None = None
        #: When set, DELETE fails with this status and deletes nothing.
        self.delete_error_before_commit: int | None = None
        #: When set, every PUT fails with this status and writes nothing.
        self.put_error: int | None = None
        #: When set, a PUT writes and then answers this status, as a write
        #: whose step after the commit fails does (PITFALLS 17).
        self.put_error_after_commit: int | None = None
        #: When set, every POST of one object fails with this status and
        #: writes nothing, as a commit that waited out SQLite's lock does
        #: (PITFALLS 6).
        self.post_error: int | None = None
        #: When set, a POST of one object writes and then answers this status.
        self.post_error_after_commit: int | None = None
        #: When set, the request carrying a file's bytes loses its connection,
        #: "before" the server stores anything or "after" it has committed, or
        #: is "refused" a connection at all (TOOL-REQUESTS #31).
        self.upload_drop: str | None = None
        #: Content-Type of every POST /api/media/, newest last.
        self.media_posts: list[str] = []
        #: When set, a new media object is stored with this mime type, whatever
        #: was sent: a server that stored something other than the file.
        self.media_mime_stored: str | None = None
        #: When set, GET /api/metadata/ fails with this status, once.
        self.metadata_error: int | None = None
        self.event_type_map: dict[str, str] = {
            "-1": "Unknown",
            "0": "Custom",
            "1": "Marriage",
            "12": "Birth",
            "13": "Death",
            "15": "Baptism",
            "19": "Burial",
            "21": "Census",
        }

    def _new_handle(self) -> str:
        return f"h{next(self._handles):06d}"

    def _new_gid(self, typ: str) -> str:
        return f"{_GID_PREFIX[typ]}{next(self._gids[typ]):04d}"

    def _change_record(self, typ: str, obj: dict, kind: str = "add") -> list[dict]:
        return [
            {
                "type": kind,
                "_class": typ.capitalize() if typ != "repository" else "Repository",
                "handle": obj["handle"],
                "old": None,
                "new": obj,
            }
        ]

    # ---- request handlers ----
    def handle(self, request: httpx.Request) -> httpx.Response:
        """Answer a request; record every object a write changed, as the undo log does.

        The server's history holds a change for each object a transaction
        touched -- the person a new family names, the event a delete removes
        a reference from -- not only the one the request named. Diffing the
        store across the write records exactly that.
        """
        before = None
        if request.method in ("POST", "PUT", "DELETE") and "/token/" not in request.url.path:
            before = copy.deepcopy(self.store)
        try:
            return self._dispatch(request)
        finally:
            # A write that landed is in the log even when a later step failed,
            # or the connection did, before the answer (PITFALLS 17); a
            # refused one changed nothing.
            if before is not None:
                self._record_history(before, _txn_description(request))
                self._learn_custom_types()

    def _learn_custom_types(self) -> None:
        """Add each type name a stored object carries that is not a standard one.

        The server keeps a name it does not know exactly, case and all, as a
        new custom type (docs/PITFALLS.md section 26), and lists it from then on.
        """
        for typ, objects in self.store.items():
            for obj in objects.values():
                for key, value in _type_fields(typ, obj):
                    name = value.get("string") if isinstance(value, dict) else value
                    standard = _SERVER_SHAPES["types"][_CUSTOM_TYPE_STANDARD[key]]
                    if isinstance(name, str) and name and name not in standard:
                        self.custom_types[key].add(name)

    def _record_history(self, before: dict, description: str = "Edit") -> None:
        """Log a write as the undo log does: a transaction of per-object changes.

        Each change keeps the object's state before and after it, which the
        server serves as ``old_data`` and ``new_data`` when asked (``{}`` for
        no state). The transaction goes into :attr:`transactions`, which
        ``GET /api/transactions/history/`` pages through as 3.21 does.
        """
        changes = []
        for typ, objects in self.store.items():
            old = before[typ]
            changes += [(typ, h, 0) for h in objects if h not in old]
            changes += [(typ, h, 1) for h in objects if h in old and old[h] != objects[h]]
            changes += [(typ, h, 2) for h in old if h not in objects]
        if not changes:
            return
        self._history_txn += 1
        now = time.time()
        connection = {
            "id": self._history_txn,
            "timestamp": now,
            "user": {"name": "mcp", "full_name": ""},
        }
        logged = []
        for number, (typ, handle, kind) in enumerate(changes, 1):
            states = {
                "_old": copy.deepcopy(before[typ].get(handle)),
                "_new": copy.deepcopy(self.store[typ].get(handle)),
            }
            change = {
                "obj_class": _TYPE_TO_CLASS[typ],
                "trans_type": kind,
                "obj_handle": handle,
                "ref_handle": None,
                "timestamp": now,
            }
            self.history.append(
                {
                    "id": len(self.history) + 1,
                    **change,
                    **states,
                    "connection": connection,
                    "transaction_id": self._history_txn,
                }
            )
            logged.append({"id": number, **change, **states})
        self.transactions.append(
            {
                "id": self._history_txn,
                "description": description,
                "timestamp": now,
                "first": 1,
                "last": len(logged),
                "undo": False,
                "connection": connection,
                "changes": logged,
            }
        )

    def _server_at_least(self, major: int, minor: int) -> bool:
        """Whether the gramps-webapi version the fake plays is this one or later."""
        version = str((self.metadata.get("gramps_webapi") or {}).get("version") or "")
        return tuple(int(v) for v in re.findall(r"\d+", version)[:2]) >= (major, minor)

    def _refusal(self, cls: str, payload: dict, label: str = "object") -> httpx.Response | None:
        """3.23 and later: refuse an unknown key, drop a served year (PITFALLS 18, 24)."""
        if not self._server_at_least(3, 23):
            return None
        refusal = _refuse_unknown_keys(cls, payload)
        if refusal is None:
            return None
        return httpx.Response(
            400, json={"code": 400, "message": f"Error while processing {label}: {refusal}"}
        )

    def _object_history(self, request: httpx.Request, cls: str, handle: str) -> httpx.Response:
        """GET /api/transactions/history/objects/{class}/{handle}, added in 3.22."""
        if not self._server_at_least(3, 22):
            return httpx.Response(404, json={"message": "not found"})
        if cls not in _CLASS_TO_TYPE:
            return httpx.Response(422, json={"message": f"Unknown object class: {cls}"})
        found = [c for c in self.history if c["obj_class"] == cls and c["obj_handle"] == handle]
        params = request.url.params
        if params.get("sort") == "-id":
            found.reverse()
        if params.get("page"):
            size = int(params.get("pagesize") or 20)
            start = (int(params["page"]) - 1) * size
            page = found[start : start + size]
        else:
            page = found
        page = [_logged_change(c, params) for c in page]
        return httpx.Response(200, json=page, headers={"X-Total-Count": str(len(found))})

    def _timeline(
        self, endpoint: str, request: httpx.Request, rows: list[dict], anchor: str | None
    ) -> httpx.Response:
        """A timeline endpoint, as 3.21.1 to 3.23.1 answer (``api/resources/timeline.py``).

        An unknown query argument is refused with 422, as every gramps-webapi
        argument schema refuses one. ``citations`` and ``confidence`` come
        only with ``ratings``, and the anchor's own rows carry no person
        profile unless ``omit_anchor`` is off -- the two defaults that made
        get_timeline report every event uncited (TOOL-REQUESTS #30).
        """
        params = request.url.params
        unknown = sorted(set(params) - _TIMELINE_ARGS[endpoint])
        if unknown:
            return httpx.Response(
                422, json={"error": {"code": 422, "message": f"{unknown}: Unknown field."}}
            )
        for depth in ("ancestors", "offspring"):
            if depth in params and not 1 <= int(params[depth]) <= 5:
                return httpx.Response(422, json={"error": {"code": 422, "message": depth}})
        ratings = _query_bool(params.get("ratings"), default=False)
        omit_anchor = _query_bool(params.get("omit_anchor"), default=True)
        shaped = []
        for row in rows:
            row = copy.deepcopy(row)
            if not ratings:
                row.pop("citations", None)
                row.pop("confidence", None)
            person = row.get("person") or {}
            if anchor and omit_anchor and person.get("handle") == anchor:
                row["person"] = {"relationship": person.get("relationship", "self")}
            shaped.append(row)
        if params.get("page"):
            size = int(params.get("pagesize") or 20)
            start = (int(params["page"]) - 1) * size
            shaped = shaped[start : start + size]
        return httpx.Response(200, json=shaped)

    def _dispatch(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        method = request.method

        if path == "/api/token/" and method == "POST":
            return httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref"})
        if path == "/api/token/refresh/" and method == "POST":
            return httpx.Response(200, json={"access_token": "acc"})

        m_hist = re.match(r"^/api/transactions/history/objects/([A-Za-z]+)/([^/]+)/?$", path)
        if m_hist and method == "GET":
            return self._object_history(request, m_hist.group(1), m_hist.group(2))
        if path.startswith("/api/transactions/history"):
            return self._transactions(path, method, request)
        if path == "/api/trees/" and method == "GET":
            return httpx.Response(200, json=self.trees)
        m_verify = re.match(r"^/api/trees/([^/]+)/verify$", path)
        if m_verify and method == "POST":
            self.verify_params = dict(request.url.params)
            if self.verify_task_id:
                return httpx.Response(201, json={"task": {"id": self.verify_task_id}})
            return httpx.Response(200, json=self.verify_findings)
        m_rel = re.match(r"^/api/relations/([^/]+)/([^/]+?)(/all)?$", path)
        if m_rel and method == "GET":
            key = (m_rel.group(1), m_rel.group(2))
            found = self.relationships.get(key)
            if found is None:
                found = {
                    "relationship_string": "",
                    "distance_common_origin": -1,
                    "distance_common_other": -1,
                }
            if m_rel.group(3) and not isinstance(found, list):
                found = [found]
            return httpx.Response(200, json=found)
        m_living = re.match(r"^/api/living/([^/]+?)(/dates)?$", path)
        if m_living and method == "GET":
            handle = m_living.group(1)
            if m_living.group(2):
                return httpx.Response(200, json=self.living_dates.get(handle, {}))
            return httpx.Response(200, json={"living": self.living.get(handle, False)})
        m_tl = re.match(r"^/api/(people|families)/([^/]+)/timeline$", path)
        if m_tl and method == "GET":
            kind, handle = m_tl.group(1), m_tl.group(2)
            self.timeline_params = dict(request.url.params)
            anchor = handle if kind == "people" else None
            return self._timeline(
                f"{kind}/timeline", request, self.timelines.get(handle, []), anchor
            )
        m_span = re.match(r"^/api/events/([^/]+)/span/([^/]+)$", path)
        if m_span and method == "GET":
            key = (m_span.group(1), m_span.group(2))
            return httpx.Response(200, json={"span": self.spans.get(key, "")})
        if path == "/api/search/index/" and method == "POST":
            self.reindex_calls.append(dict(request.url.params))
            return httpx.Response(201, json={"task": {"id": "reindex1"}})
        m_task = re.match(r"^/api/tasks/([^/]+)$", path)
        if m_task and method == "GET":
            task = self.tasks.get(m_task.group(1))
            if task is None:
                return httpx.Response(404, json={"message": "no such task"})
            return httpx.Response(200, json=task)
        if path == "/api/exporters/":
            return httpx.Response(200, json=[{"extension": "gramps"}, {"extension": "ged"}])
        if path.startswith("/api/exporters/") and path.endswith("/file"):
            return httpx.Response(200, content=b"<database>fake export</database>")
        m_map = re.match(r"^/api/types/default/([a-z_]+)/map$", path)
        if m_map and method == "GET":
            if m_map.group(1) != "event_types":
                return httpx.Response(404, json={"message": "no such datatype"})
            return httpx.Response(200, json=self.event_type_map)
        m_query = re.match(r"^/api/([a-z]+)/query/$", path)
        if m_query and method == "POST":
            typ_q = _SEG_TO_TYPE.get(m_query.group(1))
            if typ_q is None:
                return httpx.Response(404, json={"message": "unknown collection"})
            body = json.loads(request.content or b"{}")
            self.query_bodies.append((typ_q, body))
            if typ_q in self.query_rows:
                # Canned, for tests of what the service does with an answer.
                rows = list(self.query_rows[typ_q])
                total = len(rows)
            else:
                matched = self._query(typ_q, body)
                total = len(matched)
                rows = matched[: body.get("limit") or None]
            return httpx.Response(
                200,
                json={"items": rows, "next_after": self.query_cursor},
                headers={"X-Total-Count": str(total)},
            )
        if path == "/api/reports/" and method == "GET":
            return httpx.Response(200, json=self.reports)
        m_rep = re.match(r"^/api/reports/([^/]+)$", path)
        if m_rep and method == "GET":
            hit = [r for r in self.reports if r["id"] == m_rep.group(1)]
            if not hit:
                return httpx.Response(404, json={"message": "no such report"})
            return httpx.Response(200, json=hit[0])
        m_repf = re.match(r"^/api/reports/([^/]+)/file$", path)
        if m_repf and method == "POST":
            self.report_runs.append((m_repf.group(1), dict(request.url.params)))
            body: dict = {"file_name": f"{m_repf.group(1)}.pdf"}
            if self.report_task_id:
                body["task"] = {"id": self.report_task_id}
            return httpx.Response(201, json=body)
        if path == "/api/filters/" and method == "GET":
            return httpx.Response(200, json=self.filters)
        m_filt = re.match(r"^/api/filters/([^/]+)$", path)
        if m_filt and method == "GET":
            return httpx.Response(
                200, json=self.filters.get(m_filt.group(1), {"filters": [], "rules": []})
            )
        if m_filt and method == "POST":
            body = json.loads(request.content or b"{}")
            self.created_filters.append((m_filt.group(1), body))
            self.filters.setdefault(m_filt.group(1), {"filters": [], "rules": []})[
                "filters"
            ].append(body)
            return httpx.Response(201, json=body)
        m_filtn = re.match(r"^/api/filters/([^/]+)/([^/]+)$", path)
        if m_filtn and method == "DELETE":
            self.deleted_filters.append((m_filtn.group(1), m_filtn.group(2)))
            return httpx.Response(200, json={})
        m_ctl = re.match(r"^/api/timelines/(people|families)/$", path)
        if m_ctl and method == "GET":
            self.consolidated_params = dict(request.url.params)
            return self._timeline(
                f"timelines/{m_ctl.group(1)}",
                request,
                self.consolidated,
                request.url.params.get("anchor"),
            )
        if path == "/api/tasks/" and method == "GET":
            return httpx.Response(200, json=self.task_list)
        m_dna = re.match(r"^/api/people/([^/]+)/dna/matches$", path)
        if m_dna and method == "GET":
            handle = m_dna.group(1)
            if handle in self.dna_matches:
                return httpx.Response(200, json=self.dna_matches[handle])
            return httpx.Response(200, json=self._stored_dna_matches(handle))
        m_ydna = re.match(r"^/api/people/([^/]+)/ydna$", path)
        if m_ydna and method == "GET":
            return httpx.Response(200, json=self.ydna.get(m_ydna.group(1), {}))
        if path == "/api/parsers/dna-match" and method == "POST":
            body = json.loads(request.content or b"{}")
            self.parser_inputs.append(body.get("string", ""))
            if self.parsed_segments is not None:
                return httpx.Response(200, json=self.parsed_segments)
            return httpx.Response(200, json=_parse_segments(body.get("string", "")))
        if path == "/api/facts/" and method == "GET":
            # As gramps-webapi 3.21.1 answers: an anchor is only valid with a
            # built-in person filter, and a built-in filter needs an anchor.
            # Both mistakes are a bare 422 there.
            params = dict(request.url.params)
            self.facts_params.append(params)
            anchored = "handle" in params or "gramps_id" in params
            builtin = params.get("person") in (
                "Ancestors",
                "Descendants",
                "DescendantFamilies",
                "CommonAncestor",
            )
            if anchored != builtin:
                return httpx.Response(422, json={"code": 422, "status": "Unprocessable Entity"})
            return httpx.Response(200, json=self.facts)
        if path == "/api/metadata/researcher/" and method == "GET":
            return httpx.Response(200, json=self.researcher)
        if path == "/api/metadata/" and method == "GET":
            if self.metadata_error:
                status, self.metadata_error = self.metadata_error, None
                return httpx.Response(status, text="Bad Gateway")
            return httpx.Response(200, json=self.metadata)
        if path == "/api/types/":
            return httpx.Response(
                200,
                json={
                    "default": copy.deepcopy(_SERVER_SHAPES["types"]),
                    "custom": {k: sorted(v) for k, v in self.custom_types.items()},
                },
            )
        if path == "/api/objects/" and method == "POST":
            payloads = json.loads(request.content or b"[]")
            out = []
            for item in payloads:
                typ = _CLASS_TO_TYPE.get(item.get("_class", ""), "note")
                admitted = copy.deepcopy(item)
                refused = self._refusal(_TYPE_TO_CLASS[typ], admitted, "objects")
                if refused is None:
                    refused = _admit(_TYPE_TO_CLASS[typ], admitted)
                if refused is not None:
                    return refused
                obj = _complete(_TYPE_TO_CLASS[typ], admitted)
                # A handle the request makes is kept: Gramps Web's New Task
                # form links its source to its note by handles it made.
                obj["handle"] = obj.get("handle") or self._new_handle()
                obj.setdefault("gramps_id", self._new_gid(typ))
                obj["change"] = int(time.time())
                self.store[typ][obj["handle"]] = obj
                out.extend(self._change_record(typ, obj, "add"))
            return httpx.Response(201, json=out)
        if path == "/api/objects/delete-by-handle/" and method == "POST":
            for item in json.loads(request.content or b"[]"):
                typ = _CLASS_TO_TYPE.get(item.get("_class", ""))
                if typ:
                    self.store[typ].pop(item.get("handle"), None)
            return httpx.Response(200, json=[])

        m = re.match(r"^/api/([a-z]+)/(.*)?$", path)
        if not m:
            return httpx.Response(404, json={"message": "not found"})
        seg = m.group(1)
        rest = m.group(2) or ""
        typ = _SEG_TO_TYPE.get(seg)
        if typ is None:
            return httpx.Response(404, json={"message": f"unknown collection {seg}"})

        if rest == "":  # collection
            if method == "POST" and typ == "media":
                return self._create_media(request)
            if method == "POST":
                return self._create(typ, request)
            if method == "GET":
                return self._list_or_count(typ, request)
        else:  # single object or sub-resource
            handle = rest.split("/")[0]
            parts = rest.split("/")
            if len(parts) >= 3 and parts[1] == "merge" and method == "POST":
                return self._merge(typ, handle, parts[2])
            if rest.endswith("/ocr") and method == "POST":
                return self._ocr(handle, request)
            if rest.endswith("/file") and method == "PUT":
                return self._replace_file(handle, request)
            if rest.endswith("/file") and method == "GET":
                if handle not in self.store["media"] or handle not in self.files:
                    return httpx.Response(404, json={"message": "not found"})
                content, mime = self.files[handle]
                return httpx.Response(200, content=content, headers={"content-type": mime})
            m_thumb = re.match(r"^[^/]+/thumbnail/(\d+)$", rest)
            if m_thumb and method == "GET":
                return self._thumbnail(handle, int(m_thumb.group(1)))
            if method == "GET":
                return self._get(typ, handle, request.url.params)
            if method == "PUT":
                return self._update(typ, handle, request)
            if method == "DELETE":
                if handle not in self.store[typ]:
                    return httpx.Response(404, json={"message": "not found"})
                if self.delete_error_before_commit:
                    return httpx.Response(self.delete_error_before_commit, text="boom")
                self.store[typ].pop(handle, None)
                for objects in self.store.values():
                    for obj in objects.values():
                        _unreference(obj, handle)
                if self.delete_error_after_commit:
                    return httpx.Response(
                        self.delete_error_after_commit, text="<h1>Internal Server Error</h1>"
                    )
                return httpx.Response(200, json=[])
        return httpx.Response(405, json={"message": "method not allowed"})

    def _ocr(self, handle: str, request: httpx.Request) -> httpx.Response:
        """POST /api/media/{handle}/ocr, as 3.21.1 to 3.23.1 answer it.

        ``lang`` is required (422 without it); a missing media object is 404;
        without Tesseract, 501. A file that is not an image is answered
        ``{}``. ``string`` text comes back as the task's own return value,
        which Flask serves as ``text/html``; with a task queue, 202 and a task
        whose ``result_object`` is the text (``api/resources/ocr.py``,
        ``api/tasks.py``; the queued form seen on a live 3.21.1, 2026-10-06).
        """
        params = dict(request.url.params)
        self.ocr_requests.append(params)
        if not params.get("lang"):
            return httpx.Response(422, json={"code": 422, "status": "Unprocessable Entity"})
        media = self.store["media"].get(handle)
        if media is None:
            return httpx.Response(404, json={"message": "not found"})
        if self.ocr_unavailable:
            return httpx.Response(
                501, json={"error": {"code": 501, "message": self.ocr_unavailable}}
            )
        result: Any = self.ocr_text
        if not str(media.get("mime") or "").startswith("image"):
            result = {}
        if self.ocr_queued:
            task_id = f"ocr-{len(self.ocr_requests)}"
            self.tasks[task_id] = {
                "state": "SUCCESS",
                "result_object": result,
                "result": json.dumps(result),
                "info": json.dumps(result),
                "task_id": task_id,
                "name": "media_ocr",
            }
            return httpx.Response(
                202, json={"task": {"href": f"/api/tasks/{task_id}", "id": task_id}}
            )
        if isinstance(result, str):
            return httpx.Response(201, text=result, headers={"content-type": "text/html"})
        return httpx.Response(201, json=result)

    def _thumbnail(self, handle: str, size: int) -> httpx.Response:
        """GET .../thumbnail/{size}: the file scaled to ``size`` on its long edge, as AVIF.

        Every version from 3.21.1 to 3.23.1 answers AVIF (``send_thumbnail``
        in ``api/file.py``); a PDF is rendered from its first page, which the
        fake draws as a blank sheet.
        """
        from PIL import Image

        self.thumbnails.append((handle, size))
        if handle not in self.files:
            return httpx.Response(404, json={"message": "not found"})
        content, mime = self.files[handle]
        if mime == "application/pdf":
            img = Image.new("L", (850, 1100), 255)
        else:
            img = Image.open(io.BytesIO(content))
        img.thumbnail((size, size))
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="AVIF")
        return httpx.Response(200, content=buf.getvalue(), headers={"content-type": "image/avif"})

    def _create(self, typ: str, request: httpx.Request) -> httpx.Response:
        if self.write_forbidden:
            return httpx.Response(403, json={"message": "Forbidden: database is read-only"})
        if self.post_error:
            return httpx.Response(self.post_error, text="<h1>Internal Server Error</h1>")
        payload = json.loads(request.content or b"{}")
        admitted = copy.deepcopy(payload)
        refused = self._refusal(_TYPE_TO_CLASS[typ], admitted)
        if refused is None:
            refused = _admit(_TYPE_TO_CLASS[typ], admitted)
        if refused is not None:
            return refused
        # A handle the request carries is kept, and a second object with the
        # same one refused (add_object(fail_if_exists=True)): PITFALLS 28.
        if admitted.get("handle") and admitted["handle"] in self.store[typ]:
            return httpx.Response(
                400, json={"error": {"code": 400, "message": "Error while adding object"}}
            )
        obj = _complete(_TYPE_TO_CLASS[typ], admitted)
        obj["handle"] = admitted.get("handle") or self._new_handle()
        if typ != "tag":  # a tag has a name and a handle, no gramps_id
            obj.setdefault("gramps_id", self._new_gid(typ))
        obj["change"] = int(time.time())
        self.store[typ][obj["handle"]] = obj
        self.requests.append(("POST", typ, payload))
        if typ == "family":
            self._family_cascade(None, obj)
        if self.post_error_after_commit:
            return httpx.Response(
                self.post_error_after_commit, text="<h1>Internal Server Error</h1>"
            )
        return httpx.Response(201, json=self._change_record(typ, obj, "add"))

    def _create_media(self, request: httpx.Request) -> httpx.Response:
        """POST /api/media/, as 3.21.1 to 3.23.1 answer it (``MediaObjectsResource.post``).

        The body is the file, not a Media object: whatever is sent is stored
        as the file under its md5, its Content-Type becomes the mime type, and
        the server makes the handle. A Media object sent as JSON is stored as
        a ``.json`` file (docs/PITFALLS.md section 32).
        """
        if self.write_forbidden:
            return httpx.Response(403, json={"message": "Forbidden: database is read-only"})
        if self.post_error:
            return httpx.Response(self.post_error, text="<h1>Internal Server Error</h1>")
        mime = request.headers.get("content-type")
        if not mime:
            return httpx.Response(406, json={"message": "Media type not recognized"})
        if self.upload_drop == "refused":
            raise httpx.ConnectError("connection refused", request=request)
        self.media_posts.append(mime)
        if self.upload_drop == "before":
            raise httpx.ReadError("connection lost", request=request)
        content = request.content
        checksum = hashlib.md5(content).hexdigest()  # noqa: S324 - the server's own
        stored = self.media_mime_stored or mime
        obj = _complete(
            "Media",
            {
                "_class": "Media",
                "checksum": checksum,
                "path": f"{checksum}{mimetypes.guess_extension(stored) or ''}",
                "mime": stored,
            },
        )
        obj["handle"] = self._new_handle()
        obj["gramps_id"] = self._new_gid("media")
        obj["change"] = int(time.time())
        self.store["media"][obj["handle"]] = obj
        self.files[obj["handle"]] = (content, mime)
        self.requests.append(("POST", "media", {"mime": mime, "bytes": len(content)}))
        if self.upload_drop == "after":
            raise httpx.ReadError("connection lost", request=request)
        if self.post_error_after_commit:
            return httpx.Response(
                self.post_error_after_commit, text="<h1>Internal Server Error</h1>"
            )
        return httpx.Response(201, json=self._change_record("media", obj, "add"))

    def _replace_file(self, handle: str, request: httpx.Request) -> httpx.Response:
        """PUT /api/media/{handle}/file: the file replaced, and the object's
        checksum, path and mime type with it (``MediaFileResource.put``)."""
        media = self.store["media"].get(handle)
        if media is None:
            return httpx.Response(404, json={"message": "not found"})
        mime = request.headers.get("content-type")
        if not mime:
            return httpx.Response(406, json={"message": "Media type not recognized"})
        if self.upload_drop == "before":
            raise httpx.ReadError("connection lost", request=request)
        checksum = hashlib.md5(request.content).hexdigest()  # noqa: S324
        if checksum == media.get("checksum"):
            return httpx.Response(
                409,
                json={
                    "message": "Uploaded file has the same checksum as the existing media object"
                },
            )
        self.files[handle] = (request.content, mime)
        media.update(
            checksum=checksum,
            path=f"{checksum}{mimetypes.guess_extension(mime) or ''}",
            mime=mime,
            change=int(time.time()),
        )
        if self.upload_drop == "after":
            raise httpx.ReadError("connection lost", request=request)
        return httpx.Response(200, json=self._change_record("media", media, "update"))

    def _update(self, typ: str, handle: str, request: httpx.Request) -> httpx.Response:
        if self.write_forbidden:
            return httpx.Response(403, json={"message": "Forbidden: database is read-only"})
        if self.put_error:
            return httpx.Response(self.put_error, json={"message": "write failed"})
        payload = json.loads(request.content or b"{}")
        sent = copy.deepcopy({k: v for k, v in payload.items() if k not in _COMPUTED_KEYS})
        refused = self._refusal(_TYPE_TO_CLASS[typ], sent)
        if refused is None:
            refused = _admit(_TYPE_TO_CLASS[typ], sent)
        if refused is not None:
            return refused
        previous = self.store[typ].get(handle) or {}
        dropped = sorted(set(previous) - set(sent) - _COMPUTED_KEYS - {"handle", "change"})
        if dropped:
            self.partial_writes.append((typ, handle, dropped))
        obj = _complete(_TYPE_TO_CLASS[typ], sent)
        obj["handle"] = handle
        obj["change"] = int(time.time())
        if typ == "family" and (refused := self._parent_change_refusal(previous, obj)):
            return refused
        self.store[typ][handle] = obj
        self.requests.append(("PUT", typ, payload))
        if typ == "family":
            self._family_cascade(previous, obj)
        if self.put_error_after_commit:
            return httpx.Response(
                self.put_error_after_commit, text="<h1>Internal Server Error</h1>"
            )
        return httpx.Response(200, json=self._change_record(typ, obj, "update"))

    def _parent_change_refusal(self, old: dict, new: dict) -> httpx.Response | None:
        """Refuse a family write whose father or mother change the server cannot make.

        ``_fix_parent_handles`` (``api/resources/util.py``, 3.21.1 to 3.23.1)
        takes the family off its old parent with ``family_list.remove``: an old
        parent who does not list the family raises ValueError, answered 400,
        and nothing is written. An old parent who does not exist is a
        HandleError on 3.21, answered 500; 3.22 skips it. PITFALLS 15.
        """
        people = self.store["person"]
        for role in ("father_handle", "mother_handle"):
            before = old.get(role)
            if not before or before == new.get(role):
                continue
            if before not in people:
                if self._server_at_least(3, 22):
                    continue
                return httpx.Response(500, text="<h1>Internal Server Error</h1>")
            if new["handle"] not in (people[before].get("family_list") or []):
                return httpx.Response(
                    400, json={"error": {"code": 400, "message": "Error while updating object"}}
                )
        return None

    def _family_cascade(self, old: dict | None, new: dict) -> None:
        """Update family members' lists the way gramps-webapi 3.21.1 does.

        ``add_family_update_refs`` and ``update_family_update_refs`` in its
        ``api/resources/util.py``. Faithful in the two places that matter:
        a new father or mother of an existing family gets the family
        appended *without* a duplicate check, and a removed child loses the
        family through ``list.remove``, which takes only the first of two
        entries.
        """
        family = new["handle"]
        people = self.store["person"]
        if old is None:
            for role in ("father_handle", "mother_handle"):
                person = people.get(new.get(role) or "")
                if person is not None and family not in person.setdefault("family_list", []):
                    person["family_list"].append(family)
            for ref in new.get("child_ref_list") or []:
                person = people.get(ref.get("ref") or "")
                if person is not None:
                    links = person.setdefault("parent_family_list", [])
                    if family not in links:
                        links.append(family)
            return
        for role in ("father_handle", "mother_handle"):
            before, after = old.get(role), new.get(role)
            if before == after:
                continue
            if before in people and family in people[before].get("family_list", []):
                people[before]["family_list"].remove(family)
            if after in people:
                people[after].setdefault("family_list", []).append(family)
        was = {r.get("ref") for r in old.get("child_ref_list") or []}
        now = {r.get("ref") for r in new.get("child_ref_list") or []}
        for gone in was - now:
            links = (people.get(gone) or {}).get("parent_family_list") or []
            if family in links:
                links.remove(family)
        for added in now - was:
            person = people.get(added)
            if person is not None:
                links = person.setdefault("parent_family_list", [])
                if family not in links:
                    links.append(family)

    def _merge(self, typ: str, keep: str, drop: str) -> httpx.Response:
        """Merge as Gramps' MergeXxxQuery does (gramps/gen/merge, Gramps 6.0).

        The survivor takes in what its class's ``merge`` combines, list by
        list, with equivalent entries combined rather than repeated; every
        reference to the loser is repointed, collapsing references that become
        duplicates; then the loser is deleted. A person merge also merges two
        of the survivor's families that now have the same parents, and a
        family merge merges two different fathers or mothers -- both as Gramps
        does. ``test_contract_live.py`` holds this to a real server.
        """
        if keep not in self.store[typ] or drop not in self.store[typ]:
            return httpx.Response(404, json={"message": "not found"})
        pairs = [(keep, drop)] if typ == "person" else []
        if typ == "family":
            for key in ("father_handle", "mother_handle"):
                mine = self.store["family"][keep].get(key)
                theirs = self.store["family"][drop].get(key)
                if mine and theirs and mine != theirs:
                    pairs.append((mine, theirs))
        for a, b in pairs:
            refusal = _person_merge_refusal(self.store["person"][a], self.store["person"][b])
            if refusal:
                return httpx.Response(409, json={"message": refusal})
        self.merges.append((typ, keep, drop))
        if typ == "person":
            self._merge_person(keep, drop, family_merger=True)
        elif typ == "family":
            self._merge_family(keep, drop)
        else:
            winner, loser = self.store[typ][keep], self.store[typ][drop]
            _merge_privacy(winner, loser)
            if typ == "citation":
                order = [0, 4, 1, 3, 2]  # Citation.merge keeps the earlier of these
                pair = (winner.get("confidence", 2), loser.get("confidence", 2))
                winner["confidence"] = order[min(order.index(c) for c in pair)]
            for key in _MERGED_LISTS.get(typ, ()):
                _merge_entries(winner, loser, key)
            self._replace_everywhere(drop, keep)
            self.store[typ].pop(drop)
            if typ == "event":
                for person in self.store["person"].values():
                    if any(r.get("ref") == keep for r in person.get("event_ref_list") or []):
                        self._set_birth_death(person)
        return httpx.Response(200, json=[])

    def _replace_everywhere(self, old: str, new: str) -> None:
        for objects in self.store.values():
            for handle, obj in objects.items():
                if handle != old:
                    _replace_reference(obj, old, new)

    def _set_birth_death(self, person: dict) -> None:
        """The first Birth and Death in the Primary role, as the server computes them."""
        for key, wanted in (("birth_ref_index", "Birth"), ("death_ref_index", "Death")):
            person[key] = next(
                (
                    i
                    for i, ref in enumerate(person.get("event_ref_list") or [])
                    if ref.get("role", "Primary") == "Primary"
                    and (self.store["event"].get(ref.get("ref")) or {}).get("type") == wanted
                ),
                -1,
            )

    def _merge_person(self, keep: str, drop: str, *, family_merger: bool) -> None:
        winner, loser = self.store["person"][keep], self.store["person"][drop]
        # Person.merge
        if loser.get("gramps_id"):
            winner.setdefault("attribute_list", []).append(
                _complete("Attribute", {"type": "Merged Gramps ID", "value": loser["gramps_id"]})
            )
        _merge_privacy(winner, loser)
        names = [winner.get("primary_name") or {}, *(winner.get("alternate_names") or [])]
        for name in [loser.get("primary_name") or {}, *(loser.get("alternate_names") or [])]:
            match = next((n for n in names if _same(n, name, _SAME_ENTRY["alternate_names"])), None)
            if match is None:
                winner.setdefault("alternate_names", []).append(copy.deepcopy(name))
                names.append(winner["alternate_names"][-1])
            elif match != name:
                _absorb(match, name)
        for key in _MERGED_LISTS["person"]:
            _merge_entries(winner, loser, key)
        for key in ("parent_family_list", "family_list"):
            for handle in loser.get(key) or []:
                if handle not in winner.setdefault(key, []):
                    winner[key].append(handle)
        # MergePersonQuery: repoint, then merge families left with the same parents
        self._replace_everywhere(drop, keep)
        self.store["person"].pop(drop)
        if family_merger:
            seen: dict[tuple, str] = {}
            for handle in list(winner.get("family_list") or []):
                family = self.store["family"].get(handle) or {}
                parents = (family.get("father_handle"), family.get("mother_handle"))
                if parents in seen:
                    self._merge_family_into(seen[parents], handle, survivor=keep)
                    break  # Gramps merges one pair and skips anything more complex
                seen[parents] = handle
        self._set_birth_death(winner)

    def _merge_family(self, keep: str, drop: str) -> None:
        winner, loser = self.store["family"][keep], self.store["family"][drop]
        for key in ("father_handle", "mother_handle"):
            mine, theirs = winner.get(key), loser.get(key)
            if mine and theirs and mine != theirs:
                self._merge_person(mine, theirs, family_merger=False)
            elif theirs and not mine:
                winner[key] = theirs
        self._merge_family_into(keep, drop, survivor=None)

    def _merge_family_into(self, keep: str, drop: str, *, survivor: str | None) -> None:
        """MergeFamilyQuery / MergePersonQuery.merge_families, once the parents agree."""
        winner, loser = self.store["family"][keep], self.store["family"][drop]
        if _type_name(winner.get("type")) == "Unknown":
            winner["type"] = loser.get("type")
        _merge_privacy(winner, loser)
        for key in _MERGED_LISTS["family"]:
            _merge_entries(winner, loser, key)
        for ref in loser.get("child_ref_list") or []:
            child = self.store["person"].get(ref.get("ref")) or {}
            links = child.get("parent_family_list") or []
            if keep in links:
                child["parent_family_list"] = [h for h in links if h != drop]
            else:
                child["parent_family_list"] = [keep if h == drop else h for h in links]
        for key in ("father_handle", "mother_handle"):
            parent = self.store["person"].get(winner.get(key) or "")
            if parent is not None:
                parent["family_list"] = [h for h in parent.get("family_list") or [] if h != drop]
        self._replace_everywhere(drop, keep)
        self.store["family"].pop(drop)

    def _transactions(self, path: str, method: str, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if path.rstrip("/") == "/api/transactions/history":
            # 3.21's paging: sort, before_id/after_id, then page and pagesize
            # (no page: all of them), each change with its states if asked.
            found = sorted(
                self.transactions,
                key=lambda t: t["id"],
                reverse=params.get("sort") == "-id",
            )
            if params.get("before_id"):
                found = [t for t in found if t["id"] < int(params["before_id"])]
            if params.get("after_id"):
                found = [t for t in found if t["id"] > int(params["after_id"])]
            total = len(found)
            if params.get("page"):
                size = int(params.get("pagesize") or 20)
                start = (int(params["page"]) - 1) * size
                found = found[start : start + size]
            return httpx.Response(
                200,
                json=[_logged_transaction(t, params) for t in found],
                headers={"X-Total-Count": str(total)},
            )
        m_one = re.match(r"^/api/transactions/history/(\d+)$", path)
        if m_one and method == "GET":
            found = next((t for t in self.transactions if t["id"] == int(m_one.group(1))), None)
            if found is None:
                return httpx.Response(404, json={"message": "no such transaction"})
            return httpx.Response(200, json=_logged_transaction(found, params))
        m = re.match(r"^/api/transactions/history/(\d+)/undo$", path)
        if not m:
            return httpx.Response(404, json={"message": "not found"})
        txn_id = int(m.group(1))
        record = next((t for t in self.transactions if t["id"] == txn_id), None)
        conflicts = (record or {}).get("_conflicts", [])
        if method == "GET":
            return httpx.Response(
                200,
                json={
                    "transaction_id": txn_id,
                    "can_undo_without_force": not conflicts,
                    "conflicts": conflicts,
                    "conflicts_count": len(conflicts),
                    "total_changes": len((record or {}).get("changes", [])),
                },
            )
        self.undos.append(txn_id)
        return httpx.Response(200, json={"task": {"id": "t1"}})

    def _backlinks_for(self, handle: str) -> dict[str, list[str]]:
        """Which stored objects reference this handle.

        Grouped by SINGULAR object type ("citation"), which is what the real
        server returns -- not the plural REST namespace. Getting this wrong in
        the fake hid a live bug where every object reported zero references.
        """
        found: dict[str, list[str]] = {}
        for typ, objects in self.store.items():
            for other_handle, obj in objects.items():
                if other_handle == handle:
                    continue
                if _references(obj, handle):
                    found.setdefault(typ, []).append(other_handle)
        return found

    def _get(self, typ: str, handle: str, params: Any = None) -> httpx.Response:
        obj = self.store[typ].get(handle)
        if obj is None:
            return httpx.Response(404, json={"message": "not found"})
        out = _served(self._with_profile(typ, obj, params))
        if params is not None and params.get("backlinks"):
            out = dict(out)
            out["backlinks"] = self._backlinks_for(handle)
        if params is not None and params.get("keys"):
            # The real server applies keys to the WHOLE response, so backlinks
            # vanishes unless it is named. Reproducing that here is what makes
            # the get_backlinks test meaningful.
            wanted = set(params["keys"].split(","))
            out = {k: v for k, v in out.items() if k in wanted}
        return httpx.Response(200, json=out)

    def _list_or_count(self, typ: str, request: httpx.Request) -> httpx.Response:
        params = request.url.params
        if "gramps_id" in params:
            gid = params["gramps_id"]
            for obj in self.store[typ].values():
                if obj.get("gramps_id") == gid:
                    out = _served(self._with_profile(typ, obj, params))
                    if params.get("backlinks"):
                        out = dict(out, backlinks=self._backlinks_for(obj["handle"]))
                    if params.get("keys"):
                        wanted = set(params["keys"].split(","))
                        out = {k: v for k, v in out.items() if k in wanted}
                    return httpx.Response(200, json=[out])
            return httpx.Response(404, json={"message": "not found"})
        objs = [_served(self._with_profile(typ, o, params)) for o in self.store[typ].values()]
        if "handles" in params:
            wanted = set(params["handles"].split(","))
            objs = [o for o in objs if o["handle"] in wanted]
        if params.get("rules"):
            rules = json.loads(params["rules"])
            unknown = sorted({r.get("name") for r in rules.get("rules", [])} - {"HasTag"})
            if unknown:
                return httpx.Response(400, json={"message": f"the fake has no rule {unknown}"})
            objs = [o for o in objs if self._rules_hold(o, rules)]
        if params.get("gql"):
            objs = [o for o in objs if _gql_match(o, params["gql"], self._served_by_handle)]
        if params.get("backlinks"):
            objs = [dict(o, backlinks=self._backlinks_for(o["handle"])) for o in objs]
        if params.get("keys"):
            # As the real API does: only the fields asked for come back.
            wanted = set(params["keys"].split(","))
            objs = [{k: v for k, v in o.items() if k in wanted} for o in objs]
        headers = {"X-Total-Count": str(len(objs))}
        # pagesize=1 count probe
        if params.get("pagesize") == "1":
            objs = objs[:1]
        return httpx.Response(200, json=objs, headers=headers)

    def _served_by_handle(self, typ: str, handle: str) -> dict | None:
        """An object as GrampsQL's ``get_<type>`` reaches it, or None."""
        stored = self.store.get(typ, {}).get(handle)
        return _served(stored) if stored else None

    def _rules_hold(self, obj: dict, rules: dict) -> bool:
        """Gramps filter rules, as far as the tools send them: ``HasTag``.

        HasTag matches a tag by exact name (``get_tag_from_name`` in Gramps'
        rule); several rules combine by ``function``, "and" by default.
        """
        held = []
        for rule in rules.get("rules", []):
            tagged = {h for h, t in self.store["tag"].items() if t.get("name") == rule["values"][0]}
            held.append(bool(tagged & set(obj.get("tag_list") or [])))
        if rules.get("invert"):
            held = [not h for h in held]
        return any(held) if rules.get("function") == "or" else all(held)

    def _stored_dna_matches(self, handle: str) -> list[dict]:
        """Matches as gramps-webapi 3.21.1 reads them from the tree.

        An association with relationship "DNA" on the tested person; its
        segments are parsed from notes on the association and on any citation
        attached to it (gramps_webapi/api/resources/dna.py).
        """
        person = self.store["person"].get(handle) or {}
        matches = []
        for index, ref in enumerate(person.get("person_ref_list") or []):
            if ref.get("rel") != "DNA":
                continue
            notes = list(ref.get("note_list") or [])
            for citation in ref.get("citation_list") or []:
                notes += (self.store["citation"].get(citation) or {}).get("note_list") or []
            segments = []
            for note in notes:
                text = ((self.store["note"].get(note) or {}).get("text") or {}).get("string", "")
                segments += _parse_segments(text)
            matches.append(
                {
                    "handle": ref.get("ref"),
                    "segments": segments,
                    "relation": "",
                    "ancestor_handles": [],
                    "ancestor_profiles": [],
                    "person_ref_idx": index,
                    "note_handles": notes,
                }
            )
        return matches

    def _query(self, typ: str, body: dict) -> list[dict]:
        """Answer a structured query from the store: where, order_by, select.

        The engine's operators over named columns and json_paths, with SQL's
        answer for a missing value (it matches no comparison). Named columns
        are the object's own top-level fields plus a person's ``surname`` and
        ``given_name``; the server has more, which no test uses.
        """
        found = list(self.store[typ].values())
        for cond in body.get("where") or []:
            found = [o for o in found if self._holds(typ, o, cond)]
        for key in reversed(body.get("order_by") or []):
            found.sort(
                key=lambda o, k=key: _sort_key(self._column(typ, o, k["column"])),
                reverse=key.get("direction") == "desc",
            )
        select = body.get("select") or ["handle", "gramps_id"]
        return [self._project(typ, o, select) for o in found]

    def _column(self, typ: str, obj: dict, column: Any) -> Any:
        """A named column or a json_path, as the engine reads it from storage."""
        if isinstance(column, dict):
            return self._walk(typ, obj, list(column["json_path"]))
        if typ == "person" and column in ("surname", "given_name"):
            name = obj.get("primary_name") or {}
            if column == "given_name":
                return name.get("first_name")
            surnames = name.get("surname_list") or []
            primary = next(
                (s for s in surnames if s.get("primary")), surnames[0] if surnames else {}
            )
            return primary.get("surname")
        return obj.get(column)

    def _holds(self, typ: str, obj: dict, cond: dict) -> bool:
        value = self._column(typ, obj, cond["column"])
        if "value_column" in cond:
            target = self._column(typ, obj, cond["value_column"])
        else:
            target = cond.get("value")
        op = cond.get("op", "eq")
        if value is None or (target is None and op not in ("eq", "ne")):
            return False
        if op == "eq":
            return value == target
        if op == "ne":
            return value != target
        if op in ("lt", "lte", "gt", "gte"):
            try:
                return {
                    "lt": value < target,
                    "lte": value <= target,
                    "gt": value > target,
                    "gte": value >= target,
                }[op]
            except TypeError:
                return False
        if op == "in":
            return value in (target or [])
        if op == "contains":
            return isinstance(value, (str, list)) and target in value
        if op == "like":
            pattern = re.escape(str(target)).replace("%", ".*").replace("_", ".")
            return re.fullmatch(pattern, str(value), re.IGNORECASE | re.DOTALL) is not None
        if op == "regex":
            return re.search(str(target), str(value)) is not None
        raise AssertionError(f"the fake's query engine has no operator {op!r}")

    def _project(self, typ: str, obj: dict, select: list) -> dict:
        """Answer a structured-query ``select`` for one stored object."""
        row: dict = {}
        for entry in select:
            if isinstance(entry, str):
                row[entry] = self._column(typ, obj, entry)
                continue
            path = list(entry["json_path"])
            row[entry.get("as") or ".".join(map(str, path))] = self._walk(typ, obj, path)
        return row

    def _stored_event_type(self, label: str) -> dict:
        """An event type as stored: a standard one by number, a custom one by name.

        The API serves ``"Birth"``; the engine reads ``{"string": "", "value": 12}``
        (docs/PITFALLS.md section 12).
        """
        for value, name in self.event_type_map.items():
            if name == label and value != "0":
                return {"string": "", "value": int(value)}
        return {"string": label, "value": 0}

    def _walk(self, typ: str, obj: dict, path: list) -> Any:
        """Follow a json_path, crossing the relationships the engine crosses.

        Person -> Event through ``birth``/``death`` (the ref-index events), and
        Family -> Person through ``father``/``mother``.
        """
        node: Any = obj
        kind = typ
        for step in path:
            if not isinstance(node, (dict, list)) or (
                isinstance(node, list) != isinstance(step, int)
            ):
                return None
            if kind == "person" and step in ("birth", "death"):
                index = node.get(f"{step}_ref_index", -1)
                refs = node.get("event_ref_list") or []
                node = (
                    self.store["event"].get(refs[index]["ref"]) if 0 <= index < len(refs) else None
                )
                kind = "event"
            elif kind == "family" and step in ("father", "mother"):
                node = self.store["person"].get(node.get(f"{step}_handle") or "")
                kind = "person"
            elif isinstance(step, int):
                node = (
                    node[step]
                    if isinstance(node, list) and -len(node) <= step < len(node)
                    else None
                )
            else:
                node = node.get(step)
                if kind == "event" and step == "type" and isinstance(node, str):
                    node = self._stored_event_type(node)
        return node

    def _with_profile(self, typ: str, obj: dict, params: Any = None) -> dict:
        """Attach the computed profile/extended blocks, when asked for as the API requires.

        The server adds ``profile`` only for a ``profile=`` request and
        ``extended`` only for an ``extend=`` one; a tool reading either
        without asking gets nothing from a real server, so it gets nothing
        here either.
        """
        out = dict(obj)
        params = params or {}
        if typ == "person" and (params.get("profile") or params.get("extend")):
            profile = {"name": _display_name(obj)}
            events = []
            for ev_ref in obj.get("event_ref_list", []):
                ev = self.store["event"].get(ev_ref.get("ref"), {})
                events.append(ev)
                etype = ev.get("type")
                etype = etype if isinstance(etype, str) else ""
                date = ev.get("date", {})
                year = date.get("dateval", [0, 0, 0])[2] if isinstance(date, dict) else 0
                if etype == "Birth":
                    profile["birth"] = {"date": str(year) if year else ""}
                if etype == "Death":
                    profile["death"] = {"date": str(year) if year else ""}
            if params.get("profile"):
                out["profile"] = profile
            if params.get("extend"):
                out["extended"] = {"events": events}
        return out


# Blocks the API computes on read and never stores. The real server ignores them
# on write; the fake must too, or a read-modify-write silently persists them.
_COMPUTED_KEYS = {"profile", "extended", "backlinks"}

_CLASS_TO_TYPE = {
    "Person": "person",
    "Family": "family",
    "Event": "event",
    "Place": "place",
    "Source": "source",
    "Citation": "citation",
    "Repository": "repository",
    "Media": "media",
    "Note": "note",
    "Tag": "tag",
}
_TYPE_TO_CLASS = {typ: cls for cls, typ in _CLASS_TO_TYPE.items()}

#: GET /api/types/' custom lists, each with the standard list it extends.
#: Gramps keeps attribute names apart by the kind of object that carries them
#: but has one standard list for them all; a citation's go with a source's.
_CUSTOM_TYPE_STANDARD = {
    "child_reference_types": "child_reference_types",
    "event_attribute_types": "attribute_types",
    "event_role_types": "event_role_types",
    "event_types": "event_types",
    "family_attribute_types": "attribute_types",
    "family_relation_types": "family_relation_types",
    "media_attribute_types": "attribute_types",
    "name_origin_types": "name_origin_types",
    "name_types": "name_types",
    "note_types": "note_types",
    "person_attribute_types": "attribute_types",
    "place_types": "place_types",
    "repository_types": "repository_types",
    "source_attribute_types": "source_attribute_types",
    "source_media_types": "source_media_types",
    "url_types": "url_types",
}

_ATTRIBUTE_KIND = {
    "person": "person",
    "family": "family",
    "event": "event",
    "media": "media",
    "source": "source",
    "citation": "source",
}


_TXN_VERBS = {"POST": "New", "PUT": "Edit", "DELETE": "Delete"}


def _txn_description(request: httpx.Request) -> str:
    """What the server calls a write's transaction: "New Person", "Edit Family"."""
    segment = request.url.path.strip("/").split("/")
    typ = _SEG_TO_TYPE.get(segment[1] if len(segment) > 1 else "")
    verb = _TXN_VERBS.get(request.method, "Edit")
    return f"{verb} {_TYPE_TO_CLASS[typ]}" if typ else verb


def _logged_change(change: dict, params: Any) -> dict:
    """A logged change as served: its states only when ``old``/``new`` ask."""
    out = {k: v for k, v in change.items() if k not in ("_old", "_new")}
    for flag, key, state in (("old", "old_data", "_old"), ("new", "new_data", "_new")):
        if params.get(flag) in ("1", "true", "True"):
            out[key] = copy.deepcopy(change.get(state)) or {}
    return out


def _logged_transaction(txn: dict, params: Any) -> dict:
    """A logged transaction as served, its changes by :func:`_logged_change`."""
    return {**txn, "changes": [_logged_change(c, params) for c in txn.get("changes") or []]}


def _type_fields(typ: str, obj: dict) -> Iterator[tuple[str, Any]]:
    """(custom list, value) for each type-valued field of a stored object."""
    if typ in _ATTRIBUTE_KIND:
        for attribute in obj.get("attribute_list") or []:
            yield f"{_ATTRIBUTE_KIND[typ]}_attribute_types", attribute.get("type")
    if typ in ("person", "family"):
        for ref in obj.get("event_ref_list") or []:
            yield "event_role_types", ref.get("role")
    for ref in obj.get("media_list") or []:
        for attribute in ref.get("attribute_list") or []:
            yield "media_attribute_types", attribute.get("type")
    if typ in ("person", "place", "repository"):
        for url in obj.get("urls") or []:
            yield "url_types", url.get("type")
    if typ == "event":
        yield "event_types", obj.get("type")
    elif typ == "person":
        for name in [obj.get("primary_name") or {}, *(obj.get("alternate_names") or [])]:
            yield "name_types", name.get("type")
            for surname in name.get("surname_list") or []:
                yield "name_origin_types", surname.get("origintype")
    elif typ == "family":
        yield "family_relation_types", obj.get("type")
        for child in obj.get("child_ref_list") or []:
            yield "child_reference_types", child.get("frel")
            yield "child_reference_types", child.get("mrel")
    elif typ == "place":
        yield "place_types", obj.get("place_type")
    elif typ == "repository":
        yield "repository_types", obj.get("type")
    elif typ == "source":
        for ref in obj.get("reporef_list") or []:
            yield "source_media_types", ref.get("media_type")
    elif typ == "note":
        yield "note_types", obj.get("type")


def _references(node: Any, handle: str) -> bool:
    """Does this object mention that handle anywhere, at any nesting depth?

    A shallow check over the *_list fields would miss references nested inside
    ChildRefs and MediaRefs -- which is exactly where the real orphan-citation
    hunt found them.
    """
    if isinstance(node, str):
        return node == handle
    if isinstance(node, dict):
        return any(_references(v, handle) for k, v in node.items() if k != "handle")
    if isinstance(node, list):
        return any(_references(v, handle) for v in node)
    return False


def _unreference(node: Any, handle: str) -> None:
    """Drop references to a deleted object, as the server's DELETE does.

    gramps-webapi 3.21.1 deletes "the object and its references"
    (``api/resources/delete.py``): handle lists lose the handle, and ref
    lists lose the entries pointing at it.
    """
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if isinstance(value, list):
                node[key] = [
                    v
                    for v in value
                    if v != handle and not (isinstance(v, dict) and v.get("ref") == handle)
                ]
                for v in node[key]:
                    _unreference(v, handle)
            elif isinstance(value, dict):
                _unreference(value, handle)


#: How Gramps tells two entries of a list apart when it merges -- each class's
#: ``is_equivalent`` (gramps/gen/lib, Gramps 6.0). Entries agreeing on these
#: keys are one entry, whose privacy, citations, notes and attributes are
#: combined; any other entry is appended.
_SAME_ENTRY = {
    "event_ref_list": ("ref", "role"),
    "child_ref_list": ("ref",),
    "person_ref_list": ("ref", "rel"),
    "reporef_list": ("ref", "call_number", "media_type"),
    "placeref_list": ("ref", "date"),
    "attribute_list": ("type", "value"),
    "urls": ("type", "path", "desc"),
    "address_list": (
        "street",
        "locality",
        "city",
        "county",
        "state",
        "country",
        "postal",
        "phone",
        "date",
    ),
    "alternate_names": (
        "first_name",
        "call",
        "suffix",
        "title",
        "nick",
        "famnick",
        "type",
        "date",
        "surname_list",
    ),
    "alt_names": ("value", "lang", "date"),
    "media_list": ("ref", "rect"),
    "lds_ord_list": ("type", "place", "famc", "temple", "status", "date"),
}

#: The lists each class's ``merge`` combines (gramps/gen/lib, Gramps 6.0).
_MERGED_LISTS = {
    "person": (
        "event_ref_list",
        "lds_ord_list",
        "media_list",
        "address_list",
        "attribute_list",
        "urls",
        "person_ref_list",
        "note_list",
        "citation_list",
        "tag_list",
    ),
    "family": (
        "event_ref_list",
        "lds_ord_list",
        "media_list",
        "child_ref_list",
        "attribute_list",
        "note_list",
        "citation_list",
        "tag_list",
    ),
    "event": ("attribute_list", "note_list", "citation_list", "media_list", "tag_list"),
    "source": ("note_list", "media_list", "tag_list", "attribute_list", "reporef_list"),
    "citation": ("note_list", "media_list", "tag_list", "attribute_list"),
    "repository": ("address_list", "urls", "note_list", "tag_list"),
    "note": ("tag_list",),
    "media": ("attribute_list", "note_list", "citation_list", "tag_list"),
    "place": (
        "alt_names",
        "media_list",
        "urls",
        "note_list",
        "citation_list",
        "tag_list",
        "placeref_list",
    ),
}


def _person_merge_refusal(keep: dict, drop: dict) -> str | None:
    """MergePersonQuery's MergeError, which the server answers with 409."""
    keep_fams, drop_fams = set(keep.get("family_list") or []), set(drop.get("family_list") or [])
    if keep_fams & drop_fams:
        return "Spouses cannot be merged."
    if keep_fams & set(drop.get("parent_family_list") or []) or drop_fams & set(
        keep.get("parent_family_list") or []
    ):
        return "A parent and child cannot be merged."
    return None


def _comparable(value: Any) -> Any:
    """A value as an equivalence test reads it: a date by what it says, not its sort value."""
    if isinstance(value, dict):
        if "dateval" in value:
            keys = ("calendar", "modifier", "quality", "dateval", "text", "newyear")
            return {k: _comparable(value.get(k)) for k in keys}
        return {k: _comparable(v) for k, v in value.items() if k not in ("_class", "year")}
    if isinstance(value, list):
        return [_comparable(v) for v in value]
    return value


def _same(a: dict, b: dict, keys: tuple) -> bool:
    return all(_comparable(a.get(k)) == _comparable(b.get(k)) for k in keys)


def _merge_privacy(winner: dict, loser: dict) -> None:
    if "private" in winner or "private" in loser:
        winner["private"] = bool(winner.get("private")) or bool(loser.get("private"))


def _absorb(into: dict, entry: dict) -> None:
    """One entry merged into its equivalent: privacy, citations, notes, attributes."""
    _merge_privacy(into, entry)
    for key in ("citation_list", "note_list", "attribute_list"):
        if entry.get(key):
            _merge_entries(into, entry, key)


def _merge_entries(winner: dict, loser: dict, key: str) -> None:
    current = winner.setdefault(key, [])
    for entry in loser.get(key) or []:
        if not isinstance(entry, dict):
            if entry not in current:
                current.append(entry)
            continue
        match = next((e for e in current if _same(e, entry, _SAME_ENTRY.get(key, ()))), None)
        if match is None:
            current.append(copy.deepcopy(entry))
        elif match != entry:
            _absorb(match, entry)


def _collapse(key: str, entries: list) -> list:
    """A list whose references were repointed, with the duplicates that made merged."""
    out: list = []
    for entry in entries:
        if not isinstance(entry, dict):
            if entry not in out:
                out.append(entry)
            continue
        match = next((e for e in out if _same(e, entry, _SAME_ENTRY.get(key, ("ref",)))), None)
        if match is None:
            out.append(entry)
        else:
            _absorb(match, entry)
    return out


def _replace_reference(node: Any, old: str, new: str) -> None:
    """Gramps' replace_handle_reference: repoint, and merge what became duplicates."""
    if isinstance(node, dict):
        for key, value in list(node.items()):
            if key == "handle":
                continue
            if value == old:
                node[key] = new
            elif isinstance(value, list):
                touched = old in value or any(
                    isinstance(e, dict) and e.get("ref") == old for e in value
                )
                _replace_reference(value, old, new)
                if touched:
                    node[key] = _collapse(key, value)
            else:
                _replace_reference(value, old, new)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if value == old:
                node[i] = new
            else:
                _replace_reference(value, old, new)


def _type_name(value: Any) -> str:
    if isinstance(value, dict):
        return value.get("string") or str(value.get("value", ""))
    return "" if value is None else str(value)


#: One GrampsQL condition, or a quoted string to step over: AND and OR are
#: found between conditions, never inside a value.
_GQL_TOKEN = re.compile(
    r'"[^"]*"|\'[^\']*\'|(?P<word>\b(?:and|or)\b)',
    re.IGNORECASE,
)
_GQL_SINGLE = re.compile(
    r"^\s*(?P<lhs>[A-Za-z_]\w*(?:\.[A-Za-z_]\w*|\[\d+\])*)\s*"
    r"(?:(?P<op>!=|<=|>=|!~|=|<|>|~)\s*(?P<rhs>\"[^\"]*\"|'[^']*'|\S+))?\s*$"
)


def _gql_split(query: str, keyword: str) -> list[str]:
    """Split a query on AND or OR outside quoted values."""
    parts, last = [], 0
    for token in _GQL_TOKEN.finditer(query):
        if (token.group("word") or "").lower() == keyword:
            parts.append(query[last : token.start()])
            last = token.end()
    return [*parts, query[last:]]


def _gql_match(obj: dict, query: str, resolve=None) -> bool:
    """GrampsQL as gramps-ql 0.5.0 evaluates it, less parentheses.

    What gramps-webapi 3.21.1 to 3.23.1 install (``gramps_ql/gql.py``): OR of
    ANDs; a path through fields, ``[n]`` indexes, ``.length``, ``.any`` and
    ``.all`` over a list's items, and ``get_<type>`` following a handle; a
    missing field matches nothing; strings compare ignoring case, and ``~``
    on a list asks whether the value is one of its items.
    """
    return any(
        all(_gql_single(obj, clause, resolve) for clause in _gql_split(branch, "and"))
        for branch in _gql_split(query, "or")
    )


def _gql_single(obj: Any, clause: str, resolve) -> bool:
    found = _GQL_SINGLE.match(clause)
    if not found:
        raise ValueError(f"the fake cannot parse {clause!r}")
    path = re.findall(r"[A-Za-z_]\w*|\d+", found.group("lhs"))
    return _gql_path(obj, path, found.group("op") or "", found.group("rhs") or "", resolve)


def _gql_path(obj: Any, path: list[str], op: str, rhs: str, resolve) -> bool:
    result: Any = obj
    for i, part in enumerate(path):
        if part == "length":
            result = len(result)
        elif part in ("any", "all"):
            rest = path[i + 1 :]
            results = [
                _gql_path(item, rest, op, rhs, resolve) if rest else _gql_values(item, op, rhs)
                for item in result or []
            ]
            return any(results) if part == "any" else bool(results) and all(results)
        elif part.startswith("get_"):
            result = resolve(part[4:], result) if resolve and isinstance(result, str) else None
        elif part.isdigit():
            try:
                result = result[int(part)]
            except (IndexError, KeyError, TypeError):
                return False
        elif isinstance(result, dict):
            result = result.get(part)
        else:
            return False
        if result is None:
            return False
    return _gql_values(result, op, rhs)


def _gql_values(result: Any, op: str, rhs: str) -> bool:
    if not op:
        return bool(result)
    value: Any = int(rhs) if rhs.isdigit() else rhs.strip("\"'")
    if op in ("=", "!="):
        same = (
            result.casefold() == str(value).casefold()
            if isinstance(result, str)
            else result == value
        )
        return same if op == "=" else not same
    if op in ("~", "!~"):
        try:
            held = (
                str(value).casefold() in result.casefold()
                if isinstance(result, str)
                else value in result
            )
        except TypeError:
            return False
        return held if op == "~" else not held
    compare = {"<": operator.lt, ">": operator.gt, "<=": operator.le, ">=": operator.ge}[op]
    try:
        return bool(compare(result, value))
    except TypeError:
        return False


def _parse_segments(text: str) -> list[dict]:
    """Read shared-segment rows the way the server's parser does, roughly.

    Comma- or tab-separated: chromosome, start, stop, cM, SNPs, and an
    optional side. A row that does not fit -- a header -- is skipped, and
    input with no fitting row parses to nothing, as on the server.
    """
    segments = []
    for line in text.splitlines():
        fields = [f.strip() for f in re.split(r"[,\t]", line)]
        try:
            chromosome, start, stop, cm, snps = fields[:5]
            segment = {
                "chromosome": chromosome,
                "start": int(start),
                "stop": int(stop),
                "cM": float(cm),
                "SNPs": int(snps),
            }
        except ValueError:
            continue
        segment["side"] = fields[5] if len(fields) > 5 and fields[5] in "MPU" else "U"
        segment["comment"] = ""
        segments.append(segment)
    return segments


def _display_name(person: dict) -> str:
    name = person.get("primary_name", {})
    given = name.get("first_name", "")
    sl = name.get("surname_list", [])
    surname = sl[0].get("surname", "") if sl else ""
    return " ".join(p for p in (given, surname) if p)


_TIMELINE_COMMON = {
    "dates",
    "discard_empty",
    "event_classes",
    "events",
    "keys",
    "locale",
    "page",
    "pagesize",
    "ratings",
    "skipkeys",
    "strip",
}
#: The query arguments each timeline endpoint accepts, 3.21.1 to 3.23.1.
_TIMELINE_ARGS = {
    "people/timeline": _TIMELINE_COMMON
    | {
        "ancestors",
        "first",
        "last",
        "name_format",
        "offspring",
        "omit_anchor",
        "precision",
        "relative_event_classes",
        "relative_events",
        "relatives",
    },
    "families/timeline": _TIMELINE_COMMON | {"name_format"},
    "timelines/people": _TIMELINE_COMMON
    | {"anchor", "filter", "first", "handles", "last", "omit_anchor", "precision", "rules"},
    "timelines/families": _TIMELINE_COMMON | {"filter", "handles", "rules"},
}


def _query_bool(value: str | None, *, default: bool) -> bool:
    """A query-string boolean as marshmallow reads one."""
    if value is None:
        return default
    return value.lower() in {"1", "true", "t", "yes", "y", "on"}


def timeline_row(
    event: str,
    event_type: str,
    date: str,
    person: dict | None = None,
    *,
    relationship: str = "self",
    role: str = "Primary",
    place: dict | None = None,
    citations: int = 0,
    confidence: int | None = None,
) -> dict:
    """One timeline event profile, shaped as gramps-webapi's ``Timeline.profile``.

    ``label`` is the event type, with the relationship for a relative's
    event; ``person`` is the person's profile, ``place`` the place's, with
    its alternate names and enclosing places. ``citations`` and
    ``confidence`` are what ``ratings`` adds; the fake drops them without it.
    """
    profile = {}
    if person is not None:
        name = person.get("primary_name") or {}
        surnames = name.get("surname_list") or [{}]
        profile = {
            "handle": person["handle"],
            "gramps_id": person.get("gramps_id"),
            "name_given": name.get("first_name", ""),
            "name_surname": surnames[0].get("surname", ""),
            "name_suffix": "",
            "sex": "U",
            "birth": {},
            "death": {},
            "age": "",
        }
    profile["relationship"] = relationship
    label = event_type if relationship == "self" else f"{event_type} ({relationship.title()})"
    return {
        "date": date,
        "description": "",
        "gramps_id": event,
        "handle": f"h{event}",
        "label": label,
        "media": [],
        "person": profile,
        "place": place or {},
        "age": "",
        "type": event_type,
        "role": role,
        "citations": citations,
        "confidence": confidence,
    }


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail any test that opens a real connection.

    Every test runs against the in-memory fake, and a tool that grew a call
    the fake does not mock would otherwise reach whatever server the
    environment points at -- a live tree, for anyone running the suite with
    their own settings loaded. respx answers before a socket is opened, so
    mocked tests are unaffected.

    Checked at teardown rather than trusted to the raise, because every tool
    catches exceptions into an error envelope: the refusal alone would be
    swallowed and the test would still pass.

    Every name lookup is refused, and so is a connection to any address that
    is not loopback. A loopback connection is allowed because Windows' asyncio
    builds its event loop's wake-up socket pair by connecting to a port on
    127.0.0.1 that it has just opened itself; refusing that stops every async
    test before it starts. A request to a server still has to resolve its
    host name first -- the tests' fake is ``testserver`` -- so it is caught.
    """
    attempts: list = []
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def loopback(address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False  # a name or a path, never allowed

    def refuse(self, address, *args, **kwargs):
        if loopback(address):
            return real_connect(self, address, *args, **kwargs)
        attempts.append(address)
        raise RuntimeError(f"test tried to open a real connection to {address}")

    def refuse_ex(self, address, *args, **kwargs):
        if loopback(address):
            return real_connect_ex(self, address, *args, **kwargs)
        attempts.append(address)
        raise RuntimeError(f"test tried to open a real connection to {address}")

    def refuse_lookup(host, *args, **kwargs):
        attempts.append(host)
        raise RuntimeError(f"test tried to look up {host}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse_ex)
    monkeypatch.setattr(socket, "getaddrinfo", refuse_lookup)
    yield attempts
    assert not attempts, f"test tried to reach the network: {attempts}"


@pytest.fixture(autouse=True)
def _isolate_environment(tmp_path, monkeypatch):
    """Keep the developer's own settings out of every test.

    The server reads ``.env`` from the working directory, and the suite is
    usually run from a checkout where one holds real credentials. The
    ``GRAMPS_MCP_*`` variables of a shell set up to run the server would
    leak in the same way.
    """
    for var in [v for v in os.environ if v.startswith("GRAMPS_MCP_")]:
        monkeypatch.delenv(var)
    monkeypatch.chdir(tmp_path)


@pytest.fixture
def fake() -> FakeGramps:
    return FakeGramps()


@pytest.fixture
async def service(fake: FakeGramps, tmp_path):
    # A test may add a route for another host that it means never to be
    # called -- Transkribus when no credits are spent -- and asserts that itself.
    with respx.mock(base_url="http://testserver", assert_all_called=False) as router:
        router.route().mock(side_effect=fake.handle)
        fake.router = router  # so a test can answer for another host, too
        client = GrampsWebClient("http://testserver", "mcp", "pw")
        await client.login()
        cfg = Config(api_url="http://testserver", username="mcp", password="pw", cache_dir=tmp_path)
        yield GrampsService(client, cfg)
        await client.aclose()


@pytest.fixture
async def tools(fake: FakeGramps, tmp_path):
    """Wire the MCP tool surface to the in-memory fake.

    Yields a callable that invokes a tool the way a client does, via
    ``mcp.call_tool``, so declared ``Field`` defaults are resolved. Calling a
    tool function directly in Python hands it ``FieldInfo`` objects instead.
    """
    import json as _json

    from gramps_evidence_mcp import server

    # A test may add a route for another host that it means never to be
    # called -- Transkribus when no credits are spent -- and asserts that itself.
    with respx.mock(base_url="http://testserver", assert_all_called=False) as router:
        router.route().mock(side_effect=fake.handle)
        fake.router = router  # so a test can answer for another host, too
        client = GrampsWebClient("http://testserver", "mcp", "pw")
        await client.login()
        cfg = Config(
            api_url="http://testserver",
            username="mcp",
            password="pw",
            cache_dir=tmp_path,
        )
        saved = (
            server.state.config,
            server.state.client,
            server.state.service,
            server.state.library,
        )
        server.state.config = cfg
        server.state.client = client
        server.state.service = GrampsService(client, cfg)
        server.state.library = None

        async def call(tool_name: str, /, **arguments):
            """Invoke one tool and decode its JSON result."""
            result = await server.mcp.call_tool(tool_name, arguments)
            return _json.loads(result.content[0].text)

        call.fake = fake  # type: ignore[attr-defined]
        call.service = server.state.service  # type: ignore[attr-defined]
        try:
            yield call
        finally:
            (
                server.state.config,
                server.state.client,
                server.state.service,
                server.state.library,
            ) = saved
            await client.aclose()


# --------------------------------------------------------------------------- #
# Argument building for whole-surface sweeps
# --------------------------------------------------------------------------- #
#: Errors a tool raises from its own input validation, before it does any
#: work. A sweep that receives one of these has not tested what it thinks it
#: has, so :func:`assert_reached_body` treats them as a failure of the sweep
#: rather than of the tool.
LOCAL_VALIDATION_ERRORS = frozenset(
    {
        "date_year",
        "type_column",
        "list_comparison",
        "no_criteria",
        "unsupported_type",
        "unsupported_filter",
        "unsupported_field",
        "unsupported_fields",
        "unknown_event_type",
        "conflicting_arguments",
        "no_media",
        "no_such_directory",
        "invalid_image_url",
        "no_target",
        "unsupported",
        "same_person",
        "unparsed_segments",
        "citation_reuse_refused",
        "reserved_alias",
        "unsupported_format",
        "unsupported_media_type",
        "invalid_identifier",
        "gql_list_field",
        "bad_region",
        "bad_page",
    }
)

#: Nested models carry their own validators, which a generic sample cannot
#: satisfy -- a bare ``{}`` citation fails schema validation in the framework
#: before the tool body runs.
_MODEL_SAMPLES = {
    "citation": {"source_title": "Sample Source", "page": "p. 1"},
    "event": {"type": "Residence", "date": "1900"},
    "marriage": {"type": "Marriage", "date": "1900"},
    "birth": {"type": "Birth", "date": "1900"},
    "death": {"type": "Death", "date": "1900"},
    "name": {"given": "Sample", "surname": "Person"},
}

#: Samples by the model a property references, for inputs whose parameter
#: name says nothing about their shape -- ``items`` is a list of citation
#: edits on one tool and of repository links on another.
_DEF_SAMPLES = {
    "NameMatch": {"surname": "Person"},
    "CitationEdit": {"citation": "C0001", "page": "p. 1"},
    "RepositoryLink": {"source": "S0001", "repository": "R0001"},
}


def _def_name(spec: dict) -> str | None:
    """The model a property (or the items of an array property) references."""
    for node in (spec, spec.get("items") or {}, *(spec.get("anyOf") or [])):
        ref = node.get("$ref") if isinstance(node, dict) else None
        if ref:
            return ref.rsplit("/", 1)[-1]
    return None


#: Escape hatch for a tool whose "pass at least one of these" rule cannot be
#: satisfied from the schema and the parameter names alone.
#:
#: **Currently empty, and that is the point**: :func:`valid_args` reaches the
#: body of all 83 tools unaided. Add an entry only when it stops doing so,
#: and you will not have to notice that yourself --
#: :func:`assert_reached_body` fails the sweep and names the tool.
ARGUMENT_HINTS: dict[str, dict] = {}


def _is_model(spec: dict) -> bool:
    """Does this property reference a nested pydantic model?"""
    blob = json.dumps(spec)
    return "$ref" in blob or spec.get("type") == "object"


def _value_for(name: str, spec: dict):
    """Invent a value a tool will accept for one schema property.

    Driven by the property's schema first and its *name* second. Names carry
    meaning the schema does not: a parameter called ``file_path`` wants a
    path and one called ``object_type`` wants a Gramps type, and a generic
    string satisfies neither.
    """
    if _is_model(spec) and name in _MODEL_SAMPLES:
        return _MODEL_SAMPLES[name]
    model = _def_name(spec)
    if model in _DEF_SAMPLES:
        sample = _DEF_SAMPLES[model]
        return [sample] if spec.get("type") == "array" else sample
    if spec.get("enum"):
        return spec["enum"][0]

    lowered = name.lower()
    if "object_type" in lowered or lowered == "target_type":
        return "person"
    if lowered.endswith(("_path", "path", "destination")):
        return "/nonexistent/sample.jpg"
    if "url" in lowered:
        return "https://example.org/sample"
    if lowered.endswith("year") or lowered.startswith("year"):
        return 1900
    if "date" in lowered:
        return "1900"
    if lowered == "segments":
        return "chromosome,start,stop,cM,SNPs\n1,1000000,5000000,7.5,1200"

    # A declared default is the tool's own idea of a sensible value, so it
    # beats any generic this builder would invent. An empty-string default is
    # the exception: a search term of "" is exactly what makes a tool answer
    # no_criteria, so fall through to the generic instead.
    default = spec.get("default")
    if default not in (None, ""):
        return default

    kind = spec.get("type")
    if kind == "integer":
        return 1
    if kind == "number":
        return 1.0
    if kind == "boolean":
        return False
    if kind == "array":
        return []
    if kind == "object":
        return {}
    return "I0001"


def valid_args(tool, **overrides) -> dict:
    """Build arguments that carry a tool past its own input validation.

    Covers every required property from the schema, then applies any
    :data:`ARGUMENT_HINTS` entry, then the caller's overrides.

    Parameters
    ----------
    tool
        A registered tool, as ``mcp.list_tools`` returns it.
    **overrides
        Values to force, beyond what is derived.

    Returns
    -------
    dict
        Arguments to invoke the tool with.
    """
    schema = tool.input_schema or {}
    props = schema.get("properties") or {}
    args = {name: _value_for(name, props.get(name) or {}) for name in schema.get("required") or []}
    args.update(ARGUMENT_HINTS.get(tool.name, {}))
    args.update(overrides)
    return args


def assert_reached_body(tool_name: str, result) -> None:
    """Fail if a sweep stopped at input validation instead of the tool's body.

    Without this a sweep quietly stops proving anything the moment a tool
    grows a new argument check: the tool returns a tidy error envelope, the
    assertion that "an envelope came back" passes, and the behaviour under
    test is never exercised.
    """
    if isinstance(result, dict) and result.get("error") in LOCAL_VALIDATION_ERRORS:
        raise AssertionError(
            f"{tool_name} rejected the sweep's arguments with "
            f"{result['error']!r} before doing any work, so this sweep did "
            f"not test it. Add an ARGUMENT_HINTS entry for {tool_name} in "
            f"tests/conftest.py. Message was: {result.get('message')!r}"
        )
