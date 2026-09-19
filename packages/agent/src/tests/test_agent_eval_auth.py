from auth import is_admin_route
from inspect import getsource, signature
from types import SimpleNamespace

import routes_api_eval
import routes_api_agent_eval


def test_agent_eval_api_is_admin_only():
    assert is_admin_route("/api/agent-eval/cases") is True
    assert is_admin_route("/api/agent-eval/run") is True
    assert is_admin_route("/api/agent-eval/runs/run-1/results") is True
    assert is_admin_route("/api/agent-eval/runs/run-1/judge") is True
    assert is_admin_route("/api/agent-eval/release-readiness/evidence") is True
    assert is_admin_route("/api/prompt/ab/runs") is True


def test_legacy_e2e_eval_endpoints_require_platform_authentication():
    endpoint_names = (
        "get_e2e_eval_items", "add_e2e_eval_item", "update_e2e_eval_item",
        "delete_e2e_eval_item", "seed_e2e_eval_items", "e2e_eval_generate_items",
        "e2e_eval_run", "e2e_eval_report", "e2e_eval_versions", "e2e_eval_latest",
        "e2e_eval_llm_conflict", "get_e2e_quality_summary", "get_e2e_eval_analysis",
    )
    for name in endpoint_names:
        endpoint = getattr(routes_api_eval, name)
        assert "request" in signature(endpoint).parameters
        assert "_legacy_eval_principal(request)" in getsource(endpoint)


def test_agent_eval_usage_sink_keeps_workspace_and_actor_scope(monkeypatch):
    calls = []
    monkeypatch.setattr(
        routes_api_agent_eval.agent.memory,
        "record_llm_usage_event",
        lambda **kwargs: calls.append(kwargs),
    )
    principal = SimpleNamespace(tenant_id="tenant-b", user_id="user-2", agent_id="agent-2")
    routes_api_agent_eval._agent_eval_usage_sink(principal)(
        {"model": "judge-model", "usage": {"prompt_tokens": 3, "completion_tokens": 4}}, "fallback"
    )
    assert calls == [{
        "tenant_id": "tenant-b", "user_id": "user-2", "agent_id": "agent-2",
        "module": "agent_eval_judge", "model": "judge-model",
        "prompt_tokens": 3, "completion_tokens": 4,
    }]


def test_sensitive_conversation_routes_require_request_scope():
    import routes_api
    from inspect import getsource, signature

    for name in ("set_jailbreak_status", "jailbreak_report"):
        endpoint = getattr(routes_api, name)
        assert "request" in signature(endpoint).parameters
        assert "_admin_conversation_scope(request, conv_id)" in getsource(endpoint)
