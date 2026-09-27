"""Private deterministic serialization shared by stable SHA-256 identities."""

import hashlib
import json
from collections.abc import Mapping


def validate_unicode_scalar_text(value: str) -> str:
    """Reject lone UTF surrogate code points before JSON/UTF-8 boundaries."""
    if any(0xD800 <= ord(character) <= 0xDFFF for character in value):
        raise ValueError("text must not contain Unicode surrogate code points")
    return value


def _validate_canonical_text(value: object, visited_containers: set[int]) -> None:
    if isinstance(value, str):
        validate_unicode_scalar_text(value)
        return
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in visited_containers:
            return
        visited_containers.add(identity)
        for key, item in value.items():
            if isinstance(key, str):
                validate_unicode_scalar_text(key)
            _validate_canonical_text(item, visited_containers)
        return
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in visited_containers:
            return
        visited_containers.add(identity)
        for item in value:
            _validate_canonical_text(item, visited_containers)


def canonical_sha256(payload: object) -> str:
    """Return SHA-256 over compact, sorted, UTF-8 canonical JSON."""
    _validate_canonical_text(payload, set())
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
