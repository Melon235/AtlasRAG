"""Retrieval-only diagnostic: direct production LocalEvidenceRunner, no LLM."""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
from pathlib import Path
from uuid import uuid4

from atlasrag.domain.observation import ObservationStage, observation_invocation
from atlasrag.domain.requests import LocalRetrievalRequest
from benchmarks.metrics.retrieval import RankedUnit, evaluate_relaxed_document


async def execute(args: argparse.Namespace) -> int:
    spec = importlib.util.spec_from_file_location(
        "diagnostic_runtime", args.runtime_module
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load diagnostic runtime module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = module.build_runtime()
    if hasattr(runtime, "__await__"):
        runtime = await runtime
    args.output.mkdir(parents=True, exist_ok=True)
    records = []
    for query in runtime.queries:
        before = len(runtime.observer.events)
        with observation_invocation(uuid4().hex):
            await runtime.local_runner.run(
                LocalRetrievalRequest(
                    original_query=query.text,
                    retrieval_query=query.text,
                )
            )
        events = runtime.observer.events[before:]
        stages = {}
        gold = runtime.gold[query.query_id]
        for stage in (
            ObservationStage.DENSE,
            ObservationStage.BM25,
            ObservationStage.FUSION,
            ObservationStage.RERANKER,
        ):
            event = next((item for item in events if item.stage is stage), None)
            if event is None:
                continue
            ranked = [
                RankedUnit(
                    identity=item.identity,
                    document_id=item.document_id or "",
                    rank=item.rank,
                    score=item.score,
                )
                for item in event.candidates
                if item.document_id
            ]
            metrics = evaluate_relaxed_document(ranked, gold, k=10)
            stages[stage.value] = {
                "hit1": metrics.hit,
                "hit5": metrics.hit,
                "hit10": metrics.hit,
                "recall5": metrics.recall,
                "recall10": metrics.recall,
                "mrr10": metrics.mrr,
                "ndcg10": metrics.ndcg,
                "latency_ms": event.latency_ms,
                "ranked_documents": [item.document_id for item in ranked],
            }
        records.append(
            {
                "query_id": query.query_id,
                "modality": query.modality.value,
                "gold_documents": sorted(gold),
                "stages": stages,
            }
        )
    (args.output / "observations.json").write_text(
        json.dumps({"label": "RETRIEVAL_ONLY_DIAGNOSTIC", "records": records}, indent=2)
        + "\n"
    )
    print(
        json.dumps(
            {
                "label": "RETRIEVAL_ONLY_DIAGNOSTIC",
                "queries": len(records),
                "output": str(args.output / "observations.json"),
            },
            sort_keys=True,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--runtime-module", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
