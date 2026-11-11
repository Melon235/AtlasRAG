"""Immutable Stage 5 coordination DTOs; never persisted in PostgreSQL or State."""

from __future__ import annotations

import re
from enum import StrEnum
from pathlib import Path
from typing import Annotated, Self

from pydantic import (
    AfterValidator,
    BeforeValidator,
    Field,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from atlasrag.domain.base import (
    FrozenModel,
    validate_ordered_collection_input,
    validate_string_enum_input,
)
from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.source import (
    SourceFile,
    canonical_relative_source_path,
    derive_document_id,
    derive_revision_id,
    validate_document_id,
    validate_revision_id,
    validate_sha256_hex,
)
from atlasrag.repositories.postgres.records import FailedStage, IngestionStatus

_SAFE_CODE = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")


def _safe_code(value: str) -> str:
    if _SAFE_CODE.fullmatch(value) is None:
        raise ValueError("codes must be safe uppercase identifiers")
    return value


_SafeCode = Annotated[StrictStr, AfterValidator(_safe_code)]
_DocumentId = Annotated[StrictStr, AfterValidator(validate_document_id)]
_RevisionId = Annotated[StrictStr, AfterValidator(validate_revision_id)]
_SourceType = Annotated[SourceType, BeforeValidator(validate_string_enum_input)]
_IngestionStatus = Annotated[
    IngestionStatus, BeforeValidator(validate_string_enum_input)
]
_OrderedTuple = BeforeValidator(validate_ordered_collection_input)


def _optional_enum(value: object) -> object:
    if value is None:
        return None
    return validate_string_enum_input(value)


_FailedStage = Annotated[FailedStage | None, BeforeValidator(_optional_enum)]


def _hash(value: str) -> str:
    return validate_sha256_hex(value, "content hash")


def _fingerprint(value: str) -> str:
    return validate_sha256_hex(value, "pipeline fingerprint")


_Hash = Annotated[StrictStr, AfterValidator(_hash)]
_Fingerprint = Annotated[StrictStr, AfterValidator(_fingerprint)]


def _strict_path(value: object) -> object:
    if not isinstance(value, Path):
        raise ValueError("knowledge_directory must be a pathlib.Path")
    return value


class ScanEntry(FrozenModel):
    """One complete, hashed source observation."""

    source: SourceFile
    content_hash: _Hash


class ScanSnapshot(FrozenModel):
    """A complete successful directory observation in canonical path order."""

    entries: Annotated[tuple[ScanEntry, ...], _OrderedTuple] = ()

    @model_validator(mode="after")
    def _canonical_unique_entries(self) -> Self:
        paths = tuple(entry.source.relative_source_path for entry in self.entries)
        if paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise ValueError("scan entries must be canonically ordered and unique")
        return self

    def by_path(self) -> dict[str, ScanEntry]:
        return {entry.source.relative_source_path: entry for entry in self.entries}


class DatabaseDocumentSnapshot(FrozenModel):
    """Durable document fields required for deterministic sync planning."""

    document_id: _DocumentId
    relative_source_path: StrictStr
    file_name: StrictStr
    source_type: _SourceType
    current_revision_id: _RevisionId | None = None
    current_content_hash: _Hash | None = None
    current_pipeline_fingerprint: _Fingerprint | None = None
    building_revision_id: _RevisionId | None = None
    building_content_hash: _Hash | None = None
    building_pipeline_fingerprint: _Fingerprint | None = None
    ingestion_status: _IngestionStatus
    failed_stage: _FailedStage = None

    _relative_path = field_validator("relative_source_path")(
        canonical_relative_source_path
    )

    @model_validator(mode="after")
    def _ownership_and_failure_consistency(self) -> Self:
        if self.document_id != derive_document_id(self.relative_source_path):
            raise ValueError("document_id must match relative_source_path")
        for triple in (
            (
                self.current_revision_id,
                self.current_content_hash,
                self.current_pipeline_fingerprint,
            ),
            (
                self.building_revision_id,
                self.building_content_hash,
                self.building_pipeline_fingerprint,
            ),
        ):
            if any(value is None for value in triple) and not all(
                value is None for value in triple
            ):
                raise ValueError("revision triples must be complete or absent")
        if (
            self.current_revision_id is not None
            and self.current_revision_id == self.building_revision_id
        ):
            raise ValueError("current and building revisions must differ")
        if (self.ingestion_status is IngestionStatus.FAILED) != (
            self.failed_stage is not None
        ):
            raise ValueError("failed_stage must be present exactly for FAILED")
        if self.ingestion_status is IngestionStatus.READY:
            if (
                self.current_revision_id is None
                or self.building_revision_id is not None
            ):
                raise ValueError("READY requires current and forbids building")
        if (
            self.ingestion_status
            in {
                IngestionStatus.PARSING,
                IngestionStatus.PARSED,
                IngestionStatus.CHUNKING,
                IngestionStatus.CHUNKED,
                IngestionStatus.INDEXING,
            }
            and self.building_revision_id is None
        ):
            raise ValueError("building lifecycle states require a building triple")
        return self

    @property
    def has_current(self) -> bool:
        return self.current_revision_id is not None


class CanonicalDatabaseSnapshot(FrozenModel):
    """All durable documents in canonical relative-path order."""

    documents: Annotated[tuple[DatabaseDocumentSnapshot, ...], _OrderedTuple] = ()

    @model_validator(mode="after")
    def _unique_and_sorted(self) -> Self:
        paths = tuple(document.relative_source_path for document in self.documents)
        ids = tuple(document.document_id for document in self.documents)
        if paths != tuple(sorted(paths)):
            raise ValueError("database documents must be sorted by source path")
        if len(set(paths)) != len(paths) or len(set(ids)) != len(ids):
            raise ValueError("database document identities must be unique")
        return self

    def by_path(self) -> dict[str, DatabaseDocumentSnapshot]:
        return {document.relative_source_path: document for document in self.documents}


class SyncReason(StrEnum):
    NEW_SOURCE = "NEW_SOURCE"
    CONTENT_CHANGED = "CONTENT_CHANGED"
    PIPELINE_CHANGED = "PIPELINE_CHANGED"
    RETRY_FAILED_BUILD = "RETRY_FAILED_BUILD"


class SyncActionKind(StrEnum):
    BUILD = "BUILD"
    DELETE = "DELETE"
    UNCHANGED = "UNCHANGED"


class SyncAction(FrozenModel):
    """One deterministic plan action for exactly one canonical source identity."""

    kind: SyncActionKind
    document_id: _DocumentId
    relative_source_path: StrictStr
    reason: SyncReason | None = None
    content_hash: _Hash | None = None
    revision_id: _RevisionId | None = None
    current_revision_id: _RevisionId | None = None

    _relative_path = field_validator("relative_source_path")(
        canonical_relative_source_path
    )

    @model_validator(mode="after")
    def _action_completeness(self) -> Self:
        if self.document_id != derive_document_id(self.relative_source_path):
            raise ValueError("action document_id must match its source path")
        if self.kind is SyncActionKind.BUILD:
            if (
                self.reason is None
                or self.content_hash is None
                or self.revision_id is None
            ):
                raise ValueError("BUILD requires reason, content hash, and revision")
            if self.current_revision_id is not None:
                raise ValueError("BUILD must not carry a current revision")
        else:
            if (
                self.reason is not None
                or self.content_hash is not None
                or self.revision_id is not None
            ):
                raise ValueError("DELETE/UNCHANGED carry no build payload")
            if self.kind is SyncActionKind.UNCHANGED and (
                self.current_revision_id is None
            ):
                raise ValueError("UNCHANGED requires the current revision")
        return self


class SyncPlan(FrozenModel):
    """Immutable, canonically ordered plan for one complete scan snapshot."""

    pipeline_fingerprint: _Fingerprint
    actions: Annotated[tuple[SyncAction, ...], _OrderedTuple] = ()

    @model_validator(mode="after")
    def _canonical_order_and_revisions(self) -> Self:
        kind_order = {
            SyncActionKind.BUILD: 0,
            SyncActionKind.DELETE: 1,
            SyncActionKind.UNCHANGED: 2,
        }
        expected = tuple(
            sorted(
                self.actions,
                key=lambda action: (
                    kind_order[action.kind],
                    action.relative_source_path,
                ),
            )
        )
        if self.actions != expected:
            raise ValueError("plan actions must be canonically ordered")
        documents = tuple(action.document_id for action in self.actions)
        if len(set(documents)) != len(documents):
            raise ValueError("plan may contain one action per document")
        for action in self.actions:
            if action.kind is SyncActionKind.BUILD:
                assert action.content_hash is not None
                assert action.revision_id is not None
                if action.revision_id != derive_revision_id(
                    action.document_id,
                    action.content_hash,
                    self.pipeline_fingerprint,
                ):
                    raise ValueError("BUILD revision must match plan fingerprint")
        return self

    @property
    def builds(self) -> tuple[SyncAction, ...]:
        return tuple(
            action for action in self.actions if action.kind is SyncActionKind.BUILD
        )

    @property
    def deletions(self) -> tuple[SyncAction, ...]:
        return tuple(
            action for action in self.actions if action.kind is SyncActionKind.DELETE
        )

    @property
    def unchanged(self) -> tuple[SyncAction, ...]:
        return tuple(
            action for action in self.actions if action.kind is SyncActionKind.UNCHANGED
        )

    @property
    def is_empty(self) -> bool:
        return not self.actions


class RecoveryBoundary(StrEnum):
    PARSED = "PARSED"
    CHUNKED = "CHUNKED"


class RecoveryTarget(FrozenModel):
    """One durable interrupted candidate that can continue without reparse."""

    document_id: _DocumentId
    revision_id: _RevisionId
    relative_source_path: StrictStr
    boundary: RecoveryBoundary
    content_hash: _Hash
    pipeline_fingerprint: _Fingerprint

    _relative_path = field_validator("relative_source_path")(
        canonical_relative_source_path
    )

    @model_validator(mode="after")
    def _identity_matches(self) -> Self:
        if self.document_id != derive_document_id(self.relative_source_path):
            raise ValueError("recovery document identity must match its source path")
        if self.revision_id != derive_revision_id(
            self.document_id, self.content_hash, self.pipeline_fingerprint
        ):
            raise ValueError("recovery revision must match persisted candidate facts")
        return self


class ReconciliationOutcome(StrEnum):
    CLEAN = "CLEAN"
    REPAIRED = "REPAIRED"
    BLOCKED = "BLOCKED"


class ReconciliationResult(FrozenModel):
    outcome: ReconciliationOutcome
    recovery_targets: Annotated[tuple[RecoveryTarget, ...], _OrderedTuple] = ()
    repaired_document_ids: Annotated[tuple[_DocumentId, ...], _OrderedTuple] = ()
    blocked_reasons: Annotated[tuple[_SafeCode, ...], _OrderedTuple] = ()
    checked_documents: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _outcome_consistency(self) -> Self:
        target_ids = tuple(target.document_id for target in self.recovery_targets)
        if target_ids != tuple(sorted(target_ids)) or len(set(target_ids)) != len(
            target_ids
        ):
            raise ValueError("recovery targets must be sorted and unique")
        if self.repaired_document_ids != tuple(sorted(self.repaired_document_ids)):
            raise ValueError("repaired document IDs must be sorted")
        if len(set(self.repaired_document_ids)) != len(self.repaired_document_ids):
            raise ValueError("repaired document IDs must be unique")
        if self.outcome is ReconciliationOutcome.BLOCKED:
            if not self.blocked_reasons or self.recovery_targets:
                raise ValueError("BLOCKED requires reasons and forbids recovery work")
        elif self.blocked_reasons:
            raise ValueError("non-blocked reconciliation carries no blocked reasons")
        return self


class BuildOutcome(StrEnum):
    READY = "READY"
    FAILED = "FAILED"
    SOURCE_MUTATED = "SOURCE_MUTATED"
    TECHNICAL_FAILURE = "TECHNICAL_FAILURE"


class BuildMode(StrEnum):
    FULL = "FULL"
    RESUME_PARSED = "RESUME_PARSED"
    RESUME_CHUNKED = "RESUME_CHUNKED"


class ResidueCleanup(StrEnum):
    NOT_REQUIRED = "NOT_REQUIRED"
    CLEANED = "CLEANED"
    FAILED = "FAILED"


class BuildExecutionItem(FrozenModel):
    document_id: _DocumentId
    revision_id: _RevisionId
    mode: BuildMode
    outcome: BuildOutcome
    failed_stage: FailedStage | None = None
    reason_code: _SafeCode | None = None
    technical_code: _SafeCode | None = None
    residue_cleanup: ResidueCleanup = ResidueCleanup.NOT_REQUIRED

    @model_validator(mode="after")
    def _outcome_fields(self) -> Self:
        if self.outcome is BuildOutcome.FAILED:
            if self.failed_stage is None or self.reason_code is None:
                raise ValueError("FAILED requires stage and reason")
            if self.technical_code is not None:
                raise ValueError("FAILED does not carry a technical code")
        elif self.outcome is BuildOutcome.TECHNICAL_FAILURE:
            if self.technical_code is None:
                raise ValueError("TECHNICAL_FAILURE requires a safe code")
            if self.failed_stage is not None or self.reason_code is not None:
                raise ValueError("technical failure is not a business failure")
        elif (
            self.failed_stage is not None
            or self.reason_code is not None
            or self.technical_code is not None
        ):
            raise ValueError("non-failure outcomes carry no failure metadata")
        if (
            self.outcome in {BuildOutcome.FAILED, BuildOutcome.TECHNICAL_FAILURE}
            and self.residue_cleanup is ResidueCleanup.NOT_REQUIRED
        ):
            raise ValueError("failed builds must report residue cleanup")
        return self


class BuildExecutionSummary(FrozenModel):
    items: Annotated[tuple[BuildExecutionItem, ...], _OrderedTuple] = ()

    @model_validator(mode="after")
    def _sorted_unique(self) -> Self:
        ids = tuple(item.document_id for item in self.items)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("build summary items must be sorted and unique")
        return self

    @property
    def ready(self) -> tuple[BuildExecutionItem, ...]:
        return tuple(item for item in self.items if item.outcome is BuildOutcome.READY)

    @property
    def failed(self) -> tuple[BuildExecutionItem, ...]:
        return tuple(item for item in self.items if item.outcome is BuildOutcome.FAILED)

    @property
    def technical(self) -> tuple[BuildExecutionItem, ...]:
        return tuple(
            item
            for item in self.items
            if item.outcome is BuildOutcome.TECHNICAL_FAILURE
        )

    @property
    def source_mutated(self) -> tuple[BuildExecutionItem, ...]:
        return tuple(
            item for item in self.items if item.outcome is BuildOutcome.SOURCE_MUTATED
        )

    @property
    def unsafe_residue(self) -> bool:
        return any(item.residue_cleanup is ResidueCleanup.FAILED for item in self.items)


class DeletionOutcome(StrEnum):
    DELETED = "DELETED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"


class DeletionItem(FrozenModel):
    document_id: _DocumentId
    outcome: DeletionOutcome
    failure_code: _SafeCode | None = None
    milvus_deleted: bool = False
    postgres_deleted: bool = False

    @model_validator(mode="after")
    def _outcome_fields(self) -> Self:
        if self.outcome is DeletionOutcome.DELETED:
            if (
                self.failure_code is not None
                or not self.postgres_deleted
                or not self.milvus_deleted
            ):
                raise ValueError(
                    "successful deletion has no failure code and removes both stores"
                )
        elif self.outcome is DeletionOutcome.FAILED:
            if self.failure_code is None or self.postgres_deleted:
                raise ValueError("failed deletion requires a code and no PG delete")
        elif self.failure_code is not None:
            raise ValueError("skipped deletion carries no failure code")
        return self


class DeletionSummary(FrozenModel):
    items: Annotated[tuple[DeletionItem, ...], _OrderedTuple] = ()

    @model_validator(mode="after")
    def _sorted_unique(self) -> Self:
        ids = tuple(item.document_id for item in self.items)
        if ids != tuple(sorted(ids)) or len(set(ids)) != len(ids):
            raise ValueError("deletion summary items must be sorted and unique")
        return self

    @property
    def deleted(self) -> tuple[DeletionItem, ...]:
        return tuple(
            item for item in self.items if item.outcome is DeletionOutcome.DELETED
        )

    @property
    def failed(self) -> tuple[DeletionItem, ...]:
        return tuple(
            item for item in self.items if item.outcome is DeletionOutcome.FAILED
        )


class ServingSafety(StrEnum):
    SAFE_TO_SERVE = "SAFE_TO_SERVE"
    UNSAFE_TO_SERVE = "UNSAFE_TO_SERVE"


class SyncOutcome(StrEnum):
    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"


class GlobalSafetyResult(FrozenModel):
    serving_safety: ServingSafety
    unsafe_reasons: Annotated[tuple[_SafeCode, ...], _OrderedTuple] = ()
    verified_documents: Annotated[StrictInt, Field(ge=0)] = 0
    verified_records: Annotated[StrictInt, Field(ge=0)] = 0
    orphan_records: Annotated[StrictInt, Field(ge=0)] = 0
    missing_records: Annotated[StrictInt, Field(ge=0)] = 0
    mismatched_records: Annotated[StrictInt, Field(ge=0)] = 0
    required_deletion_failures: Annotated[StrictInt, Field(ge=0)] = 0

    @model_validator(mode="after")
    def _safety_consistency(self) -> Self:
        if self.unsafe_reasons != tuple(sorted(set(self.unsafe_reasons))):
            raise ValueError("unsafe reasons must be sorted and unique")
        if self.serving_safety is ServingSafety.SAFE_TO_SERVE:
            if self.unsafe_reasons:
                raise ValueError("safe verdict carries no unsafe reasons")
        elif not self.unsafe_reasons:
            raise ValueError("unsafe verdict requires at least one reason")
        return self


class SyncResult(FrozenModel):
    outcome: SyncOutcome
    serving_safety: ServingSafety
    checked_documents: Annotated[StrictInt, Field(ge=0)] = 0
    planned_builds: Annotated[StrictInt, Field(ge=0)] = 0
    planned_deletions: Annotated[StrictInt, Field(ge=0)] = 0
    ready_builds: Annotated[StrictInt, Field(ge=0)] = 0
    failed_builds: Annotated[StrictInt, Field(ge=0)] = 0
    source_mutated_builds: Annotated[StrictInt, Field(ge=0)] = 0
    technical_build_failures: Annotated[StrictInt, Field(ge=0)] = 0
    deleted_documents: Annotated[StrictInt, Field(ge=0)] = 0
    failed_deletions: Annotated[StrictInt, Field(ge=0)] = 0
    verified_documents: Annotated[StrictInt, Field(ge=0)] = 0
    verified_records: Annotated[StrictInt, Field(ge=0)] = 0
    unsafe_reasons: Annotated[tuple[_SafeCode, ...], _OrderedTuple] = ()
    blocked_code: _SafeCode | None = None

    @model_validator(mode="after")
    def _outcome_consistency(self) -> Self:
        if self.unsafe_reasons != tuple(sorted(set(self.unsafe_reasons))):
            raise ValueError("unsafe reasons must be sorted and unique")
        if self.serving_safety is ServingSafety.SAFE_TO_SERVE and self.unsafe_reasons:
            raise ValueError("safe result carries no unsafe reasons")
        if self.outcome is SyncOutcome.SUCCESS:
            if (
                self.blocked_code is not None
                or self.failed_builds
                or self.technical_build_failures
                or self.failed_deletions
                or self.source_mutated_builds
            ):
                raise ValueError("SUCCESS cannot carry failures")
        if self.outcome is SyncOutcome.FAILED and self.blocked_code is None:
            if not (
                self.technical_build_failures
                or self.failed_deletions
                or self.failed_builds
                or self.serving_safety is ServingSafety.UNSAFE_TO_SERVE
            ):
                raise ValueError("FAILED requires a concrete failure or unsafe verdict")
        return self


class SyncRequest(FrozenModel):
    """Dependency-free request entering the Stage 5 graph."""

    knowledge_directory: Annotated[Path, BeforeValidator(_strict_path)]
    expected_pipeline_fingerprint: _Fingerprint
