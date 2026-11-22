"""Executable benchmark-owned Quality Run command implementation."""

from __future__ import annotations

import argparse
import importlib.util
import inspect
import json
from pathlib import Path
from uuid import uuid4

from benchmarks.metrics.replay import AttemptSelection, replay_case_metrics
from benchmarks.observation.artifacts import (
    CaseObservationArtifact,
    ObservationArtifactError,
    stored_observation,
    validate_case_artifact,
    write_case_artifact,
)
from benchmarks.pilot.identity import load_pilot_inputs
from benchmarks.runners.manifests import (
    CaseStatus,
    RunLedger,
    RunManifest,
    assess_validity,
)
from benchmarks.runners.quality_run import QualityPolicies, run_quality
from benchmarks.snapshots.manifest import validate_snapshot


def _gold(path: Path) -> dict[str, dict[str, int]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not value:
        raise ValueError("gold must be nonempty query-specific mapping")
    result: dict[str, dict[str, int]] = {}
    for query_id, docs in value.items():
        if not isinstance(query_id, str) or not isinstance(docs, dict):
            raise ValueError("invalid gold query mapping")
        if not all(
            isinstance(doc_id, str) and type(grade) is int
            for doc_id, grade in docs.items()
        ):
            raise ValueError("invalid gold relevance")
        result[query_id] = docs
    return result


def _git(arguments: list[str]) -> str:
    import subprocess

    result = subprocess.run(
        ["git", *arguments], capture_output=True, text=True, timeout=10
    )
    return result.stdout.strip() if result.returncode == 0 else "UNKNOWN"


async def execute_quality_command(args: argparse.Namespace) -> int:
    spec = importlib.util.spec_from_file_location(
        "benchmark_runtime", args.runtime_module
    )
    if spec is None or spec.loader is None:
        raise ValueError("cannot load runtime module")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    runtime = module.build_runtime()
    if inspect.isawaitable(runtime):
        runtime = await runtime
    snapshot = validate_snapshot(args.snapshot)
    pilot = args.mode == "pilot"
    pilot_inputs = (
        load_pilot_inputs(args.snapshot, args.selection, args.gold) if pilot else None
    )
    gold = pilot_inputs.gold_canonical if pilot_inputs is not None else _gold(args.gold)
    if not runtime.corpus_ready:
        raise ValueError("published corpus readiness is not accepted")
    if runtime.web.calls or runtime.web.external_calls:
        raise ValueError("Web must be OFF for Quality Run")
    if {query.query_id for query in runtime.queries} != set(gold):
        raise ValueError("gold identities do not match configured queries")
    if pilot_inputs is not None:
        if {query.query_id for query in runtime.queries} != set(
            pilot_inputs.gold_canonical
        ):
            raise ValueError("pilot runtime query identities do not match selection")
        if runtime.gold and runtime.gold != pilot_inputs.gold_canonical:
            raise ValueError("pilot runtime gold does not match canonical mapping")
    planned = len(runtime.queries) * args.repetitions
    if planned <= 0 or planned > args.max_cases:
        raise ValueError("case count outside max-cases")
    if args.output.exists() and any(args.output.iterdir()):
        raise ValueError("output directory is not empty; refusing overwrite")
    args.output.mkdir(parents=True, exist_ok=True)
    if pilot:
        run_id = "pilot-" + uuid4().hex
        environment_class = "OPEN_RAGBENCH_PILOT"
        code_commit = _git(["rev-parse", "HEAD"])
        dirty_tree = bool(_git(["status", "--porcelain"]))
    else:
        run_id = "synthetic-" + uuid4().hex
        environment_class = "SYNTHETIC_INTEGRATION"
        code_commit = "UNCOMMITTED"
        dirty_tree = True
    snapshot_sha = snapshot.files[0].sha256 if snapshot.files else ""
    mapping_fingerprint = pilot_inputs.mapping_fingerprint if pilot_inputs else ""
    manifest = RunManifest(
        run_id=run_id,
        dataset_revision=snapshot.dataset_revision,
        snapshot_sha256=snapshot_sha,
        corpus_profile="FULL_CORPUS",
        query_profile="SUPPORTED",
        planned_queries=len(runtime.queries),
        code_commit=code_commit,
        dirty_tree=dirty_tree,
        pipeline_fingerprint=runtime.pipeline_fingerprint,
        runtime_fingerprint=runtime.runtime_fingerprint,
        evaluator_fingerprint="retrieval-metrics-v1",
        web_policy="OFF",
        query_cache_policy="OFF",
        parent_cache_policy="BYPASSED",
        concurrency=1,
        repetitions=args.repetitions,
        environment_class=environment_class,
        attempt_view=args.attempt_view,
    )
    (args.output / "manifest.json").write_text(
        manifest.model_dump_json(), encoding="utf-8"
    )
    if pilot:
        (args.output / "pilot_mapping.json").write_text(
            json.dumps(
                {
                    "mapping_fingerprint": mapping_fingerprint,
                    "snapshot_identity": pilot_inputs.snapshot_identity
                    if pilot_inputs
                    else "",
                    "mappings": [m.__dict__ for m in pilot_inputs.mappings]
                    if pilot_inputs
                    else [],
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
    result = await run_quality(
        runtime.queries,
        run_id=run_id,
        repetitions=args.repetitions,
        policies=QualityPolicies(),
        retrieve=runtime.retrieval.run,
    )
    if len(result.cases) != planned:
        raise ValueError("executed case count mismatch")
    ledger = RunLedger(args.output / "cases.jsonl")
    artifacts: list[CaseObservationArtifact] = []
    for case in result.cases:
        events = [
            event
            for event in runtime.observer.events
            if event.invocation_id == case.invocation_id
        ]
        completed = case.observation.status is CaseStatus.COMPLETED
        artifact = CaseObservationArtifact(
            run_id=run_id,
            query_id=case.query_id,
            repetition_index=case.repetition_index,
            invocation_id=case.invocation_id,
            observations=tuple(stored_observation(event) for event in events),
            terminal_outcome="SUCCESS" if completed else "TECHNICAL_FAILURE",
            case_status="COMPLETED" if completed else "TECHNICAL_FAILURE",
            attempt_view=args.attempt_view,
        )
        try:
            validate_case_artifact(artifact)
        except ObservationArtifactError as error:
            raise ValueError(
                f"case {case.query_id}:{case.repetition_index}: {error}"
            ) from error
        artifacts.append(
            write_case_artifact(args.output / "observation_cases.jsonl", artifact)
        )
        ledger.append(case.observation)
    validity = assess_validity(planned, ledger.read())
    cache_gets = runtime.cache_gets()
    cache_sets = runtime.cache_sets()
    status = validity.validity.value
    if status == "VALID" and (runtime.web.external_calls or cache_gets or cache_sets):
        status = "INVALID"
    replay = []
    for artifact in artifacts:
        replay.append(
            replay_case_metrics(
                artifact,
                gold_documents=gold[artifact.query_id],
                attempt_view=AttemptSelection(artifact.attempt_view),
            )
        )
    print(
        json.dumps(
            {
                "status": status,
                "label": "OPEN_RAGBENCH_PILOT" if pilot else "SYNTHETIC_INTEGRATION",
                "run_id": run_id,
                "queries": [query.query_id for query in runtime.queries],
                "cases": len(artifacts),
                "invocations": [
                    [case.query_id, case.repetition_index, case.invocation_id]
                    for case in result.cases
                ],
                "validity": validity.model_dump(mode="json"),
                "web_calls": runtime.web.external_calls,
                "cache_gets": cache_gets,
                "cache_sets": cache_sets,
                "artifacts": str(args.output / "observation_cases.jsonl"),
                "manifest": str(args.output / "manifest.json"),
                "replay_stages": [
                    [stage.stage.value, stage.attempt_index]
                    for item in replay
                    for stage in item.stages
                ],
            },
            sort_keys=True,
            default=str,
        )
    )
    return 0 if status == "VALID" else 2
