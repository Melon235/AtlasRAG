"""Canonical local evidence and provenance contracts."""

from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath
from typing import Annotated, Self

from pydantic import Field, field_validator, model_validator

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.enums import (
    EvidenceType,
    LocalEvidenceStatus,
    LocalExecutionStatus,
)


def _validate_nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class SourceAnchor(FrozenModel):
    """Minimal source-specific location for canonical local evidence."""

    page_number: Annotated[int, Field(ge=1)] | None = None
    heading: str | None = None
    sheet_name: str | None = None
    cell_range: str | None = None

    @field_validator("heading", "sheet_name", "cell_range")
    @classmethod
    def _nonblank_if_present(cls, value: str | None) -> str | None:
        if value is not None:
            _validate_nonblank(value)
        return value

    @model_validator(mode="after")
    def _contains_an_anchor(self) -> Self:
        if all(
            value is None
            for value in (
                self.page_number,
                self.heading,
                self.sheet_name,
                self.cell_range,
            )
        ):
            raise ValueError("at least one source anchor value is required")
        return self


class LocalProvenance(FrozenModel):
    """Canonical file and structural location for local evidence."""

    file_name: str
    relative_source_path: str
    section_path: tuple[str, ...] = ()
    source_anchor: SourceAnchor | None = None

    _nonblank_required_strings = field_validator("file_name", "relative_source_path")(
        _validate_nonblank
    )

    @field_validator("relative_source_path")
    @classmethod
    def _relative_path_without_parent_traversal(cls, value: str) -> str:
        posix_path = PurePosixPath(value)
        windows_path = PureWindowsPath(value)
        if posix_path.is_absolute() or windows_path.anchor:
            raise ValueError("relative_source_path must be relative")
        if ".." in posix_path.parts or ".." in windows_path.parts:
            raise ValueError("relative_source_path must not contain '..'")
        return value

    @field_validator("section_path")
    @classmethod
    def _nonblank_section_path(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not part.strip() for part in value):
            raise ValueError("section_path values must not be blank")
        return value


class EvidenceRef(FrozenModel):
    """Retrieval-to-evidence bridge containing only canonical context identity."""

    evidence_type: EvidenceType
    context_id: str
    document_id: str
    revision_id: str

    _nonblank_ids = field_validator("context_id", "document_id", "revision_id")(
        _validate_nonblank
    )


class LocalEvidence(FrozenModel):
    """Complete canonical parent or table context for answer generation."""

    evidence_type: EvidenceType
    context_id: str
    document_id: str
    revision_id: str
    content: str
    provenance: LocalProvenance

    _nonblank_identity_and_content = field_validator(
        "context_id", "document_id", "revision_id", "content"
    )(_validate_nonblank)

    @property
    def identity(self) -> tuple[str, str, EvidenceType, str]:
        """Return the stable evidence identity without adding serialized state."""
        return (
            self.document_id,
            self.revision_id,
            self.evidence_type,
            self.context_id,
        )


class LocalEvidenceResult(FrozenModel):
    """Local execution outcome kept separate from evidence sufficiency."""

    execution_status: LocalExecutionStatus
    evidence_status: LocalEvidenceStatus | None = None
    selected_evidence: tuple[LocalEvidence, ...] = ()
    best_available_evidence: LocalEvidence | None = None

    @model_validator(mode="after")
    def _status_matches_evidence(self) -> Self:
        if self.execution_status is LocalExecutionStatus.UNAVAILABLE:
            if (
                self.evidence_status is not None
                or self.selected_evidence
                or self.best_available_evidence is not None
            ):
                raise ValueError("UNAVAILABLE carries no evidence data")
            return self

        if self.evidence_status is None:
            raise ValueError("available execution requires evidence_status")

        if self.evidence_status is LocalEvidenceStatus.SUFFICIENT:
            if not self.selected_evidence:
                raise ValueError("SUFFICIENT requires non-empty selected_evidence")
        elif self.evidence_status is LocalEvidenceStatus.INSUFFICIENT:
            if self.selected_evidence:
                raise ValueError("INSUFFICIENT requires empty selected_evidence")
        elif self.selected_evidence or self.best_available_evidence is not None:
            raise ValueError("AMBIGUOUS carries no selected or best evidence")

        return self
