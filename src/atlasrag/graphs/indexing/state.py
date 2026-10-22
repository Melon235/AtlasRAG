"""Transient canonical business data only; dependencies and proof stay outside."""

from typing import TypedDict

from atlasrag.domain.ingestion.chunks import ChunkRecord
from atlasrag.domain.ingestion.document import DocumentContainer
from atlasrag.domain.ingestion.source import CandidateBuildContext
from atlasrag.graphs.indexing.contracts import BuildResult
from atlasrag.repositories.postgres.records import FailedStage
from atlasrag.services.indexing.ports import BuildFailureCode


class DocumentBuildInput(TypedDict):
    context: CandidateBuildContext


class DocumentBuildOutput(TypedDict):
    result: BuildResult


class DocumentBuildState(TypedDict, total=False):
    context: CandidateBuildContext
    document: DocumentContainer | None
    chunks: tuple[ChunkRecord, ...] | None
    source_mutated: bool
    failed_stage: FailedStage | None
    reason_code: BuildFailureCode | None
    result: BuildResult
