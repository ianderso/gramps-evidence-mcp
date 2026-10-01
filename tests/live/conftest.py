"""The live suite: tests against a real, throwaway gramps-webapi.

Everything else under tests/ runs against the in-memory fake and never opens a
connection. These tests exist because the fake is only as right as our
reading of the server: they check the behaviour the tools rely on, and the
fake's answers, against the real thing, pinned to the versions the project
supports (``tests/live/server-*.txt``).

Skipped unless ``GRAMPS_LIVE_URL`` is set. ``tests/live/start_server.sh``
starts a server and prints the settings (CONTRIBUTING.md has the details)::

    export $(tests/live/start_server.sh 3.21.1)
    uv run pytest tests/live

This suite creates, edits and deletes freely, so it guards where it runs:

- it reads ``GRAMPS_LIVE_*`` and never ``GRAMPS_MCP_*``, so the MCP server's
  own configuration cannot point it at a real tree;
- the URL must be a loopback address, written as one;
- the tree must be empty when the session starts, or nothing runs;
- every test is followed by a wipe of the tree.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from pathlib import Path

import pytest

from .harness import (
    LIVE_URL,
    count_objects,
    fake_tools,
    live_tools,
    loopback_url,
)

_HERE = Path(__file__).parent


def pytest_collection_modifyitems(config, items):
    """Skip the live suite unless a throwaway server was named.

    CI sets ``GRAMPS_LIVE_REQUIRED``, so a server that failed to start or
    settings that failed to arrive fail the job instead of skipping every
    test and passing it.
    """
    if LIVE_URL:
        return
    if os.environ.get("GRAMPS_LIVE_REQUIRED"):
        raise pytest.UsageError(
            "GRAMPS_LIVE_REQUIRED is set but GRAMPS_LIVE_URL is not: the live suite "
            "would skip every test. Did tests/live/start_server.sh run?"
        )
    skip = pytest.mark.skip(
        reason="needs a throwaway gramps-webapi: export $(tests/live/start_server.sh 3.21.1)"
    )
    for item in items:
        if _HERE in Path(item.fspath).parents:
            item.add_marker(skip)


@pytest.fixture(scope="session")
def live_server() -> str:
    """Refuse to run anywhere but an empty tree on a loopback address."""
    if not loopback_url(LIVE_URL):
        pytest.exit(
            f"GRAMPS_LIVE_URL must be a loopback address such as http://127.0.0.1:5555, "
            f"not {LIVE_URL!r}: this suite deletes everything it finds.",
            returncode=3,
        )
    counts = count_objects(LIVE_URL)
    if any(counts.values()):
        pytest.exit(
            f"The tree at {LIVE_URL} is not empty ({counts}). The live suite writes and "
            "wipes, so it runs only against a fresh throwaway tree from start_server.sh.",
            returncode=3,
        )
    return LIVE_URL


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Overrides the root guard here: loopback only, so only the throwaway server.

    The root guard refuses every name lookup, which a connection to an IP
    literal still makes. This one allows a lookup or connection when the
    address is loopback, and refuses everything else, as the root guard does.
    """
    attempts: list = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo

    def loopback(host) -> bool:
        try:
            return ipaddress.ip_address(host).is_loopback
        except ValueError:
            return False

    def connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if loopback(host):
            return real_connect(self, address, *args, **kwargs)
        attempts.append(address)
        raise RuntimeError(f"live test tried to reach {address}")

    def getaddrinfo(host, *args, **kwargs):
        if loopback(host):
            return real_getaddrinfo(host, *args, **kwargs)
        attempts.append(host)
        raise RuntimeError(f"live test tried to look up {host}")

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    yield attempts
    assert not attempts, f"live test tried to leave the machine: {attempts}"


@pytest.fixture
async def live(live_server, tmp_path):
    """Tool calls against the throwaway server, as a client makes them."""
    async with live_tools(tmp_path) as call:
        yield call


@pytest.fixture
async def fake(tmp_path):
    """The same calls against the in-memory fake, for contract comparisons."""
    async with fake_tools(tmp_path) as call:
        yield call
