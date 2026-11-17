"""Benchmark-only contracts; no production dependency on these types."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, StrictStr, field_validator


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class Modality(StrEnum):
    TEXT = "text"
    TEXT_TABLE = "text-table"
    TEXT_IMAGE = "text-image"
    TEXT_TABLE_IMAGE = "text-table-image"


SUPPORTED_MODALITIES = frozenset({Modality.TEXT, Modality.TEXT_TABLE})


class CorpusProfile(StrEnum):
    FULL_CORPUS = "FULL_CORPUS"
    NEGATIVE_ONLY_CORPUS = "NEGATIVE_ONLY_CORPUS"
    PILOT = "PILOT"


class AlignmentConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    UNRESOLVED = "UNRESOLVED"


class PartitionStatus(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    CONFLICT = "CONFLICT"


class RunValidity(StrEnum):
    VALID = "VALID"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"


class BenchmarkDocument(_Frozen):
    doc_id: StrictStr
    source_path: StrictStr
    source_url: StrictStr | None = None

    _nonblank = field_validator("doc_id", "source_path")(
        lambda value: value if value.strip() else (_ for _ in ()).throw(ValueError())
    )


class BenchmarkQuery(_Frozen):
    query_id: StrictStr
    text: StrictStr
    modality: Modality
    query_type: StrictStr
    reference_answer: StrictStr | None = None

    _nonblank = field_validator("query_id", "text", "query_type")(
        lambda value: value if value.strip() else (_ for _ in ()).throw(ValueError())
    )


class BenchmarkQrel(_Frozen):
    query_id: StrictStr
    doc_id: StrictStr
    section_id: int = Field(ge=0)
    relevance: int = Field(default=1, ge=0)

    @field_validator("query_id", "doc_id")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("must not be blank")
        return value


class DocumentPartition(_Frozen):
    """Observed document references are not automatically hard negatives."""

    qrel_referenced_ids: tuple[StrictStr, ...]
    unreferenced_ids: tuple[StrictStr, ...]
    verified_hard_negative_ids: tuple[StrictStr, ...] | None = None
    status: PartitionStatus
    evidence: StrictStr


class GoldSection(_Frozen):
    gold_section_id: StrictStr
    doc_id: StrictStr
    content: StrictStr


class CanonicalEvidence(_Frozen):
    evidence_id: StrictStr
    document_id: StrictStr
    evidence_type: StrictStr
    content: StrictStr
    source_anchor: StrictStr | None = None


class GoldEvidenceAlignment(_Frozen):
    query_id: StrictStr
    gold_section_id: StrictStr
    gold_document_id: StrictStr
    aligned_evidence_ids: tuple[StrictStr, ...] = ()
    confidence: AlignmentConfidence
    score: float = Field(ge=0.0, le=1.0)


class RankedUnit(_Frozen):
    identity: StrictStr
    document_id: StrictStr
    rank: int = Field(ge=1)
    score: float | None = None


class SnapshotFile(_Frozen):
    relative_path: StrictStr
    sha256: StrictStr = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)


class SnapshotManifest(_Frozen):
    schema_version: int = 1
    dataset_name: StrictStr = "vectara/open_ragbench"
    dataset_revision: StrictStr
    license: StrictStr
    source_url: StrictStr
    files: tuple[SnapshotFile, ...]
    total_documents: int = Field(ge=0)
    corpus_document_ids: tuple[StrictStr, ...] = ()
    total_queries: int = Field(ge=0)
    supported_queries: int = Field(default=0, ge=0)
    modality_counts: dict[StrictStr, int] = Field(default_factory=dict)

    @field_validator("files")
    @classmethod
    def _unique_files(cls, value: tuple[SnapshotFile, ...]) -> tuple[SnapshotFile, ...]:
        paths = [item.relative_path for item in value]
        if len(paths) != len(set(paths)):
            raise ValueError("duplicate snapshot file path")
        return value
