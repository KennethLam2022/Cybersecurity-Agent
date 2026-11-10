from e2e_eval_contract import (
    evaluate_e2e_gates,
    metric_score_value,
    normalize_metric_result,
    summarize_e2e_gates,
)


def _result(**overrides):
    result = {"auto_status": "有来源(2条)", "sources": [{"file_name": "policy.pdf"}],
              "scores": {name: {"score": 0.8} for name in (
                  "context_precision", "context_recall", "context_relevance", "faithfulness", "relevancy")}}
    result["scores"]["hallucination"] = {"score": 0.8}
    result.update(overrides)
    return result


def test_e2e_gate_blocks_missing_sources_and_runtime_errors():
    gate = evaluate_e2e_gates(_result(sources=[], auto_status="无来源"))
    assert gate["passed"] is False
    assert "has_retrieved_sources" in gate["failed_checks"]


def test_e2e_gate_blocks_low_faithfulness_or_low_no_hallucination():
    gate = evaluate_e2e_gates(_result(scores={
        "context_precision": {"score": 0.9}, "context_recall": {"score": 0.9},
        "faithfulness": {"score": 0.4}, "relevancy": {"score": 0.9}, "hallucination": {"score": 0.4},
    }))
    assert "faithfulness_above_minimum" in gate["failed_checks"]
    assert "no_hallucination_above_minimum" in gate["failed_checks"]


def test_e2e_gate_summary_requires_every_case_to_pass():
    summary = summarize_e2e_gates([_result(), _result(sources=[])])
    assert summary["passed"] is False
    assert summary["failed_cases"] == 1


def test_metric_result_has_stable_reason_and_evaluator_fields():
    result = normalize_metric_result({"score": 0.7, "explanation": "有依据", "details": {"method": "heuristic"}}, "fallback")

    assert result["score"] == 0.7
    assert result["reason"] == "有依据"
    assert result["evaluator"] == "heuristic"


def test_metric_result_preserves_unscored_state_and_evaluator_metadata():
    evaluator = {"method": "llm_judge", "model_id": "judge-v1"}
    result = normalize_metric_result({
        "score": None,
        "status": "judge_error",
        "explanation": "Judge unavailable",
        "details": {"failure_category": "call_failed"},
        "evaluator": evaluator,
    }, "fallback")

    assert result["score"] is None
    assert result["status"] == "judge_error"
    assert result["evaluator"] == evaluator
    assert result["details"] == {"failure_category": "call_failed"}


def test_non_scored_metric_status_takes_precedence_over_stale_score():
    assert metric_score_value({"score": 0.9, "status": "judge_error"}) is None
