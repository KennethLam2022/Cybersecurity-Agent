import json

import pytest

import eval_e2e
from e2e_eval_contract import evaluate_e2e_gates


def test_e2e_persists_relevance_and_counts_only_scored_samples(tmp_path, monkeypatch):
    class Agent:
        llm = None

        def ask(self, query, profiles=None):
            return {"answer": "answer", "sources": [{"content": "evidence", "file_name": "policy"}], "stats": {}}

    def score(*args, **kwargs):
        return {"score": 0.8}

    def relevance(query, docs, **options):
        return {
            "score": 0.75 if query == "scored" else None,
            "status": "scored" if query == "scored" else "judge_error",
            "details": {"unit_count": 4, "relevant_unit_count": 3} if query == "scored" else {"failure_category": "call_failed"},
            "evaluator": {"method": "llm_judge", "model_id": "test", "prompt_version": "v1"},
        }

    for name in ("eval_context_precision", "eval_context_recall", "eval_faithfulness", "eval_relevancy", "eval_hallucination"):
        monkeypatch.setattr(eval_e2e, name, score)
    monkeypatch.setattr(eval_e2e, "eval_context_relevance", relevance, raising=False)
    questions = [{"id": "E1", "query": "scored", "domain": "test", "difficulty": "基础"},
                 {"id": "E2", "query": "failed", "domain": "test", "difficulty": "基础"}]
    path = tmp_path / "e2e.json"
    results = eval_e2e.run_evaluation(Agent(), questions, use_llm=False, output_file=path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[0]["scores"]["context_relevance"]["score"] == 0.75
    assert saved[1]["scores"]["context_relevance"]["status"] == "judge_error"
    stats = eval_e2e._compute_stats(results)
    assert stats["avg_scores"]["context_relevance"] == 0.75
    assert stats["profile_stats"]["general"]["avg_scores"]["context_relevance"] == 0.75
    assert stats["metric_coverage"]["context_relevance"] == {"scored": 1, "total": 2, "unscored_reasons": {"judge_error": 1}}
    monkeypatch.setattr(eval_e2e, "_VERSION_FILE", tmp_path / "versions.json")
    eval_e2e._save_version(path, tmp_path / "report.html", "test", stats)
    version = eval_e2e._load_versions()[0]
    assert version["avg_scores"]["context_relevance"] == 0.75
    assert version["metric_coverage"] == stats["metric_coverage"]


@pytest.mark.parametrize("metric", [None, {"score": None, "status": "judge_error"}])
def test_e2e_completeness_reports_missing_or_unscored_relevance(metric):
    result = {"auto_status": "有来源", "sources": [{"file_name": "policy"}],
              "scores": {key: {"score": 0.8} for key in (
                  "context_precision", "context_recall", "faithfulness", "relevancy", "hallucination")}}
    if metric is not None:
        result["scores"]["context_relevance"] = metric
    gate = evaluate_e2e_gates(result)
    assert gate["checks"]["all_required_metrics_present"] is False
    assert gate["checks"]["faithfulness_above_minimum"] is True
    assert gate["checks"]["no_hallucination_above_minimum"] is True
    stats = eval_e2e._compute_stats([result])
    assert stats["avg_scores"]["context_relevance"] is None
    assert stats["avg_scores"]["context_precision"] == 0.8


def test_zero_relevance_is_present_and_does_not_add_an_unapproved_threshold():
    result = {"auto_status": "有来源", "sources": [{"file_name": "policy"}],
              "scores": {key: {"score": 0.8} for key in (
                  "context_precision", "context_recall", "faithfulness", "relevancy", "hallucination")}}
    result["scores"]["context_relevance"] = {"score": 0, "status": "scored"}
    assert evaluate_e2e_gates(result)["passed"] is True
