"""Determinism guarantees for checked-in Stage 3 parser fixtures."""

from __future__ import annotations

import importlib.util
import sys
from io import BytesIO
from pathlib import Path
from types import ModuleType
from typing import Protocol, cast
from zipfile import ZIP_DEFLATED, ZipFile

FIXTURE_SCRIPT = (
    Path(__file__).resolve().parents[2] / "scripts" / "generate_stage3_fixtures.py"
)


class _ArchiveNormalizer(Protocol):
    def __call__(self, payload: bytes) -> bytes: ...


def _load_fixture_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "atlasrag_stage3_fixture_generator",
        FIXTURE_SCRIPT,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _office_archive(modified_at: str) -> bytes:
    core = (
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/'
        '2006/metadata/core-properties" '
        'xmlns:dcterms="http://purl.org/dc/terms/">'
        f"<dcterms:modified>{modified_at}</dcterms:modified>"
        "</cp:coreProperties>"
    )
    output = BytesIO()
    with ZipFile(output, "w", compression=ZIP_DEFLATED) as archive:
        archive.writestr("docProps/core.xml", core)
        archive.writestr("word/document.xml", "<document />")
    return output.getvalue()


def test_office_archive_normalizes_volatile_modified_timestamp() -> None:
    module = _load_fixture_module()
    normalize = cast(
        _ArchiveNormalizer,
        getattr(module, "_deterministic_office_archive"),
    )
    first = normalize(_office_archive("2026-10-10T04:26:13Z"))
    second = normalize(_office_archive("2026-10-10T04:27:49Z"))

    assert first == second
