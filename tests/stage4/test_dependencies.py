"""Approved Stage 4 dependencies without model or tracing requirements by default."""

from __future__ import annotations

import os
import socket
import tomllib
from pathlib import Path
from typing import Any, TypedDict

import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_embedding_dependencies_are_explicit_nondefault_group() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert "langgraph==1.2.15" in document["project"]["dependencies"]
    assert document["dependency-groups"]["indexing-embedding"] == [
        "torch==2.13.0; sys_platform == 'linux' or sys_platform == 'win32'",
        "torch==2.14.1; sys_platform == 'darwin'",
        "transformers==5.19.0",
        "huggingface-hub==1.33.0",
    ]
    assert document["tool"]["uv"].get("default-groups", ["dev"]) == ["dev"]


def test_cuda_wheels_have_explicit_official_source_and_locked_hashes() -> None:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert document["tool"]["uv"]["index"] == [
        {
            "name": "pytorch-cu126",
            "url": "https://download.pytorch.org/whl/cu126",
            "explicit": True,
        }
    ]
    assert document["tool"]["uv"]["sources"]["torch"] == [
        {
            "index": "pytorch-cu126",
            "marker": "sys_platform == 'linux' or sys_platform == 'win32'",
        }
    ]
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    cuda = [
        p
        for p in lock["package"]
        if p["name"] == "torch" and p["version"] == "2.13.0+cu126"
    ]
    assert len(cuda) == 1
    assert cuda[0]["source"] == {"registry": "https://download.pytorch.org/whl/cu126"}
    wheels = cuda[0]["wheels"]
    assert any("cp311-cp311-manylinux_2_28_x86_64" in w["url"] for w in wheels)
    assert all(w["hash"].startswith("sha256:") for w in wheels)


def test_frozen_parser_versions_and_macos_torch_are_retained() -> None:
    lock = tomllib.loads((ROOT / "uv.lock").read_text())
    versions = {(p["name"], p["version"]) for p in lock["package"]}
    assert {
        ("docling-slim", "2.135.0"),
        ("mineru", "4.0.11"),
        ("torch", "2.14.1"),
        ("torchvision", "0.29.1"),
        ("transformers", "5.19.0"),
    } <= versions


def test_langgraph_executes_without_keys_tracing_or_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for key in (
        "LANGCHAIN_API_KEY",
        "LANGSMITH_API_KEY",
        "OPENAI_API_KEY",
        "LANGCHAIN_ENDPOINT",
        "LANGSMITH_ENDPOINT",
    ):
        monkeypatch.delenv(key, raising=False)
        assert key not in os.environ
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")
    monkeypatch.setenv("LANGSMITH_TRACING_V2", "false")
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ.get("LANGSMITH_TRACING_V2") == "false"
    attempts: list[str] = []
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def reject_network(*args: object, **kwargs: object) -> None:
        attempts.append("outbound network")
        pytest.fail("local graph execution attempted network access")

    def guarded_connect(sock: socket.socket, address: Any) -> None:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            reject_network()
        original_connect(sock, address)

    def guarded_connect_ex(sock: socket.socket, address: Any) -> int:
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            reject_network()
        return original_connect_ex(sock, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(socket, "getaddrinfo", reject_network)
    from langgraph.graph import END, START, StateGraph

    class State(TypedDict):
        value: int

    graph = StateGraph[State, None, State, State](State)
    graph.add_node("increment", lambda state: {"value": state["value"] + 1})
    graph.add_edge(START, "increment")
    graph.add_edge("increment", END)
    assert graph.compile().invoke(State(value=1)) == {"value": 2}
    assert attempts == []
