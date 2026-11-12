"""Shared retrieval scope and safe Milvus filter compilation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Self

from pydantic import StrictStr, field_validator, model_validator

from atlasrag.domain.base import FrozenModel
from atlasrag.domain.enums import RetrievalChunkType
from atlasrag.domain.errors import CanonicalDataError
from atlasrag.domain.ingestion.source import (
    derive_revision_id,
    validate_document_id,
    validate_revision_id,
)
from atlasrag.domain.requests import LocalRetrievalRequest, ResolvedRetrievalScope
from atlasrag.repositories.postgres.records import StoredDocument
from atlasrag.services.ingestion.mapping import load_document_parse_metadata_envelope
from atlasrag.services.local_evidence.ports import LocalEvidenceUnitOfWorkFactory


class PublishedRevision(FrozenModel):
    """One validated published-current document/revision pair."""

    document_id: StrictStr
    revision_id: StrictStr
    file_name: StrictStr
    source_type: StrictStr

    _document_id = field_validator("document_id")(validate_document_id)
    _revision_id = field_validator("revision_id")(validate_revision_id)


class PublishedRevisionSet(FrozenModel):
    revisions: tuple[PublishedRevision, ...] = ()

    @field_validator("revisions")
    @classmethod
    def _canonical(
        cls, value: tuple[PublishedRevision, ...]
    ) -> tuple[PublishedRevision, ...]:
        ordered = tuple(sorted(value, key=lambda item: item.document_id))
        if len({item.document_id for item in ordered}) != len(ordered):
            raise ValueError("published revision documents must be unique")
        return ordered

    @property
    def pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple((item.document_id, item.revision_id) for item in self.revisions)

    @model_validator(mode="after")
    def _unique_revisions(self) -> Self:
        if len({item.revision_id for item in self.revisions}) != len(self.revisions):
            raise ValueError("published revisions must be unique")
        return self

    def contains(self, document_id: str, revision_id: str) -> bool:
        return (document_id, revision_id) in set(self.pairs)


class PublishedRevisionSnapshotService:
    """Read the published-current revision manifest from canonical PostgreSQL."""

    def __init__(self, uow_factory: LocalEvidenceUnitOfWorkFactory) -> None:
        self._uow_factory = uow_factory

    async def load(self) -> PublishedRevisionSet:
        async with self._uow_factory() as unit:
            documents = await unit.documents.list()
        revisions: list[PublishedRevision] = []
        for stored in documents:
            document = StoredDocument.model_validate(stored)
            if document.current_revision_id is None:
                continue
            try:
                envelope = load_document_parse_metadata_envelope(document)
                slot = envelope.current
                if (
                    slot is None
                    or document.current_revision_id != slot.revision_id
                    or document.current_content_hash is None
                    or document.current_pipeline_fingerprint is None
                    or derive_revision_id(
                        document.document_id,
                        document.current_content_hash,
                        document.current_pipeline_fingerprint,
                    )
                    != document.current_revision_id
                ):
                    raise CanonicalDataError
                revisions.append(
                    PublishedRevision(
                        document_id=document.document_id,
                        revision_id=document.current_revision_id,
                        file_name=document.file_name,
                        source_type=document.source_type.value,
                    )
                )
            except (CanonicalDataError, ValueError):
                raise CanonicalDataError from None
        return PublishedRevisionSet(revisions=tuple(revisions))


class RetrievalScopeBuilder:
    """Build the one deterministic scope shared by Dense and BM25."""

    def build(self, request: LocalRetrievalRequest) -> ResolvedRetrievalScope:
        request = LocalRetrievalRequest.model_validate(request)
        filters = request.filters
        return ResolvedRetrievalScope(
            file_name=None if filters is None else filters.file_name,
            source_type=None if filters is None else filters.source_type,
            sheet_name=None if filters is None else filters.sheet_name,
            chunk_types=(
                RetrievalChunkType.TEXT_CHILD,
                RetrievalChunkType.TABLE,
            ),
        )


@dataclass(frozen=True)
class CompiledRetrievalFilter:
    expression: str
    empty: bool
    published_revisions: int


def _literal(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("filter value must be nonempty text without NUL")
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class RetrievalFilterCompiler:
    """Compile only the closed allowlist into a safe Milvus expression."""

    def compile(
        self,
        scope: ResolvedRetrievalScope,
        published: PublishedRevisionSet,
    ) -> CompiledRetrievalFilter:
        scope = ResolvedRetrievalScope.model_validate(scope)
        published = PublishedRevisionSet.model_validate(published)
        if not published.revisions:
            return CompiledRetrievalFilter("", True, 0)
        clauses: list[str] = []
        if scope.file_name is not None:
            clauses.append(f"file_name == {_literal(scope.file_name)}")
        if scope.source_type is not None:
            clauses.append(f"source_type == {_literal(scope.source_type.value)}")
        if scope.sheet_name is not None:
            clauses.append(
                'structured_metadata["provenance"]["sheet_name"] == '
                f"{_literal(scope.sheet_name)}"
            )
        chunk_types = ", ".join(_literal(item.value) for item in scope.chunk_types)
        clauses.append(f"chunk_type in [{chunk_types}]")
        published_clause = " or ".join(
            "(document_id == "
            f"{_literal(document_id)} and revision_id == {_literal(revision_id)})"
            for document_id, revision_id in published.pairs
        )
        clauses.append(f"({published_clause})")
        return CompiledRetrievalFilter(
            " and ".join(clauses), False, len(published.revisions)
        )
