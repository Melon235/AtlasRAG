"""Thin argparse + httpx CLI entrypoint for AtlasRAG."""

from __future__ import annotations

import argparse
import hmac
import json
import os
import select
import signal
import sys
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import FrameType

import httpx
from pydantic import ValidationError

import atlasrag.runtime.ownership as _ownership_module
from atlasrag.domain.errors import ConfigurationError
from atlasrag.domain.requests import QueryFilters
from atlasrag.domain.results import FinalLocalCitation
from atlasrag.runtime.config import load_runtime_config
from atlasrag.server.auth import (
    ProcessIdentityVerificationError,
    TargetServerIdentity,
    canonical_state_directory,
    cli_session_path,
    control_token_path,
    load_clean_shutdown_receipt,
    load_server_discovery,
    read_proc_stat_starttime,
    read_secure_local_file,
    shutdown_receipt_path,
    validate_loopback_url,
    verify_process_holds_flock,
    verify_target_process,
    write_secure_local_file,
)
from atlasrag.server.config import ServerConfig, load_server_config
from atlasrag.server.schemas import (
    CliSessionRecord,
    LivenessResponse,
    LivenessStatus,
    OperationAcceptedResponse,
    OperationStatus,
    OperationStatusResponse,
    QueryRequest,
    QueryResponse,
    ReadinessResponse,
    RuntimeStatusResponse,
    ServerDiscoveryRecord,
    SessionResponse,
    ShutdownAcceptedResponse,
    SseAcceptedEvent,
    SseErrorEvent,
    SseProgressEvent,
)
from atlasrag.services.sync.models import ServingSafety

EXIT_OK = 0
EXIT_OPERATIONAL_ERROR = 1
EXIT_USAGE_OR_CONFIG_ERROR = 2
EXIT_UNAVAILABLE = 3
EXIT_FATAL_LIFECYCLE = 4
EXIT_SIGINT = 130
EXIT_SIGTERM = 143

_DEFAULT_SERVER_URL = "http://127.0.0.1:8000"
_CONNECT_TIMEOUT_SECONDS = 3.0
_CONTROL_HTTP_TIMEOUT_SECONDS = 10.0
_DEFAULT_STOP_TIMEOUT_SECONDS = 65.0


class _ClientSignalInterrupt(BaseException):
    """Raised on SIGINT/SIGTERM during thin CLI client commands."""

    def __init__(self, exit_code: int) -> None:
        self.exit_code = exit_code
        super().__init__(exit_code)


class _CliUsageOrConfigError(ValueError):
    """Raised when CLI options or configuration fail validation (exit code 2)."""


class _UnsafeDiscoveryError(RuntimeError):
    """Raised when local server state files fail security validation."""


def _add_shared_client_flags(
    parser: argparse.ArgumentParser,
    *,
    include_server_url: bool = True,
) -> None:
    if include_server_url:
        parser.add_argument(
            "--server-url",
            default=None,
            help="Explicit loopback server URL (overrides ATLASRAG_SERVER_URL and server.json)",
        )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=None,
        help="Override local state directory (default: ATLASRAG_STATE_DIR or ~/.local/state/atlasrag)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Emit machine-readable JSON output",
    )


def build_parser() -> argparse.ArgumentParser:
    """Create the top-level CLI parser for AtlasRAG."""

    parser = argparse.ArgumentParser(
        prog="atlasrag",
        description="AtlasRAG local application server and operational CLI.",
    )
    subparsers = parser.add_subparsers(dest="command", required=False)

    serve_parser = subparsers.add_parser(
        "serve", help="Run the managed single-worker local HTTP/SSE application server."
    )
    serve_parser.add_argument(
        "--host",
        default=None,
        help="Loopback bind host (default: 127.0.0.1)",
    )
    serve_parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="Loopback bind port (default: 8000)",
    )
    serve_parser.add_argument(
        "--config",
        type=Path,
        default=None,
        help="Path to RuntimeConfig JSON file",
    )
    serve_parser.add_argument(
        "--server-config",
        type=Path,
        default=None,
        help="Path to ServerConfig JSON file",
    )
    _add_shared_client_flags(serve_parser, include_server_url=False)

    ask_parser = subparsers.add_parser(
        "ask", help="Execute a grounded RAG query against the running server."
    )
    ask_parser.add_argument("query", help="Question or query text to submit")
    ask_parser.add_argument(
        "--session-id",
        default=None,
        help="Use an explicit existing server session ID",
    )
    ask_parser.add_argument(
        "--new-session",
        action="store_true",
        help="Create a new CLI session before submitting the query",
    )
    ask_parser.add_argument(
        "--file-name", default=None, help="Filter by source file name"
    )
    ask_parser.add_argument(
        "--source-type", default=None, help="Filter by canonical source type"
    )
    ask_parser.add_argument("--sheet-name", default=None, help="Filter by sheet name")
    ask_parser.add_argument(
        "--stream",
        action="store_true",
        help="Stream query lifecycle progress over SSE",
    )
    ask_parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
        help="Query read/stream timeout in seconds (default: 120.0)",
    )
    _add_shared_client_flags(ask_parser)

    health_parser = subparsers.add_parser(
        "health", help="Check /api/v1/health/ready or /api/v1/health/live."
    )
    health_group = health_parser.add_mutually_exclusive_group()
    health_group.add_argument(
        "--live",
        action="store_true",
        help="Check process liveness (/api/v1/health/live)",
    )
    health_group.add_argument(
        "--ready",
        action="store_true",
        help="Check serving readiness (/api/v1/health/ready, default)",
    )
    _add_shared_client_flags(health_parser)

    status_parser = subparsers.add_parser(
        "status", help="Inspect sanitized server status (/api/v1/status)."
    )
    status_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Request bounded manual dependency probe refresh",
    )
    _add_shared_client_flags(status_parser)

    for cmd_name, help_text in (
        ("sync", "Trigger guarded index synchronization (/api/v1/control/sync)."),
        (
            "recover",
            "Trigger blocked-state runtime recovery (/api/v1/control/recover).",
        ),
    ):
        op_parser = subparsers.add_parser(cmd_name, help=help_text)
        wait_group = op_parser.add_mutually_exclusive_group()
        wait_group.add_argument(
            "--wait",
            action="store_true",
            dest="wait",
            default=True,
            help="Poll until terminal operation completion (default)",
        )
        wait_group.add_argument(
            "--no-wait",
            action="store_false",
            dest="wait",
            help="Return immediately after HTTP 202 acceptance",
        )
        op_parser.add_argument(
            "--poll-interval",
            type=float,
            default=1.0,
            help="Polling interval in seconds while waiting (default: 1.0)",
        )
        _add_shared_client_flags(op_parser)

    stop_parser = subparsers.add_parser(
        "stop",
        help="Verify process identity via pidfd, request graceful shutdown, and verify clean receipt.",
    )
    stop_parser.add_argument(
        "--timeout",
        type=float,
        default=None,
        help="Graceful shutdown wait timeout in seconds",
    )
    stop_parser.add_argument(
        "--force",
        action="store_true",
        help="Escalate verified pidfd via SIGTERM/SIGKILL if graceful wait expires",
    )
    _add_shared_client_flags(stop_parser)

    return parser


def _emit_json(payload: object) -> None:
    print(json.dumps(payload, indent=2, default=str))


def _emit_error(message: str, *, json_output: bool, error_code: str = "ERROR") -> None:
    if json_output:
        _emit_json({"error_code": error_code, "message": message})
    else:
        print(f"error: {message}", file=sys.stderr)


def _resolve_state_dir(
    cli_state_dir: Path | None, *, environ: Mapping[str, str]
) -> Path:
    if cli_state_dir is not None:
        raw = str(cli_state_dir)
        if not raw.strip():
            raise _CliUsageOrConfigError("state directory must not be blank")
        return Path(raw)
    env_dir = environ.get("ATLASRAG_STATE_DIR")
    if env_dir is not None:
        if not env_dir.strip():
            raise _CliUsageOrConfigError("ATLASRAG_STATE_DIR must not be blank")
        return Path(env_dir)
    return canonical_state_directory(None)


def _validate_url_option(raw_url: str, *, label: str) -> str:
    try:
        return validate_loopback_url(raw_url)
    except ValueError as error:
        raise _CliUsageOrConfigError(
            f"{label} must be a valid http(s) loopback URL without unsafe components"
        ) from error


def _resolve_server_target(
    *,
    cli_server_url: str | None,
    state_dir: Path,
    environ: Mapping[str, str],
) -> tuple[str, ServerDiscoveryRecord | None]:
    discovery: ServerDiscoveryRecord | None = None
    discovery_error: _UnsafeDiscoveryError | None = None
    try:
        discovery = load_server_discovery(state_dir)
    except FileNotFoundError:
        discovery = None
    except (OSError, PermissionError, ValueError, ValidationError) as error:
        discovery_error = _UnsafeDiscoveryError(
            "server discovery metadata is unsafe or invalid"
        )
        discovery_error.__cause__ = error

    if cli_server_url is not None:
        return _validate_url_option(cli_server_url, label="server URL"), discovery

    env_url = environ.get("ATLASRAG_SERVER_URL")
    if env_url is not None and env_url != "":
        return _validate_url_option(env_url, label="ATLASRAG_SERVER_URL"), discovery

    if discovery_error is not None:
        raise discovery_error
    if discovery is not None:
        return discovery.url, discovery
    return _DEFAULT_SERVER_URL, None


def _resolve_bound_control_override(
    *,
    expected_url: str,
    cli_server_url: str | None,
    environ: Mapping[str, str],
) -> str | None:
    """Validate optional CLI/env URL overrides match the verified server URL."""

    canonical_expected = validate_loopback_url(expected_url)
    if cli_server_url is not None:
        explicit = _validate_url_option(cli_server_url, label="server URL")
        if explicit != canonical_expected:
            return "Explicit --server-url does not match the verified running server."
    env_url = environ.get("ATLASRAG_SERVER_URL")
    if env_url is not None and env_url != "":
        explicit_env = _validate_url_option(env_url, label="ATLASRAG_SERVER_URL")
        if explicit_env != canonical_expected:
            return "ATLASRAG_SERVER_URL does not match the verified running server."
    return None


def _build_client(target_url: str, *, timeout: httpx.Timeout) -> httpx.Client:
    validated = validate_loopback_url(target_url)
    return httpx.Client(
        base_url=validated,
        timeout=timeout,
        follow_redirects=False,
        trust_env=False,
        headers={
            "origin": validated,
            "x-atlasrag-client": "cli",
        },
    )


def _read_control_token(target_url: str, state_dir: Path) -> str:
    validate_loopback_url(target_url)
    raw = read_secure_local_file(control_token_path(state_dir), max_bytes=8_192)
    token = raw.strip()
    if not token:
        raise ValueError("control token is empty")
    return token


def _control_headers(target_url: str, token: str) -> dict[str, str]:
    validated = validate_loopback_url(target_url)
    return {
        "authorization": f"Bearer {token}",
        "origin": validated,
    }


def _parse_api_error_message(response: httpx.Response) -> tuple[str, str]:
    try:
        payload = response.json()
    except ValueError:
        return f"HTTP_{response.status_code}", f"HTTP {response.status_code} response"
    if isinstance(payload, dict):
        code = str(payload.get("error_code") or f"HTTP_{response.status_code}")
        msg = str(payload.get("message") or f"HTTP {response.status_code} response")
        return code, msg
    return f"HTTP_{response.status_code}", f"HTTP {response.status_code} response"


def _load_or_create_cli_session(
    *,
    client: httpx.Client,
    state_dir: Path,
    target_url: str,
    discovery: ServerDiscoveryRecord | None,
    force_new: bool,
) -> CliSessionRecord:
    session_file = cli_session_path(state_dir)
    if not force_new:
        try:
            raw = read_secure_local_file(session_file, max_bytes=16_384)
            cached = CliSessionRecord.model_validate_json(raw)
        except FileNotFoundError:
            cached = None
        except (ValueError, ValidationError):
            cached = None
        except (OSError, PermissionError) as error:
            raise _UnsafeDiscoveryError(
                "cli session file failed local security checks"
            ) from error
        if cached is not None:
            instance_ok = (
                discovery is None
                or cached.server_instance_id is None
                or cached.server_instance_id == discovery.server_instance_id
            )
            if (
                cached.server_url == target_url
                and cached.expires_at > datetime.now(UTC)
                and instance_ok
            ):
                return cached

    resp = client.post("/api/v1/sessions", json={"client_kind": "cli"})
    if resp.status_code != 201:
        code, message = _parse_api_error_message(resp)
        raise RuntimeError(f"{code}: {message}")
    try:
        created = SessionResponse.model_validate_json(resp.text)
    except ValidationError as error:
        raise RuntimeError(
            "INVALID_SERVER_RESPONSE: malformed session response"
        ) from error
    record = CliSessionRecord(
        server_instance_id=(
            discovery.server_instance_id if discovery is not None else None
        ),
        server_url=target_url,
        session_id=created.session_id,
        expires_at=created.expires_at,
    )
    write_secure_local_file(session_file, record.model_dump_json(indent=2))
    return record


def _render_query_result(result: QueryResponse, *, json_output: bool) -> None:
    if json_output:
        _emit_json(result.model_dump(mode="json"))
        return
    print(result.response.answer_text)
    if result.response.citations:
        print("\nCitations:")
        for citation in result.response.citations:
            if isinstance(citation, FinalLocalCitation):
                anchor_parts: list[str] = []
                sa = citation.source_anchor
                if sa is not None:
                    if sa.page_number is not None:
                        anchor_parts.append(f"p.{sa.page_number}")
                    if sa.heading is not None:
                        anchor_parts.append(sa.heading)
                    if sa.sheet_name is not None:
                        anchor_parts.append(f"sheet={sa.sheet_name}")
                    if sa.cell_range is not None:
                        anchor_parts.append(sa.cell_range)
                anchor = f" ({', '.join(anchor_parts)})" if anchor_parts else ""
                print(
                    f"- [{citation.citation_id}] {citation.file_name} ({citation.relative_source_path}){anchor}"
                )
            else:
                print(f"- [{citation.citation_id}] {citation.title}: {citation.url}")
    print(
        f"[cache={result.cache_status.value}, trace={result.trace_id}, elapsed={result.elapsed_ms:.1f}ms]"
    )


def _iter_sse_events(response: httpx.Response) -> Iterator[tuple[str, str]]:
    event_name = "message"
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                yield event_name, "\n".join(data_lines)
                event_name = "message"
                data_lines = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if value.startswith(" "):
            value = value[1:]
        if field == "event":
            event_name = value
        elif field == "data":
            data_lines.append(value)
    if data_lines:
        yield event_name, "\n".join(data_lines)


def _wait_pidfd_exit(pidfd: int, timeout_seconds: float) -> bool:
    """Wait up to timeout_seconds for Linux pidfd readability (process exit)."""

    deadline = time.monotonic() + max(0.0, timeout_seconds)
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    try:
        while True:
            remaining_ms = int(max(0.0, (deadline - time.monotonic()) * 1000.0))
            events = poller.poll(remaining_ms)
            if events:
                return True
            if time.monotonic() >= deadline:
                return False
    finally:
        poller.unregister(pidfd)


@contextmanager
def _client_signal_handlers() -> Iterator[None]:
    """Map SIGINT -> 130 and SIGTERM -> 143 for thin CLI client subcommands."""

    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def _on_signal(signum: int, _frame: FrameType | None) -> None:
        if signum == signal.SIGINT:
            raise _ClientSignalInterrupt(EXIT_SIGINT)
        raise _ClientSignalInterrupt(EXIT_SIGTERM)

    previous_sigint = signal.signal(signal.SIGINT, _on_signal)
    previous_sigterm = signal.signal(signal.SIGTERM, _on_signal)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        signal.signal(signal.SIGTERM, previous_sigterm)


def _cmd_serve(args: argparse.Namespace, *, environ: Mapping[str, str]) -> int:
    try:
        runtime_config = load_runtime_config(environ=environ, config_file=args.config)
        base_server_config = load_server_config(
            environ=environ, config_file=args.server_config
        )
        payload = base_server_config.model_dump(mode="python")
        if args.host is not None:
            payload["host"] = args.host
        if args.port is not None:
            payload["port"] = args.port
        if args.state_dir is not None:
            raw_state = str(args.state_dir)
            if not raw_state.strip():
                raise ConfigurationError
            payload["state_directory"] = raw_state
        elif "ATLASRAG_STATE_DIR" in environ:
            env_state = environ["ATLASRAG_STATE_DIR"]
            if not env_state.strip():
                raise ConfigurationError
            payload["state_directory"] = env_state
        server_config = ServerConfig.model_validate(payload)
    except (ConfigurationError, ValidationError, ValueError):
        _emit_error(
            "Invalid runtime or server configuration.",
            json_output=bool(args.json),
            error_code="INVALID_CONFIGURATION",
        )
        return EXIT_USAGE_OR_CONFIG_ERROR

    from atlasrag.server.launcher import ServerLauncher

    launcher = ServerLauncher(
        runtime_config=runtime_config,
        server_config=server_config,
        log_level="warning" if args.json else None,
    )
    exit_code = launcher.run()
    if args.json:
        receipt = launcher.shutdown_receipt
        discovery = launcher.discovery_record
        _emit_json(
            {
                "exit_code": exit_code,
                "server_instance_id": launcher.server_instance_id,
                "url": discovery.url if discovery is not None else None,
                "shutdown_status": receipt.status if receipt is not None else None,
            }
        )
    return exit_code


def _cmd_ask(args: argparse.Namespace, *, environ: Mapping[str, str]) -> int:
    if args.session_id is not None and args.new_session:
        _emit_error(
            "--session-id and --new-session cannot be combined.",
            json_output=bool(args.json),
            error_code="INVALID_REQUEST",
        )
        return EXIT_USAGE_OR_CONFIG_ERROR
    if not (0 < args.timeout <= 86_400.0):
        _emit_error(
            "--timeout must be finite and greater than zero.",
            json_output=bool(args.json),
            error_code="INVALID_REQUEST",
        )
        return EXIT_USAGE_OR_CONFIG_ERROR
    if not str(args.query).strip():
        _emit_error(
            "Query must not be blank.",
            json_output=bool(args.json),
            error_code="INVALID_REQUEST",
        )
        return EXIT_USAGE_OR_CONFIG_ERROR

    filters: QueryFilters | None = None
    if (
        args.file_name is not None
        or args.source_type is not None
        or args.sheet_name is not None
    ):
        try:
            filters = QueryFilters(
                file_name=args.file_name,
                source_type=args.source_type,
                sheet_name=args.sheet_name,
            )
        except ValidationError:
            _emit_error(
                "Invalid query filter arguments.",
                json_output=bool(args.json),
                error_code="INVALID_REQUEST",
            )
            return EXIT_USAGE_OR_CONFIG_ERROR

    state_dir = _resolve_state_dir(args.state_dir, environ=environ)
    target_url, discovery = _resolve_server_target(
        cli_server_url=args.server_url, state_dir=state_dir, environ=environ
    )
    timeout = httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS,
        read=float(args.timeout),
        write=10.0,
        pool=10.0,
    )
    pinned_session = args.session_id is not None

    try:
        with _build_client(target_url, timeout=timeout) as client:
            if args.session_id is not None:
                session_id = str(args.session_id).strip()
                if not session_id:
                    _emit_error(
                        "--session-id must not be blank.",
                        json_output=bool(args.json),
                        error_code="INVALID_REQUEST",
                    )
                    return EXIT_USAGE_OR_CONFIG_ERROR
            else:
                record = _load_or_create_cli_session(
                    client=client,
                    state_dir=state_dir,
                    target_url=target_url,
                    discovery=discovery,
                    force_new=bool(args.new_session),
                )
                session_id = record.session_id

            for attempt in range(2):
                req = QueryRequest(
                    session_id=session_id,
                    query=args.query,
                    filters=filters,
                )
                payload = req.model_dump(mode="json")
                if not args.stream:
                    resp = client.post("/api/v1/query", json=payload)
                    if resp.status_code == 200:
                        parsed = QueryResponse.model_validate_json(resp.text)
                        _render_query_result(parsed, json_output=bool(args.json))
                        return EXIT_OK
                    code, message = _parse_api_error_message(resp)
                    if (
                        resp.status_code == 404
                        and code == "SESSION_NOT_FOUND"
                        and not pinned_session
                        and attempt == 0
                    ):
                        renewed = _load_or_create_cli_session(
                            client=client,
                            state_dir=state_dir,
                            target_url=target_url,
                            discovery=discovery,
                            force_new=True,
                        )
                        session_id = renewed.session_id
                        continue
                    _emit_error(message, json_output=bool(args.json), error_code=code)
                    return EXIT_OPERATIONAL_ERROR

                with client.stream(
                    "POST", "/api/v1/query/stream", json=payload
                ) as stream_resp:
                    if stream_resp.status_code != 200:
                        stream_resp.read()
                        code, message = _parse_api_error_message(stream_resp)
                        if (
                            stream_resp.status_code == 404
                            and code == "SESSION_NOT_FOUND"
                            and not pinned_session
                            and attempt == 0
                        ):
                            renewed = _load_or_create_cli_session(
                                client=client,
                                state_dir=state_dir,
                                target_url=target_url,
                                discovery=discovery,
                                force_new=True,
                            )
                            session_id = renewed.session_id
                            continue
                        _emit_error(
                            message, json_output=bool(args.json), error_code=code
                        )
                        return EXIT_OPERATIONAL_ERROR

                    for event_name, raw_data in _iter_sse_events(stream_resp):
                        if event_name == "accepted":
                            accepted = SseAcceptedEvent.model_validate_json(raw_data)
                            if not args.json:
                                print(
                                    f"[accepted trace={accepted.trace_id} session={accepted.session_id}]",
                                    file=sys.stderr,
                                )
                        elif event_name == "progress":
                            progress = SseProgressEvent.model_validate_json(raw_data)
                            if not args.json:
                                print(
                                    f"[progress stage={progress.stage.value} status={progress.status} attempt={progress.attempt} degraded={progress.degraded}]",
                                    file=sys.stderr,
                                )
                        elif event_name == "final":
                            parsed = QueryResponse.model_validate_json(raw_data)
                            _render_query_result(parsed, json_output=bool(args.json))
                            return EXIT_OK
                        elif event_name == "error":
                            err = SseErrorEvent.model_validate_json(raw_data)
                            _emit_error(
                                err.message,
                                json_output=bool(args.json),
                                error_code=err.error_code,
                            )
                            return EXIT_OPERATIONAL_ERROR

                    _emit_error(
                        "Query SSE stream ended without a terminal event.",
                        json_output=bool(args.json),
                        error_code="STREAM_INCOMPLETE",
                    )
                    return EXIT_OPERATIONAL_ERROR
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _emit_error(
            f"Unable to reach AtlasRAG server at {target_url}.",
            json_output=bool(args.json),
            error_code="SERVER_UNREACHABLE",
        )
        return EXIT_UNAVAILABLE
    except ValidationError:
        _emit_error(
            "Received malformed server query response.",
            json_output=bool(args.json),
            error_code="INVALID_SERVER_RESPONSE",
        )
        return EXIT_OPERATIONAL_ERROR
    except httpx.HTTPError as error:
        _emit_error(
            f"HTTP request failed: {type(error).__name__}.",
            json_output=bool(args.json),
            error_code="HTTP_ERROR",
        )
        return EXIT_OPERATIONAL_ERROR
    except RuntimeError as error:
        _emit_error(str(error), json_output=bool(args.json))
        return EXIT_OPERATIONAL_ERROR

    return EXIT_OPERATIONAL_ERROR


def _cmd_health(args: argparse.Namespace, *, environ: Mapping[str, str]) -> int:
    state_dir = _resolve_state_dir(args.state_dir, environ=environ)
    target_url, _ = _resolve_server_target(
        cli_server_url=args.server_url, state_dir=state_dir, environ=environ
    )
    endpoint = "/api/v1/health/live" if args.live else "/api/v1/health/ready"
    timeout = httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS, read=10.0, write=10.0, pool=10.0
    )
    try:
        with _build_client(target_url, timeout=timeout) as client:
            resp = client.get(endpoint)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _emit_error(
            f"Unable to reach AtlasRAG server at {target_url}.",
            json_output=bool(args.json),
            error_code="SERVER_UNREACHABLE",
        )
        return EXIT_UNAVAILABLE
    except httpx.HTTPError as error:
        _emit_error(
            f"Health request failed: {type(error).__name__}.",
            json_output=bool(args.json),
            error_code="HTTP_ERROR",
        )
        return EXIT_OPERATIONAL_ERROR

    if args.live:
        try:
            live = LivenessResponse.model_validate_json(resp.text)
            if args.json:
                _emit_json(live.model_dump(mode="json"))
            else:
                print(f"status={live.status.value} mode={live.mode.value}")
            return EXIT_OK if resp.status_code == 200 else EXIT_OPERATIONAL_ERROR
        except ValidationError:
            code, msg = _parse_api_error_message(resp)
            _emit_error(msg, json_output=bool(args.json), error_code=code)
            return EXIT_OPERATIONAL_ERROR

    try:
        ready = ReadinessResponse.model_validate_json(resp.text)
        if args.json:
            _emit_json(ready.model_dump(mode="json"))
        else:
            print(f"ready={ready.ready} mode={ready.mode.value}")
        return (
            EXIT_OK
            if (resp.status_code == 200 and ready.ready)
            else EXIT_OPERATIONAL_ERROR
        )
    except ValidationError:
        code, msg = _parse_api_error_message(resp)
        _emit_error(msg, json_output=bool(args.json), error_code=code)
        return EXIT_OPERATIONAL_ERROR


def _cmd_status(args: argparse.Namespace, *, environ: Mapping[str, str]) -> int:
    state_dir = _resolve_state_dir(args.state_dir, environ=environ)
    target_url, _ = _resolve_server_target(
        cli_server_url=args.server_url, state_dir=state_dir, environ=environ
    )
    timeout = httpx.Timeout(
        connect=_CONNECT_TIMEOUT_SECONDS, read=10.0, write=10.0, pool=10.0
    )
    try:
        with _build_client(target_url, timeout=timeout) as client:
            resp = client.get(
                "/api/v1/status",
                params={"refresh": "true" if args.refresh else "false"},
            )
    except (httpx.ConnectError, httpx.ConnectTimeout):
        _emit_error(
            f"Unable to reach AtlasRAG server at {target_url}.",
            json_output=bool(args.json),
            error_code="SERVER_UNREACHABLE",
        )
        return EXIT_UNAVAILABLE
    except httpx.HTTPError as error:
        _emit_error(
            f"Status request failed: {type(error).__name__}.",
            json_output=bool(args.json),
            error_code="HTTP_ERROR",
        )
        return EXIT_OPERATIONAL_ERROR

    if resp.status_code != 200:
        code, msg = _parse_api_error_message(resp)
        _emit_error(msg, json_output=bool(args.json), error_code=code)
        return EXIT_OPERATIONAL_ERROR

    try:
        status = RuntimeStatusResponse.model_validate_json(resp.text)
    except ValidationError:
        _emit_error(
            "Received malformed server status response.",
            json_output=bool(args.json),
            error_code="INVALID_SERVER_RESPONSE",
        )
        return EXIT_OPERATIONAL_ERROR
    if args.json:
        _emit_json(status.model_dump(mode="json"))
    else:
        print(
            f"mode={status.mode.value} ready={status.ready} verified={status.knowledge_verified}"
        )
        print(
            f"active_queries={status.active_queries} query_cache={status.query_cache}"
        )
        deps = status.dependencies
        print(
            "dependencies: "
            f"postgres={deps.postgres.value} milvus={deps.milvus.value} "
            f"redis={deps.redis.value} searxng={deps.searxng.value} "
            f"embedding={deps.embedding.value} reranker={deps.reranker.value}"
        )
        print(
            f"fingerprints: pipeline={status.pipeline_fingerprint} runtime={status.runtime_fingerprint}"
        )
    return EXIT_OK


def _render_operation_status(op: OperationStatusResponse, *, json_output: bool) -> None:
    if json_output:
        _emit_json(op.model_dump(mode="json"))
        return
    sync_outcome = op.sync_outcome.value if op.sync_outcome is not None else "-"
    safety = op.serving_safety.value if op.serving_safety is not None else "-"
    print(
        f"operation={op.operation_id} kind={op.kind.value} status={op.status.value} "
        f"sync_outcome={sync_outcome} serving_safety={safety} runtime_mode={op.runtime_mode.value}"
    )
    if op.blocked_code:
        print(f"blocked_code={op.blocked_code}")
    if op.unsafe_reasons:
        print(f"unsafe_reasons={', '.join(op.unsafe_reasons)}")
    if op.message:
        print(f"message={op.message}")


def _cmd_maintenance(
    args: argparse.Namespace, *, kind: str, environ: Mapping[str, str]
) -> int:
    if not (0 < args.poll_interval <= 3600.0):
        _emit_error(
            "--poll-interval must be finite and greater than zero.",
            json_output=bool(args.json),
            error_code="INVALID_REQUEST",
        )
        return EXIT_USAGE_OR_CONFIG_ERROR

    if args.server_url is not None:
        _validate_url_option(args.server_url, label="server URL")
    env_url = environ.get("ATLASRAG_SERVER_URL")
    if env_url is not None and env_url != "":
        _validate_url_option(env_url, label="ATLASRAG_SERVER_URL")

    state_dir = _resolve_state_dir(args.state_dir, environ=environ)
    try:
        verified = verify_target_process(state_dir)
    except FileNotFoundError:
        _emit_error(
            "No verified running server discovery metadata found; refusing control transmission.",
            json_output=bool(args.json),
            error_code="SERVER_UNREACHABLE",
        )
        return EXIT_UNAVAILABLE
    except ProcessIdentityVerificationError as error:
        _emit_error(
            f"Refusing {kind}: {error}.",
            json_output=bool(args.json),
            error_code="UNVERIFIED_PROCESS_IDENTITY",
        )
        return EXIT_FATAL_LIFECYCLE

    with verified:
        identity = verified.identity
        mismatch_msg = _resolve_bound_control_override(
            expected_url=identity.url,
            cli_server_url=args.server_url,
            environ=environ,
        )
        if mismatch_msg is not None:
            _emit_error(
                mismatch_msg,
                json_output=bool(args.json),
                error_code="CONTROL_TARGET_MISMATCH",
            )
            return EXIT_FATAL_LIFECYCLE

        target_url = identity.url
        try:
            token = _read_control_token(target_url, state_dir)
        except (FileNotFoundError, OSError, PermissionError, ValueError):
            _emit_error(
                "Valid local control authorization token could not be loaded.",
                json_output=bool(args.json),
                error_code="UNAUTHORIZED",
            )
            return EXIT_OPERATIONAL_ERROR

        if not _verify_pre_shutdown_stability(
            state_dir=state_dir,
            identity=identity,
            captured_token=token,
            pidfd=verified.pidfd,
        ):
            _emit_error(
                "Server discovery, control token, or process lock changed before maintenance transmission.",
                json_output=bool(args.json),
                error_code="UNVERIFIED_PROCESS_IDENTITY",
            )
            return EXIT_FATAL_LIFECYCLE

        headers = _control_headers(target_url, token)
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT_SECONDS,
            read=_CONTROL_HTTP_TIMEOUT_SECONDS,
            write=10.0,
            pool=10.0,
        )
        try:
            with _build_client(target_url, timeout=timeout) as client:
                resp = client.post(
                    f"/api/v1/control/{kind}",
                    json={},
                    headers=headers,
                )
                if resp.status_code != 202:
                    code, msg = _parse_api_error_message(resp)
                    _emit_error(msg, json_output=bool(args.json), error_code=code)
                    return EXIT_OPERATIONAL_ERROR
                accepted = OperationAcceptedResponse.model_validate_json(resp.text)
                if accepted.kind.value != kind.upper():
                    _emit_error(
                        "Maintenance response operation kind does not match requested operation.",
                        json_output=bool(args.json),
                        error_code="SERVER_IDENTITY_MISMATCH",
                    )
                    return EXIT_FATAL_LIFECYCLE
                if not args.wait:
                    if args.json:
                        _emit_json(accepted.model_dump(mode="json"))
                    else:
                        print(
                            f"operation={accepted.operation_id} kind={accepted.kind.value} status={accepted.status.value}"
                        )
                    return EXIT_OK

                while True:
                    if not _verify_pre_shutdown_stability(
                        state_dir=state_dir,
                        identity=identity,
                        captured_token=token,
                        pidfd=verified.pidfd,
                    ):
                        _emit_error(
                            "Server discovery, control token, or process lock changed during maintenance polling.",
                            json_output=bool(args.json),
                            error_code="UNVERIFIED_PROCESS_IDENTITY",
                        )
                        return EXIT_FATAL_LIFECYCLE
                    status_resp = client.get(
                        f"/api/v1/control/operations/{accepted.operation_id}",
                        headers=headers,
                    )
                    if status_resp.status_code != 200:
                        code, msg = _parse_api_error_message(status_resp)
                        _emit_error(msg, json_output=bool(args.json), error_code=code)
                        return EXIT_OPERATIONAL_ERROR
                    current = OperationStatusResponse.model_validate_json(
                        status_resp.text
                    )
                    if (
                        current.operation_id != accepted.operation_id
                        or current.kind != accepted.kind
                    ):
                        _emit_error(
                            "Maintenance status response operation ID or kind mismatch.",
                            json_output=bool(args.json),
                            error_code="SERVER_IDENTITY_MISMATCH",
                        )
                        return EXIT_FATAL_LIFECYCLE
                    if current.status in {
                        OperationStatus.SUCCEEDED,
                        OperationStatus.FAILED,
                    }:
                        _render_operation_status(current, json_output=bool(args.json))
                        if (
                            current.status is OperationStatus.SUCCEEDED
                            and current.serving_safety is ServingSafety.SAFE_TO_SERVE
                        ):
                            return EXIT_OK
                        return EXIT_OPERATIONAL_ERROR
                    time.sleep(float(args.poll_interval))
        except (httpx.ConnectError, httpx.ConnectTimeout):
            _emit_error(
                f"Unable to reach AtlasRAG server at {target_url}.",
                json_output=bool(args.json),
                error_code="SERVER_UNREACHABLE",
            )
            return EXIT_UNAVAILABLE
        except ValidationError:
            _emit_error(
                "Received malformed server maintenance response.",
                json_output=bool(args.json),
                error_code="INVALID_SERVER_RESPONSE",
            )
            return EXIT_OPERATIONAL_ERROR
        except httpx.HTTPError as error:
            _emit_error(
                f"Control request failed: {type(error).__name__}.",
                json_output=bool(args.json),
                error_code="HTTP_ERROR",
            )
            return EXIT_OPERATIONAL_ERROR


def _pidfd_has_exited(pidfd: int | None) -> bool:
    """Return True iff an opened Linux pidfd already indicates process exit."""

    if pidfd is None:
        return False
    poller = select.poll()
    poller.register(pidfd, select.POLLIN)
    try:
        return bool(poller.poll(0))
    finally:
        poller.unregister(pidfd)


def _verify_pre_shutdown_stability(
    *,
    state_dir: Path,
    identity: TargetServerIdentity,
    captured_token: str,
    pidfd: int | None = None,
) -> bool:
    """Confirm pidfd liveness, discovery, token, and process lock before HTTP send."""

    if _pidfd_has_exited(pidfd):
        return False
    try:
        latest = load_server_discovery(state_dir)
        latest_token = _read_control_token(identity.url, state_dir)
    except (FileNotFoundError, OSError, PermissionError, ValueError, ValidationError):
        return False
    if (
        latest.server_instance_id != identity.server_instance_id
        or latest.pid != identity.pid
        or latest.process_start_ticks != identity.process_start_ticks
        or latest.boot_id != identity.boot_id
        or latest.url != identity.url
    ):
        return False
    if not hmac.compare_digest(
        latest_token.encode("utf-8"), captured_token.encode("utf-8")
    ):
        return False
    if not verify_process_holds_flock(
        identity.pid, _ownership_module.canonical_lock_path()
    ):
        return False
    try:
        return read_proc_stat_starttime(identity.pid) == identity.process_start_ticks
    except ProcessIdentityVerificationError:
        return False


def _cmd_stop(args: argparse.Namespace, *, environ: Mapping[str, str]) -> int:
    if args.timeout is not None and not (0 < args.timeout <= 86_400.0):
        _emit_error(
            "--timeout must be finite and greater than zero.",
            json_output=bool(args.json),
            error_code="INVALID_REQUEST",
        )
        return EXIT_USAGE_OR_CONFIG_ERROR
    if args.server_url is not None:
        _validate_url_option(args.server_url, label="server URL")
    env_url = environ.get("ATLASRAG_SERVER_URL")
    if env_url is not None and env_url != "":
        _validate_url_option(env_url, label="ATLASRAG_SERVER_URL")

    state_dir = _resolve_state_dir(args.state_dir, environ=environ)

    # Pre-shutdown verification & pidfd capture strictly BEFORE any HTTP/signal call.
    try:
        verified = verify_target_process(state_dir)
    except FileNotFoundError:
        if args.force:
            _emit_error(
                "Refusing --force stop: server discovery metadata is missing.",
                json_output=bool(args.json),
                error_code="UNVERIFIED_PROCESS_IDENTITY",
            )
            return EXIT_FATAL_LIFECYCLE
        _emit_error(
            "No running server discovery metadata found.",
            json_output=bool(args.json),
            error_code="SERVER_UNREACHABLE",
        )
        return EXIT_UNAVAILABLE
    except ProcessIdentityVerificationError as error:
        _emit_error(
            f"Refusing stop: {error}.",
            json_output=bool(args.json),
            error_code="UNVERIFIED_PROCESS_IDENTITY",
        )
        return EXIT_FATAL_LIFECYCLE

    with verified:
        identity: TargetServerIdentity = verified.identity
        assert verified.pidfd is not None
        mismatch_msg = _resolve_bound_control_override(
            expected_url=identity.url,
            cli_server_url=args.server_url,
            environ=environ,
        )
        if mismatch_msg is not None:
            _emit_error(
                mismatch_msg,
                json_output=bool(args.json),
                error_code="CONTROL_TARGET_MISMATCH",
            )
            return EXIT_FATAL_LIFECYCLE

        # Observation default preserved per D11-02/D11-11 (not an upper bound on every full shutdown path).
        wait_timeout = (
            float(args.timeout)
            if args.timeout is not None
            else (
                max(1.0, 2.0 * float(identity.shutdown_timeout_seconds) + 5.0)
                if identity.shutdown_timeout_seconds is not None
                else _DEFAULT_STOP_TIMEOUT_SECONDS
            )
        )
        target_url = identity.url

        try:
            token = _read_control_token(target_url, state_dir)
        except (FileNotFoundError, OSError, PermissionError, ValueError):
            _emit_error(
                "Valid local control authorization token could not be loaded.",
                json_output=bool(args.json),
                error_code="UNAUTHORIZED",
            )
            return EXIT_FATAL_LIFECYCLE

        if not _verify_pre_shutdown_stability(
            state_dir=state_dir,
            identity=identity,
            captured_token=token,
            pidfd=verified.pidfd,
        ):
            _emit_error(
                "Server discovery or control token changed during pre-shutdown verification.",
                json_output=bool(args.json),
                error_code="UNVERIFIED_PROCESS_IDENTITY",
            )
            return EXIT_FATAL_LIFECYCLE

        headers = _control_headers(target_url, token)
        timeout = httpx.Timeout(
            connect=_CONNECT_TIMEOUT_SECONDS,
            read=_CONTROL_HTTP_TIMEOUT_SECONDS,
            write=10.0,
            pool=10.0,
        )
        try:
            with _build_client(target_url, timeout=timeout) as client:
                resp = client.post(
                    "/api/v1/control/shutdown",
                    json={},
                    headers=headers,
                )
        except (httpx.ConnectError, httpx.ConnectTimeout):
            _emit_error(
                f"Unable to reach AtlasRAG server at {target_url}.",
                json_output=bool(args.json),
                error_code="SERVER_UNREACHABLE",
            )
            return EXIT_UNAVAILABLE
        except httpx.HTTPError as error:
            _emit_error(
                f"Shutdown request failed: {type(error).__name__}.",
                json_output=bool(args.json),
                error_code="HTTP_ERROR",
            )
            return EXIT_OPERATIONAL_ERROR

        if resp.status_code != 202:
            code, msg = _parse_api_error_message(resp)
            _emit_error(msg, json_output=bool(args.json), error_code=code)
            return EXIT_OPERATIONAL_ERROR

        try:
            accepted = ShutdownAcceptedResponse.model_validate_json(resp.text)
        except ValidationError:
            _emit_error(
                "Received malformed shutdown acceptance response.",
                json_output=bool(args.json),
                error_code="INVALID_SERVER_RESPONSE",
            )
            return EXIT_FATAL_LIFECYCLE

        if (
            accepted.status is not LivenessStatus.SHUTTING_DOWN
            or accepted.pid != identity.pid
            or accepted.server_instance_id != identity.server_instance_id
        ):
            _emit_error(
                "Shutdown response identity does not match captured server identity.",
                json_output=bool(args.json),
                error_code="SERVER_IDENTITY_MISMATCH",
            )
            return EXIT_FATAL_LIFECYCLE

        exited = _wait_pidfd_exit(verified.pidfd, wait_timeout)
        if exited:
            receipt = load_clean_shutdown_receipt(state_dir, identity)
            if receipt is not None:
                try:
                    shutdown_receipt_path(
                        state_dir, identity.server_instance_id
                    ).unlink(missing_ok=True)
                except OSError:
                    pass
                if args.json:
                    _emit_json(
                        {
                            "status": receipt.status,
                            "server_instance_id": receipt.server_instance_id,
                            "pid": receipt.pid,
                            "completed_at": receipt.completed_at.isoformat(),
                        }
                    )
                else:
                    print(
                        f"Clean shutdown verified for pid={receipt.pid} instance={receipt.server_instance_id}."
                    )
                return EXIT_OK

            _emit_error(
                f"Process pid={identity.pid} exited without a valid CLEAN_SHUTDOWN receipt.",
                json_output=bool(args.json),
                error_code="UNVERIFIED_EXIT",
            )
            return EXIT_FATAL_LIFECYCLE

        if not args.force:
            _emit_error(
                f"Timed out waiting {wait_timeout:.1f}s for pid={accepted.pid} to exit.",
                json_output=bool(args.json),
                error_code="SHUTDOWN_TIMEOUT",
            )
            return EXIT_OPERATIONAL_ERROR

        try:
            signal.pidfd_send_signal(verified.pidfd, signal.SIGTERM)
        except ProcessLookupError:
            pass
        grace = min(5.0, max(0.5, wait_timeout / 2.0))
        terminated = _wait_pidfd_exit(verified.pidfd, grace)
        escalation_signal = "SIGTERM"
        if not terminated:
            escalation_signal = "SIGKILL"
            try:
                signal.pidfd_send_signal(verified.pidfd, signal.SIGKILL)
            except ProcessLookupError:
                pass
            terminated = _wait_pidfd_exit(verified.pidfd, 2.0)

        if args.json:
            _emit_json(
                {
                    "status": "FORCED_TERMINATION",
                    "signal": escalation_signal,
                    "pid": identity.pid,
                    "server_instance_id": identity.server_instance_id,
                    "terminated": terminated,
                }
            )
        else:
            state_desc = "exited" if terminated else "still alive after reap grace"
            print(
                f"error: forced termination ({escalation_signal}) applied to pid={identity.pid} ({state_desc}).",
                file=sys.stderr,
            )
        return EXIT_FATAL_LIFECYCLE


def main(
    argv: Sequence[str] | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> int:
    """CLI entrypoint returning explicit deterministic POSIX exit codes."""

    env = os.environ if environ is None else environ
    parser = build_parser()
    try:
        args = parser.parse_args(list(argv) if argv is not None else None)
    except SystemExit as exc:
        code = exc.code
        return int(code) if isinstance(code, int) else EXIT_USAGE_OR_CONFIG_ERROR

    json_output = bool(getattr(args, "json", False))
    if args.command is None:
        parser.print_help()
        return EXIT_USAGE_OR_CONFIG_ERROR
    try:
        if args.command == "serve":
            return _cmd_serve(args, environ=env)

        with _client_signal_handlers():
            if args.command == "ask":
                return _cmd_ask(args, environ=env)
            if args.command == "health":
                return _cmd_health(args, environ=env)
            if args.command == "status":
                return _cmd_status(args, environ=env)
            if args.command == "sync":
                return _cmd_maintenance(args, kind="sync", environ=env)
            if args.command == "recover":
                return _cmd_maintenance(args, kind="recover", environ=env)
            if args.command == "stop":
                return _cmd_stop(args, environ=env)
    except _ClientSignalInterrupt as sig_exc:
        return sig_exc.exit_code
    except KeyboardInterrupt:
        return EXIT_SIGINT
    except _CliUsageOrConfigError as error:
        _emit_error(str(error), json_output=json_output, error_code="INVALID_REQUEST")
        return EXIT_USAGE_OR_CONFIG_ERROR
    except _UnsafeDiscoveryError as error:
        _emit_error(str(error), json_output=json_output, error_code="UNSAFE_STATE")
        return EXIT_FATAL_LIFECYCLE

    return EXIT_USAGE_OR_CONFIG_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
