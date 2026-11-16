"""Minimal deterministic benchmark report renderer."""

from __future__ import annotations

from collections.abc import Mapping


def render_metrics_report(
    *,
    run_id: str,
    dataset_revision: str,
    counts: Mapping[str, object],
    metrics: Mapping[str, object],
    validity: str,
) -> str:
    lines = [
        "# AtlasRAG Stage 9 Benchmark Report",
        "",
        f"- run_id: `{run_id}`",
        f"- dataset_revision: `{dataset_revision}`",
        f"- run_validity: `{validity}`",
        "",
        "## Dataset",
        "",
    ]
    for key in sorted(counts):
        lines.append(f"- {key}: {counts[key]}")
    lines += ["", "## Metrics", ""]
    for key in sorted(metrics):
        lines.append(f"- {key}: {metrics[key]}")
    lines += [
        "",
        "> This report is valid only for the declared snapshot, corpus profile, "
        "query set, evaluator fingerprint, and run configuration.",
    ]
    return "\n".join(lines) + "\n"
