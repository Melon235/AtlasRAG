"""Open RAGBench frozen-snapshot adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.contracts.models import (
    SUPPORTED_MODALITIES,
    BenchmarkQrel,
    BenchmarkQuery,
    Modality,
    SnapshotManifest,
)
from benchmarks.snapshots.manifest import validate_snapshot


class DatasetAdapterError(ValueError):
    pass


class OpenRagBenchAdapter:
    """Reads only validated snapshot files; never downloads at run time."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.manifest: SnapshotManifest = validate_snapshot(root)
        self._queries = self._load_object("queries.json")
        self._qrels = self._load_object("qrels.json")
        self._answers = self._load_object("answers.json")
        self._validate()

    def _load_object(self, name: str) -> dict[str, Any]:
        if name not in {item.relative_path for item in self.manifest.files}:
            raise DatasetAdapterError(f"{name} is not covered by the snapshot manifest")
        path = self.root / name
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            raise DatasetAdapterError(f"cannot read {name}") from error
        if not isinstance(value, dict):
            raise DatasetAdapterError(f"{name} must be an object")
        return value

    def _validate(self) -> None:
        if not self._queries:
            raise DatasetAdapterError("queries.json is empty")
        for query_id, raw in self._queries.items():
            if not isinstance(query_id, str) or not query_id.strip():
                raise DatasetAdapterError("invalid query identity")
            if not isinstance(raw, dict):
                raise DatasetAdapterError("invalid query record")
            if not isinstance(raw.get("query"), str) or not raw["query"].strip():
                raise DatasetAdapterError("invalid query text")
            source = raw.get("source")
            if source not in {item.value for item in Modality}:
                raise DatasetAdapterError(f"unsupported modality: {source!r}")
            qrel = self._qrels.get(query_id)
            if not isinstance(qrel, dict):
                raise DatasetAdapterError(f"missing qrel for {query_id}")
            if not isinstance(qrel.get("doc_id"), str) or not isinstance(
                qrel.get("section_id"), int
            ):
                raise DatasetAdapterError(f"invalid qrel for {query_id}")
            if query_id not in self._answers or not isinstance(
                self._answers[query_id], str
            ):
                raise DatasetAdapterError(f"missing answer for {query_id}")
        if set(self._qrels) != set(self._queries):
            raise DatasetAdapterError("qrel/query identity mismatch")
        if set(self._answers) != set(self._queries):
            raise DatasetAdapterError("answer/query identity mismatch")
        counts = self.counts()
        if self.manifest.total_queries != counts["total"]:
            raise DatasetAdapterError("manifest total_queries mismatch")
        if (
            self.manifest.supported_queries
            and self.manifest.supported_queries != counts["supported"]
        ):
            raise DatasetAdapterError("manifest supported_queries mismatch")
        if self.manifest.modality_counts:
            by_modality = counts["by_modality"]
            if not isinstance(by_modality, dict):
                raise DatasetAdapterError("invalid modality counts")
            actual = {key: value for key, value in by_modality.items() if value}
            expected = {
                key: value
                for key, value in self.manifest.modality_counts.items()
                if value
            }
            if actual != expected:
                raise DatasetAdapterError("manifest modality_counts mismatch")

    @property
    def queries(self) -> tuple[BenchmarkQuery, ...]:
        values = []
        for query_id, raw in self._queries.items():
            values.append(
                BenchmarkQuery(
                    query_id=query_id,
                    text=raw["query"].strip(),
                    modality=Modality(raw["source"]),
                    query_type=str(raw.get("type", "unknown")),
                    reference_answer=self._answers.get(query_id),
                )
            )
        return tuple(values)

    @property
    def qrels(self) -> tuple[BenchmarkQrel, ...]:
        return tuple(
            BenchmarkQrel(
                query_id=query_id,
                doc_id=raw["doc_id"],
                section_id=raw["section_id"],
            )
            for query_id, raw in self._qrels.items()
        )

    def supported_queries(self) -> tuple[BenchmarkQuery, ...]:
        return tuple(
            item for item in self.queries if item.modality in SUPPORTED_MODALITIES
        )

    def counts(self) -> dict[str, object]:
        counts = {item.value: 0 for item in Modality}
        for item in self.queries:
            counts[item.modality.value] += 1
        return {
            "total": len(self.queries),
            "supported": len(self.supported_queries()),
            "excluded": len(self.queries) - len(self.supported_queries()),
            "by_modality": counts,
        }

    def positive_document_ids(self) -> tuple[str, ...]:
        return tuple(sorted({qrel.doc_id for qrel in self.qrels}))

    def supported_profiles(self) -> dict[str, object]:
        return {
            "FULL_CORPUS": {
                "documents": self.manifest.total_documents,
                "queries": len(self.supported_queries()),
                "positive_documents": len(self.positive_document_ids()),
                "hard_negative_documents": self.manifest.total_documents
                - len(self.positive_document_ids()),
            },
            "NEGATIVE_ONLY_CORPUS": {
                "documents": self.manifest.total_documents
                - len(self.positive_document_ids()),
                "queries": len(self.supported_queries()),
            },
            "PILOT": {"status": "declared separately; never an official scorecard"},
        }
