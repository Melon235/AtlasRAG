"""Frozen Redis key contracts for AtlasRAG-owned cache namespaces."""

from __future__ import annotations

from atlasrag._canonical import validate_unicode_scalar_text
from atlasrag.domain.errors import InvariantViolationError

QUERY_WRITE_FENCE_KEY = "atlasrag:fence:query_write"


def is_cache_key_component_safe(value: object) -> bool:
    """Return whether a value satisfies the strict Redis cache key component contract."""

    if not isinstance(value, str):
        return False
    try:
        validate_unicode_scalar_text(value)
    except ValueError:
        return False
    return not (
        not value
        or value != value.strip()
        or ":" in value
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    )


def _key_component(value: object) -> str:
    if not isinstance(value, str) or not is_cache_key_component_safe(value):
        raise InvariantViolationError
    return value


def validate_write_fence_input(value: object) -> str:
    """Validate an explicit write-fence token passed to a fenced cache write."""

    if not isinstance(value, str):
        raise InvariantViolationError
    try:
        validate_unicode_scalar_text(value)
    except ValueError:
        raise InvariantViolationError from None
    if (
        not value
        or len(value) > 128
        or any(
            character.isspace() or ord(character) < 32 or ord(character) == 127
            for character in value
        )
    ):
        raise InvariantViolationError
    return value


def parent_cache_key(revision_id: str, parent_id: str) -> str:
    """Build the frozen revision-aware Parent Context Cache key."""

    return f"atlasrag:parent:{_key_component(revision_id)}:{_key_component(parent_id)}"


def query_cache_key(
    session_id: str,
    runtime_fingerprint: str,
    request_hash: str,
) -> str:
    """Build the frozen session/runtime/request Query Cache key."""

    return (
        f"atlasrag:query:{_key_component(session_id)}:"
        f"{_key_component(runtime_fingerprint)}:{_key_component(request_hash)}"
    )
