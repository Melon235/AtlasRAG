"""Content-authoritative source mutation guard."""

from atlasrag.domain.errors import SourceMutationError
from atlasrag.domain.ingestion.source import SourceFile, validate_sha256_hex
from atlasrag.services.ingestion.identity import content_hash


def ensure_source_unchanged(source: SourceFile, expected_content_hash: str) -> str:
    """Rehash a parsed source and fail if its authoritative bytes changed."""
    validate_sha256_hex(expected_content_hash, "content hash")
    try:
        verified_hash = content_hash(source)
    except (OSError, ValueError):
        raise SourceMutationError from None
    if verified_hash != expected_content_hash:
        raise SourceMutationError
    return verified_hash
