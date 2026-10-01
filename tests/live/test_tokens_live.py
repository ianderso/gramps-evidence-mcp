"""PITFALLS section 23: token expiry and the token endpoints' rate limit.

An access token lasts 15 minutes; an expired one is answered 401, which is
what makes a client renew it. Each token endpoint allows one request a second
per address. Together they mean parallel requests after a quiet spell must
renew the token once between them, which ``tests/test_token_renewal.py``
checks against a model and the last test here checks against the server.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import time

import httpx
import pytest

from gramps_evidence_mcp.client import GrampsWebClient

from .harness import LIVE_PASSWORD, LIVE_SECRET_KEY, LIVE_URL, LIVE_USERNAME, session_tokens


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _expired(token: str) -> str:
    """The same token, run out ten seconds ago and signed again with the server's key."""
    if not LIVE_SECRET_KEY:
        pytest.skip("needs GRAMPS_LIVE_SECRET_KEY, which start_server.sh prints")
    _, payload, _ = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["exp"] = int(time.time()) - 10
    signing_input = (
        _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
        + "."
        + _b64(json.dumps(claims).encode())
    )
    signature = hmac.new(LIVE_SECRET_KEY.encode(), signing_input.encode(), hashlib.sha256)
    return signing_input + "." + _b64(signature.digest())


def _people(token: str) -> httpx.Response:
    return httpx.get(
        f"{LIVE_URL}/api/people/", headers={"Authorization": f"Bearer {token}"}, timeout=30
    )


def test_23_an_expired_token_is_401_and_a_malformed_one_422(live_server):
    access, _ = session_tokens()
    assert _people(access).status_code == 200
    expired = _people(_expired(access))
    assert expired.status_code == 401
    assert "expired" in expired.json()["message"].lower()
    assert _people("not-a-token").status_code == 422


@pytest.mark.parametrize("path", ["/api/token/", "/api/token/refresh/"])
def test_23_a_token_endpoint_takes_one_request_a_second(live_server, path):
    _, refresh = session_tokens()
    kwargs = (
        {"json": {"username": LIVE_USERNAME, "password": LIVE_PASSWORD}}
        if path == "/api/token/"
        else {"headers": {"Authorization": f"Bearer {refresh}"}}
    )
    time.sleep(1.1)
    with httpx.Client(base_url=LIVE_URL, timeout=30) as http:
        codes = [http.post(path, **kwargs).status_code for _ in range(3)]
    time.sleep(1.1)  # leave the window clear for whatever runs next
    assert codes[0] == 200
    assert 429 in codes, codes


async def test_23_parallel_requests_on_an_expired_token_all_succeed(live_server):
    """What a burst of parallel tool calls after 15 idle minutes does."""
    access, refresh = session_tokens()
    time.sleep(1.1)
    stale = _expired(access)
    client = GrampsWebClient(LIVE_URL, LIVE_USERNAME, LIVE_PASSWORD)
    client._access, client._refresh = stale, refresh
    try:
        results = await asyncio.gather(
            *(client.list_objects(t, keys="handle") for t in ("person", "event", "note", "tag"))
        )
    finally:
        await client.aclose()
    time.sleep(1.1)
    assert results == [[], [], [], []]
    assert client._access != stale
