"""Lazy local MinerU 4 PDF parser adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    ProviderResponseError,
)
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    SourceFile,
    resolve_source_target,
)
from atlasrag.providers.parsing.base import validate_parse_request
from atlasrag.providers.parsing.mineru_normalizer import normalize_mineru_output

_TIMEOUT_CODES = frozenset({"parse_timeout", "parse_wait_timeout", "remote_timeout"})
_UNAVAILABLE_CODES = frozenset(
    {
        "engine_unavailable",
        "quality_tier_unavailable",
        "model_preload_dependency_missing",
        "model_preload_files_missing",
        "model_preload_device_unavailable",
        "model_preload_failed",
        "service_unavailable",
        "server_not_running",
        "server_busy",
        "remote_unreachable",
        "parse_server_unavailable",
        "parse_server_dependency_missing",
        "parse_server_model_not_ready",
        "no_engine",
    }
)


class _ParseFunction(Protocol):
    def __call__(
        self,
        path: str | Path,
        *,
        tier: str,
        page_range: str,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class _MinerUSDK:
    parse: _ParseFunction
    error_type: type[Exception]


def _load_mineru_sdk() -> _MinerUSDK:
    """Import the optional SDK only when the PDF route is invoked."""
    from mineru import parse  # type: ignore[import-not-found]
    from mineru.errors import MineruError  # type: ignore[import-not-found]

    parse_function: object = parse
    error_type: object = MineruError
    if (
        not callable(parse_function)
        or not isinstance(error_type, type)
        or not issubclass(error_type, Exception)
    ):
        raise ImportError("unsupported MinerU SDK surface")
    return _MinerUSDK(
        parse=cast(_ParseFunction, parse_function),
        error_type=error_type,
    )


def _provider_payload(result: object) -> object:
    try:
        to_dict = getattr(result, "to_dict")
    except AttributeError:
        raise ProviderResponseError from None
    if not callable(to_dict):
        raise ProviderResponseError
    try:
        return to_dict(skip_defaults=False)
    except (AttributeError, TypeError, ValueError):
        raise ProviderResponseError from None


class MinerUParser:
    """Parse PDFs through the local full-page MinerU flash SDK route."""

    async def parse(
        self,
        source: SourceFile,
        context: CandidateBuildContext,
    ) -> DocumentContainer:
        source, context = validate_parse_request(
            source,
            context,
            allowed_source_types=(SourceType.PDF,),
        )
        path = resolve_source_target(source)
        try:
            sdk = _load_mineru_sdk()
        except (ImportError, ModuleNotFoundError):
            raise DependencyUnavailableError from None

        try:
            # MinerU 4.0.11 flash starts its own PDF render processes. Running
            # that process lifecycle inside a Python worker thread leaves its
            # completion notification stranded, including through the SDK's
            # own parse_async wrapper. Keep this SDK boundary on the invoking
            # thread; the mandatory real smoke protects the compatibility fact.
            result = sdk.parse(
                path,
                tier="flash",
                page_range="",
            )
        except TimeoutError:
            raise DependencyTimeoutError from None
        except (ImportError, ModuleNotFoundError):
            raise DependencyUnavailableError from None
        except sdk.error_type as error:
            code = getattr(error, "code", None)
            if code in _TIMEOUT_CODES:
                raise DependencyTimeoutError from None
            if code in _UNAVAILABLE_CODES:
                raise DependencyUnavailableError from None
            raise

        payload = _provider_payload(result)
        if not isinstance(payload, dict):
            raise ProviderResponseError
        return normalize_mineru_output(payload, source, context)
