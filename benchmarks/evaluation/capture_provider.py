"""Benchmark-owned generation capture wrapper; no production DTO changes."""

from __future__ import annotations

from typing import Any

from atlasrag.domain.observation import current_invocation_id


class CapturingGenerationProvider:
    def __init__(self, provider: Any) -> None:
        self._provider = provider
        self.inputs: list[tuple[str | None, Any]] = []

    async def generate(self, input: Any) -> Any:
        self.inputs.append((current_invocation_id(), input))
        return await self._provider.generate(input)

    async def aclose(self) -> None:
        close = getattr(self._provider, "aclose", None)
        if close is not None:
            await close()
