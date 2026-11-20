"""Pilot selection, official-to-canonical mapping, and dry-run validation."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from atlasrag.domain.ingestion.source import derive_document_id
from benchmarks.contracts.models import BenchmarkQuery, Modality
from benchmarks.datasets.open_ragbench import OpenRagBenchAdapter


class PilotIdentityError(ValueError):
    pass


@dataclass(frozen=True)
class PilotMapping:
    official_document_id: str
    local_source_path: str
    canonical_document_id: str
    source_url: str
    query_id: str
    modality: str
    section_id: int
    relevance: int


@dataclass(frozen=True)
class PilotInputs:
    queries: tuple[BenchmarkQuery, ...]
    gold_canonical: dict[str, dict[str, int]]
    gold_official: dict[str, dict[str, int]]
    mappings: tuple[PilotMapping, ...]
    snapshot_identity: str
    mapping_fingerprint: str


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise PilotIdentityError(f"cannot load {path.name}") from error
    if not isinstance(value, dict):
        raise PilotIdentityError(f"{path.name} must be an object")
    return value


def load_pilot_inputs(
    snapshot_root: Path, selection_path: Path, gold_path: Path
) -> PilotInputs:
    adapter = OpenRagBenchAdapter(snapshot_root)
    selection = _load(selection_path)
    selected = selection.get("selected")
    if not isinstance(selected, list) or len(selected) != 10:
        raise PilotIdentityError("selection must contain exactly 10 documents/queries")
    if selection.get("dataset_revision") != adapter.manifest.dataset_revision:
        raise PilotIdentityError("selection revision does not match snapshot")
    query_by_id = {query.query_id: query for query in adapter.queries}
    qrels = {qrel.query_id: qrel for qrel in adapter.qrels}
    gold_raw = _load(gold_path)
    mappings: list[PilotMapping] = []
    official_gold: dict[str, dict[str, int]] = {}
    canonical_gold: dict[str, dict[str, int]] = {}
    seen_queries: set[str] = set()
    seen_docs: set[str] = set()
    for raw in selected:
        if not isinstance(raw, dict):
            raise PilotIdentityError("selection entry must be an object")
        query_id = raw.get("query_id")
        doc_id = raw.get("official_document_id")
        if not isinstance(query_id, str) or not isinstance(doc_id, str):
            raise PilotIdentityError("selection query/document identity invalid")
        if query_id in seen_queries or doc_id in seen_docs:
            raise PilotIdentityError("duplicate selected query or document")
        seen_queries.add(query_id)
        seen_docs.add(doc_id)
        query = query_by_id.get(query_id)
        if query is None:
            raise PilotIdentityError(f"unknown selected query {query_id}")
        if query.modality not in {Modality.TEXT, Modality.TEXT_TABLE}:
            raise PilotIdentityError(f"unsupported modality for {query_id}")
        qrel = qrels.get(query_id)
        if qrel is None or qrel.doc_id != doc_id:
            raise PilotIdentityError(f"qrel mismatch for {query_id}")
        if doc_id not in adapter.corpus_document_ids():
            raise PilotIdentityError(f"document not in pinned corpus: {doc_id}")
        expected_path = f"{doc_id}.pdf"
        canonical = derive_document_id(expected_path)
        if raw.get("local_source_path") != expected_path:
            raise PilotIdentityError(f"local source path mismatch for {doc_id}")
        if raw.get("derived_local_document_id") != canonical:
            raise PilotIdentityError(f"canonical identity mismatch for {doc_id}")
        if raw.get("section_id") != qrel.section_id:
            raise PilotIdentityError(f"section mismatch for {query_id}")
        official_entry = gold_raw.get(query_id)
        if not isinstance(official_entry, dict) or official_entry != {doc_id: 1}:
            raise PilotIdentityError(f"pilot gold mismatch for {query_id}")
        official_gold[query_id] = {doc_id: 1}
        canonical_gold[query_id] = {canonical: 1}
        mappings.append(
            PilotMapping(
                official_document_id=doc_id,
                local_source_path=expected_path,
                canonical_document_id=canonical,
                source_url=str(raw.get("source_url", "")),
                query_id=query_id,
                modality=query.modality.value,
                section_id=qrel.section_id,
                relevance=1,
            )
        )
    if set(gold_raw) != seen_queries:
        raise PilotIdentityError("gold query set does not match selection")
    if len(mappings) != 10 or len({m.canonical_document_id for m in mappings}) != 10:
        raise PilotIdentityError("pilot requires 10 unique canonical documents")
    payload = tuple(
        (m.official_document_id, m.canonical_document_id, m.query_id)
        for m in sorted(mappings, key=lambda item: item.query_id)
    )
    fingerprint = hashlib.sha256(repr(payload).encode("utf-8")).hexdigest()
    queries = tuple(
        BenchmarkQuery(
            query_id=query_by_id[m.query_id].query_id,
            text=query_by_id[m.query_id].text,
            modality=query_by_id[m.query_id].modality,
            query_type=query_by_id[m.query_id].query_type,
            reference_answer=None,
        )
        for m in sorted(mappings, key=lambda item: item.query_id)
    )
    return PilotInputs(
        queries=queries,
        gold_canonical=canonical_gold,
        gold_official=official_gold,
        mappings=tuple(mappings),
        snapshot_identity=adapter.manifest.model_dump_json(),
        mapping_fingerprint=fingerprint,
    )


def pilot_required_source_paths(inputs: PilotInputs) -> tuple[str, ...]:
    return tuple(sorted(m.local_source_path for m in inputs.mappings))


def preflight_status(ingested: bool) -> str:
    return (
        "PILOT_READY_FOR_APPROVAL"
        if ingested
        else "PILOT_AWAITING_AUTHORIZED_INGESTION"
    )
