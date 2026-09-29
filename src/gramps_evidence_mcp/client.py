"""Async HTTP client for gramps-webapi.

Covers JWT auth with automatic refresh and a one-shot retry on 401, generic
CRUD over the object endpoints, full-text search, transactions and exports.

gramps-webapi wraps each write in its own ``DbTxn`` server-side, so Gramps'
undo history stays coherent one object at a time.

Log lines carry handles, ids, object types and HTTP status only, never record
contents.
"""

from __future__ import annotations

import json
import logging
from typing import Any
from urllib.parse import quote

import httpx

logger = logging.getLogger("gramps_evidence_mcp.client")

# gramps-webapi object type -> REST collection segment.
ENDPOINTS = {
    "person": "people",
    "family": "families",
    "event": "events",
    "place": "places",
    "source": "sources",
    "citation": "citations",
    "repository": "repositories",
    "media": "media",
    "note": "notes",
    "tag": "tags",
}

#: Syntax summary handed to callers writing GrampsQL. See ``docs/PITFALLS.md``
#: section 7 for the traps behind it.
GQL_NOTES = """GrampsQL runs over the raw object JSON. Use a single '=' for \
equality (not '=='), '~' for substring, and '<field>.length' for list sizes; \
combine with AND/OR. A field the object does not have matches nothing rather \
than raising, so a zero count can mean a misspelt field. Event 'type' is such a \
field -- use query_records, which reaches it through type.value. Sources have \
no citation_list; find uncited sources via backlinks. Booleans compare as \
integers: 'private = 1', not 'private = true'."""

#: Seconds allowed for ``GET /api/facts/``, whatever the configured default.
#: The server computes every statistic on each call, and excluding living
#: people runs Gramps' living proxy over the whole tree first. Measured on
#: gramps-webapi 3.21.1 against a tree of about 900 people: 21 s as is,
#: 42 s with living people excluded -- both past the 30 s default.
FACTS_TIMEOUT = 180.0

# gramps-webapi object type -> Gramps _class name (used to pick the right change
# record out of a write response that may cascade across several objects).
_CLASS_NAMES = {
    "person": "Person",
    "family": "Family",
    "event": "Event",
    "place": "Place",
    "source": "Source",
    "citation": "Citation",
    "repository": "Repository",
    "media": "Media",
    "note": "Note",
    "tag": "Tag",
}


class InvalidIdentifierError(ValueError):
    """Raised when a value bound for a request path could leave its segment."""


def _seg(value: Any) -> str:
    """Escape one path segment completely, or refuse it.

    ``quote`` keeps ``/`` by default, and httpx collapses ``..`` in a path, so
    a filter named ``../../people/<handle>`` once turned ``delete_filter`` into
    ``DELETE /api/people/<handle>``. Escaping every character that is not
    unreserved keeps a value inside its segment; ``.`` and ``..`` are refused
    outright because they survive escaping and still collapse.

    Raises
    ------
    InvalidIdentifierError
        For an empty value, ``.`` or ``..``.
    """
    text = str(value)
    if text in ("", ".", ".."):
        raise InvalidIdentifierError(f"{text!r} is not a valid identifier.")
    return quote(text, safe="")


class GrampsApiError(RuntimeError):
    """Raised on a non-2xx response, carrying status + server detail."""

    def __init__(self, status: int, detail: str, *, method: str, path: str):
        """Record the failing request and the server's explanation."""
        self.status = status
        self.detail = detail
        super().__init__(f"{method} {path} -> {status}: {detail}")


class GrampsWebClient:
    """Authenticated async client for one gramps-webapi instance.

    Usable as an async context manager, which logs in on entry and closes the
    transport on exit.

    Parameters
    ----------
    base_url : str
        Base URL of the gramps-webapi instance.
    username, password : str
        Credentials for a Gramps Web account.
    timeout : float, optional
        Per-request HTTP timeout in seconds.
    """

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        *,
        timeout: float = 30.0,
    ):
        self._base = base_url.rstrip("/")
        self._username = username
        self._password = password
        self._timeout = timeout
        self._access: str | None = None
        self._refresh: str | None = None
        self._http = httpx.AsyncClient(base_url=self._base, timeout=timeout)

    # ----- lifecycle -----
    async def aclose(self) -> None:
        """Close the underlying HTTP transport."""
        await self._http.aclose()

    async def __aenter__(self) -> GrampsWebClient:
        """Log in and return the client."""
        await self.login()
        return self

    async def __aexit__(self, *exc: object) -> None:
        """Close the transport."""
        await self.aclose()

    # ----- auth -----
    async def login(self) -> None:
        """Exchange the configured credentials for access and refresh tokens.

        Raises
        ------
        GrampsApiError
            If the server rejects the credentials.
        """
        resp = await self._http.post(
            "/api/token/",
            json={"username": self._username, "password": self._password},
        )
        if resp.status_code != 200:
            raise GrampsApiError(
                resp.status_code,
                _detail(resp),
                method="POST",
                path="/api/token/",
            )
        data = resp.json()
        self._access = data["access_token"]
        self._refresh = data.get("refresh_token")
        logger.info("authenticated to gramps-webapi")

    async def _refresh_token(self) -> bool:
        """Renew the access token. Returns False if no refresh token works."""
        if not self._refresh:
            return False
        resp = await self._http.post(
            "/api/token/refresh/",
            headers={"Authorization": f"Bearer {self._refresh}"},
        )
        if resp.status_code == 200:
            self._access = resp.json()["access_token"]
            return True
        return False

    def _auth_headers(self) -> dict[str, str]:
        """Bearer header for the current access token, empty if unauthenticated."""
        return {"Authorization": f"Bearer {self._access}"} if self._access else {}

    # ----- core request with 401 retry -----
    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: Any = None,
        timeout: float | None = None,
    ) -> httpx.Response:
        """Send an authenticated request, refreshing once on 401.

        Parameters
        ----------
        method : str
            HTTP method.
        path : str
            Path below the base URL.
        params : dict, optional
            Query parameters.
        json : Any, optional
            JSON request body.
        timeout : float, optional
            Seconds for this request only. Omitted, the client default applies.

        Returns
        -------
        httpx.Response
            The successful response.

        Raises
        ------
        GrampsApiError
            On any status of 400 or above.
        """
        if self._access is None:
            await self.login()
        extra: dict[str, Any] = {} if timeout is None else {"timeout": timeout}
        resp = await self._http.request(
            method, path, params=params, json=json, headers=self._auth_headers(), **extra
        )
        if resp.status_code == 401:
            # Token probably expired; refresh or re-login, then retry once.
            if not await self._refresh_token():
                await self.login()
            resp = await self._http.request(
                method, path, params=params, json=json, headers=self._auth_headers(), **extra
            )
        if resp.status_code >= 400:
            raise GrampsApiError(resp.status_code, _detail(resp), method=method, path=path)
        return resp

    # ----- generic object CRUD -----
    def _collection(self, object_type: str) -> str:
        """Map an object type to its collection path, e.g. ``/api/people/``."""
        try:
            seg = ENDPOINTS[object_type]
        except KeyError as exc:
            raise ValueError(f"Unknown object type: {object_type}") from exc
        return f"/api/{seg}/"

    async def get_object(
        self,
        object_type: str,
        handle: str,
        *,
        keys: str | None = None,
        extend: str | None = None,
        profile: str | None = None,
        locale: str | None = None,
        backlinks: bool = False,
    ) -> dict:
        """Fetch one object by handle.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`, e.g. ``"person"``.
        handle : str
            The object's internal handle.
        keys, extend, profile, locale : str, optional
            Passed through to the API. Note that ``keys`` filters the whole
            response, including backlinks.
        backlinks : bool, optional
            Ask which objects point at this one. Must be requested explicitly:
            the key is omitted otherwise and ``extend="all"`` does not imply
            it. See ``docs/PITFALLS.md`` sections 1 and 2.

        Returns
        -------
        dict
            The object as returned by the API.
        """
        params: dict[str, Any] = {}
        for k, v in (("keys", keys), ("extend", extend), ("profile", profile), ("locale", locale)):
            if v:
                params[k] = v
        if backlinks:
            params["backlinks"] = "1"
        resp = await self._request(
            "GET", self._collection(object_type) + _seg(handle), params=params or None
        )
        return resp.json()

    async def get_by_gramps_id(
        self, object_type: str, gramps_id: str, **kwargs: Any
    ) -> dict | None:
        """Look up one object by its Gramps id.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`.
        gramps_id : str
            The user-facing id, e.g. ``"I0001"``.
        **kwargs
            ``keys``, ``extend``, ``profile``, ``locale``, ``backlinks``.

        Returns
        -------
        dict or None
            The object, or None when no object has that id, so callers can
            fall back rather than handle a 404.
        """
        params: dict[str, Any] = {"gramps_id": gramps_id}
        for k in ("keys", "extend", "profile", "locale"):
            if kwargs.get(k):
                params[k] = kwargs[k]
        if kwargs.get("backlinks"):
            params["backlinks"] = "1"
        try:
            resp = await self._request("GET", self._collection(object_type), params=params)
        except GrampsApiError as exc:
            if exc.status == 404:
                return None
            raise
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else None
        return data

    async def list_objects(
        self,
        object_type: str,
        *,
        rules: dict | None = None,
        gql: str | None = None,
        oql: str | None = None,
        handles: list[str] | str | None = None,
        gramps_id: str | None = None,
        dates: str | None = None,
        filter_name: str | None = None,
        keys: str | None = None,
        skipkeys: str | None = None,
        page: int | None = None,
        pagesize: int | None = None,
        extend: str | None = None,
        profile: str | None = None,
        sort: str | None = None,
        locale: str | None = None,
        backlinks: bool = False,
        strip: bool = False,
        filemissing: bool = False,
    ) -> list[dict]:
        """List a collection, filtered server-side.

        Filtering on the server rather than in Python is what keeps audit
        passes over a large tree from timing out. Three mechanisms, in
        increasing power:

        ``handles``
            Batch-fetch an explicit set, replacing an N+1 loop.
        ``gql``
            A GrampsQL expression over the raw object JSON, e.g.
            ``'confidence >= 3 AND page = ""'``. See :data:`GQL_NOTES`.
        ``rules``
            Gramps' own filter-rule objects, for what GrampsQL cannot express.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`.
        rules : dict, optional
            Gramps filter-rule object, JSON-encoded into the query.
        gql, oql : str, optional
            Query expressions.
        handles : list of str or str, optional
            Explicit handles to fetch.
        gramps_id, dates, filter_name, sort, locale : str, optional
            Passed through to the API.
        keys, skipkeys, extend, profile : str, optional
            Response shaping. ``keys`` filters the whole response.
        page, pagesize : int, optional
            Pagination.
        backlinks, strip, filemissing : bool, optional
            Boolean flags passed as ``1`` when set.

        Returns
        -------
        list of dict
            Matching objects. A single-object response is wrapped in a list.
        """
        params: dict[str, Any] = {}
        if rules is not None:
            import json as _json

            params["rules"] = _json.dumps(rules)
        if handles is not None:
            params["handles"] = handles if isinstance(handles, str) else ",".join(handles)
        for k, v in (
            ("gql", gql),
            ("oql", oql),
            ("gramps_id", gramps_id),
            ("dates", dates),
            ("filter", filter_name),
            ("keys", keys),
            ("skipkeys", skipkeys),
            ("page", page),
            ("pagesize", pagesize),
            ("extend", extend),
            ("profile", profile),
            ("sort", sort),
            ("locale", locale),
        ):
            if v is not None:
                params[k] = v
        for k, flag in (("backlinks", backlinks), ("strip", strip), ("filemissing", filemissing)):
            if flag:
                params[k] = "1"
        resp = await self._request("GET", self._collection(object_type), params=params or None)
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def create_object(self, object_type: str, payload: dict) -> dict:
        """Create one object.

        The server assigns the handle and gramps_id, coerces English type
        strings such as ``"Birth"`` to internal type dicts, and wraps the write
        in its own transaction.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`.
        payload : dict
            The object to create, without handle or gramps_id.

        Returns
        -------
        dict
            ``{"handle", "gramps_id", "new", "_raw"}`` for the created object,
            extracted from the list of change records the API returns.
        """
        expected = _CLASS_NAMES[object_type]
        resp = await self._request("POST", self._collection(object_type), json=payload)
        return _normalize_write_response(resp.json(), expected)

    async def update_object(self, object_type: str, handle: str, payload: dict) -> dict:
        """Replace one object.

        ``PUT`` replaces the whole record: any field absent from ``payload`` is
        lost. Callers should send a full object. See ``docs/PITFALLS.md``
        section 1.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`.
        handle : str
            Handle of the object to replace.
        payload : dict
            The complete object.

        Returns
        -------
        dict
            ``{"handle", "gramps_id", "new", "_raw"}`` for the updated object.
        """
        expected = _CLASS_NAMES[object_type]
        resp = await self._request(
            "PUT", self._collection(object_type) + _seg(handle), json=payload
        )
        return _normalize_write_response(resp.json(), expected)

    async def delete_object(self, object_type: str, handle: str) -> None:
        """Delete one object by handle."""
        await self._request("DELETE", self._collection(object_type) + _seg(handle))

    # ----- search -----
    async def search(self, query: str, *, page: int = 1, pagesize: int = 20) -> list[dict]:
        """Full-text search the tree.

        Parameters
        ----------
        query : str
            Search string.
        page, pagesize : int, optional
            Pagination.

        Returns
        -------
        list of dict
            Search hits.
        """
        resp = await self._request(
            "GET",
            "/api/search/",
            params={"query": query, "page": page, "pagesize": pagesize},
        )
        return resp.json()

    # ----- metadata / stats -----
    async def metadata(self) -> dict:
        """Return the instance's metadata, including object counts."""
        resp = await self._request("GET", "/api/metadata/")
        return resp.json()

    async def count(self, object_type: str) -> int:
        """Count objects of a type.

        Reads the ``X-Total-Count`` header, requesting a single-item page so
        the body stays small.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`.

        Returns
        -------
        int
            Total objects of that type.
        """
        resp = await self._request(
            "GET",
            self._collection(object_type),
            params={"page": 1, "pagesize": 1, "keys": "handle"},
        )
        total = resp.headers.get("X-Total-Count")
        if total is not None:
            try:
                return int(total)
            except ValueError:
                pass
        body = resp.json()
        return len(body) if isinstance(body, list) else 0

    async def upload_media_bytes(self, handle: str, content: bytes, mime: str) -> None:
        """Upload the binary for an existing Media object.

        The second half of a two-step create: :meth:`create_object` reserves a
        handle, then this sends the bytes. The server copies them into the
        tree's managed media directory and records the checksum. The body is
        sent raw, not multipart, with the file's Content-Type.

        Parameters
        ----------
        handle : str
            Handle of an existing Media object.
        content : bytes
            The file's bytes.
        mime : str
            The file's Content-Type.

        Raises
        ------
        GrampsApiError
            On any status of 400 or above, after one retry on 401.
        """
        if self._access is None:
            await self.login()
        path = f"/api/media/{_seg(handle)}/file"
        headers = {**self._auth_headers(), "Content-Type": mime}
        resp = await self._http.put(path, content=content, headers=headers)
        if resp.status_code == 401:
            if not await self._refresh_token():
                await self.login()
            headers = {**self._auth_headers(), "Content-Type": mime}
            resp = await self._http.put(path, content=content, headers=headers)
        if resp.status_code >= 400:
            raise GrampsApiError(resp.status_code, _detail(resp), method="PUT", path=path)

    # ----- merge -----
    async def merge(self, object_type: str, keep_handle: str, drop_handle: str) -> dict:
        """Merge one object into another using the server's merge endpoint.

        Gramps' native merge, not a re-implementation: the server re-points
        every reference, unions the subordinate lists and deletes the loser
        inside one transaction. Hand-rolled merges drop data -- see
        ``docs/PITFALLS.md`` section 8.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`.
        keep_handle : str
            The survivor.
        drop_handle : str
            The object merged away and deleted.

        Returns
        -------
        dict
            ``{"raw": <response body or None>}``.
        """
        path = f"{self._collection(object_type)}{_seg(keep_handle)}/merge/{_seg(drop_handle)}"
        resp = await self._request("POST", path)
        try:
            return {"raw": resp.json()}
        except Exception:
            return {"raw": None}

    # ----- bulk object operations -----
    async def create_objects(self, payloads: list[dict]) -> list[dict]:
        """Create several objects in one transaction.

        All of them land or none do, which suits a set that only makes sense
        together -- a source, its citation, and the note recording why it was
        created. The tool surface writes one object at a time instead; see
        ``docs/ARCHITECTURE.md``.

        Parameters
        ----------
        payloads : list of dict
            Objects to create.

        Returns
        -------
        list of dict
            The API's change records.
        """
        resp = await self._request("POST", "/api/objects/", json=payloads)
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def delete_by_handles(self, items: list[dict]) -> dict:
        """Delete several objects in one transaction.

        Parameters
        ----------
        items : list of dict
            Objects to delete, each ``{"_class": ..., "handle": ...}``.

        Returns
        -------
        dict
            The API's response, or an empty dict if it returned no body.
        """
        resp = await self._request("POST", "/api/objects/delete-by-handle/", json=items)
        try:
            return resp.json()
        except Exception:
            return {}

    # ----- transaction log / undo -----
    async def transactions(
        self,
        *,
        page: int | None = None,
        pagesize: int | None = None,
        sort: str = "-id",
        before: float | None = None,
        after: float | None = None,
        old: bool = False,
        new: bool = False,
    ) -> list[dict]:
        """Read the change log.

        Shows who changed what, when, and in which transaction -- the way to
        check whether another session has been writing.

        Parameters
        ----------
        page, pagesize : int, optional
            Pagination.
        sort : str, optional
            Sort order. Defaults to newest first.
        before, after : float, optional
            Unix timestamp bounds.
        old, new : bool, optional
            Include the before and after object states.

        Returns
        -------
        list of dict
            Change records.
        """
        params: dict[str, Any] = {"sort": sort}
        for k, v in (("page", page), ("pagesize", pagesize), ("before", before), ("after", after)):
            if v is not None:
                params[k] = v
        for k, flag in (("old", old), ("new", new)):
            if flag:
                params[k] = "1"
        resp = await self._request("GET", "/api/transactions/history/", params=params)
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def undo_check(self, transaction_id: int) -> dict:
        """Report whether a transaction can be undone cleanly.

        Parameters
        ----------
        transaction_id : int
            Id from the change log.

        Returns
        -------
        dict
            The server's verdict. Nothing is changed.
        """
        resp = await self._request("GET", f"/api/transactions/history/{transaction_id}/undo")
        return resp.json()

    async def undo(
        self, transaction_id: int, *, force: bool = False, message: str | None = None
    ) -> dict:
        """Undo a transaction.

        Runs as a background task server-side.

        Parameters
        ----------
        transaction_id : int
            Id from the change log.
        force : bool, optional
            Undo even when :meth:`undo_check` objects.
        message : str, optional
            Label recorded against the undo.

        Returns
        -------
        dict
            The server's response, or an empty dict if it returned no body.
        """
        params: dict[str, Any] = {}
        if force:
            params["force"] = "1"
        if message:
            params["message"] = message
        resp = await self._request(
            "POST",
            f"/api/transactions/history/{transaction_id}/undo",
            params=params or None,
        )
        try:
            return resp.json()
        except Exception:
            return {}

    # ----- export / import -----
    async def exporters(self) -> list[dict]:
        """List the export formats this instance offers."""
        resp = await self._request("GET", "/api/exporters/")
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def export_file(self, extension: str, **options: Any) -> bytes:
        """Download a full-tree export.

        The primitive behind taking a backup before a bulk write.

        Parameters
        ----------
        extension : str
            Format extension. ``gramps`` (Gramps XML) is lossless; ``ged``,
            ``json`` and ``csv`` are also available.
        **options
            Exporter options. None values are dropped.

        Returns
        -------
        bytes
            The export file.
        """
        params = {k: v for k, v in options.items() if v is not None}
        resp = await self._request(
            "GET", f"/api/exporters/{_seg(extension)}/file", params=params or None
        )
        return resp.content

    # ----- media extras -----
    async def ocr_media(
        self, handle: str, *, lang: str = "eng", output_format: str = "string"
    ) -> Any:
        """Run OCR on a media object's file, server-side.

        Parameters
        ----------
        handle : str
            Handle of the Media object.
        lang : str, optional
            Tesseract language code.
        output_format : str, optional
            Tesseract output format.

        Returns
        -------
        Any
            Parsed JSON if the server sends it, else the raw text.
        """
        resp = await self._request(
            "POST",
            f"/api/media/{_seg(handle)}/ocr",
            params={"lang": lang, "format": output_format},
        )
        try:
            return resp.json()
        except Exception:
            return resp.text

    # ----- reports -----
    async def reports(self, report_id: str | None = None) -> Any:
        """List the available reports, or one report's attributes.

        Parameters
        ----------
        report_id : str, optional
            A report id. Omit to list every report.

        Returns
        -------
        Any
            A list of report descriptors, or one descriptor.
        """
        path = f"/api/reports/{_seg(report_id)}" if report_id else "/api/reports/"
        resp = await self._request("GET", path)
        return resp.json()

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
            Report to run.
        options : dict, optional
            Report options, sent as a JSON string. The keys a report accepts
            are in its ``options_dict``.
        locale : str, optional
            Language for the output.

        Returns
        -------
        dict
            Normally a task reference to poll, plus the filename to fetch.
        """
        params: dict[str, Any] = {}
        if options:
            params["options"] = json.dumps(options)
        if locale:
            params["locale"] = locale
        resp = await self._request(
            "POST", f"/api/reports/{_seg(report_id)}/file", params=params or None
        )
        try:
            return resp.json()
        except Exception:
            return {}

    # ----- custom filters -----
    async def filters(self, namespace: str | None = None) -> dict:
        """List custom filters and the rule vocabulary.

        Parameters
        ----------
        namespace : str, optional
            A Gramps namespace such as ``"people"``. Omit for every namespace.

        Returns
        -------
        dict
            ``filters`` already defined and ``rules`` available to build them.
        """
        path = f"/api/filters/{_seg(namespace)}" if namespace else "/api/filters/"
        resp = await self._request("GET", path)
        return resp.json()

    async def create_filter(self, namespace: str, body: dict) -> dict:
        """Create a custom filter in a namespace."""
        resp = await self._request("POST", f"/api/filters/{_seg(namespace)}", json=body)
        try:
            return resp.json()
        except Exception:
            return {}

    async def delete_filter(self, namespace: str, name: str) -> None:
        """Delete a custom filter by name."""
        await self._request("DELETE", f"/api/filters/{_seg(namespace)}/{_seg(name)}")

    # ----- consolidated timelines, tasks, transactions -----
    async def consolidated_timeline(self, kind: str, **params: Any) -> list[dict]:
        """Fetch one timeline spanning several people or families.

        Parameters
        ----------
        kind : {"people", "families"}
            Which consolidated endpoint to read.
        **params
            ``handles``, ``anchor``, ``events``, ``event_classes``, ``dates``,
            ``ratings``, ``discard_empty``, ``page``, ``pagesize``.

        Returns
        -------
        list of dict
            Timeline event profiles across all the named objects.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._request("GET", f"/api/timelines/{kind}/", params=clean or None)
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def task_list(self, limit: int = 100, include_state: bool = True) -> list[dict]:
        """List recent background tasks for this tree."""
        resp = await self._request(
            "GET",
            "/api/tasks/",
            params={"limit": limit, "include_state": "1" if include_state else "0"},
        )
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def transaction(self, transaction_id: int) -> dict:
        """Read one transaction from the change log."""
        resp = await self._request("GET", f"/api/transactions/history/{transaction_id}")
        return resp.json()

    # ----- statistics and researcher -----
    async def facts(self, **params: Any) -> list:
        """Read the tree's record-holders, optionally over a filtered set.

        Allowed :data:`FACTS_TIMEOUT` seconds, or the client default if that
        is longer.
        """
        clean = {k: v for k, v in params.items() if v is not None}
        resp = await self._request(
            "GET",
            "/api/facts/",
            params=clean or None,
            timeout=max(self._timeout, FACTS_TIMEOUT),
        )
        return resp.json()

    async def researcher(self) -> dict:
        """Read the researcher details embedded in exports."""
        resp = await self._request("GET", "/api/metadata/researcher/")
        return resp.json()

    # ----- structured query -----
    async def structured_query(
        self, object_type: str, body: dict
    ) -> tuple[list[dict], int | None, Any]:
        """Run a structured query against one collection.

        The query engine reads indexed columns plus arbitrary ``json_path``
        expressions over the stored object, and a path may cross a
        relationship -- Person to Event via ``birth``/``death``, Family to
        Person via ``father``/``mother``, Event to Place via ``place``.

        Parameters
        ----------
        object_type : str
            Key of :data:`ENDPOINTS`.
        body : dict
            ``select``, ``where``, ``where_expr``, ``order_by``, ``limit``,
            ``after``, ``count``.

        Returns
        -------
        tuple of (list of dict, int or None, Any)
            The rows, the total match count when ``count`` was requested, and
            the keyset cursor for the next page.

        Raises
        ------
        GrampsApiError
            On a rejected column, a malformed clause, or any other 4xx/5xx.
        """
        seg = ENDPOINTS[object_type]
        resp = await self._request("POST", f"/api/{seg}/query/", json=body)
        data = resp.json()
        if isinstance(data, dict):
            rows = data.get("items") or []
            cursor = data.get("next_after")
        else:
            rows, cursor = data or [], None
        total = resp.headers.get("X-Total-Count")
        return rows, (int(total) if total is not None else None), cursor

    async def type_map(self, datatype: str) -> dict:
        """Fetch the server's integer-to-name map for a default type.

        Gramps stores a built-in type as an integer; the ``string`` member is
        populated only for custom types. Filtering on a type therefore means
        filtering on its integer, and this is where the numbers come from.

        Parameters
        ----------
        datatype : str
            A default type name, e.g. ``"event_types"``.

        Returns
        -------
        dict
            Mapping of stringified integer to label.
        """
        resp = await self._request("GET", f"/api/types/default/{_seg(datatype)}/map")
        return resp.json()

    # ----- DNA -----
    async def dna_matches(
        self, handle: str, *, raw: bool = False, locale: str | None = None
    ) -> list[dict]:
        """Fetch the DNA matches recorded against a person.

        Gramps stores a match as an association in the person's
        ``person_ref_list`` whose notes carry the shared-segment data; the
        server parses those into structured segments.

        Parameters
        ----------
        handle : str
            The person's handle.
        raw : bool, optional
            Include the unparsed note strings alongside the segments.
        locale : str, optional
            Language for the estimated relationship wording.

        Returns
        -------
        list of dict
            One entry per match, each with segments and any identified
            common ancestors. Empty when the person has no matches recorded.
        """
        params: dict[str, Any] = {}
        if raw:
            params["raw"] = "1"
        if locale:
            params["locale"] = locale
        resp = await self._request(
            "GET",
            f"/api/people/{_seg(handle)}/dna/matches",
            params=params or None,
        )
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def ydna(self, handle: str, *, raw: bool = False, locale: str | None = None) -> dict:
        """Fetch a person's Y-DNA haplogroup assignment.

        Parameters
        ----------
        handle : str
            The person's handle.
        raw : bool, optional
            Include the raw SNP data string.
        locale : str, optional
            Language for any translated wording.

        Returns
        -------
        dict
            The clade lineage and the YFull tree version. Empty when no
            Y-DNA data is recorded.
        """
        params: dict[str, Any] = {}
        if raw:
            params["raw"] = "1"
        if locale:
            params["locale"] = locale
        resp = await self._request("GET", f"/api/people/{_seg(handle)}/ydna", params=params or None)
        return resp.json()

    async def parse_dna_match(self, data: str) -> list[dict]:
        """Parse a raw DNA match string into structured segments.

        Parameters
        ----------
        data : str
            Segment data as exported by a testing company.

        Returns
        -------
        list of dict
            One entry per segment parsed. **Unparseable input returns an
            empty list with HTTP 200**, not an error, so a caller must treat
            zero segments as a parse failure rather than as no data.
        """
        resp = await self._request("POST", "/api/parsers/dna-match", json={"string": data})
        parsed = resp.json()
        return parsed if isinstance(parsed, list) else []

    # ----- derived views -----
    async def relationship(
        self,
        handle1: str,
        handle2: str,
        *,
        all_paths: bool = False,
        depth: int | None = None,
        locale: str | None = None,
    ) -> dict:
        """Ask the server how two people are related.

        Parameters
        ----------
        handle1, handle2 : str
            Handles of the two people.
        all_paths : bool, optional
            Return every relationship path rather than the most direct one.
        depth : int, optional
            Generations to search.
        locale : str, optional
            Language for the relationship wording.

        Returns
        -------
        dict
            ``relationship_string`` and the generation distances to the common
            ancestor, or a list of them when ``all_paths`` is set.
        """
        suffix = "/all" if all_paths else ""
        path = f"/api/relations/{_seg(handle1)}/{_seg(handle2)}{suffix}"
        resp = await self._request(
            "GET",
            path,
            params={k: v for k, v in (("depth", depth), ("locale", locale)) if v is not None}
            or None,
        )
        return resp.json()

    async def living(self, handle: str, **options: Any) -> dict:
        """Ask the server whether a person is estimated to be alive.

        Parameters
        ----------
        handle : str
            The person's handle.
        **options
            ``average_generation_gap``, ``max_age_probably_alive``,
            ``max_sibling_age_difference``. None values are dropped.

        Returns
        -------
        dict
            ``{"living": bool}``.
        """
        params = {k: v for k, v in options.items() if v is not None}
        resp = await self._request("GET", f"/api/living/{_seg(handle)}", params=params or None)
        return resp.json()

    async def living_dates(self, handle: str, **options: Any) -> dict:
        """Ask the server to estimate a person's birth and death dates.

        Parameters
        ----------
        handle : str
            The person's handle.
        **options
            The same tuning options as :meth:`living`, plus ``locale``.

        Returns
        -------
        dict
            Estimated ``birth`` and ``death``, and an ``explain`` string
            describing which relative the estimate came from.
        """
        params = {k: v for k, v in options.items() if v is not None}
        resp = await self._request(
            "GET", f"/api/living/{_seg(handle)}/dates", params=params or None
        )
        return resp.json()

    async def timeline(self, object_type: str, handle: str, **options: Any) -> list[dict]:
        """Fetch a person's or family's event timeline.

        Parameters
        ----------
        object_type : {"person", "family"}
            Whose timeline to build.
        handle : str
            The anchor object's handle.
        **options
            ``ancestors``, ``offspring``, ``events``, ``event_classes``,
            ``discard_empty``, ``first``, ``last``, ``page``, ``pagesize``,
            ``locale``. None values are dropped.

        Returns
        -------
        list of dict
            Timeline event profiles, each carrying the anchor person's age,
            the citation count and the highest confidence among them.
        """
        seg = ENDPOINTS[object_type]
        params = {k: v for k, v in options.items() if v is not None}
        resp = await self._request(
            "GET", f"/api/{seg}/{_seg(handle)}/timeline", params=params or None
        )
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def event_span(
        self,
        handle1: str,
        handle2: str,
        *,
        as_age: bool | None = None,
        precision: int | None = None,
        locale: str | None = None,
    ) -> dict:
        """Ask the server for the elapsed time between two events.

        Parameters
        ----------
        handle1, handle2 : str
            Handles of the two events.
        as_age : bool, optional
            Phrase the span as an age rather than an interval.
        precision : int, optional
            How many units to include, e.g. years, months, days.
        locale : str, optional
            Language for the wording.

        Returns
        -------
        dict
            ``{"span": str}``.
        """
        params = {
            k: v
            for k, v in (("as_age", as_age), ("precision", precision), ("locale", locale))
            if v is not None
        }
        resp = await self._request(
            "GET",
            f"/api/events/{_seg(handle1)}/span/{_seg(handle2)}",
            params=params or None,
        )
        return resp.json()

    async def reindex_search(self, *, full: bool = False, semantic: bool | None = None) -> dict:
        """Trigger a rebuild of the full-text search index.

        Parameters
        ----------
        full : bool, optional
            Rebuild from scratch rather than updating incrementally.
        semantic : bool, optional
            Rebuild the semantic index instead of the keyword one.

        Returns
        -------
        dict
            The response, normally carrying a task to poll.
        """
        params: dict[str, Any] = {}
        if full:
            params["full"] = "1"
        if semantic is not None:
            params["semantic"] = "1" if semantic else "0"
        resp = await self._request("POST", "/api/search/index/", params=params or None)
        try:
            return resp.json()
        except Exception:
            return {}

    # ----- trees, tasks, verification -----
    async def trees(self) -> list[dict]:
        """List the trees this account can reach.

        Returns
        -------
        list of dict
            One entry per tree, carrying ``id``, ``name`` and usage counts.
        """
        resp = await self._request("GET", "/api/trees/")
        data = resp.json()
        return data if isinstance(data, list) else [data]

    async def task(self, task_id: str) -> dict:
        """Read a background task's status.

        Long operations -- undo, import, reindex, verification -- are dispatched
        to a worker and answer immediately with a task id. This is the only way
        to learn whether one finished.

        Parameters
        ----------
        task_id : str
            The task UUID returned by whichever call dispatched it.

        Returns
        -------
        dict
            ``state``, ``info``, ``result``, ``result_object`` and timestamps.
        """
        resp = await self._request("GET", f"/api/tasks/{_seg(task_id)}")
        return resp.json()

    async def verify(self, tree_id: str, **thresholds: Any) -> Any:
        """Run Gramps' genealogical verification checks against a tree.

        Parameters
        ----------
        tree_id : str
            Id of the tree to check.
        **thresholds
            Verification bounds such as ``oldage`` and ``yngmom``. None values
            are dropped so the server's own defaults apply.

        Returns
        -------
        Any
            The findings, or a task handle when the server runs it in the
            background.
        """
        params = {k: v for k, v in thresholds.items() if v is not None}
        resp = await self._request(
            "POST", f"/api/trees/{_seg(tree_id)}/verify", params=params or None
        )
        try:
            return resp.json()
        except Exception:
            return {}

    # ----- type registry -----
    async def types(self) -> dict:
        """Return the tree's default and custom type vocabularies.

        Worth checking before writing a type string: an unknown event type is
        silently accepted as a new custom type, so a typo becomes part of the
        tree's vocabulary rather than an error.

        Returns
        -------
        dict
            Type vocabularies keyed by category.
        """
        resp = await self._request("GET", "/api/types/")
        return resp.json()


def _detail(resp: httpx.Response) -> str:
    """Pull a human-readable explanation out of an error response body."""
    try:
        body = resp.json()
        if isinstance(body, dict):
            return str(body.get("message") or body.get("error") or body)
        return str(body)
    except Exception:
        return resp.text[:300]


def _normalize_write_response(data: Any, expected_class: str | None = None) -> dict:
    """Extract the object a write was about from the API's change records.

    Write endpoints return a list of records shaped like ``{"type": "add",
    "_class": "Person", "handle": ..., "new": {...}}``. One create can cascade
    -- creating a Family also updates the parent Persons -- so the record
    matching ``expected_class`` wins, preferring ``add`` over ``update``.

    Parameters
    ----------
    data : Any
        The write response body.
    expected_class : str, optional
        Gramps class name of the object the caller wrote.

    Returns
    -------
    dict
        ``{"handle", "gramps_id", "new", "_raw"}``. Handle is None when no
        record could be identified.
    """
    if isinstance(data, dict) and "handle" in data:  # already unwrapped
        return {
            "handle": data["handle"],
            "gramps_id": data.get("gramps_id"),
            "new": data,
            "_raw": data,
        }
    if isinstance(data, list) and data:
        candidates = [r for r in data if isinstance(r, dict) and r.get("handle")]
        if expected_class:
            matching = [r for r in candidates if r.get("_class") == expected_class]
            if matching:
                candidates = matching
        # The requested write outranks incidental cascade updates.
        candidates.sort(key=lambda r: 0 if r.get("type") == "add" else 1)
        if candidates:
            rec = candidates[0]
            new = rec.get("new") if isinstance(rec.get("new"), dict) else {}
            return {
                "handle": rec["handle"],
                "gramps_id": (new or {}).get("gramps_id"),
                "new": new,
                "_raw": data,
            }
    return {"handle": None, "gramps_id": None, "new": {}, "_raw": data}
