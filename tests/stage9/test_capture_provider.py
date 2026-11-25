import pytest

from benchmarks.evaluation.capture_provider import CapturingGenerationProvider


class Provider:
    async def generate(self, value: object) -> object:
        return value


@pytest.mark.asyncio
async def test_capture_records_exact_generation_input() -> None:
    provider = CapturingGenerationProvider(Provider())
    assert await provider.generate({"context": "exact evidence"}) == {
        "context": "exact evidence"
    }
    assert provider.inputs[0][1] == {"context": "exact evidence"}
