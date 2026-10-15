"""Mandatory real MinerU flash integration gate for the Stage 3 PDF route."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from atlasrag.domain.ingestion.enums import ElementType

pytestmark = pytest.mark.integration

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SMOKE_SCRIPT = REPOSITORY_ROOT / "scripts" / "stage3_parser_smoke.py"
EXPECTED_FIELDS = {
    "sdk_version",
    "producer_version",
    "page_count",
    "element_count",
    "element_type_counts",
}


def test_real_mineru_flash_script_parses_pdf_to_document_container() -> None:
    completed = subprocess.run(
        (sys.executable, str(SMOKE_SCRIPT)),
        cwd=REPOSITORY_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stderr == ""
    raw_report: object = json.loads(completed.stdout)
    assert isinstance(raw_report, dict)
    assert set(raw_report) == EXPECTED_FIELDS
    report = cast(dict[str, object], raw_report)
    assert isinstance(report["sdk_version"], str) and report["sdk_version"]
    assert isinstance(report["producer_version"], str) and report["producer_version"]
    assert type(report["page_count"]) is int and report["page_count"] >= 1
    assert type(report["element_count"]) is int and report["element_count"] >= 1
    counts = report["element_type_counts"]
    assert isinstance(counts, dict)
    assert set(counts) == {element_type.value for element_type in ElementType}
    assert all(type(count) is int and count >= 0 for count in counts.values())
    assert sum(counts.values()) == report["element_count"]

    print(completed.stdout, end="")
