"""Per-build parsers and shared resource fixtures using real persistence services."""

from pathlib import Path
from typing import Any

from stage3.test_candidate_services import NOW, _context, _document

from atlasrag.config.fingerprint import pipeline_fingerprint
from atlasrag.domain.ingestion.source import CandidateBuildContext, SourceFile
from atlasrag.providers.parsing.routing import ParserRouter
from atlasrag.services.ingestion.identity import build_candidate_context
from stage4.candidate_fakes import Embedding, Index, IndexFactory, config
from stage4.test_document_build import module
from stage4.test_publication import PublicationFactory


class Parser:
    def __init__(self) -> None:
        self.error: BaseException | None = None
        self.mutate = False
        self.failed = False

    async def parse(self, source: SourceFile, context: CandidateBuildContext) -> Any:
        if self.error is not None:
            raise self.error
        if self.mutate:
            source.absolute_path.write_text("changed", encoding="utf-8")
        if self.failed:
            from atlasrag.providers.parsing.base import failed_document

            return failed_document(
                source,
                context,
                parser_name="fake",
                parser_version="1",
                failure_code="INVALID_SOURCE",
            )
        return _document(context, "canonical source text")


def resources(root: Path) -> tuple[Any, Any, Any, Any, Any]:
    build = module("atlasrag.services.indexing.build")
    assert hasattr(build, "BuildResources"), "missing T8 BuildResources"
    context = _context(root)
    runtime = config()
    context = build_candidate_context(context.source, pipeline_fingerprint(runtime))
    factory = IndexFactory()
    embedding = Embedding(factory, runtime)
    index = Index(factory, embedding)
    parser = Parser()
    bundle = build.BuildResources(
        config=runtime,
        uow_factory=PublicationFactory(factory),
        parsers=ParserRouter(parser, parser, parser, parser),
        embedding=embedding,
        index=index,
        clock=lambda: NOW,
    )
    return bundle, context, factory, parser, index


async def healthy() -> None:
    pass
