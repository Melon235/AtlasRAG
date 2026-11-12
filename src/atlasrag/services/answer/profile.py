"""Immutable Answer generation/regeneration profiles."""

from typing import Annotated

from pydantic import Field, StrictInt

from atlasrag.domain.base import FrozenModel


class AnswerProfile(FrozenModel):
    max_regenerations: Annotated[StrictInt, Field(ge=0, le=3)] = 1
    max_local_item_chars: Annotated[StrictInt, Field(ge=1)] = 4_000
    max_web_item_chars: Annotated[StrictInt, Field(ge=1)] = 3_000
    max_total_chars: Annotated[StrictInt, Field(ge=1)] = 16_000
