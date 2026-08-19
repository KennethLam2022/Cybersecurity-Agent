"""Compare advisory LLM-judge scores with independently recorded human reviews."""
from __future__ import annotations

from typing import Any


DEFAULT_METRICS = ("answer_completeness", "faithfulness", "relevancy")


def _score(value: Any) -> float | None:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(1.0, score))


def build_judge_calibration_report(
    items: list[dict[str, Any]], *, metric_keys: tuple[str, ...] = DEFAULT_METRICS,
    agreement_tolerance: float = 0.2,
) -> dict[str, Any]:
    """Calculate agreement and error only for scores reviewed by a human.

    A missing judge score or missing human score is excluded rather than treated
    as zero.  This keeps partial calibration sets honest.
    """
    tolerance = max(0.0, min(float(agreement_tolerance), 1.0))
    per_metric: dict[str, dict[str, Any]] = {
        metric: {"count": 0, "agreement_count": 0, "absolute_errors": []}
        for metric in metric_keys
    }
    disagreements = []
    reviewed_count = 0
    for item in items:
        judge_scores = item.get("judge_scores") or {}
        human_scores = item.get("human_scores") or {}
        compared = []
        for metric in metric_keys:
            judge = _score(judge_scores.get(metric))
            human = _score(human_scores.get(metric))
            if judge is None or human is None:
                continue
            error = round(abs(judge - human), 4)
            metric_stats = per_metric[metric]
            metric_stats["count"] += 1
            metric_stats["absolute_errors"].append(error)
            if error <= tolerance:
                metric_stats["agreement_count"] += 1
            else:
                compared.append({"metric": metric, "judge": judge, "human": human, "absolute_error": error})
        if compared:
            disagreements.append({
                "result_key": str(item.get("result_key") or item.get("id") or ""),
                "case_key": str(item.get("case_key") or ""),
                "differences": compared,
                "reviewer": str(item.get("reviewer") or ""),
                "note": str(item.get("note") or ""),
            })
        if any(_score(human_scores.get(metric)) is not None for metric in metric_keys):
            reviewed_count += 1

    total_pairs = sum(stats["count"] for stats in per_metric.values())
    total_agreements = sum(stats["agreement_count"] for stats in per_metric.values())
    for stats in per_metric.values():
        errors = stats.pop("absolute_errors")
        stats["mean_absolute_error"] = round(sum(errors) / len(errors), 4) if errors else None
        stats["agreement_rate"] = round(stats["agreement_count"] / stats["count"], 4) if stats["count"] else None

    disagreements.sort(key=lambda item: max(diff["absolute_error"] for diff in item["differences"]), reverse=True)
    return {
        "reviewed_count": reviewed_count,
        "compared_score_count": total_pairs,
        "agreement_tolerance": tolerance,
        "agreement_rate": round(total_agreements / total_pairs, 4) if total_pairs else None,
        "mean_absolute_error": round(
            sum(stats["mean_absolute_error"] * stats["count"] for stats in per_metric.values()
                if stats["mean_absolute_error"] is not None) / total_pairs,
            4,
        ) if total_pairs else None,
        "per_metric": per_metric,
        "disagreements": disagreements,
    }
