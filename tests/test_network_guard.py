"""The guard in conftest.py that keeps every test off the network."""

from __future__ import annotations

import socket

import pytest


def test_a_loopback_socket_pair_still_connects():
    """What Windows' asyncio does to build its event loop, and must be allowed.

    Refusing it failed every async test on the Windows CI runner.
    """
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        client.connect(listener.getsockname())
        server, _ = listener.accept()
        server.close()
    finally:
        client.close()
        listener.close()


def test_a_name_lookup_is_refused(_no_network):
    """How a request to any server, the fake's testserver included, starts."""
    with pytest.raises(RuntimeError):
        socket.getaddrinfo("testserver", 80)
    assert _no_network == ["testserver"]
    _no_network.clear()


def test_a_connection_beyond_loopback_is_refused(_no_network):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(RuntimeError):
            sock.connect(("192.0.2.1", 80))  # TEST-NET-1, never routed
        with pytest.raises(RuntimeError):
            sock.connect_ex(("192.0.2.1", 80))
    finally:
        sock.close()
    assert _no_network == [("192.0.2.1", 80), ("192.0.2.1", 80)]
    _no_network.clear()
