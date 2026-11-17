"""Open RAGBench frozen-snapshot adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from benchmarks.contracts.models import (
    SUPPORTED_MODALITIES,
    BenchmarkQrel,
    BenchmarkQuery,
    DocumentPartition,
    Modality,
    PartitionStatus,
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
        self._urls = self._load_object("pdf_urls.json")
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
        if not self._urls:
            raise DatasetAdapterError("pdf_urls.json is empty")
        if set(self._urls) != set(self.manifest.corpus_document_ids):
            raise DatasetAdapterError("pdf_urls/corpus identity mismatch")
        referenced = {item.doc_id for item in self.qrels}
        if not referenced <= set(self._urls):
            raise DatasetAdapterError("qrel document is not in the corpus identity set")
        for doc_id, url in self._urls.items():
            if not isinstance(doc_id, str) or not doc_id.strip():
                raise DatasetAdapterError("invalid corpus document identity")
            if not isinstance(url, str) or not url.strip():
                raise DatasetAdapterError(f"missing source URL for {doc_id}")
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

    def qrel_referenced_document_ids(self) -> tuple[str, ...]:
        return tuple(sorted({qrel.doc_id for qrel in self.qrels}))

    def corpus_document_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._urls))

    def document_partition(self) -> DocumentPartition:
        referenced = set(self.qrel_referenced_document_ids())
        corpus = set(self.corpus_document_ids())
        unreferenced = corpus - referenced
        return DocumentPartition(
            qrel_referenced_ids=tuple(sorted(referenced)),
            unreferenced_ids=tuple(sorted(unreferenced)),
            verified_hard_negative_ids=None,
            status=PartitionStatus.UNVERIFIED,
            evidence=(
                "qrels reference 396 of 1000 documents; the pinned release has "
                "no authoritative positive/hard-negative partition file. "
                "Unreferenced documents are not certified negatives."
            ),
        )

    def negative_only_profile(self) -> dict[str, object]:
        partition = self.document_partition()
        if partition.verified_hard_negative_ids is None:
            return {
                "status": "BLOCKED_UNVERIFIED_PARTITION",
                "observed_unreferenced_documents": len(partition.unreferenced_ids),
                "verified_hard_negative_documents": None,
                "queries": len(self.supported_queries()),
            }
        return {
            "status": "VERIFIED",
            "documents": len(partition.verified_hard_negative_ids),
            "queries": len(self.supported_queries()),
        }

    def supported_profiles(self) -> dict[str, object]:
        partition = self.document_partition()
        return {
            "FULL_CORPUS": {
                "documents": len(self.corpus_document_ids()),
                "queries": len(self.supported_queries()),
                "qrel_referenced_documents": len(partition.qrel_referenced_ids),
                "unreferenced_documents": len(partition.unreferenced_ids),
                "verified_hard_negative_documents": None,
                "partition_status": partition.status.value,
            },
            "NEGATIVE_ONLY_CORPUS": self.negative_only_profile(),
            "PILOT": {"status": "declared separately; never an official scorecard"},
        }
