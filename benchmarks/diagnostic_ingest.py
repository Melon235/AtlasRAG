"""Benchmark-owned ingestion orchestration for a diagnostic subcorpus."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

from benchmarks.pilot.ingestion import ingest_selected_pdfs


def _inputs(selection: dict) -> SimpleNamespace:
    selected = selection.get("selected", [])
    documents = selection.get("documents", [])
    if len(selected) < 25 or len(documents) < 40:
        raise ValueError("diagnostic selection has insufficient identities")
    mappings = tuple(
        SimpleNamespace(local_source_path=f"{doc}.pdf", official_document_id=doc)
        for doc in documents
    )
    fingerprint = hashlib.sha256(
        repr(tuple((doc, f"{doc}.pdf") for doc in sorted(documents))).encode()
    ).hexdigest()
    return SimpleNamespace(
        mappings=mappings,
        mapping_fingerprint=fingerprint,
        queries=(),
        gold_canonical={},
    )


async def execute(args: argparse.Namespace) -> int:
    selection = json.loads(args.selection.read_text(encoding="utf-8"))
    inputs = _inputs(selection)
    spec = importlib.util.spec_from_file_location(
        "diagnostic_ingestion_runtime", args.runtime_module
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load diagnostic ingestion runtime")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = module.build_ingestion_runtime()
    if hasattr(runtime, "__await__"):
        runtime = await runtime
    result = await ingest_selected_pdfs(
        pdf_directory=args.pdf_directory,
        inputs=inputs,
        backend=runtime.backend,
        uow_factory=runtime.uow_factory,
        runtime=runtime.runtime_config,
        verifier=runtime.verifier,
        expected_pipeline_fingerprint=runtime.pipeline_fingerprint,
    )
    args.output.mkdir(parents=True, exist_ok=True)
    evidence = {
        "status": result.gate_status,
        "label": "DIAGNOSTIC_SUBCORPUS_50",
        "mapping_fingerprint": inputs.mapping_fingerprint,
        "pipeline_fingerprint": runtime.pipeline_fingerprint,
        "selected_paths": sorted(paths.local_source_path for paths in inputs.mappings),
        "canonical_documents": sorted(
            __import__(
                "atlasrag.domain.ingestion.source", fromlist=["derive_document_id"]
            ).derive_document_id(path)
            for path in (m.local_source_path for m in inputs.mappings)
        ),
        "gate_reasons": list(result.gate_reasons),
        "ingestion": __import__("dataclasses").asdict(result.summary),
        "readiness": __import__("dataclasses").asdict(result.readiness),
    }
    (args.output / "readiness.json").write_text(
        json.dumps(evidence, indent=2, default=str) + "\n"
    )
    print(
        json.dumps(
            {
                "status": result.gate_status,
                "paths": len(result.readiness.published_source_paths),
                "reasons": list(result.gate_reasons),
            },
            sort_keys=True,
        )
    )
    return 0 if result.gate_status == "READY" else 2


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", type=Path, required=True)
    parser.add_argument("--pdf-directory", type=Path, required=True)
    parser.add_argument("--runtime-module", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    return asyncio.run(execute(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
