#!/usr/bin/env python3
"""Run the mandatory real MinerU flash smoke with bounded safe output."""

from __future__ import annotations

import asyncio
import hashlib
import importlib.metadata
import json
import os
import sys
from collections import Counter
from collections.abc import Awaitable, Callable
from io import TextIOWrapper
from pathlib import Path
from typing import TypeVar, cast

from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.enums import ElementType, ParseOutcome
from atlasrag.domain.ingestion.source import SourceFile
from atlasrag.providers.parsing.mineru import MinerUParser
from atlasrag.services.ingestion.identity import build_candidate_context

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIRECTORY = REPOSITORY_ROOT / "tests" / "fixtures" / "stage3"
PIPELINE_FINGERPRINT = hashlib.sha256(
    b"atlasrag-stage3-mineru-flash-smoke-v1"
).hexdigest()
_T = TypeVar("_T")
_MISSING = object()


def _flush_standard_streams() -> None:
    sys.stdout.flush()
    sys.stderr.flush()


def _capture_cleanup(
    failures: list[tuple[str, BaseException]],
    operation: str,
    action: Callable[[], object],
) -> None:
    try:
        action()
    except BaseException as error:
        failures.append((operation, error))


def _capture_descriptor_close(
    failures: list[tuple[str, BaseException]],
    operation: str,
    descriptor: int,
) -> None:
    try:
        os.close(descriptor)
    except BaseException as error:
        failures.append((operation, error))


def _raise_output_boundary_outcome(
    primary: BaseException | None,
    cleanup_failures: list[tuple[str, BaseException]],
) -> None:
    if primary is not None:
        for operation, cleanup_error in cleanup_failures:
            primary.add_note(
                "MinerU output boundary cleanup failure during "
                f"{operation}: {type(cleanup_error).__name__}"
            )
        raise primary
    if cleanup_failures:
        operation, cleanup_error = cleanup_failures[0]
        cleanup_error.add_note(
            f"MinerU output boundary cleanup failure during {operation}"
        )
        for later_operation, later_error in cleanup_failures[1:]:
            cleanup_error.add_note(
                "Additional MinerU output boundary cleanup failure during "
                f"{later_operation}: {type(later_error).__name__}"
            )
        raise cleanup_error


async def _run_with_discarded_process_output(
    operation: Callable[[], Awaitable[_T]],
) -> _T:
    """Run one provider boundary with inherited process output sent to null."""
    _flush_standard_streams()
    null_fd: int | None = None
    saved_stdout_fd: int | None = None
    saved_stderr_fd: int | None = None
    saved_stdout_stream = sys.stdout
    saved_stderr_stream = sys.stderr
    discarded_stdout_stream: TextIOWrapper | None = None
    discarded_stderr_stream: TextIOWrapper | None = None
    stdout_redirected = False
    stderr_redirected = False
    streams_redirected = False
    try:
        null_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stdout_fd = os.dup(1)
        saved_stderr_fd = os.dup(2)
        os.dup2(null_fd, 1)
        stdout_redirected = True
        os.dup2(null_fd, 2)
        stderr_redirected = True
        discarded_stdout_stream = open(os.devnull, "w", encoding="utf-8")
        discarded_stderr_stream = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = discarded_stdout_stream
        sys.stderr = discarded_stderr_stream
        streams_redirected = True
    except BaseException as setup_error:
        setup_cleanup_failures: list[tuple[str, BaseException]] = []
        if streams_redirected:
            sys.stdout = saved_stdout_stream
            sys.stderr = saved_stderr_stream
        if stdout_redirected and saved_stdout_fd is not None:
            _capture_cleanup(
                setup_cleanup_failures,
                "stdout setup rollback",
                lambda: os.dup2(saved_stdout_fd, 1),
            )
        if stderr_redirected and saved_stderr_fd is not None:
            _capture_cleanup(
                setup_cleanup_failures,
                "stderr setup rollback",
                lambda: os.dup2(saved_stderr_fd, 2),
            )
        for operation_name, descriptor in (
            ("saved stdout close", saved_stdout_fd),
            ("saved stderr close", saved_stderr_fd),
            ("null descriptor close", null_fd),
        ):
            if descriptor is not None:
                _capture_descriptor_close(
                    setup_cleanup_failures,
                    operation_name,
                    descriptor,
                )
        for operation_name, stream in (
            ("discarded stdout close", discarded_stdout_stream),
            ("discarded stderr close", discarded_stderr_stream),
        ):
            if stream is not None:
                _capture_cleanup(
                    setup_cleanup_failures,
                    operation_name,
                    stream.close,
                )
        _raise_output_boundary_outcome(setup_error, setup_cleanup_failures)
        raise AssertionError("unreachable output-boundary setup outcome")

    result: _T | object = _MISSING
    primary: BaseException | None = None
    try:
        result = await operation()
    except BaseException as error:
        primary = error

    cleanup_failures: list[tuple[str, BaseException]] = []
    _capture_cleanup(cleanup_failures, "stream flush", _flush_standard_streams)
    assert saved_stdout_fd is not None
    assert saved_stderr_fd is not None
    assert null_fd is not None
    _capture_cleanup(
        cleanup_failures,
        "stdout restore",
        lambda: os.dup2(saved_stdout_fd, 1),
    )
    _capture_cleanup(
        cleanup_failures,
        "stderr restore",
        lambda: os.dup2(saved_stderr_fd, 2),
    )
    sys.stdout = saved_stdout_stream
    sys.stderr = saved_stderr_stream
    assert discarded_stdout_stream is not None
    assert discarded_stderr_stream is not None
    _capture_cleanup(
        cleanup_failures,
        "discarded stdout close",
        discarded_stdout_stream.close,
    )
    _capture_cleanup(
        cleanup_failures,
        "discarded stderr close",
        discarded_stderr_stream.close,
    )
    for operation_name, descriptor in (
        ("saved stdout close", saved_stdout_fd),
        ("saved stderr close", saved_stderr_fd),
        ("null descriptor close", null_fd),
    ):
        _capture_descriptor_close(
            cleanup_failures,
            operation_name,
            descriptor,
        )
    _raise_output_boundary_outcome(primary, cleanup_failures)
    if result is _MISSING:
        raise AssertionError("provider boundary completed without a result")
    return cast(_T, result)


def _build_report(
    document: DocumentContainer,
    *,
    sdk_version: str,
) -> dict[str, object]:
    if not isinstance(document, DocumentContainer):
        raise TypeError("MinerU smoke result must be a DocumentContainer")
    if (
        document.source.source_type is not SourceType.PDF
        or document.parse_info.outcome is not ParseOutcome.SUCCESS
        or document.parse_info.parser_name != "mineru"
        or document.metadata.page_count is None
        or not document.elements
    ):
        raise RuntimeError("MinerU smoke produced invalid canonical semantics")

    observed = Counter(element.element_type for element in document.elements)
    element_type_counts = {
        element_type.value: observed[element_type] for element_type in ElementType
    }
    return {
        "sdk_version": sdk_version,
        "producer_version": document.parse_info.parser_version,
        "page_count": document.metadata.page_count,
        "element_count": len(document.elements),
        "element_type_counts": element_type_counts,
    }


async def _run_smoke() -> dict[str, object]:
    source = SourceFile.from_knowledge_directory(
        FIXTURE_DIRECTORY,
        "simple.pdf",
    )
    context = build_candidate_context(source, PIPELINE_FINGERPRINT)

    async def parse_document() -> DocumentContainer:
        return await MinerUParser().parse(source, context)

    document = await _run_with_discarded_process_output(parse_document)
    return _build_report(
        document,
        sdk_version=importlib.metadata.version("mineru"),
    )


def main() -> int:
    try:
        report = asyncio.run(_run_smoke())
    except KeyboardInterrupt:
        print("MinerU Stage 3 parser smoke failed", file=sys.stderr)
        return 130
    except BaseException:
        print("MinerU Stage 3 parser smoke failed", file=sys.stderr)
        return 1
    print(json.dumps(report, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
