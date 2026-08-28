"""Offline, paired A/B comparison for saved System Prompt versions."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from evaluation_matrix import evaluate_with_weights
from prompt_test_manager import run_single_test


def _shared_docs(agent: Any, query: str) -> list[dict]:
    """Retrieve once so A and B receive the identical context snapshot."""
    return agent.retriever.search_multi(
        [query], top_k=agent.top_k, use_rerank=agent.use_rerank,
        metadata_filter={}, rerank_query=query, use_chroma_where=True,
    )


def _summary(results: list[dict]) -> dict:
    total = len(results)
    passed = sum(1 for result in results if result.get("passed"))
    dimensions: dict[str, list[float]] = defaultdict(list)
    for result in results:
        for name, score in (result.get("scores") or {}).items():
            dimensions[name].append(float(score))
    dimension_scores = {
        name: round(sum(values) / len(values), 4)
        for name, values in dimensions.items() if values
    }
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total * 100, 1) if total else 0,
        "weighted_score": round(evaluate_with_weights(dimension_scores), 2),
        "overall_score": round(
            sum(float(result.get("avg_score") or 0) for result in results) / total * 100, 2
        ) if total else 0,
        "avg_duration": round(
            sum(float(result.get("duration") or 0) for result in results) / total, 3
        ) if total else 0,
        "dimension_scores": dimension_scores,
    }


def run_prompt_ab_test(agent: Any, version_a: dict, version_b: dict,
                       items: list[dict]) -> dict:
    """Run paired test cases. Only the System Prompt differs between variants."""
    paired = []
    results_a = []
    results_b = []
    for item in items:
        docs = _shared_docs(agent, item["query"])
        result_a = run_single_test(
            item, agent, retrieved_docs=docs,
            system_prompt_override=version_a["system_prompt"],
        )
        result_b = run_single_test(
            item, agent, retrieved_docs=docs,
            system_prompt_override=version_b["system_prompt"],
        )
        results_a.append(result_a)
        results_b.append(result_b)
        delta = round(float(result_b.get("weighted_score") or 0) - float(result_a.get("weighted_score") or 0), 2)
        paired.append({
            "id": item.get("id"), "query": item.get("query", ""),
            "category": item.get("category", ""), "difficulty": item.get("difficulty", "medium"),
            "a": result_a, "b": result_b, "weighted_score_delta_b_minus_a": delta,
            "winner": "B" if delta > 0.01 else ("A" if delta < -0.01 else "平局"),
        })

    summary_a = _summary(results_a)
    summary_b = _summary(results_b)
    delta = {
        key: round(float(summary_b[key]) - float(summary_a[key]), 2)
        for key in ("pass_rate", "weighted_score", "overall_score", "avg_duration")
    }
    return {
        "version_a": {"id": version_a["id"], "name": version_a["name"]},
        "version_b": {"id": version_b["id"], "name": version_b["name"]},
        "summary_a": summary_a,
        "summary_b": summary_b,
        "delta_b_minus_a": delta,
        "winner": "B" if delta["weighted_score"] > 0.01 else (
            "A" if delta["weighted_score"] < -0.01 else "平局"
        ),
        "paired_results": paired,
    }
