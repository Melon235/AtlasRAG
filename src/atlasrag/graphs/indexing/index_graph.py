"""Thin six-node knowledge maintenance graph; no serving lifecycle control."""

from __future__ import annotations

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph
from langgraph.graph.state import CompiledStateGraph

from atlasrag.graphs.indexing.sync_contracts import IndexGraphSteps
from atlasrag.graphs.indexing.sync_state import (
    IndexGraphInput,
    IndexGraphOutput,
    IndexGraphState,
)
from atlasrag.services.sync.failures import ScanError, SyncPlanError
from atlasrag.services.sync.models import ReconciliationOutcome
from atlasrag.services.sync.results import finalize_sync_result


def build_index_graph(
    services: IndexGraphSteps,
) -> CompiledStateGraph[IndexGraphState, None, IndexGraphInput, IndexGraphOutput]:
    """Compile fresh for one invocation; no checkpointer or runtime dependencies."""

    async def reconcile(state: IndexGraphState) -> IndexGraphState:
        result = await services.reconcile()
        if result.outcome is ReconciliationOutcome.BLOCKED:
            return {
                "reconciliation_result": result,
                "final_result": finalize_sync_result(
                    reconciliation=result,
                    plan=None,
                    builds=None,
                    deletions=None,
                    safety=None,
                ),
            }
        return {"reconciliation_result": result}

    async def scan(state: IndexGraphState) -> IndexGraphState:
        reconciliation = state.get("reconciliation_result")
        if reconciliation is None:
            raise RuntimeError("reconciliation result is missing")
        try:
            snapshot = await services.scan(state["request"].knowledge_directory)
        except ScanError as error:
            return {
                "final_result": finalize_sync_result(
                    reconciliation=reconciliation,
                    plan=None,
                    builds=None,
                    deletions=None,
                    safety=None,
                    blocked_code=error.code,
                )
            }
        return {"scan_snapshot": snapshot}

    async def plan(state: IndexGraphState) -> IndexGraphState:
        reconciliation = state.get("reconciliation_result")
        scan_snapshot = state.get("scan_snapshot")
        if reconciliation is None or scan_snapshot is None:
            raise RuntimeError("plan inputs are missing")
        try:
            value = await services.plan(scan_snapshot)
        except SyncPlanError as error:
            return {
                "final_result": finalize_sync_result(
                    reconciliation=reconciliation,
                    plan=None,
                    builds=None,
                    deletions=None,
                    safety=None,
                    blocked_code=error.code,
                )
            }
        return {"sync_plan": value}

    def blocked(state: IndexGraphState) -> bool:
        return "final_result" in state

    def next_node(name: str) -> Callable[[IndexGraphState], str]:
        def choose(state: IndexGraphState) -> str:
            return END if blocked(state) else name

        return choose

    async def execute_builds(state: IndexGraphState) -> IndexGraphState:
        reconciliation = state.get("reconciliation_result")
        scan_snapshot = state.get("scan_snapshot")
        plan_value = state.get("sync_plan")
        if reconciliation is None or scan_snapshot is None or plan_value is None:
            raise RuntimeError("build inputs are missing")
        summary = await services.execute_builds(
            plan_value, scan_snapshot, reconciliation
        )
        return {"build_summary": summary}

    async def execute_deletions(state: IndexGraphState) -> IndexGraphState:
        scan_snapshot = state.get("scan_snapshot")
        plan_value = state.get("sync_plan")
        if scan_snapshot is None or plan_value is None:
            raise RuntimeError("deletion inputs are missing")
        summary = await services.execute_deletions(plan_value, scan_snapshot)
        return {"deletion_summary": summary}

    async def final_verify(state: IndexGraphState) -> IndexGraphState:
        reconciliation = state.get("reconciliation_result")
        plan_value = state.get("sync_plan")
        builds = state.get("build_summary")
        deletions = state.get("deletion_summary")
        if (
            reconciliation is None
            or plan_value is None
            or builds is None
            or deletions is None
        ):
            raise RuntimeError("final verification inputs are missing")
        safety = await services.final_verify(builds, deletions)
        return {
            "final_result": finalize_sync_result(
                reconciliation=reconciliation,
                plan=plan_value,
                builds=builds,
                deletions=deletions,
                safety=safety,
            )
        }

    graph = StateGraph(
        IndexGraphState,
        input_schema=IndexGraphInput,
        output_schema=IndexGraphOutput,
    )
    graph.add_node("reconcile_runtime", reconcile)
    graph.add_node("scan_directory", scan)
    graph.add_node("build_sync_plan", plan)
    graph.add_node("execute_builds", execute_builds)
    graph.add_node("execute_deletions", execute_deletions)
    graph.add_node("final_verify", final_verify)
    graph.add_edge(START, "reconcile_runtime")
    graph.add_conditional_edges(
        "reconcile_runtime", next_node("scan_directory"), ["scan_directory", END]
    )
    graph.add_conditional_edges(
        "scan_directory", next_node("build_sync_plan"), ["build_sync_plan", END]
    )
    graph.add_conditional_edges(
        "build_sync_plan", next_node("execute_builds"), ["execute_builds", END]
    )
    graph.add_edge("execute_builds", "execute_deletions")
    graph.add_edge("execute_deletions", "final_verify")
    graph.add_edge("final_verify", END)
    return graph.compile()
