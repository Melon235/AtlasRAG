"""Deterministic regressions for F-R10-02-01 last-good R1 verification alongside R2."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from ar3_support import (
    add_orphan_projection,
    build_environment,
    empty_document_without_current,
    rebuild_environment,
    start_runtime,
)
from stage3.test_candidate_services import _document

from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    SourceFile,
    derive_revision_id,
)
from atlasrag.providers.index.projection import MilvusRevisionProjection
from atlasrag.repositories.postgres.records import (
    FailedStage,
    IngestionStatus,
    StoredDocument,
)
from atlasrag.services.ingestion.chunking import ChunkingProfile, build_chunks
from atlasrag.services.ingestion.identity import content_hash
from atlasrag.services.ingestion.mapping import (
    build_document_parse_metadata_slot,
    load_document_parse_metadata_envelope,
    map_chunks_to_stored,
    map_elements_to_stored,
    replace_document_parse_metadata_slot,
    serialize_document_parse_metadata_envelope,
)
from atlasrag.services.sync.models import (
    BuildExecutionItem,
    BuildExecutionSummary,
    BuildMode,
    BuildOutcome,
    DeletionSummary,
    ResidueCleanup,
    ServingSafety,
)

UPDATED_BYTES = b"Updated v2 source content for candidate build R2.\n"


def _seed_candidate_r2(
    root: Path,
    environment: object,
    *,
    status: IngestionStatus,
    failed_stage: FailedStage | None = None,
    corrupt_current: bool = False,
) -> tuple[StoredDocument, str]:
    from ar3_support import AR3Environment

    env = environment
    assert isinstance(env, AR3Environment)
    assert env.stored is not None and env.document is not None

    source_file = root / env.stored.relative_source_path
    source_file.write_bytes(UPDATED_BYTES)
    source = SourceFile(
        knowledge_directory=root,
        relative_source_path=env.stored.relative_source_path,
        file_name=env.stored.file_name,
        source_type=SourceType(env.stored.source_type.value),
        absolute_path=source_file,
    )
    r2_hash = content_hash(source)
    r2_id = derive_revision_id(env.stored.document_id, r2_hash, env.pipeline)
    r2_context = CandidateBuildContext(
        source=source,
        document_id=env.stored.document_id,
        content_hash=r2_hash,
        pipeline_fingerprint=env.pipeline,
        revision_id=r2_id,
    )
    r2_doc = _document(
        r2_context,
        "Updated v2 source content for candidate build R2.",
        revision_id=r2_id,
    )
    envelope = load_document_parse_metadata_envelope(env.stored)
    now = datetime(2026, 11, 26, 12, 0, tzinfo=UTC)
    if status is IngestionStatus.FAILED and failed_stage is FailedStage.PARSE:
        envelope_with_r2 = envelope
    else:
        envelope_with_r2 = replace_document_parse_metadata_slot(
            envelope,
            slot_name="building",
            slot=build_document_parse_metadata_slot(r2_doc),
        )

    stored_r2 = env.stored.model_copy(
        update={
            "observed_content_hash": r2_hash,
            "building_revision_id": r2_id,
            "building_content_hash": r2_hash,
            "building_pipeline_fingerprint": env.pipeline,
            "ingestion_status": status,
            "failed_stage": failed_stage,
            "parse_metadata": serialize_document_parse_metadata_envelope(
                envelope_with_r2
            ),
            "current_content_hash": (
                "c" * 64 if corrupt_current else env.stored.current_content_hash
            ),
            "updated_at": now,
        }
    )
    env.factory.database.documents[stored_r2.document_id] = stored_r2
    r2_scope = (stored_r2.document_id, r2_id)
    if not (status is IngestionStatus.FAILED and failed_stage is FailedStage.PARSE):
        env.factory.database.elements[r2_scope] = map_elements_to_stored(
            r2_doc.elements, created_at=now
        )
    if status in {IngestionStatus.CHUNKED, IngestionStatus.INDEXING} or (
        status is IngestionStatus.FAILED and failed_stage is FailedStage.INDEX_VERIFY
    ):
        profile = ChunkingProfile()
        env.factory.database.chunks[r2_scope] = map_chunks_to_stored(
            build_chunks(r2_doc, profile=profile),
            chunker_behavior_version=env.runtime.part_i.chunker_behavior_version,
            profile_fingerprint=profile.profile_fingerprint,
            created_at=now,
        )
    env.stored = stored_r2
    return stored_r2, r2_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "failed_stage"),
    [
        (IngestionStatus.FAILED, FailedStage.PARSE),
        (IngestionStatus.FAILED, FailedStage.INDEX_VERIFY),
        (IngestionStatus.PARSED, None),
        (IngestionStatus.CHUNKING, None),
        (IngestionStatus.CHUNKED, None),
        (IngestionStatus.INDEXING, None),
    ],
)
async def test_f_r10_02_01_valid_published_r1_survives_failed_or_interrupted_r2(
    tmp_path: Path,
    status: IngestionStatus,
    failed_stage: FailedStage | None,
) -> None:
    env = await build_environment(tmp_path)
    _seed_candidate_r2(tmp_path, env, status=status, failed_stage=failed_stage)

    safety = await env.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert safety.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert safety.unsafe_reasons == ()
    assert safety.verified_documents == 1
    assert safety.verified_records == len(env.rows)

    fresh = rebuild_environment(env)
    runtime = await start_runtime(fresh)
    assert runtime.gate.mode.value == "SERVING"
    assert await runtime.coordinator.readiness() is True
    assert fresh.sync_calls == 0
    await runtime.coordinator.shutdown()


@pytest.mark.asyncio
async def test_f_r10_02_01_ready_r1_without_candidate_and_no_published_revision(
    tmp_path: Path,
) -> None:
    env = await build_environment(tmp_path)
    safety_r1 = await env.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert safety_r1.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert safety_r1.verified_documents == 1

    empty_document_without_current(env)
    safety_none = await env.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert safety_none.serving_safety is ServingSafety.SAFE_TO_SERVE
    assert safety_none.verified_documents == 0


@pytest.mark.asyncio
async def test_f_r10_02_01_blocks_on_invalid_r1_or_corrupt_milvus_or_r2_residue(
    tmp_path: Path,
) -> None:
    # 1) Invalid published R1 -> CURRENT_CANONICAL_INVALID + BLOCKED
    env_bad_r1 = await build_environment(tmp_path / "bad_r1")
    _seed_candidate_r2(
        tmp_path / "bad_r1",
        env_bad_r1,
        status=IngestionStatus.FAILED,
        failed_stage=FailedStage.INDEX_VERIFY,
        corrupt_current=True,
    )
    res_bad = await env_bad_r1.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert res_bad.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "CURRENT_CANONICAL_INVALID" in res_bad.unsafe_reasons

    # 2) Missing Milvus row alongside valid R1 + FAILED R2 -> MISSING_RECORDS
    env_missing = await build_environment(tmp_path / "missing")
    _seed_candidate_r2(
        tmp_path / "missing",
        env_missing,
        status=IngestionStatus.FAILED,
        failed_stage=FailedStage.PARSE,
    )
    removed = env_missing.index.rows.pop()
    res_missing = await env_missing.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert res_missing.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "EXACT_SET_MISMATCH" in res_missing.unsafe_reasons or (
        "MISSING_RECORDS" in res_missing.unsafe_reasons
    )

    # 3) Duplicate Milvus row alongside valid R1 + PARSED R2 -> DUPLICATE_RECORDS
    env_missing.index.rows.append(removed)
    env_missing.index.rows.append(env_missing.index.rows[0].model_copy())
    res_dup = await env_missing.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert res_dup.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "DUPLICATE_RECORDS" in res_dup.unsafe_reasons

    # 4) Mismatched Milvus projection alongside valid R1 + CHUNKED R2 -> PROJECTION_MISMATCH
    env_mismatch = await build_environment(tmp_path / "mismatch")
    _seed_candidate_r2(
        tmp_path / "mismatch",
        env_mismatch,
        status=IngestionStatus.CHUNKED,
    )
    env_mismatch.index.rows[0] = MilvusRevisionProjection.model_validate(
        env_mismatch.index.rows[0].model_dump() | {"content": "tampered content"}
    )
    res_mismatch = await env_mismatch.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert res_mismatch.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "PROJECTION_MISMATCH" in res_mismatch.unsafe_reasons

    # 5) Searchable candidate R2 orphan residue or unsafe_residue -> UNSAFE_TO_SERVE + BLOCKED
    env_residue = await build_environment(tmp_path / "residue")
    stored_res, r2_id = _seed_candidate_r2(
        tmp_path / "residue",
        env_residue,
        status=IngestionStatus.FAILED,
        failed_stage=FailedStage.INDEX_WRITE,
    )
    add_orphan_projection(
        env_residue, document_id=stored_res.document_id, revision_id=r2_id
    )
    res_orphan = await env_residue.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(),
    )
    assert res_orphan.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "ORPHAN_RECORDS" in res_orphan.unsafe_reasons

    env_residue.index.rows.pop()
    res_unsafe_flag = await env_residue.verification_service.verify(
        deletion_summary=DeletionSummary(),
        build_summary=BuildExecutionSummary(
            items=(
                BuildExecutionItem(
                    document_id=stored_res.document_id,
                    revision_id=r2_id,
                    mode=BuildMode.FULL,
                    outcome=BuildOutcome.FAILED,
                    failed_stage=FailedStage.INDEX_WRITE,
                    reason_code="INDEX_WRITE",
                    residue_cleanup=ResidueCleanup.FAILED,
                ),
            )
        ),
    )
    assert res_unsafe_flag.serving_safety is ServingSafety.UNSAFE_TO_SERVE
    assert "CANDIDATE_RESIDUE_UNSAFE" in res_unsafe_flag.unsafe_reasons

    # 6) Indexing residue cleaned up by startup reconciliation -> safe recovery to SERVING.
    env_recover = await build_environment(tmp_path / "recover")
    stored_rec, rec_r2 = _seed_candidate_r2(
        tmp_path / "recover",
        env_recover,
        status=IngestionStatus.INDEXING,
    )
    add_orphan_projection(
        env_recover, document_id=stored_rec.document_id, revision_id=rec_r2
    )
    runtime_rec = await start_runtime(rebuild_environment(env_recover))
    assert runtime_rec.gate.mode.value == "SERVING"
    assert [row.revision_id for row in env_recover.rows] == [
        stored_rec.current_revision_id
    ] * len(env_recover.rows)
    await runtime_rec.coordinator.shutdown()
