from p5_release_gate import evaluate_p5_release_readiness


def test_p5_release_gate_distinguishes_automatic_and_manual_acceptance():
    result = evaluate_p5_release_readiness({
        "tests_passed": True, "build_passed": True, "p5_eval_passed": True,
        "artifact_quality_passed": True, "http_isolation_passed": True,
    })
    assert result["status"] == "blocked"
    assert "real_model_baseline_approved" in result["blocking_checks"]
    assert "tests_passed" not in result["blocking_checks"]
    assert "real_model_baseline_approved" in result["manual_checks"]


def test_p5_release_gate_can_close_when_all_required_evidence_exists():
    evidence = {
        name: True for name in (
            "tests_passed", "build_passed", "p5_eval_passed", "artifact_quality_passed",
            "http_isolation_passed", "real_model_baseline_approved",
            "human_judge_calibration_approved", "langfuse_scope_approved_or_disabled",
        )
    }
    result = evaluate_p5_release_readiness(evidence)
    assert result["passed"] is True
    assert result["blocking_checks"] == []


def test_mcp_execution_is_optional_until_explicitly_required():
    evidence = {
        name: True for name in (
            "tests_passed", "build_passed", "p5_eval_passed", "artifact_quality_passed",
            "http_isolation_passed", "real_model_baseline_approved",
            "human_judge_calibration_approved", "langfuse_scope_approved_or_disabled",
        )
    }
    assert evaluate_p5_release_readiness(evidence)["passed"] is True
    result = evaluate_p5_release_readiness(evidence, require_mcp_execution=True)
    assert result["passed"] is False
    assert "mcp_execution_approved" in result["blocking_checks"]
