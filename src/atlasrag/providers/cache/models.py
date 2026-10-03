"""Typed immutable payloads stored in disposable Redis caches."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BeforeValidator, StrictStr, field_validator

from atlasrag.domain.base import FrozenModel, validate_string_enum_input
from atlasrag.domain.enums import EvidenceType
from atlasrag.domain.evidence import LocalProvenance
from atlasrag.domain.results import FinalResponse

_EvidenceTypeInput = Annotated[
    EvidenceType, BeforeValidator(validate_string_enum_input)
]


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("must not be blank")
    return value


class CachedParentContext(FrozenModel):
    """Complete canonical parent/table context for cache-aside reconstruction."""

    evidence_type: _EvidenceTypeInput
    context_id: StrictStr
    document_id: StrictStr
    revision_id: StrictStr
    content: StrictStr
    provenance: LocalProvenance

    _nonblank_identity_and_content = field_validator(
        "context_id", "document_id", "revision_id", "content"
    )(_nonblank)


class CachedQueryResult(FrozenModel):
    """One complete validated final response in the Session Query Cache."""

    schema_version: Literal[1] = 1
    created_at: AwareDatetime
    final_response: FinalResponse
