"""Token renewal under the server's rate limit.

gramps-webapi expires an access token after 15 minutes and allows each token
endpoint one request a second per address (``docs/PITFALLS.md`` section 23).
An MCP client runs tool calls in parallel, so after a quiet spell several
requests find the token expired at once. If each renewed it, every renewal
after the first would be refused with 429 and its tool call would fail.

The model server below has just those two behaviours, so the tests show what
the client sends rather than what a fuller fake happens to tolerate.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
import respx

from gramps_evidence_mcp.client import GrampsApiError, GrampsWebClient

TOKEN_PATHS = ("/api/token/", "/api/token/refresh/")


class TokenServer:
    """Expiring access tokens and token endpoints limited to one request a second."""

    def __init__(self) -> None:
        self.issued = 0
        self.valid: str | None = None
        self.last: dict[str, float] = {}
        self.token_calls: list[tuple[str, int]] = []
        self.refresh_revoked = False
        self.password_wrong = False

    def expire(self) -> None:
        """Let the current access token run out, as 15 idle minutes would."""
        self.valid = None
        self.last.clear()

    async def handle(self, request: httpx.Request) -> httpx.Response:
        # Latency, so parallel requests interleave as they do over a network;
        # without it each would run to completion before the next began.
        await asyncio.sleep(0.01)
        path = request.url.path
        refused = (path == "/api/token/refresh/" and self.refresh_revoked) or (
            path == "/api/token/" and self.password_wrong
        )
        if refused:
            self.token_calls.append((path, 401))
            return httpx.Response(401, json={"message": "Refused"})
        if path in TOKEN_PATHS:
            now = time.monotonic()
            if now - self.last.get(path, -10.0) < 1.0:
                self.token_calls.append((path, 429))
                return httpx.Response(429, json={"message": "Too many requests"})
            self.last[path] = now
            self.token_calls.append((path, 200))
            self.issued += 1
            self.valid = f"access-{self.issued}"
            body = {"access_token": self.valid}
            if path == "/api/token/":
                body["refresh_token"] = "refresh"
            return httpx.Response(200, json=body)
        if request.headers.get("Authorization") != f"Bearer {self.valid}":
            return httpx.Response(401, json={"message": "Token has expired"})
        return httpx.Response(200, json=[], headers={"X-Total-Count": "0"})


@pytest.fixture
async def model():
    server = TokenServer()
    with respx.mock(base_url="http://testserver") as router:
        router.route().mock(side_effect=server.handle)
        client = GrampsWebClient("http://testserver", "mcp", "pw")
        try:
            yield server, client
        finally:
            await client.aclose()


async def test_parallel_requests_on_an_expired_token_renew_it_once(model):
    server, client = model
    await client.login()
    server.expire()
    await asyncio.gather(*(client.list_objects("person") for _ in range(4)))
    assert server.token_calls == [("/api/token/", 200), ("/api/token/refresh/", 200)]


async def test_parallel_first_requests_log_in_once(model):
    server, client = model
    await asyncio.gather(*(client.list_objects("person") for _ in range(4)))
    assert server.token_calls == [("/api/token/", 200)]


async def test_a_rate_limited_login_is_waited_out_once(model):
    """Another client at the same address logged in this second."""
    server, client = model
    async with GrampsWebClient("http://testserver", "other", "pw"):
        pass
    await client.login()
    assert server.token_calls == [
        ("/api/token/", 200),
        ("/api/token/", 429),
        ("/api/token/", 200),
    ]


async def test_a_refused_refresh_falls_back_to_logging_in(model):
    server, client = model
    await client.login()
    server.expire()
    server.refresh_revoked = True
    assert await client.list_objects("person") == []
    assert server.token_calls == [
        ("/api/token/", 200),
        ("/api/token/refresh/", 401),
        ("/api/token/", 200),
    ]


async def test_bad_credentials_still_fail_plainly(model):
    server, client = model
    server.password_wrong = True
    with pytest.raises(GrampsApiError) as exc:
        await client.list_objects("person")
    assert exc.value.status == 401
    assert server.token_calls == [("/api/token/", 401)]
