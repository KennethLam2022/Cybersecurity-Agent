from agent_eval.comparison import compare_runs


def test_compare_runs_reports_metric_and_case_regressions():
    baseline = {"run_id": "base", "summary": {"pass_rate": 0.9, "p95_latency_ms": 1000, "errors": 0, "security_failures": 0}}
    candidate = {"run_id": "candidate", "summary": {"pass_rate": 0.8, "p95_latency_ms": 1200, "errors": 1, "security_failures": 1}}
    base_results = [{"case_key": "A", "metrics": {"task_success": True}}]
    candidate_results = [{"case_key": "A", "metrics": {"task_success": False}}]

    result = compare_runs(baseline, candidate, base_results, candidate_results)

    assert result["passed"] is False
    assert result["case_regressions"] == ["A"]
    assert "case_regressions" in result["blockers"]
    assert result["metrics"]["pass_rate"]["delta"] == -0.1
