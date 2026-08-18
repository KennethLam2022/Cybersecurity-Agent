from agent_eval.release_gate import evaluate_release_gate


def test_release_gate_passes_stable_run():
    result = evaluate_release_gate({
        "total": 30, "pass_rate": 0.9, "flaky_rate": 0,
        "errors": 0, "p95_latency_ms": 1200,
    })
    assert result["passed"] is True
    assert result["reasons"] == []


def test_release_gate_reports_all_blockers():
    result = evaluate_release_gate({
        "total": 10, "pass_rate": 0.4, "flaky_rate": 0.2,
        "errors": 1, "security_failures": 1, "p95_latency_ms": 12000,
    })
    assert result["passed"] is False
    assert len(result["reasons"]) == 5
