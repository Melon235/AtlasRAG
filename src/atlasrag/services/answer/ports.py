"""Answer generation provider protocol at the service boundary."""

from __future__ import annotations

from typing import Protocol

from atlasrag.domain.answer import GenerationResult
from atlasrag.services.answer.generation_input import GenerationInput


class GenerationProtocolError(ValueError):
    """Provider returned a contract-invalid structured answer."""


class GenerationProvider(Protocol):
    async def generate(self, input: GenerationInput) -> GenerationResult: ...
