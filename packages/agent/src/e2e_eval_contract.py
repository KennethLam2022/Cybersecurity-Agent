"""Deterministic hard-gate checks for E2E RAG evaluation results."""
from __future__ import annotations

import math
from typing import Any


REQUIRED_METRICS = ("context_precision", "context_recall", "faithfulness", "relevancy", "hallucination")
DEFAULT_THRESHOLDS = {"faithfulness_min": 0.5, "no_hallucination_min": 0.5}


def normalize_metric_result(value: Any, evaluator: str) -> dict[str, Any]:
    """Normalize LLM and heuristic judge outputs for report/storage consumers."""
    if not isinstance(value, dict):
        value = {"score": value}
    raw_score = value.get("score")
    if raw_score is None:
        score = None
    else:
        try:
            score = max(0.0, min(1.0, float(raw_score)))
        except (TypeError, ValueError):
            score = 0.0
    reason = value.get("reason") or value.get("explanation") or ""
    details = value.get("details") if isinstance(value.get("details"), dict) else {}
    method = value.get("evaluator") or details.get("method") or evaluator
    return {
        "score": round(score, 4) if score is not None else None,
        "status": value.get("status") or ("scored" if score is not None else "unscored"),
        "reason": str(reason),
        "explanation": str(reason),
        "evaluator": method,
        "details": details,
    }


def metric_score_value(value: Any) -> float | None:
    """Return a finite score only for metrics explicitly marked as scored."""
    if isinstance(value, dict):
        if value.get("status") not in (None, "scored"):
            return None
        value = value.get("score")
    if isinstance(value, bool):
        return None
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return score if math.isfinite(score) else None


def _score(scores: dict[str, Any], name: str) -> float | None:
    return metric_score_value(scores.get(name))


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
    no_hallucination = _score(scores, "hallucination")
    checks["faithfulness_above_minimum"] = faithfulness is not None and faithfulness >= limits["faithfulness_min"]
    checks["no_hallucination_above_minimum"] = no_hallucination is not None and no_hallucination >= limits["no_hallucination_min"]
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
