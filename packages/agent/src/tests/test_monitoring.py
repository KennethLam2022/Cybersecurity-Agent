from memory import ConversationMemory
from monitoring import build_issue_report, generate_issue_diagnosis, record_event, scan_cost_spike
from monitoring_adapters import execute_approved_change, get_change_adapter, run_approved_change_action, validate_change_plan


def test_monitoring_event_report_and_review_state_are_scoped_and_non_executable(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring.db"))
    owner = memory.register_user("monitor-owner@example.com", "Correct-Horse-30", "Owner")
    event = record_event(memory, owner["tenant_id"], "llm_timeout", "P1", "chat", {"count": 3}, "trace-1", "模型调用连续超时")
    assert event["severity"] == "P1"
    report = memory.create_monitoring_issue_report(
        owner["tenant_id"], event["id"], build_issue_report(event), owner["id"]
    )
    assert report["state"] == "pending_review"
    assert report["report"]["review_only"] is True
    reviewed = memory.update_monitoring_issue_report(
        report["id"], owner["tenant_id"], "review_plan", owner["id"], "补充回滚和验证步骤"
    )
    assert reviewed["state"] == "review_plan"
    edited = memory.update_monitoring_issue_report_content(
        report["id"], owner["tenant_id"],
        {"change_plan": {"change": "切换到备用模型", "rollback": "恢复原模型", "validation": "运行 10 条 Agent 评测"}},
        owner["id"],
    )
    assert edited["report"]["change_plan"]["rollback"] == "恢复原模型"
    assert edited["report"]["review_only"] is True
    other = memory.register_user("monitor-other@example.com", "Correct-Horse-30", "Other")
    assert memory.list_monitoring_events(other["tenant_id"]) == []
    assert memory.get_monitoring_issue_report(report["id"], other["tenant_id"]) is None


def test_monitoring_rejects_invalid_state_and_severity(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring-invalid.db"))
    owner = memory.register_user("monitor-invalid@example.com", "Correct-Horse-30", "Owner")
    try:
        record_event(memory, owner["tenant_id"], "bad", "P9")
    except ValueError as exc:
        assert "级别" in str(exc)
    else:
        raise AssertionError("invalid severity must be rejected")


def test_monitoring_report_state_is_a_controlled_workflow(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring-state.db"))
    owner = memory.register_user("monitor-state@example.com", "Correct-Horse-31", "Owner")
    event = record_event(memory, owner["tenant_id"], "llm_timeout")
    report = memory.create_monitoring_issue_report(
        owner["tenant_id"], event["id"], build_issue_report(event), owner["id"]
    )
    try:
        memory.update_monitoring_issue_report(
            report["id"], owner["tenant_id"], "approved", owner["id"], "绕过审阅"
        )
    except ValueError as exc:
        assert "不允许" in str(exc)
    else:
        raise AssertionError("report must not skip the review workflow")

    memory.update_monitoring_issue_report(
        report["id"], owner["tenant_id"], "review_plan", owner["id"], "开始编制预案"
    )
    memory.update_monitoring_issue_report(
        report["id"], owner["tenant_id"], "pending_approval", owner["id"], "提交评审"
    )
    assert memory.update_monitoring_issue_report(
        report["id"], owner["tenant_id"], "approved", owner["id"], "评审通过"
    )["state"] == "approved"


def test_monitoring_verification_requires_approved_report_and_is_audited(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring-verification.db"))
    owner = memory.register_user("monitor-verify@example.com", "Correct-Horse-32", "Owner")
    event = record_event(memory, owner["tenant_id"], "llm_timeout")
    report = memory.create_monitoring_issue_report(
        owner["tenant_id"], event["id"], build_issue_report(event), owner["id"]
    )
    try:
        memory.create_monitoring_report_verification(report["id"], owner["tenant_id"], "passed")
    except ValueError as exc:
        assert "已通过" in str(exc)
    else:
        raise AssertionError("unapproved report must not be verified")
    for state in ("review_plan", "pending_approval", "approved"):
        memory.update_monitoring_issue_report(report["id"], owner["tenant_id"], state, owner["id"])
    verification = memory.create_monitoring_report_verification(
        report["id"], owner["tenant_id"], "passed",
        [{"name": "回归评测", "status": "passed", "detail": "10 条用例通过"}],
        "验证完成", owner["id"],
    )
    assert verification["result"] == "passed"
    assert verification["checks"][0]["name"] == "回归评测"


def test_monitoring_summary_groups_severity_and_event_type(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring-summary.db"))
    owner = memory.register_user("monitor-summary@example.com", "Correct-Horse-30", "Owner")
    record_event(memory, owner["tenant_id"], "llm_timeout", "P1")
    record_event(memory, owner["tenant_id"], "llm_timeout", "P1")
    record_event(memory, owner["tenant_id"], "retrieval_empty", "P2")
    summary = memory.summarize_monitoring_events(owner["tenant_id"])
    assert summary["total"] == 3
    assert summary["by_severity"]["P1"] == 2
    assert summary["by_type"]["llm_timeout"] == 2


def test_cost_spike_scan_is_configurable_and_metadata_only(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring-cost.db"))
    owner = memory.register_user("monitor-cost@example.com", "Correct-Horse-30", "Owner")
    memory.upsert_model_pricing({"provider": "test", "model": "model", "input_price_per_million": 100000,
                                 "output_price_per_million": 0, "quality_score": 0.5, "updated_by": owner["id"]})
    memory.record_llm_usage_event(owner["tenant_id"], owner["id"], owner["agent_id"], module="chat",
                                  provider="test", model="model", prompt_tokens=100)
    result = scan_cost_spike(memory, owner["tenant_id"], multiplier=2, minimum_cost=0.001)
    assert result["triggered"] is True
    assert result["event"]["event_type"] == "llm_cost_spike"
    assert "query" not in result["event"]["metrics"]


def test_monitoring_ai_diagnosis_is_review_only_and_metadata_bounded():
    class FakeLLM:
        model = "test-diagnosis-model"

        def chat(self, messages, **kwargs):
            assert "用户原始问题" not in messages[-1]["content"]
            return {"content": '{"root_cause_hypothesis":"连接超时假设","recommendation":"检查服务健康状态","confidence":0.7}',
                    "usage": {"prompt_tokens": 10, "completion_tokens": 8}, "model": self.model}

    event = {"event_type": "llm_timeout", "severity": "P1", "module": "chat",
             "metrics": {"count": 3}, "trace_id": "trace-1", "detail": "模型调用超时"}
    report = build_issue_report(event)
    diagnosis = generate_issue_diagnosis(FakeLLM(), event, report)
    assert diagnosis["status"] == "generated"
    assert diagnosis["review_only"] is True
    assert diagnosis["confidence"] == 0.7


def test_monitoring_change_adapter_requires_approved_report_and_is_side_effect_free(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring-adapter.db"))
    owner = memory.register_user("monitor-adapter@example.com", "Correct-Horse-30", "Owner")
    event = record_event(memory, owner["tenant_id"], "llm_timeout")
    report = memory.create_monitoring_issue_report(owner["tenant_id"], event["id"], build_issue_report(event), owner["id"])
    try:
        execute_approved_change(report)
    except ValueError as exc:
        assert "评审通过" in str(exc)
    else:
        raise AssertionError("unapproved report must not execute")
    for state in ("review_plan", "pending_approval", "approved"):
        memory.update_monitoring_issue_report(report["id"], owner["tenant_id"], state, owner["id"])
    current = memory.get_monitoring_issue_report(report["id"], owner["tenant_id"])
    result = execute_approved_change(current)
    assert result["status"] == "manual_required"
    assert result["side_effects"] is False
    assert validate_change_plan({"change": "x", "rollback": "y", "validation": "z"})["adapter"] == "manual_handoff"


def test_webhook_adapter_requires_https_and_explicit_host_allowlist(monkeypatch):
    monkeypatch.setenv("CYBER_AGENT_MONITORING_WEBHOOK_URL", "http://gateway.example.test/change")
    monkeypatch.setenv("CYBER_AGENT_MONITORING_WEBHOOK_ALLOWED_HOSTS", "gateway.example.test")
    try:
        get_change_adapter("webhook_change")
    except ValueError as exc:
        assert "HTTPS" in str(exc)
    else:
        raise AssertionError("non-HTTPS webhook must be rejected")
    monkeypatch.setenv("CYBER_AGENT_MONITORING_WEBHOOK_URL", "https://gateway.example.test/change")
    monkeypatch.setenv("CYBER_AGENT_MONITORING_WEBHOOK_ALLOWED_HOSTS", "other.example.test")
    try:
        get_change_adapter("webhook_change")
    except ValueError as exc:
        assert "允许主机" in str(exc)
    else:
        raise AssertionError("unallowlisted webhook host must be rejected")


def test_manual_adapter_supports_verify_and_rollback_without_side_effects(tmp_path):
    memory = ConversationMemory(str(tmp_path / "monitoring-adapter-actions.db"))
    owner = memory.register_user("monitor-actions@example.com", "Correct-Horse-30", "Owner")
    event = record_event(memory, owner["tenant_id"], "llm_timeout")
    report = memory.create_monitoring_issue_report(owner["tenant_id"], event["id"], build_issue_report(event), owner["id"])
    for state in ("review_plan", "pending_approval", "approved"):
        memory.update_monitoring_issue_report(report["id"], owner["tenant_id"], state, owner["id"])
    current = memory.get_monitoring_issue_report(report["id"], owner["tenant_id"])
    for action in ("verify", "rollback"):
        result = run_approved_change_action(current, action)
        assert result["status"] == "manual_required"
        assert result["side_effects"] is False
