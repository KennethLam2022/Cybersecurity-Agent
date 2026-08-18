from types import SimpleNamespace

from agent_eval.preflight import build_preflight


def test_preflight_is_no_call_and_reports_estimate():
    agent = SimpleNamespace(llm=SimpleNamespace(model="test-model", base_url="http://test"))
    cases = [{"case_key": "GEN-1", "profile": "general"}]

    result = build_preflight(agent, cases, "general", repetitions=3)

    assert result["ready"] is True
    assert result["estimated_agent_calls"] == 3
    assert result["estimated_judge_calls"] == 0
    assert result["model"] == "test-model"


def test_preflight_rejects_missing_endpoint_and_judge():
    agent = SimpleNamespace(llm=SimpleNamespace(model="test-model", base_url=""))

    result = build_preflight(agent, [], "general", judge_requested=True)

    assert result["ready"] is False
    assert "聊天模型地址未配置" in result["errors"]
    assert "profile=general 没有可运行用例" in result["errors"]
    assert "独立评测 LLM 未配置" in result["errors"]
