"""Deterministic baseline/candidate comparison for Agent Evaluation runs."""
from __future__ import annotations

from typing import Any


def _metric(summary: dict[str, Any], key: str) -> float:
    try:
        return float(summary.get(key) or 0)
    except (TypeError, ValueError):
        return 0.0


def compare_runs(baseline: dict[str, Any], candidate: dict[str, Any],
                 baseline_results: list[dict[str, Any]] | None = None,
                 candidate_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Compare two completed runs and identify release-relevant regressions."""
    base_summary = baseline.get("summary") or {}
    cand_summary = candidate.get("summary") or {}
    metrics = {}
    for key in ("pass_rate", "p95_latency_ms", "flaky_rate", "errors", "security_failures",
                "answer_point_coverage_avg"):
        base = _metric(base_summary, key)
        cand = _metric(cand_summary, key)
        metrics[key] = {"baseline": base, "candidate": cand, "delta": round(cand - base, 4)}

    base_by_case = {item.get("case_key"): item for item in (baseline_results or [])}
    cand_by_case = {item.get("case_key"): item for item in (candidate_results or [])}
    regressions = []
    improvements = []
    for case_key in sorted(set(base_by_case) | set(cand_by_case)):
        before = base_by_case.get(case_key, {}).get("metrics", {}).get("task_success")
        after = cand_by_case.get(case_key, {}).get("metrics", {}).get("task_success")
        if before is True and after is False:
            regressions.append(case_key)
        elif before is False and after is True:
            improvements.append(case_key)

    blockers = []
    if metrics["pass_rate"]["delta"] < 0:
        blockers.append("pass_rate_regressed")
    if metrics["p95_latency_ms"]["delta"] > 0:
        blockers.append("p95_latency_regressed")
    if metrics["errors"]["delta"] > 0:
        blockers.append("errors_increased")
    if metrics["security_failures"]["delta"] > 0:
        blockers.append("security_failures_increased")
    if regressions:
        blockers.append("case_regressions")
    return {
        "baseline_run_id": baseline.get("run_id"),
        "candidate_run_id": candidate.get("run_id"),
        "metrics": metrics,
        "case_regressions": regressions,
        "case_improvements": improvements,
        "blockers": blockers,
        "passed": not blockers,
    }
