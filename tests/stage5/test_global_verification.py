"""Global exact-set published-knowledge verification."""

from __future__ import annotations

from pathlib import Path

import pytest
from stage4.candidate_fakes import config

from atlasrag.domain.ingestion.source import derive_document_id
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.services.sync.models import (
    BuildExecutionSummary,
    DeletionItem,
    DeletionOutcome,
    DeletionSummary,
    ServingSafety,
)
from atlasrag.services.sync.verification import GlobalKnowledgeVerificationService
from stage5.test_current_repair import published


def verifier(factory: object, index: object) -> GlobalKnowledgeVerificationService:
    return GlobalKnowledgeVerificationService(factory, index, config())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_complete_published_view_is_safe(tmp_path: Path) -> None:
    factory, stored, repair, _, index, _ = published(tmp_path)
    await repair.reconcile_current(stored.document_id)
    result = await verifier(factory, index).verify(
        deletion_summary=DeletionSummary(), build_summary=BuildExecutionSummary()
    )
    assert result.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert result.verified_documents == 1
    assert result.verified_records > 0
    assert result.unsafe_reasons == ()


@pytest.mark.asyncio
async def test_orphan_row_makes_the_view_unsafe(tmp_path: Path) -> None:
    factory, stored, repair, _, index, _ = published(tmp_path)
    await repair.reconcile_current(stored.document_id)
    index.rows.append(
        MilvusRevisionProjection.model_validate(
            index.rows[0].model_dump()
            | {
                "chunk_id": "chunk_" + "e" * 64,
                "document_id": derive_document_id("elsewhere.txt"),
                "revision_id": "rev_" + "e" * 64,
            }
        )
    )
    result = await verifier(factory, index).verify(
        deletion_summary=DeletionSummary(), build_summary=BuildExecutionSummary()
    )
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "ORPHAN_RECORDS" in result.unsafe_reasons
    assert result.orphan_records >= 1


@pytest.mark.asyncio
async def test_missing_row_makes_the_view_unsafe(tmp_path: Path) -> None:
    factory, stored, repair, _, index, _ = published(tmp_path)
    await repair.reconcile_current(stored.document_id)
    index.rows.pop()
    result = await verifier(factory, index).verify(
        deletion_summary=DeletionSummary(), build_summary=BuildExecutionSummary()
    )
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "MISSING_RECORDS" in result.unsafe_reasons
    assert result.missing_records >= 1


@pytest.mark.asyncio
async def test_parent_row_is_never_searchable(tmp_path: Path) -> None:
    factory, stored, repair, _, index, _ = published(tmp_path)
    await repair.reconcile_current(stored.document_id)
    index.rows.append(
        MilvusRevisionProjection.model_validate(
            index.rows[0].model_dump()
            | {"chunk_id": "chunk_" + "d" * 64, "chunk_type": "TEXT_PARENT"}
        )
    )
    result = await verifier(factory, index).verify(
        deletion_summary=DeletionSummary(), build_summary=BuildExecutionSummary()
    )
    assert "TEXT_PARENT_SEARCHABLE" in result.unsafe_reasons


@pytest.mark.asyncio
async def test_failed_required_deletion_is_unsafe_even_when_rows_agree(
    tmp_path: Path,
) -> None:
    factory, stored, repair, _, index, _ = published(tmp_path)
    await repair.reconcile_current(stored.document_id)
    deletion = DeletionItem(
        document_id=derive_document_id("gone.txt"),
        outcome=DeletionOutcome.FAILED,
        failure_code="MILVUS_DELETE_FAILED",
    )
    result = await verifier(factory, index).verify(
        deletion_summary=DeletionSummary(items=(deletion,)),
        build_summary=BuildExecutionSummary(),
    )
    assert result.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "REQUIRED_DELETION_FAILED" in result.unsafe_reasons
    assert result.required_deletion_failures == 1
