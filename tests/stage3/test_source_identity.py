"""Secure Stage 3 source models and deterministic build identities."""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path

import pytest
from pydantic import ValidationError

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.enums import SourceType
from atlasrag.domain.ingestion.source import (
    CandidateBuildContext,
    CanonicalSource,
    SourceFile,
)
from atlasrag.services.ingestion.identity import (
    build_candidate_context,
    content_hash,
    document_id,
    revision_id,
)

PIPELINE_FINGERPRINT = "f" * 64
ID_PATTERN = re.compile(r"^(doc|rev)_[0-9a-f]{64}$")


def write_source(root: Path, relative_path: str, content: bytes = b"content") -> Path:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


@pytest.mark.parametrize(
    ("relative_path", "source_type"),
    [
        ("manual.PDF", SourceType.PDF),
        ("nested/policy.DocX", SourceType.DOCX),
        ("data/report.XLSX", SourceType.XLSX),
        ("readme.Md", SourceType.MD),
        ("notes.TxT", SourceType.TXT),
    ],
)
def test_source_file_routes_supported_suffixes_case_insensitively(
    tmp_path: Path, relative_path: str, source_type: SourceType
) -> None:
    expected_path = write_source(tmp_path, relative_path)

    source = SourceFile.from_knowledge_directory(tmp_path, relative_path)

    assert source.absolute_path == expected_path
    assert source.knowledge_directory == tmp_path.resolve()
    assert source.relative_source_path == relative_path
    assert source.file_name == Path(relative_path).name
    assert source.source_type is source_type


@pytest.mark.parametrize(
    "relative_path",
    [
        "",
        ".",
        "./manual.txt",
        "nested/../manual.txt",
        "../manual.txt",
        "/absolute/manual.txt",
        "C:/manual.txt",
        "nested\\manual.txt",
        "nested//manual.txt",
        "nested/manual.txt/",
        "nested/\x00manual.txt",
        "nested/\nmanual.txt",
        "nested/\x7fmanual.txt",
    ],
)
def test_source_file_rejects_noncanonical_or_unsafe_relative_paths(
    tmp_path: Path, relative_path: str
) -> None:
    with pytest.raises((ValueError, ValidationError)):
        SourceFile.from_knowledge_directory(tmp_path, relative_path)


def test_source_file_rejects_unsupported_suffix_and_directory(tmp_path: Path) -> None:
    write_source(tmp_path, "document.html")
    (tmp_path / "folder.txt").mkdir()

    with pytest.raises(ValueError, match="supported"):
        SourceFile.from_knowledge_directory(tmp_path, "document.html")
    with pytest.raises(ValueError, match="regular file"):
        SourceFile.from_knowledge_directory(tmp_path, "folder.txt")


def test_source_file_allows_symlink_only_when_target_stays_in_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "knowledge"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    write_source(root, "targets/inside.txt", b"inside")
    write_source(outside, "secret.txt", b"outside")
    (root / "inside.txt").symlink_to(root / "targets" / "inside.txt")
    (root / "escape.txt").symlink_to(outside / "secret.txt")

    allowed = SourceFile.from_knowledge_directory(root, "inside.txt")

    assert allowed.absolute_path == root / "inside.txt"
    with pytest.raises(ValueError, match="knowledge directory"):
        SourceFile.from_knowledge_directory(root, "escape.txt")


def test_canonical_source_excludes_execution_paths(tmp_path: Path) -> None:
    write_source(tmp_path, "产品/手册.pdf")
    source = SourceFile.from_knowledge_directory(tmp_path, "产品/手册.pdf")

    canonical = CanonicalSource.from_source_file(source)

    assert canonical.model_dump(mode="json") == {
        "relative_source_path": "产品/手册.pdf",
        "file_name": "手册.pdf",
        "source_type": "PDF",
    }
    assert "absolute_path" not in CanonicalSource.model_fields
    assert "knowledge_directory" not in CanonicalSource.model_fields


def test_source_contracts_are_immutable_and_extra_forbid(tmp_path: Path) -> None:
    write_source(tmp_path, "guide.md")
    source = SourceFile.from_knowledge_directory(tmp_path, "guide.md")

    with pytest.raises(ValidationError, match="frozen"):
        source.file_name = "changed.md"
    with pytest.raises(ValidationError, match="Extra inputs"):
        CanonicalSource.model_validate(
            {
                "relative_source_path": "guide.md",
                "file_name": "guide.md",
                "source_type": "MD",
                "absolute_path": "/private/guide.md",
            }
        )


def test_document_id_depends_only_on_canonical_relative_path(tmp_path: Path) -> None:
    first_root = tmp_path / "first"
    second_root = tmp_path / "second"
    first_root.mkdir()
    second_root.mkdir()
    write_source(first_root, "docs/guide.md", b"first")
    write_source(second_root, "docs/guide.md", b"second")
    first = SourceFile.from_knowledge_directory(first_root, "docs/guide.md")
    second = SourceFile.from_knowledge_directory(second_root, "docs/guide.md")

    assert document_id(first) == document_id(second)
    assert document_id(first) == "doc_" + canonical_sha256(
        {"relative_source_path": "docs/guide.md"}
    )
    assert document_id(first) != document_id("docs/renamed.md")
    assert ID_PATTERN.fullmatch(document_id(first))


def test_content_hash_is_raw_streaming_sha256(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = bytes(range(251)) * 4
    write_source(tmp_path, "large.txt", data)
    source = SourceFile.from_knowledge_directory(tmp_path, "large.txt")
    requested_sizes: list[int] = []
    buffer = io.BytesIO(data)

    class TrackingReader:
        def __enter__(self) -> TrackingReader:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self, size: int = -1) -> bytes:
            requested_sizes.append(size)
            return buffer.read(size)

    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: TrackingReader())

    actual = content_hash(source, chunk_size=31)

    assert actual == hashlib.sha256(data).hexdigest()
    assert len(requested_sizes) > 2
    assert set(requested_sizes) == {31}


def test_content_hash_revalidates_constructed_source_before_opening(
    tmp_path: Path,
) -> None:
    write_source(tmp_path, "safe.txt", b"safe")
    outside = tmp_path.parent / "outside.txt"
    outside.write_bytes(b"outside")
    forged = SourceFile.model_construct(
        absolute_path=outside,
        knowledge_directory=tmp_path.resolve(),
        relative_source_path="safe.txt",
        file_name="safe.txt",
        source_type=SourceType.TXT,
    )

    with pytest.raises(ValidationError, match="absolute_path"):
        content_hash(forged)


@pytest.mark.parametrize("chunk_size", [0, -1, True, 1.5])
def test_content_hash_rejects_invalid_chunk_size(
    tmp_path: Path, chunk_size: object
) -> None:
    write_source(tmp_path, "guide.txt")
    source = SourceFile.from_knowledge_directory(tmp_path, "guide.txt")

    with pytest.raises(ValueError, match="chunk_size"):
        content_hash(source, chunk_size=chunk_size)  # type: ignore[arg-type]


def test_revision_id_uses_unambiguous_canonical_payload() -> None:
    document = "doc_" + "a" * 64
    source_hash = "b" * 64

    actual = revision_id(document, source_hash, PIPELINE_FINGERPRINT)

    assert actual == "rev_" + canonical_sha256(
        {
            "document_id": document,
            "content_hash": source_hash,
            "pipeline_fingerprint": PIPELINE_FINGERPRINT,
        }
    )
    assert ID_PATTERN.fullmatch(actual)
    assert actual != revision_id(document, "c" * 64, PIPELINE_FINGERPRINT)
    assert actual != revision_id(document, source_hash, "e" * 64)


def test_candidate_context_is_deterministic_data_only(tmp_path: Path) -> None:
    write_source(tmp_path, "docs/guide.md", b"canonical bytes")
    source = SourceFile.from_knowledge_directory(tmp_path, "docs/guide.md")

    first = build_candidate_context(source, PIPELINE_FINGERPRINT)
    second = build_candidate_context(source, PIPELINE_FINGERPRINT)

    assert first == second
    assert set(CandidateBuildContext.model_fields) == {
        "source",
        "document_id",
        "content_hash",
        "pipeline_fingerprint",
        "revision_id",
    }
    assert first.document_id == document_id(source)
    assert first.content_hash == hashlib.sha256(b"canonical bytes").hexdigest()
    assert first.revision_id == revision_id(
        first.document_id,
        first.content_hash,
        PIPELINE_FINGERPRINT,
    )


def test_candidate_context_revalidates_forged_identity(tmp_path: Path) -> None:
    write_source(tmp_path, "guide.txt")
    source = SourceFile.from_knowledge_directory(tmp_path, "guide.txt")
    valid = build_candidate_context(source, PIPELINE_FINGERPRINT)
    payload = valid.model_dump()
    payload["revision_id"] = "rev_" + "0" * 64

    with pytest.raises(ValidationError, match="revision_id"):
        CandidateBuildContext.model_validate(payload)


def test_relocation_does_not_put_absolute_path_in_any_identity(tmp_path: Path) -> None:
    first_root = tmp_path / "location-a"
    second_root = tmp_path / "location-b"
    first_root.mkdir()
    second_root.mkdir()
    write_source(first_root, "same/file.txt", b"same")
    write_source(second_root, "same/file.txt", b"same")

    first = build_candidate_context(
        SourceFile.from_knowledge_directory(first_root, "same/file.txt"),
        PIPELINE_FINGERPRINT,
    )
    second = build_candidate_context(
        SourceFile.from_knowledge_directory(second_root, "same/file.txt"),
        PIPELINE_FINGERPRINT,
    )

    assert first.document_id == second.document_id
    assert first.content_hash == second.content_hash
    assert first.revision_id == second.revision_id
