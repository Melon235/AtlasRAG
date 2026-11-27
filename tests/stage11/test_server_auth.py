"""Unit tests for Stage 11 local transport security, Bearer auth, and file safety."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from atlasrag.server.app import create_app
from atlasrag.server.auth import (
    build_allowed_hosts,
    build_allowed_origins,
    read_secure_local_file,
    validate_bearer_token,
    validate_client_csrf_header,
    validate_host_header,
    validate_loopback_url,
    validate_origin_and_referer,
    write_secure_local_file,
)
from atlasrag.server.config import ServerConfig
from atlasrag.server.errors import ServerHttpError, TransportErrorCode
from stage11.conftest import sample_runtime_config, start_test_coordinator


def test_host_origin_csrf_and_loopback_url_checks() -> None:
    cfg = ServerConfig(port=8123)
    hosts = build_allowed_hosts(cfg)
    origins = build_allowed_origins(cfg)

    validate_host_header("127.0.0.1:8123", hosts)
    validate_host_header("localhost:8123", hosts)
    validate_host_header("[::1]:8123", hosts)

    for bad_host in [None, "", "evil.example.com:8123", "127.0.0.1:9999"]:
        with pytest.raises(ServerHttpError) as exc_info:
            validate_host_header(bad_host, hosts)
        assert exc_info.value.code is TransportErrorCode.FORBIDDEN

    validate_origin_and_referer(
        method="POST",
        origin="http://127.0.0.1:8123",
        referer=None,
        allowed_origins=origins,
    )
    validate_origin_and_referer(
        method="POST",
        origin=None,
        referer="http://localhost:8123/index.html",
        allowed_origins=origins,
    )
    for bad_origin in ["null", "http://evil.example:8123", "http://127.0.0.1:9999"]:
        with pytest.raises(ServerHttpError) as exc_info:
            validate_origin_and_referer(
                method="POST",
                origin=bad_origin,
                referer=None,
                allowed_origins=origins,
            )
        assert exc_info.value.code is TransportErrorCode.FORBIDDEN

    assert validate_client_csrf_header("web") == "web"
    assert validate_client_csrf_header("CLI") == "cli"
    for bad_client in [None, "", "curl"]:
        with pytest.raises(ServerHttpError) as exc_info:
            validate_client_csrf_header(bad_client)
        assert exc_info.value.code is TransportErrorCode.FORBIDDEN

    assert validate_loopback_url("http://127.0.0.1:8000/") == "http://127.0.0.1:8000"
    for bad_url in [
        "http://example.com:8000",
        "http://user:pass@127.0.0.1:8000",
        "ftp://127.0.0.1:8000",
    ]:
        with pytest.raises(ValueError):
            validate_loopback_url(bad_url)


def test_bearer_token_validation_and_secure_file_helpers(tmp_path: Path) -> None:
    validate_bearer_token("Bearer secret-tok", "secret-tok")
    for bad_auth in [None, "", "Basic abc", "Bearer wrong", "Bearer "]:
        with pytest.raises(ServerHttpError) as exc_info:
            validate_bearer_token(bad_auth, "secret-tok")
        assert exc_info.value.code is TransportErrorCode.UNAUTHORIZED

    state_dir = tmp_path / "state"
    token_file = state_dir / "control.token"
    write_secure_local_file(token_file, "token-value")
    assert read_secure_local_file(token_file) == "token-value"
    assert (os.stat(token_file).st_mode & 0o777) == 0o600
    assert (os.stat(state_dir).st_mode & 0o777) == 0o700

    os.chmod(token_file, 0o644)
    with pytest.raises(PermissionError):
        read_secure_local_file(token_file)
    os.chmod(token_file, 0o600)

    symlink_path = state_dir / "link.token"
    symlink_path.symlink_to(token_file)
    with pytest.raises(OSError):
        read_secure_local_file(symlink_path)


@pytest.mark.asyncio
async def test_http_middleware_enforces_host_origin_cors_and_body_limit() -> None:
    coordinator, _ = await start_test_coordinator()
    try:
        cfg = ServerConfig(port=8000, max_request_bytes=128)
        app = create_app(
            runtime_config=sample_runtime_config(),
            server_config=cfg,
            coordinator=coordinator,
        )

        @app.post("/_test/body")
        async def _consume_body(request: Request) -> dict[str, int]:
            return {"size": len(await request.body())}

        with TestClient(app, base_url="http://127.0.0.1:8000") as client:
            ok = client.get("/api/v1/health/live")
            assert ok.status_code == 200
            assert "access-control-allow-origin" not in {
                k.casefold() for k in ok.headers
            }

            bad_host = client.get(
                "/api/v1/health/live",
                headers={"Host": "evil.attacker:8000"},
            )
            assert bad_host.status_code == 403
            assert bad_host.json()["error_code"] == TransportErrorCode.FORBIDDEN.value

            bad_origin = client.post(
                "/_test/body",
                headers={"Origin": "null"},
                json={"x": 1},
            )
            assert bad_origin.status_code == 403

            too_large = client.post(
                "/_test/body",
                headers={"Origin": "http://127.0.0.1:8000"},
                content=json.dumps({"payload": "x" * 512}),
            )
            assert too_large.status_code == 413
            assert (
                too_large.json()["error_code"]
                == TransportErrorCode.PAYLOAD_TOO_LARGE.value
            )

        async def _chunked_body() -> AsyncIterator[bytes]:
            yield b'{"a":"'
            yield b"x" * 200
            yield b'"}'

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://127.0.0.1:8000"
        ) as async_client:
            chunked_res = await async_client.post(
                "/_test/body",
                headers={"Origin": "http://127.0.0.1:8000"},
                content=_chunked_body(),
            )
            assert chunked_res.status_code == 413
            assert (
                chunked_res.json()["error_code"]
                == TransportErrorCode.PAYLOAD_TOO_LARGE.value
            )
    finally:
        await coordinator.shutdown()
