"""Immutable Web evidence acquisition profiles."""

from typing import Annotated

from pydantic import Field, StrictInt

from atlasrag.domain.base import FrozenModel, StrictReal

PositiveInt = Annotated[StrictInt, Field(ge=1)]


class WebEvidenceProfile(FrozenModel):
    top_k: PositiveInt = 5
    max_title_chars: PositiveInt = 500
    max_snippet_chars: PositiveInt = 2_000
    max_page_chars: PositiveInt = 4_000
    total_content_chars: PositiveInt = 12_000
    max_concurrent_fetches: PositiveInt = 4
    fetch_timeout_seconds: Annotated[StrictReal, Field(gt=0.0, le=30.0)] = 5.0
    max_response_bytes: PositiveInt = 1_000_000
    max_redirects: Annotated[StrictInt, Field(ge=0, le=10)] = 5
