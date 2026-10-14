"""Lazy MinerU PDF adapter behavior without requiring the optional SDK."""

from __future__ import annotations

import builtins
import copy
import importlib
import json
import sys
import threading
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast

import pytest

from atlasrag.domain.errors import (
    DependencyTimeoutError,
    DependencyUnavailableError,
    InvariantViolationError,
    ProviderResponseError,
)
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing import mineru as mineru_adapter
from atlasrag.services.ingestion.identity import build_candidate_context

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
MINERU_FIXTURE = FIXTURES / "parser_outputs" / "mineru" / "v4" / "simple.json"
PDF_FIXTURE = FIXTURES / "stage3" / "simple.pdf"
PIPELINE_FINGERPRINT = "c" * 64


class FakeMineruError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("provider detail must not escape")


class FakeResult:
    def __init__(self, payload: object) -> None:
        self.payload = payload
        self.calls: list[bool] = []

    def to_dict(self, *, skip_defaults: bool = True) -> object:
        self.calls.append(skip_defaults)
        return copy.deepcopy(self.payload)


def output_payload() -> dict[str, Any]:
    payload = json.loads(MINERU_FIXTURE.read_text(encoding="utf-8"))
    assert isinstance(payload, dict)
    return cast(dict[str, Any], payload)


def prepared_source(
    root: Path, name: str = "document.pdf"
) -> tuple[SourceFile, CandidateBuildContext]:
    (root / name).write_bytes(PDF_FIXTURE.read_bytes())
    source = SourceFile.from_knowledge_directory(root, name)
    return source, build_candidate_context(source, PIPELINE_FINGERPRINT)


def fake_sdk(parse: object) -> SimpleNamespace:
    return SimpleNamespace(parse=parse, error_type=FakeMineruError)


def test_importing_adapter_does_not_import_optional_mineru_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sys.modules.pop("mineru", None)
    sys.modules.pop("mineru.errors", None)
    original_import = builtins.__import__

    def guarded_import(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "mineru" or name.startswith("mineru."):
            raise AssertionError("adapter imported MinerU before parse")
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", guarded_import)

    importlib.reload(mineru_adapter)

    assert "mineru" not in sys.modules


@pytest.mark.asyncio
async def test_missing_optional_sdk_is_dependency_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path)
    sys.modules.pop("mineru", None)
    sys.modules.pop("mineru.errors", None)
    original_import = builtins.__import__

    def missing_sdk(
        name: str,
        globals: dict[str, Any] | None = None,
        locals: dict[str, Any] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> Any:
        if name == "mineru" or name.startswith("mineru."):
            error = ModuleNotFoundError("No module named 'mineru'")
            error.name = "mineru"
            raise error
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", missing_sdk)

    with pytest.raises(DependencyUnavailableError) as raised:
        await mineru_adapter.MinerUParser().parse(source, context)
    assert str(raised.value) == "A required dependency is unavailable."


@pytest.mark.asyncio
async def test_adapter_calls_local_flash_full_pages_and_normalizes_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path)
    calls: list[tuple[Path, str, str]] = []
    calling_thread = threading.get_ident()
    parse_threads: list[int] = []
    result = FakeResult(output_payload())

    def parse(path: str | Path, *, tier: str, page_range: str) -> FakeResult:
        parse_threads.append(threading.get_ident())
        calls.append((Path(path), tier, page_range))
        return result

    fake_root = ModuleType("mineru")
    fake_errors = ModuleType("mineru.errors")
    setattr(fake_root, "__path__", [])
    setattr(fake_root, "parse", parse)
    setattr(fake_root, "errors", fake_errors)
    setattr(fake_errors, "MineruError", FakeMineruError)
    monkeypatch.setitem(sys.modules, "mineru", fake_root)
    monkeypatch.setitem(sys.modules, "mineru.errors", fake_errors)

    parsed = await mineru_adapter.MinerUParser().parse(source, context)

    assert calls == [(source.absolute_path.resolve(), "flash", "")]
    assert parse_threads == [calling_thread]
    assert result.calls == [False]
    assert parsed.parse_info.parser_name == "mineru"
    assert parsed.parse_info.parser_version == "4.0.11"
    assert parsed.elements


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    ["parse_timeout", "parse_wait_timeout", "remote_timeout"],
)
async def test_documented_mineru_timeout_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    source, context = prepared_source(tmp_path)

    def parse(*_args: object, **_kwargs: object) -> object:
        raise FakeMineruError(code)

    monkeypatch.setattr(mineru_adapter, "_load_mineru_sdk", lambda: fake_sdk(parse))

    with pytest.raises(DependencyTimeoutError):
        await mineru_adapter.MinerUParser().parse(source, context)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "code",
    [
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
    ],
)
async def test_documented_mineru_unavailability_is_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    source, context = prepared_source(tmp_path)

    def parse(*_args: object, **_kwargs: object) -> object:
        raise FakeMineruError(code)

    monkeypatch.setattr(mineru_adapter, "_load_mineru_sdk", lambda: fake_sdk(parse))

    with pytest.raises(DependencyUnavailableError):
        await mineru_adapter.MinerUParser().parse(source, context)


@pytest.mark.asyncio
async def test_unknown_provider_errors_are_safe_and_cancellation_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path)

    for provider_error in (
        FakeMineruError("file_corrupted"),
        RuntimeError(f"unexpected provider defect at {source.absolute_path}"),
    ):

        def unexpected(*_args: object, **_kwargs: object) -> object:
            raise provider_error

        monkeypatch.setattr(
            mineru_adapter, "_load_mineru_sdk", lambda: fake_sdk(unexpected)
        )
        with pytest.raises(ProviderResponseError) as raised:
            await mineru_adapter.MinerUParser().parse(source, context)
        assert str(raised.value) == "A provider returned an invalid response."
        assert raised.value.args == ()
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert str(source.absolute_path) not in str(raised.value)

    def broken_loader() -> object:
        raise RuntimeError(f"SDK setup exposed {source.absolute_path}")

    monkeypatch.setattr(mineru_adapter, "_load_mineru_sdk", broken_loader)
    with pytest.raises(ProviderResponseError) as raised:
        await mineru_adapter.MinerUParser().parse(source, context)
    assert raised.value.__context__ is None

    class BuildCancellation(BaseException):
        pass

    def cancelled(*_args: object, **_kwargs: object) -> object:
        raise BuildCancellation

    monkeypatch.setattr(mineru_adapter, "_load_mineru_sdk", lambda: fake_sdk(cancelled))
    with pytest.raises(BuildCancellation):
        await mineru_adapter.MinerUParser().parse(source, context)

    result = FakeResult(output_payload())
    monkeypatch.setattr(
        mineru_adapter,
        "_load_mineru_sdk",
        lambda: fake_sdk(lambda *_args, **_kwargs: result),
    )

    def invalid_canonical_output(*_args: object, **_kwargs: object) -> object:
        raise InvariantViolationError

    monkeypatch.setattr(
        mineru_adapter,
        "normalize_mineru_output",
        invalid_canonical_output,
    )
    with pytest.raises(InvariantViolationError) as raised_invariant:
        await mineru_adapter.MinerUParser().parse(source, context)
    assert str(raised_invariant.value) == "An internal invariant was violated."
    assert raised_invariant.value.__context__ is None


@pytest.mark.asyncio
async def test_malformed_provider_error_codes_are_safely_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path)

    class ExplodingCodeError(Exception):
        @property
        def code(self) -> object:
            raise RuntimeError(f"provider code exposed {source.absolute_path}")

    class ExplodingHash:
        def __hash__(self) -> int:
            raise RuntimeError(f"provider hash exposed {source.absolute_path}")

    class InvalidCodeError(Exception):
        code = ExplodingHash()

    for provider_error in (ExplodingCodeError(), InvalidCodeError()):

        def unexpected(
            *_args: object,
            provider_error: Exception = provider_error,
            **_kwargs: object,
        ) -> object:
            raise provider_error

        error_type = type(provider_error)
        monkeypatch.setattr(
            mineru_adapter,
            "_load_mineru_sdk",
            lambda: SimpleNamespace(parse=unexpected, error_type=error_type),
        )
        with pytest.raises(ProviderResponseError) as raised:
            await mineru_adapter.MinerUParser().parse(source, context)
        assert str(raised.value) == "A provider returned an invalid response."
        assert raised.value.args == ()
        assert raised.value.__cause__ is None
        assert raised.value.__context__ is None
        assert str(source.absolute_path) not in str(raised.value)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_error"),
    [
        (TimeoutError("provider detail"), DependencyTimeoutError),
        (ModuleNotFoundError("late optional dependency"), DependencyUnavailableError),
    ],
)
async def test_runtime_dependency_failures_are_normalized(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
    expected_error: type[Exception],
) -> None:
    source, context = prepared_source(tmp_path)

    def parse(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(mineru_adapter, "_load_mineru_sdk", lambda: fake_sdk(parse))

    with pytest.raises(expected_error):
        await mineru_adapter.MinerUParser().parse(source, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("payload", [None, [], {"schema": "bad"}])
async def test_malformed_parse_result_is_provider_response_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: object
) -> None:
    source, context = prepared_source(tmp_path)
    result = FakeResult(payload)
    monkeypatch.setattr(
        mineru_adapter,
        "_load_mineru_sdk",
        lambda: fake_sdk(lambda *_args, **_kwargs: result),
    )

    with pytest.raises(ProviderResponseError):
        await mineru_adapter.MinerUParser().parse(source, context)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [object(), SimpleNamespace(to_dict=None)])
async def test_missing_or_non_callable_to_dict_is_provider_response_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, result: object
) -> None:
    source, context = prepared_source(tmp_path)
    monkeypatch.setattr(
        mineru_adapter,
        "_load_mineru_sdk",
        lambda: fake_sdk(lambda *_args, **_kwargs: result),
    )

    with pytest.raises(ProviderResponseError):
        await mineru_adapter.MinerUParser().parse(source, context)


@pytest.mark.asyncio
async def test_to_dict_failures_are_safely_normalized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source, context = prepared_source(tmp_path)

    def invalid_to_dict(*, skip_defaults: bool) -> object:
        del skip_defaults
        raise TypeError("unsupported result surface")

    monkeypatch.setattr(
        mineru_adapter,
        "_load_mineru_sdk",
        lambda: fake_sdk(
            lambda *_args, **_kwargs: SimpleNamespace(to_dict=invalid_to_dict)
        ),
    )
    with pytest.raises(ProviderResponseError):
        await mineru_adapter.MinerUParser().parse(source, context)

    def broken_to_dict(*, skip_defaults: bool) -> object:
        del skip_defaults
        raise RuntimeError("unexpected serialization defect")

    monkeypatch.setattr(
        mineru_adapter,
        "_load_mineru_sdk",
        lambda: fake_sdk(
            lambda *_args, **_kwargs: SimpleNamespace(to_dict=broken_to_dict)
        ),
    )
    with pytest.raises(ProviderResponseError) as raised:
        await mineru_adapter.MinerUParser().parse(source, context)
    assert str(raised.value) == "A provider returned an invalid response."
    assert raised.value.__context__ is None


@pytest.mark.asyncio
async def test_adapter_rejects_wrong_route_and_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_source, pdf_context = prepared_source(tmp_path)
    (tmp_path / "document.txt").write_text("text", encoding="utf-8")
    text_source = SourceFile.from_knowledge_directory(tmp_path, "document.txt")
    text_context = build_candidate_context(text_source, PIPELINE_FINGERPRINT)
    monkeypatch.setattr(
        mineru_adapter,
        "_load_mineru_sdk",
        lambda: pytest.fail("wrong route must fail before loading MinerU"),
    )

    with pytest.raises(InvariantViolationError):
        await mineru_adapter.MinerUParser().parse(text_source, text_context)
    with pytest.raises(InvariantViolationError):
        await mineru_adapter.MinerUParser().parse(pdf_source, text_context)
    assert pdf_context.source == pdf_source
