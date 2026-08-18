from auth import is_admin_route


def test_agent_eval_api_is_admin_only():
    assert is_admin_route("/api/agent-eval/cases") is True
    assert is_admin_route("/api/agent-eval/run") is True
    assert is_admin_route("/api/agent-eval/runs/run-1/results") is True
