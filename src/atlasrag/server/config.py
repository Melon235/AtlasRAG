"""Validated local server transport configuration and loader."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from ipaddress import ip_address
from pathlib import Path
from typing import Annotated, Any, Self

from pydantic import Field, StrictStr, ValidationError, field_validator, model_validator

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.config.models import (
    NonNegativeReal,
    Port,
    PositiveReal,
    PositiveStrictInt,
)
from atlasrag.domain.base import FrozenModel
from atlasrag.domain.errors import ConfigurationError

_DEFAULT_ALLOWED_HOSTS_MAX = 16


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return validate_unicode_scalar_text(value)


def validate_loopback_host(value: str) -> str:
    """Validate that a bind host resolves strictly to loopback."""

    cleaned = _validate_nonblank(value)
    if cleaned != cleaned.strip():
        raise ValueError("host must not have leading or trailing whitespace")
    if cleaned.casefold() == "localhost":
        return "localhost"
    stripped = (
        cleaned[1:-1] if cleaned.startswith("[") and cleaned.endswith("]") else cleaned
    )
    try:
        parsed = ip_address(stripped)
    except ValueError as error:
        raise ValueError(
            "server host must be loopback (127.0.0.1, ::1, or localhost)"
        ) from error
    if not parsed.is_loopback:
        raise ValueError("server host must be loopback (127.0.0.1, ::1, or localhost)")
    return str(parsed)


def _validate_allowed_host_entry(value: str) -> str:
    cleaned = _validate_nonblank(value)
    if cleaned != cleaned.strip() or "*" in cleaned or "/" in cleaned or "@" in cleaned:
        raise ValueError("allowed_hosts entries must be exact host[:port] values")
    host_part = cleaned
    if cleaned.startswith("["):
        end_bracket = cleaned.find("]")
        if end_bracket <= 1:
            raise ValueError("invalid bracketed host")
        host_part = cleaned[1:end_bracket]
        remainder = cleaned[end_bracket + 1 :]
        if remainder:
            if not remainder.startswith(":") or not remainder[1:].isdigit():
                raise ValueError("invalid host port suffix")
            port_val = int(remainder[1:])
            if not 1 <= port_val <= 65535:
                raise ValueError("invalid host port")
    elif ":" in cleaned:
        host_part, _, port_str = cleaned.partition(":")
        if not port_str.isdigit() or not 1 <= int(port_str) <= 65535:
            raise ValueError("invalid host port")
    validate_loopback_host(host_part)
    return cleaned.casefold()


class ServerConfig(FrozenModel):
    """Transport and operational bounds for the local AtlasRAG HTTP/SSE server."""

    host: StrictStr = "127.0.0.1"
    port: Port = 8000
    allowed_hosts: tuple[StrictStr, ...] | None = None
    max_request_bytes: Annotated[PositiveStrictInt, Field(le=1_048_576)] = 65_536
    max_response_bytes: Annotated[PositiveStrictInt, Field(ge=1024, le=33_554_432)] = (
        4_194_304
    )
    max_progress_event_bytes: Annotated[PositiveStrictInt, Field(ge=128, le=16_384)] = (
        1_024
    )
    max_query_chars: Annotated[PositiveStrictInt, Field(ge=1, le=65_536)] = 8_192
    max_concurrent_queries: Annotated[PositiveStrictInt, Field(le=256)] = 16
    max_concurrent_streams: Annotated[PositiveStrictInt, Field(le=256)] = 16
    query_timeout_seconds: Annotated[PositiveReal, Field(le=600.0)] = 120.0
    sse_heartbeat_seconds: Annotated[PositiveReal, Field(le=120.0)] = 15.0
    sse_send_timeout_seconds: Annotated[PositiveReal, Field(le=60.0)] = 5.0
    sse_queue_maxsize: Annotated[PositiveStrictInt, Field(ge=16, le=512)] = 64
    max_sessions: Annotated[PositiveStrictInt, Field(le=4096)] = 256
    session_ttl_seconds: Annotated[PositiveReal, Field(le=86_400.0)] = 3600.0
    max_in_flight_per_session: Annotated[PositiveStrictInt, Field(le=64)] = 4
    max_retained_operations: Annotated[PositiveStrictInt, Field(le=512)] = 32
    min_manual_refresh_interval_seconds: Annotated[NonNegativeReal, Field(le=60.0)] = (
        1.0
    )
    state_directory: StrictStr | None = None

    @field_validator("host")
    @classmethod
    def _loopback_only(cls, value: str) -> str:
        return validate_loopback_host(value)

    @field_validator("allowed_hosts")
    @classmethod
    def _valid_allowed_hosts(
        cls, value: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if value is None:
            return None
        if len(value) > _DEFAULT_ALLOWED_HOSTS_MAX:
            raise ValueError("too many allowed_hosts entries")
        return tuple(_validate_allowed_host_entry(item) for item in value)

    @field_validator("state_directory")
    @classmethod
    def _nonblank_state_dir(cls, value: str | None) -> str | None:
        if value is not None:
            return _validate_nonblank(value)
        return None

    @model_validator(mode="after")
    def _validate_bounds(self) -> Self:
        if self.max_concurrent_streams > self.max_concurrent_queries:
            raise ValueError(
                "max_concurrent_streams must not exceed max_concurrent_queries"
            )
        if self.max_in_flight_per_session > self.max_concurrent_queries:
            raise ValueError(
                "max_in_flight_per_session must not exceed max_concurrent_queries"
            )
        return self


def _merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def load_server_config(
    *,
    environ: Mapping[str, str] | None = None,
    config_file: Path | None = None,
) -> ServerConfig:
    """Load ServerConfig from optional JSON file and explicit env overrides."""

    source_env = os.environ if environ is None else environ
    payload: dict[str, Any] = {}
    selected = config_file
    if selected is None:
        raw_path = source_env.get("ATLASRAG_SERVER_CONFIG")
        selected = Path(raw_path) if raw_path else None
    if selected is not None:
        try:
            loaded = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError from error
        if not isinstance(loaded, dict):
            raise ConfigurationError
        _merge(payload, loaded)
    raw_env = source_env.get("ATLASRAG_SERVER_CONFIG_JSON")
    if raw_env:
        try:
            override = json.loads(raw_env)
        except json.JSONDecodeError as error:
            raise ConfigurationError from error
        if not isinstance(override, dict):
            raise ConfigurationError
        _merge(payload, override)
    try:
        return ServerConfig.model_validate(payload)
    except ValidationError as error:
        safe_errors = error.errors(include_url=False, include_input=False)
        raise ConfigurationError from ValueError(safe_errors)
