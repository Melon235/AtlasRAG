"""Executable pilot PDF ingestion command."""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
from dataclasses import asdict

from benchmarks.pilot.identity import load_pilot_inputs
from benchmarks.pilot.ingestion import ingest_selected_pdfs


async def execute_pilot_ingest(args: argparse.Namespace) -> int:
    inputs = load_pilot_inputs(args.snapshot, args.selection, args.gold)
    spec = importlib.util.spec_from_file_location(
        "pilot_ingestion_runtime", args.runtime_module
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load pilot ingestion runtime module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = module.build_ingestion_runtime()
    if inspect.isawaitable(runtime):
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
        "label": "PILOT_INGESTION",
        "mapping_fingerprint": inputs.mapping_fingerprint,
        "gate_reasons": list(result.gate_reasons),
        "ingestion": asdict(result.summary),
        "readiness": asdict(result.readiness),
        "selected_paths": sorted(m.local_source_path for m in inputs.mappings),
        "canonical_documents": sorted(m.canonical_document_id for m in inputs.mappings),
    }
    (args.output / "readiness.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(
        json.dumps(
            {
                "status": result.gate_status,
                "output": str(args.output / "readiness.json"),
                "paths": len(result.readiness.published_source_paths),
                "reason_count": len(result.gate_reasons),
            },
            sort_keys=True,
        )
    )
    return 0 if result.gate_status == "READY" else 2
