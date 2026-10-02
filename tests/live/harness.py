"""Running tool calls against a real gramps-webapi, or the fake, the same way.

The live suite and the contract tests share this: a ``call(tool, **args)``
that invokes a registered tool exactly as a client does, wired either to the
in-memory fake (``tests/conftest.py``) or to the throwaway server named by the
``GRAMPS_LIVE_*`` settings, plus a normalisation that lets the two runs'
answers be compared.
"""

from __future__ import annotations

import contextlib
import functools
import ipaddress
import json
import os
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx
import respx

from gramps_evidence_mcp import server
from gramps_evidence_mcp.client import ENDPOINTS, GrampsWebClient
from gramps_evidence_mcp.config import Config
from gramps_evidence_mcp.service import GrampsService

#: Read once, at import: the root conftest clears GRAMPS_MCP_* for every test,
#: and these names are deliberately different, so the MCP server's own .env
#: can never point this suite at a real tree.
LIVE_URL = os.environ.get("GRAMPS_LIVE_URL", "")
LIVE_USERNAME = os.environ.get("GRAMPS_LIVE_USERNAME", "")
LIVE_PASSWORD = os.environ.get("GRAMPS_LIVE_PASSWORD", "")
LIVE_VERSION = os.environ.get("GRAMPS_LIVE_VERSION", "")
LIVE_SECRET_KEY = os.environ.get("GRAMPS_LIVE_SECRET_KEY", "")

#: Deleted children before parents, so each delete has the least to clean up.
WIPE_ORDER = (
    "family",
    "event",
    "person",
    "citation",
    "note",
    "media",
    "source",
    "repository",
    "place",
    "tag",
)


def live_version() -> tuple[int, ...]:
    """The server's version as a tuple, e.g. (3, 21, 1); () when unknown."""
    return tuple(int(p) for p in re.findall(r"\d+", LIVE_VERSION)[:3])


def loopback_url(url: str) -> bool:
    """Whether a URL names a loopback address literally, as the guard requires."""
    host = urlparse(url).hostname or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


@functools.cache
def session_tokens() -> tuple[str, str]:
    """Log in once for the whole session: (access token, refresh token).

    gramps-webapi limits its token endpoints to one request a second per
    address (``@limiter.limit("1/second")`` in ``api/resources/token.py``,
    3.21.1), so a login per test is refused with HTTP 429. Every test's client
    is handed these tokens instead, and refreshes them as any client does.
    """
    with httpx.Client(base_url=LIVE_URL, timeout=30) as http:
        for _ in range(5):
            resp = http.post(
                "/api/token/", json={"username": LIVE_USERNAME, "password": LIVE_PASSWORD}
            )
            if resp.status_code != 429:
                break
            time.sleep(1.1)
        resp.raise_for_status()
        data = resp.json()
        return data["access_token"], data["refresh_token"]


def count_objects(url: str) -> dict[str, int]:
    """Count every object type on a server, synchronously, for the empty-tree guard."""
    with httpx.Client(base_url=url, timeout=30) as http:
        headers = {"Authorization": f"Bearer {session_tokens()[0]}"}
        counts = {}
        for object_type, segment in ENDPOINTS.items():
            resp = http.get(
                f"/api/{segment}/", params={"pagesize": 1, "keys": "handle"}, headers=headers
            )
            resp.raise_for_status()
            counts[object_type] = int(resp.headers.get("X-Total-Count") or len(resp.json()))
        return counts


async def wipe(client: GrampsWebClient) -> None:
    """Delete every object in the throwaway tree, until none is left."""
    for _ in range(3):
        left = 0
        for object_type in WIPE_ORDER:
            for row in await client.list_objects(object_type, keys="handle"):
                left += 1
                with contextlib.suppress(Exception):
                    await client.delete_object(object_type, row["handle"])
        if not left:
            return


def _bind(client: GrampsWebClient, cfg: Config):
    """Point the tool layer at a client, and return a client-style ``call``."""
    service = GrampsService(client, cfg)
    server.state.config, server.state.client = cfg, client
    server.state.service, server.state.library = service, None

    async def call(tool_name: str, /, **arguments: Any) -> Any:
        result = await server.mcp.call_tool(tool_name, arguments)
        return json.loads(result.content[0].text)

    call.client = client  # type: ignore[attr-defined]
    call.service = service  # type: ignore[attr-defined]
    return call


@contextlib.asynccontextmanager
async def _saved_state() -> AsyncIterator[None]:
    saved = (server.state.config, server.state.client, server.state.service, server.state.library)
    try:
        yield
    finally:
        (
            server.state.config,
            server.state.client,
            server.state.service,
            server.state.library,
        ) = saved


@contextlib.asynccontextmanager
async def live_tools(cache_dir: Path) -> AsyncIterator[Any]:
    """Tool calls against the throwaway server; the tree is wiped afterwards."""
    client = GrampsWebClient(LIVE_URL, LIVE_USERNAME, LIVE_PASSWORD, timeout=120)
    client._access, client._refresh = session_tokens()
    cfg = Config(
        api_url=LIVE_URL, username=LIVE_USERNAME, password=LIVE_PASSWORD, cache_dir=cache_dir
    )
    async with _saved_state():
        try:
            yield _bind(client, cfg)
        finally:
            await wipe(client)
            await client.aclose()


@contextlib.asynccontextmanager
async def fake_tools(cache_dir: Path) -> AsyncIterator[Any]:
    """Tool calls against the in-memory fake, exactly as tests/conftest.py wires it."""
    from tests.conftest import FakeGramps

    fake = FakeGramps()
    if LIVE_VERSION:  # answer as the version it is being compared with
        fake.metadata["gramps_webapi"]["version"] = LIVE_VERSION
    with respx.mock(base_url="http://testserver") as router:
        router.route().mock(side_effect=fake.handle)
        client = GrampsWebClient("http://testserver", "mcp", "pw")
        await client.login()
        cfg = Config(
            api_url="http://testserver", username="mcp", password="pw", cache_dir=cache_dir
        )
        async with _saved_state():
            try:
                call = _bind(client, cfg)
                call.fake = fake  # type: ignore[attr-defined]
                yield call
            finally:
                await client.aclose()


# --------------------------------------------------------------------------- #
# Normalising two runs' answers so they can be compared
# --------------------------------------------------------------------------- #
class Normaliser:
    """Replace handles and gramps_ids with names that mean the same in both runs.

    The fake numbers ids from I0001 and makes handles like h000001; the server
    numbers from I0000 and makes long random handles. After every call the
    tree is listed, and each object not seen before is named by its type and
    the order it appeared in -- ``person#1``, ``event#2`` -- under its handle
    and its gramps_id alike. Both runs create in the same order, so the same
    object gets the same name in both.
    """

    def __init__(self) -> None:
        self.names: dict[str, str] = {}
        self.counts: dict[str, int] = {}

    async def learn(self, client: GrampsWebClient) -> None:
        for object_type in ENDPOINTS:
            rows = await client.list_objects(object_type, keys="handle,gramps_id")
            fresh = [r for r in rows if r.get("handle") not in self.names]
            for row in sorted(fresh, key=lambda r: (r.get("gramps_id") or "", r["handle"])):
                self.counts[object_type] = self.counts.get(object_type, 0) + 1
                name = f"<{object_type}#{self.counts[object_type]}>"
                self.names[row["handle"]] = name
                if row.get("gramps_id"):
                    self.names[row["gramps_id"]] = name

    def __call__(self, value: Any) -> Any:
        if isinstance(value, dict):
            return {k: self(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self(v) for v in value]
        if isinstance(value, str):
            if value in self.names:
                return self.names[value]
            # Longest first, so a handle is never half-replaced by a shorter id.
            for raw in sorted(self.names, key=len, reverse=True):
                if raw in value:
                    value = re.sub(rf"(?<![\w]){re.escape(raw)}(?![\w])", self.names[raw], value)
            return value
        return value
