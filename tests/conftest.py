"""Test-wide guards.

The suite must never reach the network. A test that quietly hits SEC would be
slow and flaky, and would spend the very request budget this library exists to
protect — so the socket is blocked rather than trusted.
"""
from __future__ import annotations

import socket

import pytest


class NetworkAccessAttempted(RuntimeError):
    """A test tried to open a socket."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise NetworkAccessAttempted(
            "a test attempted a network connection. Substitute "
            "EdgarClient._http_get instead of calling out."
        )

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
