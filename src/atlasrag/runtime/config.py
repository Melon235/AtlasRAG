"""Configuration loading for the local application runtime."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from atlasrag.config.models import RuntimeConfig
from atlasrag.domain.errors import ConfigurationError

_SECRET_KEYS = frozenset({"postgres_password", "deepseek_api_key", "redis_password"})


def _merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value


def _redacted(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            key: "***" if key.lower() in _SECRET_KEYS else _redacted(item)
            for key, item in value.items()
        }
    return value


def load_runtime_config(
    *,
    environ: Mapping[str, str] | None = None,
    config_file: Path | None = None,
) -> RuntimeConfig:
    """Load checked-in JSON config then explicit ``ATLASRAG_`` env overrides.

    Only the explicit ``ATLASRAG_CONFIG`` file and ``ATLASRAG_*`` JSON/env
    overrides are interpreted. Unrelated OS environment variables are ignored.
    Secret values are never included in validation errors.
    """

    source_env = os.environ if environ is None else environ
    payload: dict[str, Any] = {}
    selected = config_file
    if selected is None:
        raw_path = source_env.get("ATLASRAG_CONFIG")
        selected = Path(raw_path) if raw_path else None
    if selected is not None:
        try:
            loaded = json.loads(selected.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigurationError from error
        if not isinstance(loaded, dict):
            raise ConfigurationError
        _merge(payload, loaded)
    raw_env = source_env.get("ATLASRAG_CONFIG_JSON")
    if raw_env:
        try:
            override = json.loads(raw_env)
        except json.JSONDecodeError as error:
            raise ConfigurationError from error
        if not isinstance(override, dict):
            raise ConfigurationError
        _merge(payload, override)
    try:
        return RuntimeConfig.model_validate(payload)
    except ValidationError as error:
        safe = _redacted(error.errors(include_url=False, include_input=False))
        raise ConfigurationError from ValueError(safe)
