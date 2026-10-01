"""Test fixtures: an in-memory fake gramps-webapi served via respx.

Rather than mock each HTTP call, we stand up a tiny stateful fake that mimics
the parts of gramps-webapi the service uses: JWT login, object create (assigns
handle + gramps_id, returns a change-record array), get-by-handle,
get-by-gramps_id, list, and count. This lets tests exercise the *real*
GrampsWebClient + GrampsService orchestration end-to-end.
"""

from __future__ import annotations

import copy
import ipaddress
import itertools
import json
import os
import re
import socket
import time
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
    and a key the class does not have is kept as sent (docs/PITFALLS.md
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
        #: handle -> timeline event profiles.
        self.timelines: dict[str, list[dict]] = {}
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
        }
        #: GET /api/types/: standard English names under "default" (Gramps'
        #: get_standard_xml, every standard name but Custom) and the tree's
        #: own under "custom". The keys are gramps-webapi 3.21.1's.
        self.types: dict = {
            "default": {
                "event_types": [
                    "Unknown",
                    "Marriage",
                    "Birth",
                    "Death",
                    "Baptism",
                    "Burial",
                    "Census",
                    "Elected",
                    "Occupation",
                    "Property",
                    "Residence",
                    "Military Service",
                ],
                "event_role_types": [
                    "Unknown",
                    "Primary",
                    "Clergy",
                    "Celebrant",
                    "Aide",
                    "Bride",
                    "Groom",
                    "Witness",
                    "Family",
                    "Informant",
                    "Godparent",
                ],
                "child_reference_types": [
                    "None",
                    "Birth",
                    "Adopted",
                    "Stepchild",
                    "Sponsored",
                    "Foster",
                    "Unknown",
                ],
                "name_types": ["Unknown", "Also Known As", "Birth Name", "Married Name"],
            },
            "custom": {
                "event_types": ["Widowhood"],
                "event_role_types": [],
                "child_reference_types": [],
                "name_types": [],
                "family_relation_types": [],
            },
        }
        #: When set, DELETE commits and then answers this status, as
        #: gramps-webapi 3.21.1 does when its search-index step fails after
        #: the transaction has landed.
        self.delete_error_after_commit: int | None = None
        #: When set, DELETE fails with this status and deletes nothing.
        self.delete_error_before_commit: int | None = None
        #: When set, every PUT fails with this status and writes nothing.
        self.put_error: int | None = None
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
        path = request.url.path
        method = request.method

        if path == "/api/token/" and method == "POST":
            return httpx.Response(200, json={"access_token": "acc", "refresh_token": "ref"})
        if path == "/api/token/refresh/" and method == "POST":
            return httpx.Response(200, json={"access_token": "acc"})

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
            return httpx.Response(200, json=self.timelines.get(m_tl.group(2), []))
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
            return httpx.Response(200, json=self.consolidated)
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
            return httpx.Response(200, json=self.types)
        if path == "/api/objects/" and method == "POST":
            payloads = json.loads(request.content or b"[]")
            out = []
            for item in payloads:
                typ = _CLASS_TO_TYPE.get(item.get("_class", ""), "note")
                admitted = dict(item)
                refused = _admit(_TYPE_TO_CLASS[typ], admitted)
                if refused is not None:
                    return refused
                obj = _complete(_TYPE_TO_CLASS[typ], admitted)
                obj["handle"] = self._new_handle()
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
                return httpx.Response(200, json="OCRED TEXT")
            if rest.endswith("/file"):
                return httpx.Response(200, json={"handle": handle})
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

    def _create(self, typ: str, request: httpx.Request) -> httpx.Response:
        if self.write_forbidden:
            return httpx.Response(403, json={"message": "Forbidden: database is read-only"})
        payload = json.loads(request.content or b"{}")
        admitted = dict(payload)
        refused = _admit(_TYPE_TO_CLASS[typ], admitted)
        if refused is not None:
            return refused
        obj = _complete(_TYPE_TO_CLASS[typ], admitted)
        obj["handle"] = self._new_handle()
        if typ != "tag":  # a tag has a name and a handle, no gramps_id
            obj.setdefault("gramps_id", self._new_gid(typ))
        obj["change"] = int(time.time())
        self.store[typ][obj["handle"]] = obj
        self.requests.append(("POST", typ, payload))
        if typ == "family":
            self._family_cascade(None, obj)
        return httpx.Response(201, json=self._change_record(typ, obj, "add"))

    def _update(self, typ: str, handle: str, request: httpx.Request) -> httpx.Response:
        if self.write_forbidden:
            return httpx.Response(403, json={"message": "Forbidden: database is read-only"})
        if self.put_error:
            return httpx.Response(self.put_error, json={"message": "write failed"})
        payload = json.loads(request.content or b"{}")
        sent = {k: v for k, v in payload.items() if k not in _COMPUTED_KEYS}
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
        self.store[typ][handle] = obj
        self.requests.append(("PUT", typ, payload))
        if typ == "family":
            self._family_cascade(previous, obj)
        return httpx.Response(200, json=self._change_record(typ, obj, "update"))

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
        """Mimic the server-side merge: re-point references, then delete the loser.

        Deliberately simple, but it does move references -- a test that merged
        without re-pointing would pass against a fake that only deleted.
        """
        if keep not in self.store[typ] or drop not in self.store[typ]:
            return httpx.Response(404, json={"message": "not found"})
        self.merges.append((typ, keep, drop))
        for objects in self.store.values():
            for obj in objects.values():
                _repoint(obj, drop, keep)
        loser = self.store[typ].pop(drop)
        winner = self.store[typ][keep]
        # Place.merge also unions the enclosures (_merge_placeref_list), which
        # is how a merged place ends up with two undated parents.
        for key in ("media_list", "note_list", "citation_list", "tag_list", "placeref_list"):
            if loser.get(key):
                merged = list(winner.get(key) or [])
                for entry in loser[key]:
                    if entry not in merged:
                        merged.append(entry)
                winner[key] = merged
        return httpx.Response(200, json=[])

    def _transactions(self, path: str, method: str, request: httpx.Request) -> httpx.Response:
        if path.rstrip("/") == "/api/transactions/history":
            return httpx.Response(200, json=self.transactions)
        m_one = re.match(r"^/api/transactions/history/(\d+)$", path)
        if m_one and method == "GET":
            found = next((t for t in self.transactions if t["id"] == int(m_one.group(1))), None)
            if found is None:
                return httpx.Response(404, json={"message": "no such transaction"})
            return httpx.Response(200, json=found)
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
        if params.get("gql"):
            objs = [o for o in objs if _gql_match(o, params["gql"])]
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


def _repoint(node: Any, old: str, new: str) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "handle":
                continue
            if value == old:
                node[key] = new
            else:
                _repoint(value, old, new)
    elif isinstance(node, list):
        for i, value in enumerate(node):
            if value == old:
                node[i] = new
            else:
                _repoint(value, old, new)


def _gql_match(obj: dict, query: str) -> bool:
    """A deliberately tiny GrampsQL subset: '<path> <op> <value>' joined by AND.

    Enough to exercise the call path and the single-'=' syntax; the real parser
    lives on the server.
    """
    for clause in query.split(" AND "):
        parts = clause.strip().split(None, 2)
        if len(parts) != 3:
            return False
        path, op, raw = parts
        value: Any = raw.strip('"')
        try:
            value = int(value)
        except ValueError:
            pass
        current: Any = obj
        for segment in path.split("."):
            if segment == "length":
                current = len(current or [])
            elif isinstance(current, dict):
                current = current.get(segment)
            else:
                current = None
        if current is None:
            current = "" if isinstance(value, str) else 0
        if op == "=":
            ok = current == value
        elif op == ">":
            ok = current > value
        elif op == ">=":
            ok = current >= value
        elif op == "<":
            ok = current < value
        elif op == "<=":
            ok = current <= value
        elif op == "~":
            ok = str(value) in str(current)
        else:
            ok = False
        if not ok:
            return False
    return True


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
async def service(fake: FakeGramps):
    with respx.mock(base_url="http://testserver") as router:
        router.route().mock(side_effect=fake.handle)
        client = GrampsWebClient("http://testserver", "mcp", "pw")
        await client.login()
        cfg = Config(api_url="http://testserver", username="mcp", password="pw")
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

    with respx.mock(base_url="http://testserver") as router:
        router.route().mock(side_effect=fake.handle)
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
