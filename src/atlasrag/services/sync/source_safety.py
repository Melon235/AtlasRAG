"""Conservative startup source-availability safety check."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from pydantic import Field, StrictInt

from atlasrag.domain.base import FrozenModel
from atlasrag.services.sync.models import SourceAvailability
from atlasrag.services.sync.ports import SyncUnitOfWorkFactory
from atlasrag.services.sync.reconciliation import StartupReconciliationService


class SourceSafetyFinding(FrozenModel):
    document_id: str
    relative_source_path: str
    current_revision_id: str
    availability: SourceAvailability


class SourceSafetyResult(FrozenModel):
    safe: bool
    findings: tuple[SourceSafetyFinding, ...] = ()
    checked_documents: Annotated[StrictInt, Field(ge=0)] = 0
    missing_documents: Annotated[StrictInt, Field(ge=0)] = 0
    unreadable_documents: Annotated[StrictInt, Field(ge=0)] = 0


class StartupSourceSafetyService:
    """Verify every published source is still present and readable."""

    def __init__(
        self,
        uow_factory: SyncUnitOfWorkFactory,
        *,
        knowledge_directory: Path,
        reconciliation: StartupReconciliationService,
    ) -> None:
        self._uow_factory = uow_factory
        self._knowledge_directory = knowledge_directory
        self._reconciliation = reconciliation

    def _validated_root(self) -> Path:
        root = self._knowledge_directory.resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
        # Reading one entry proves the directory is scannable without running a
        # full startup scan; an inaccessible root is a hard bootstrap failure.
        with os.scandir(root) as entries:
            next(entries, None)
        return root

    async def check(self) -> SourceSafetyResult:
        self._validated_root()
        async with self._uow_factory() as unit:
            documents = await unit.documents.list()
        findings: list[SourceSafetyFinding] = []
        checked = 0
        missing = 0
        unreadable = 0
        for document in documents:
            if document.current_revision_id is None:
                continue
            checked += 1
            availability, _ = self._reconciliation.classify_source(document)
            if availability is SourceAvailability.AVAILABLE:
                continue
            findings.append(
                SourceSafetyFinding(
                    document_id=document.document_id,
                    relative_source_path=document.relative_source_path,
                    current_revision_id=document.current_revision_id,
                    availability=availability,
                )
            )
            if availability is SourceAvailability.MISSING:
                missing += 1
            else:
                unreadable += 1
        return SourceSafetyResult(
            safe=not findings,
            findings=tuple(findings),
            checked_documents=checked,
            missing_documents=missing,
            unreadable_documents=unreadable,
        )
