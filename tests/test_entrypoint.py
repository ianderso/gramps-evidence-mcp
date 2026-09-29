"""The stdio entry point.

Nothing else in the suite calls ``run()``, which is how a broken one
shipped: ``MCPServer.run`` is synchronous and driving its own event loop,
so wrapping it in ``asyncio.run()`` passed None where a coroutine was
expected and raised ValueError as the server stopped.
"""

from __future__ import annotations

import inspect

from gramps_evidence_mcp import server


def test_the_server_run_method_is_synchronous():
    """The assumption the entry point rests on.

    If a future MCP release makes this a coroutine, ``run()`` has to change
    with it, and this test is what says so.
    """
    assert not inspect.iscoroutinefunction(server.mcp.run)


def test_the_entry_point_calls_run_without_wrapping_it(monkeypatch):
    """Calling it must not raise, and must actually start the server."""
    called: list[tuple] = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: called.append((a, k)))
    monkeypatch.setattr(server.logging, "basicConfig", lambda **k: None)

    server.run()

    assert called, "run() did not start the server"


def test_the_entry_point_does_not_wrap_run_in_asyncio():
    """A guard against the specific mistake, not just its symptom.

    Parsed rather than grepped: the source explains the mistake in a
    comment, and a substring search would match that.
    """
    import ast
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(server.run)))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "asyncio"
    ]
    assert calls == []


def _capture_run(monkeypatch) -> list[tuple]:
    called: list[tuple] = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: called.append((a, k)))
    monkeypatch.setattr(server.logging, "basicConfig", lambda **k: None)
    return called


def test_stdio_is_the_default_transport(monkeypatch):
    monkeypatch.delenv("GRAMPS_MCP_TRANSPORT", raising=False)
    called = _capture_run(monkeypatch)
    server.run()
    assert called == [(("stdio",), {})]


def test_http_transport_serves_streamable_http_on_host_and_port(monkeypatch):
    """The toggle the README used to describe as an open request."""
    monkeypatch.setenv("GRAMPS_MCP_TRANSPORT", "http")
    monkeypatch.setenv("GRAMPS_MCP_HOST", "0.0.0.0")
    monkeypatch.setenv("GRAMPS_MCP_PORT", "8091")
    called = _capture_run(monkeypatch)
    server.run()
    assert called == [(("streamable-http",), {"host": "0.0.0.0", "port": 8091})]


def test_http_transport_defaults_to_loopback(monkeypatch):
    """Listening beyond the machine has to be asked for."""
    monkeypatch.setenv("GRAMPS_MCP_TRANSPORT", "http")
    monkeypatch.delenv("GRAMPS_MCP_HOST", raising=False)
    monkeypatch.delenv("GRAMPS_MCP_PORT", raising=False)
    called = _capture_run(monkeypatch)
    server.run()
    assert called == [(("streamable-http",), {"host": "127.0.0.1", "port": 8090})]


def test_an_unknown_transport_stops_the_server_before_it_starts(monkeypatch):
    import pytest

    from gramps_evidence_mcp.config import ConfigError

    monkeypatch.setenv("GRAMPS_MCP_TRANSPORT", "websocket")
    called = _capture_run(monkeypatch)
    with pytest.raises(ConfigError, match="not a transport"):
        server.run()
    assert called == []


def test_request_urls_stay_out_of_the_log(monkeypatch):
    """httpx logs each URL at INFO; a GrampsQL filter rides in the URL."""
    import logging

    _capture_run(monkeypatch)
    monkeypatch.delenv("GRAMPS_MCP_TRANSPORT", raising=False)
    logging.getLogger("httpx").setLevel(logging.NOTSET)
    server.run()
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
