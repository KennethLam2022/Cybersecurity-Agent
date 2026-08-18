"""Deterministic hard-gate checks for E2E RAG evaluation results."""
from __future__ import annotations

from typing import Any


REQUIRED_METRICS = ("context_precision", "context_recall", "faithfulness", "relevancy", "hallucination")
DEFAULT_THRESHOLDS = {"faithfulness_min": 0.5, "hallucination_max": 0.5}


def _score(scores: dict[str, Any], name: str) -> float | None:
    value = scores.get(name)
    if isinstance(value, dict):
        value = value.get("score")
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def evaluate_e2e_gates(result: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """Evaluate release-blocking conditions separately from soft quality scores."""
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    scores = result.get("scores") or {}
    checks = {
        "no_runtime_error": result.get("auto_status") != "运行错误" and not result.get("error"),
        "has_retrieved_sources": bool(result.get("sources")),
        "all_required_metrics_present": all(_score(scores, name) is not None for name in REQUIRED_METRICS),
    }
    faithfulness = _score(scores, "faithfulness")
    hallucination = _score(scores, "hallucination")
    checks["faithfulness_above_minimum"] = faithfulness is not None and faithfulness >= limits["faithfulness_min"]
    checks["hallucination_below_maximum"] = hallucination is not None and hallucination <= limits["hallucination_max"]
    failed = [name for name, passed in checks.items() if not passed]
    return {"passed": not failed, "checks": checks, "failed_checks": failed, "thresholds": limits}


def summarize_e2e_gates(results: list[dict[str, Any]], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    evaluated = [evaluate_e2e_gates(result, thresholds) for result in results]
    failed_items = [
        {"id": result.get("id"), "query": result.get("query", ""), "failed_checks": gate["failed_checks"]}
        for result, gate in zip(results, evaluated) if not gate["passed"]
    ]
    return {"passed": bool(results) and not failed_items, "total": len(results),
            "passed_cases": len(results) - len(failed_items), "failed_cases": len(failed_items),
            "failed_items": failed_items,
            "thresholds": evaluated[0]["thresholds"] if evaluated else {**DEFAULT_THRESHOLDS, **(thresholds or {})}}
