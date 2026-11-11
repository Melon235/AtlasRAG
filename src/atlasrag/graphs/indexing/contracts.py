"""Immutable internal Part I build boundary, independent of runtime resources."""

from typing import Literal, Self

from pydantic import StrictStr, field_validator, model_validator

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.ingestion.source import (
    SourceFile,
    validate_document_id,
    validate_revision_id,
    validate_sha256_hex,
)
from atlasrag.repositories.postgres.records import FailedStage
from atlasrag.services.indexing.ports import BuildFailureCode


class BuildRequest(FrozenModel):
    source: SourceFile
    expected_pipeline_fingerprint: StrictStr
    expected_revision_id: StrictStr | None = None

    @field_validator("expected_pipeline_fingerprint")
    @classmethod
    def _fingerprint(cls, value: str) -> str:
        return validate_sha256_hex(value, "pipeline fingerprint")

    @field_validator("expected_revision_id")
    @classmethod
    def _revision(cls, value: str | None) -> str | None:
        return None if value is None else validate_revision_id(value)


class BuildResult(FrozenModel):
    document_id: StrictStr
    revision_id: StrictStr
    outcome: Literal["READY", "FAILED", "SOURCE_MUTATED"]
    failed_stage: FailedStage | None = None
    reason_code: BuildFailureCode | None = None

    _document_id = field_validator("document_id")(validate_document_id)
    _revision_id = field_validator("revision_id")(validate_revision_id)

    @model_validator(mode="after")
    def _failure(self) -> Self:
        if self.outcome == "FAILED":
            if self.failed_stage is None or self.reason_code is None:
                raise ValueError("FAILED requires classified stage and safe reason")
            if self.failed_stage.value != self.reason_code.value:
                raise ValueError("failure stage and reason must agree")
        elif self.failed_stage is not None or self.reason_code is not None:
            raise ValueError("only FAILED carries failure metadata")
        return self
