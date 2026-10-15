"""Immutable source identity contracts for the Stage 3 build boundary."""

from __future__ import annotations

import re
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Annotated, Self

from pydantic import BeforeValidator, StrictStr, field_validator, model_validator

from atlasrag._canonical import canonical_sha256, validate_unicode_scalar_text
from atlasrag.domain.base import FrozenModel, validate_string_enum_input
from atlasrag.domain.enums import SourceType

_SOURCE_TYPE_BY_SUFFIX = {
    ".pdf": SourceType.PDF,
    ".docx": SourceType.DOCX,
    ".xlsx": SourceType.XLSX,
    ".md": SourceType.MD,
    ".txt": SourceType.TXT,
}
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_DOCUMENT_ID = re.compile(r"^doc_[0-9a-f]{64}$")
_REVISION_ID = re.compile(r"^rev_[0-9a-f]{64}$")


def _strict_path_input(value: object) -> object:
    if not isinstance(value, Path):
        raise ValueError("path input must be a pathlib.Path")
    return value


_StrictPath = Annotated[Path, BeforeValidator(_strict_path_input)]
_SourceType = Annotated[SourceType, BeforeValidator(validate_string_enum_input)]


def canonical_relative_source_path(value: str) -> str:
    """Validate and return a canonical POSIX source identity path."""
    value = validate_unicode_scalar_text(value)
    posix_path = PurePosixPath(value)
    windows_path = PureWindowsPath(value)
    if (
        not value
        or value == "."
        or value != value.strip()
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or "\\" in value
        or posix_path.is_absolute()
        or bool(windows_path.anchor)
        or ".." in posix_path.parts
        or posix_path.as_posix() != value
    ):
        raise ValueError("relative_source_path must be a canonical POSIX relative path")
    return value


def source_type_for_path(relative_source_path: str) -> SourceType:
    """Return the exact supported source type selected by file suffix."""
    canonical = canonical_relative_source_path(relative_source_path)
    suffix = PurePosixPath(canonical).suffix.casefold()
    try:
        return _SOURCE_TYPE_BY_SUFFIX[suffix]
    except KeyError as error:
        raise ValueError("source path must have a supported file suffix") from error


def _canonical_file_name(value: str) -> str:
    value = validate_unicode_scalar_text(value)
    if (
        not value
        or value != value.strip()
        or "/" in value
        or "\\" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        raise ValueError("file_name must be one canonical nonblank path component")
    return value


def _resolved_target(absolute_path: Path, knowledge_directory: Path) -> Path:
    try:
        target = absolute_path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise ValueError("source must exist as a regular file") from error
    if not target.is_relative_to(knowledge_directory):
        raise ValueError("source target must remain inside the knowledge directory")
    if not target.is_file():
        raise ValueError("source must exist as a regular file")
    return target


def resolve_source_target(source: SourceFile) -> Path:
    """Re-resolve one source path and enforce its root before every read."""
    if not isinstance(source, SourceFile):
        raise TypeError("source must be a SourceFile")
    validated = SourceFile.model_validate(source)
    return _resolved_target(validated.absolute_path, validated.knowledge_directory)


def validate_sha256_hex(value: str, field_name: str) -> str:
    """Require one canonical lowercase SHA-256 hexadecimal digest."""
    value = validate_unicode_scalar_text(value)
    if _SHA256_HEX.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be lowercase SHA-256 hexadecimal")
    return value


def validate_document_id(value: str) -> str:
    """Require one canonical Stage 3 document identity."""
    value = validate_unicode_scalar_text(value)
    if _DOCUMENT_ID.fullmatch(value) is None:
        raise ValueError("document_id must be a canonical document identity")
    return value


def validate_revision_id(value: str) -> str:
    """Require one canonical Stage 3 revision identity."""
    value = validate_unicode_scalar_text(value)
    if _REVISION_ID.fullmatch(value) is None:
        raise ValueError("revision_id must be a canonical revision identity")
    return value


def derive_document_id(relative_source_path: str) -> str:
    """Derive the stable v1 source-path document identity."""
    canonical = canonical_relative_source_path(relative_source_path)
    return "doc_" + canonical_sha256({"relative_source_path": canonical})


def derive_revision_id(
    document_id: str,
    content_hash: str,
    pipeline_fingerprint: str,
) -> str:
    """Derive a revision identity with canonical, unambiguous serialization."""
    validate_document_id(document_id)
    validate_sha256_hex(content_hash, "content hash")
    validate_sha256_hex(pipeline_fingerprint, "pipeline fingerprint")
    return "rev_" + canonical_sha256(
        {
            "document_id": document_id,
            "content_hash": content_hash,
            "pipeline_fingerprint": pipeline_fingerprint,
        }
    )


class SourceFile(FrozenModel):
    """Validated execution data for one source below a knowledge directory."""

    absolute_path: _StrictPath
    knowledge_directory: _StrictPath
    relative_source_path: StrictStr
    file_name: StrictStr
    source_type: _SourceType

    _canonical_relative_path = field_validator("relative_source_path")(
        canonical_relative_source_path
    )
    _canonical_name = field_validator("file_name")(_canonical_file_name)

    @model_validator(mode="after")
    def _path_and_type_consistency(self) -> Self:
        root = self.knowledge_directory
        if not root.is_absolute():
            raise ValueError("knowledge_directory must be absolute")
        try:
            resolved_root = root.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("knowledge_directory must exist") from error
        if resolved_root != root or not root.is_dir():
            raise ValueError("knowledge_directory must be a resolved directory")

        relative = PurePosixPath(self.relative_source_path)
        expected_path = root.joinpath(*relative.parts)
        if not self.absolute_path.is_absolute() or self.absolute_path != expected_path:
            raise ValueError("absolute_path must match the canonical source path")
        _resolved_target(self.absolute_path, root)

        if self.file_name != relative.name:
            raise ValueError("file_name must equal the source path basename")
        if self.source_type is not source_type_for_path(self.relative_source_path):
            raise ValueError("source_type must match the supported file suffix")
        return self

    @classmethod
    def from_knowledge_directory(
        cls,
        knowledge_directory: Path,
        relative_source_path: str,
    ) -> SourceFile:
        """Resolve a user-facing relative path into validated execution data."""
        if not isinstance(knowledge_directory, Path):
            raise TypeError("knowledge_directory must be a pathlib.Path")
        canonical = canonical_relative_source_path(relative_source_path)
        try:
            root = knowledge_directory.resolve(strict=True)
        except (OSError, RuntimeError) as error:
            raise ValueError("knowledge_directory must exist") from error
        if not root.is_dir():
            raise ValueError("knowledge_directory must be a directory")
        relative = PurePosixPath(canonical)
        return cls(
            absolute_path=root.joinpath(*relative.parts),
            knowledge_directory=root,
            relative_source_path=canonical,
            file_name=relative.name,
            source_type=source_type_for_path(canonical),
        )


class CanonicalSource(FrozenModel):
    """Serializable source projection without host filesystem paths."""

    relative_source_path: StrictStr
    file_name: StrictStr
    source_type: _SourceType

    _canonical_relative_path = field_validator("relative_source_path")(
        canonical_relative_source_path
    )
    _canonical_name = field_validator("file_name")(_canonical_file_name)

    @model_validator(mode="after")
    def _path_and_type_consistency(self) -> Self:
        relative = PurePosixPath(self.relative_source_path)
        if self.file_name != relative.name:
            raise ValueError("file_name must equal the source path basename")
        if self.source_type is not source_type_for_path(self.relative_source_path):
            raise ValueError("source_type must match the supported file suffix")
        return self

    @classmethod
    def from_source_file(cls, source: SourceFile) -> CanonicalSource:
        """Drop execution-only path data from one validated source."""
        if not isinstance(source, SourceFile):
            raise TypeError("source must be a SourceFile")
        source = SourceFile.model_validate(source)
        return cls(
            relative_source_path=source.relative_source_path,
            file_name=source.file_name,
            source_type=source.source_type,
        )


class CandidateBuildContext(FrozenModel):
    """Dependency-free identity data for one candidate production build."""

    source: SourceFile
    document_id: StrictStr
    content_hash: StrictStr
    pipeline_fingerprint: StrictStr
    revision_id: StrictStr

    @field_validator("document_id")
    @classmethod
    def _canonical_document_id(cls, value: str) -> str:
        return validate_document_id(value)

    @field_validator("content_hash")
    @classmethod
    def _canonical_content_hash(cls, value: str) -> str:
        return validate_sha256_hex(value, "content hash")

    @field_validator("pipeline_fingerprint")
    @classmethod
    def _canonical_pipeline_fingerprint(cls, value: str) -> str:
        return validate_sha256_hex(value, "pipeline fingerprint")

    @field_validator("revision_id")
    @classmethod
    def _canonical_revision_id(cls, value: str) -> str:
        return validate_revision_id(value)

    @model_validator(mode="after")
    def _identities_match_source_and_inputs(self) -> Self:
        if self.document_id != derive_document_id(self.source.relative_source_path):
            raise ValueError("document_id does not match relative_source_path")
        expected_revision = derive_revision_id(
            self.document_id,
            self.content_hash,
            self.pipeline_fingerprint,
        )
        if self.revision_id != expected_revision:
            raise ValueError("revision_id does not match candidate inputs")
        return self
