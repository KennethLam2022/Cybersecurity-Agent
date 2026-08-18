from e2e_eval_contract import evaluate_e2e_gates, summarize_e2e_gates


def _result(**overrides):
    result = {"auto_status": "有来源(2条)", "sources": [{"file_name": "policy.pdf"}],
              "scores": {name: {"score": 0.8} for name in (
                  "context_precision", "context_recall", "faithfulness", "relevancy", "hallucination")}}
    result.update(overrides)
    return result


def test_e2e_gate_blocks_missing_sources_and_runtime_errors():
    gate = evaluate_e2e_gates(_result(sources=[], auto_status="无来源"))
    assert gate["passed"] is False
    assert "has_retrieved_sources" in gate["failed_checks"]


def test_e2e_gate_blocks_low_faithfulness_or_high_hallucination():
    gate = evaluate_e2e_gates(_result(scores={
        "context_precision": {"score": 0.9}, "context_recall": {"score": 0.9},
        "faithfulness": {"score": 0.4}, "relevancy": {"score": 0.9}, "hallucination": {"score": 0.6},
    }))
    assert "faithfulness_above_minimum" in gate["failed_checks"]
    assert "hallucination_below_maximum" in gate["failed_checks"]


def test_e2e_gate_summary_requires_every_case_to_pass():
    summary = summarize_e2e_gates([_result(), _result(sources=[])])
    assert summary["passed"] is False
    assert summary["failed_cases"] == 1
