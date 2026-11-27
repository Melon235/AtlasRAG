"""Invocation-scoped ProductionObserver routing without transport coupling."""

from __future__ import annotations

import threading

from atlasrag.domain.observation import (
    ProductionObservation,
    ProductionObserver,
    current_invocation_id,
)


class InvocationObserverMux:
    """Dispatch production observations strictly to the matching invocation sink."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sinks: dict[str, ProductionObserver] = {}

    def bind(self, invocation_id: str, observer: ProductionObserver) -> None:
        """Bind a per-invocation observer sink before retrieval observations start."""

        if not invocation_id.strip():
            raise ValueError("invocation_id must be nonempty")
        with self._lock:
            self._sinks[invocation_id] = observer

    def unbind(self, invocation_id: str) -> None:
        """Remove a bound invocation observer sink on settlement."""

        with self._lock:
            self._sinks.pop(invocation_id, None)

    def bound_count(self) -> int:
        with self._lock:
            return len(self._sinks)

    def observe(self, event: ProductionObservation) -> None:
        """Route an observation strictly to the sink matching its invocation_id."""

        invocation_id = event.invocation_id or current_invocation_id()
        if not invocation_id:
            return
        with self._lock:
            sink = self._sinks.get(invocation_id)
        if sink is not None:
            sink.observe(event)
