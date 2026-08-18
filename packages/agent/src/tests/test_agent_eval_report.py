from agent_eval.report import render_agent_eval_report


def test_agent_eval_report_contains_gate_and_failed_case_details():
    report = render_agent_eval_report(
        {"run_id": "run-1", "summary": {"total": 1}, "gate": {"passed": False, "reasons": ["安全失败"]}},
        [{"case_key": "SAFE-1", "query": "危险问题", "elapsed_ms": 12,
          "metrics": {"task_success": False, "safety_pass": False}}],
    )

    assert "发布门禁：阻塞" in report
    assert "SAFE-1" in report
    assert "safety_pass" in report
