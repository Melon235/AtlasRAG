"""Lazy local MinerU 4 PDF parser adapter."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast

from atlasrag.domain.enums import SourceType
from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
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
    failure: ProviderResponseError | None = None
    try:
        to_dict = getattr(result, "to_dict")
        if not callable(to_dict):
            raise TypeError
        return to_dict(skip_defaults=False)
    except Exception:
        failure = ProviderResponseError()
    raise failure


def _translate_mineru_failure(
    error: Exception,
    provider_error_type: type[Exception],
) -> DependencyTimeoutError | DependencyUnavailableError | ProviderResponseError:
    if isinstance(error, TimeoutError):
        return DependencyTimeoutError()
    if isinstance(error, (ImportError, ModuleNotFoundError)):
        return DependencyUnavailableError()
    try:
        is_provider_error = isinstance(error, provider_error_type)
        code = getattr(error, "code", None) if is_provider_error else None
    except Exception:
        return ProviderResponseError()
    if type(code) is str:
        if code in _TIMEOUT_CODES:
            return DependencyTimeoutError()
        if code in _UNAVAILABLE_CODES:
            return DependencyUnavailableError()
    return ProviderResponseError()


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
        load_failure: DependencyUnavailableError | ProviderResponseError | None = None
        try:
            sdk = _load_mineru_sdk()
        except (ImportError, ModuleNotFoundError):
            load_failure = DependencyUnavailableError()
        except Exception:
            load_failure = ProviderResponseError()
        if load_failure is not None:
            raise load_failure

        parse_failure: (
            DependencyTimeoutError
            | DependencyUnavailableError
            | ProviderResponseError
            | None
        ) = None
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
        except Exception as error:
            parse_failure = _translate_mineru_failure(error, sdk.error_type)
        if parse_failure is not None:
            raise parse_failure

        payload = _provider_payload(result)
        if not isinstance(payload, dict):
            raise ProviderResponseError
        normalization_failure: (
            InvariantViolationError | ProviderResponseError | None
        ) = None
        try:
            return normalize_mineru_output(payload, source, context)
        except InvariantViolationError:
            normalization_failure = InvariantViolationError()
        except Exception:
            normalization_failure = ProviderResponseError()
        raise normalization_failure
