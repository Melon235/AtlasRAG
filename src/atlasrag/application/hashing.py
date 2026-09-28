"""Stable hashing at the application request boundary."""

from atlasrag._canonical import canonical_sha256
from atlasrag.domain.requests import QueryFilters, UserTurnRequest


def _normalized_filters(filters: QueryFilters | None) -> object:
    if filters is None:
        return None
    payload = filters.model_dump(mode="json", exclude_none=True)
    return payload or None


def request_hash(request: UserTurnRequest) -> str:
    """Hash answer-affecting raw request data, excluding session identity."""
    payload: dict[str, object] = {
        "query": request.query,
        "filters": _normalized_filters(request.filters),
    }
    return canonical_sha256(payload)
