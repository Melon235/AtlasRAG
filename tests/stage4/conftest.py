"""Hermetic offline guards for every Stage 4 graph, runner, and service test."""

from __future__ import annotations

import os
import socket
from collections.abc import Iterator

import pytest

_FORBIDDEN_ENVIRONMENT = (
    "LANGSMITH_API_KEY",
    "LANGCHAIN_API_KEY",
    "LANGSMITH_ENDPOINT",
    "LANGCHAIN_ENDPOINT",
)
_TRACING_FLAGS = (
    "LANGSMITH_TRACING",
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING",
    "LANGCHAIN_TRACING_V2",
)


def _deny_network(*args: object, **kwargs: object) -> None:
    raise AssertionError("Stage 4 test attempted outbound network access")


@pytest.fixture(autouse=True)
def stage4_offline_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Override inherited tracing opt-ins and reject every outbound socket path."""

    for name in _FORBIDDEN_ENVIRONMENT:
        monkeypatch.delenv(name, raising=False)
    for name in _TRACING_FLAGS:
        monkeypatch.setenv(name, "false")
        assert os.environ[name] == "false"
    monkeypatch.setattr(socket.socket, "connect", _deny_network)
    monkeypatch.setattr(socket.socket, "connect_ex", _deny_network)
    monkeypatch.setattr(socket, "create_connection", _deny_network)
    monkeypatch.setattr(socket, "getaddrinfo", _deny_network)
    yield
