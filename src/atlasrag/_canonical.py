"""Private deterministic serialization shared by stable SHA-256 identities."""

import hashlib
import json


def canonical_sha256(payload: object) -> str:
    """Return SHA-256 over compact, sorted, UTF-8 canonical JSON."""
    serialized = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()
