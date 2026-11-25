"""Benchmark-owned durable pre-request budget guard."""

from __future__ import annotations

import fcntl
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class BudgetExceededError(RuntimeError):
    pass


@dataclass(frozen=True)
class BudgetState:
    attempts: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_micro_usd: int = 0


@dataclass(frozen=True)
class BudgetPolicy:
    max_attempts: int
    max_tokens: int
    max_cost_micro_usd: int


class BudgetLedger:
    """Durable shared ledger; every HTTP attempt is reserved before dispatch."""

    def __init__(self, path: Path, policy: BudgetPolicy) -> None:
        self.path = path
        self.policy = policy
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self, stream: Any) -> BudgetState:
        stream.seek(0)
        text = stream.read()
        if not text.strip():
            return BudgetState()
        try:
            return BudgetState(**json.loads(text))
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise BudgetExceededError("corrupted budget ledger") from error

    def _check(self, state: BudgetState) -> None:
        if state.attempts >= self.policy.max_attempts:
            raise BudgetExceededError("attempt budget exhausted")
        if state.input_tokens + state.output_tokens >= self.policy.max_tokens:
            raise BudgetExceededError("token budget exhausted")
        if state.cost_micro_usd >= self.policy.max_cost_micro_usd:
            raise BudgetExceededError("cost budget exhausted")

    def reserve_attempt(self) -> int:
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read(stream)
                self._check(state)
                state = BudgetState(
                    attempts=state.attempts + 1,
                    input_tokens=state.input_tokens,
                    output_tokens=state.output_tokens,
                    cost_micro_usd=state.cost_micro_usd,
                )
                stream.seek(0)
                stream.truncate()
                stream.write(json.dumps(asdict(state)) + "\n")
                stream.flush()
                import os

                os.fsync(stream.fileno())
                return state.attempts
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def reconcile(
        self, *, input_tokens: int, output_tokens: int, cost_micro_usd: int
    ) -> None:
        if input_tokens < 0 or output_tokens < 0 or cost_micro_usd < 0:
            raise BudgetExceededError("invalid usage")
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read(stream)
                state = BudgetState(
                    attempts=state.attempts,
                    input_tokens=state.input_tokens + input_tokens,
                    output_tokens=state.output_tokens + output_tokens,
                    cost_micro_usd=state.cost_micro_usd + cost_micro_usd,
                )
                if state.input_tokens + state.output_tokens > self.policy.max_tokens:
                    raise BudgetExceededError("token budget exceeded")
                if state.cost_micro_usd > self.policy.max_cost_micro_usd:
                    raise BudgetExceededError("cost budget exceeded")
                stream.seek(0)
                stream.truncate()
                stream.write(json.dumps(asdict(state)) + "\n")
                stream.flush()
                import os

                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def reserve_budget(
        self, *, input_tokens: int, output_tokens: int, cost_micro_usd: int
    ) -> None:
        """Reserve conservative headroom before dispatch; caller reconciles afterwards."""
        if min(input_tokens, output_tokens, cost_micro_usd) < 0:
            raise BudgetExceededError("invalid reservation")
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read(stream)
                projected = BudgetState(
                    attempts=state.attempts,
                    input_tokens=state.input_tokens + input_tokens,
                    output_tokens=state.output_tokens + output_tokens,
                    cost_micro_usd=state.cost_micro_usd + cost_micro_usd,
                )
                if (
                    projected.input_tokens + projected.output_tokens
                    > self.policy.max_tokens
                ):
                    raise BudgetExceededError("token reservation exceeds budget")
                if projected.cost_micro_usd > self.policy.max_cost_micro_usd:
                    raise BudgetExceededError("cost reservation exceeds budget")
                stream.seek(0)
                stream.truncate()
                stream.write(json.dumps(asdict(projected)) + "\n")
                stream.flush()
                import os

                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    def adjust(
        self, *, input_tokens: int, output_tokens: int, cost_micro_usd: int
    ) -> None:
        """Correct a prior reservation after actual usage is known."""
        with self.path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                state = self._read(stream)
                values = (
                    state.attempts,
                    max(0, state.input_tokens + input_tokens),
                    max(0, state.output_tokens + output_tokens),
                    max(0, state.cost_micro_usd + cost_micro_usd),
                )
                if (
                    values[1] + values[2] > self.policy.max_tokens
                    or values[3] > self.policy.max_cost_micro_usd
                ):
                    raise BudgetExceededError("actual usage exceeds budget")
                stream.seek(0)
                stream.truncate()
                stream.write(
                    json.dumps(
                        {
                            "attempts": values[0],
                            "input_tokens": values[1],
                            "output_tokens": values[2],
                            "cost_micro_usd": values[3],
                        }
                    )
                    + "\n"
                )
                stream.flush()
                import os

                os.fsync(stream.fileno())
            finally:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


class BudgetedTransport:
    """httpx transport wrapper that reserves one attempt before every request."""

    def __init__(self, transport: Any, ledger: BudgetLedger) -> None:
        self._transport = transport
        self._ledger = ledger

    def __getattr__(self, name: str) -> Any:
        return getattr(self._transport, name)

    async def handle_async_request(self, request: Any) -> Any:
        self._ledger.reserve_attempt()
        return await self._transport.handle_async_request(request)

    async def aclose(self) -> None:
        await self._transport.aclose()
