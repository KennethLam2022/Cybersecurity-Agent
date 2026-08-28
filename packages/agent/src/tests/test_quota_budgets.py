from memory import ConversationMemory


def test_quota_budget_warns_and_blocks_by_user_and_tenant(tmp_path):
    memory = ConversationMemory(str(tmp_path / "quota.db"))
    user = memory.register_user("quota-owner@example.com", "Correct-Horse-37", "Quota Owner")
    memory.save_quota_budget("tenant", user["tenant_id"], token_limit=100, warn_percent=80,
                             tenant_id=user["tenant_id"], created_by=user["id"])
    memory.record_llm_usage_event(user["tenant_id"], user["id"], user["agent_id"],
                                  model="unpriced", prompt_tokens=85, completion_tokens=0)
    warned = memory.check_quota(user["tenant_id"], user["id"])
    assert warned["allowed"] is True
    assert warned["warnings"]
    blocked = memory.check_quota(user["tenant_id"], user["id"], requested_tokens=16)
    assert blocked["allowed"] is False


def test_quota_budget_cost_limit_uses_local_model_pricing(tmp_path):
    memory = ConversationMemory(str(tmp_path / "quota-cost.db"))
    user = memory.register_user("quota-cost@example.com", "Correct-Horse-38", "Quota Cost")
    memory.upsert_model_pricing({"provider": "test-provider", "model": "test-model",
                                 "input_price_per_million": 1.0,
                                 "output_price_per_million": 1.0,
                                 "updated_by": "tester"})
    memory.save_quota_budget("user", user["id"], cost_limit=0.00001,
                             tenant_id=user["tenant_id"], created_by=user["id"])
    memory.record_llm_usage_event(user["tenant_id"], user["id"], user["agent_id"],
                                  provider="test-provider", model="test-model",
                                  prompt_tokens=20, completion_tokens=0)
    assert memory.check_quota(user["tenant_id"], user["id"])["allowed"] is False


def test_usage_cost_analysis_groups_model_module_user_agent_and_day(tmp_path):
    memory = ConversationMemory(str(tmp_path / "usage-analysis.db"))
    user = memory.register_user("analysis@example.com", "Correct-Horse-39", "Analysis")
    memory.record_llm_usage_event(user["tenant_id"], user["id"], user["agent_id"], module="reflection",
                                  provider="p", model="m", prompt_tokens=10, completion_tokens=5)
    analysis = memory.get_usage_cost_analysis(user["tenant_id"])
    assert analysis["by_model"][0]["key"] == "p/m"
    assert analysis["by_module"][0]["key"] == "reflection"
    assert analysis["by_user"][0]["key"] == user["id"]
    assert analysis["by_agent"][0]["key"] == user["agent_id"]
    assert analysis["by_day"]


def test_department_cost_view_assigns_user_and_aggregates_usage(tmp_path):
    memory = ConversationMemory(str(tmp_path / "department-cost.db"))
    owner = memory.register_user("department-owner@example.com", "Correct-Horse-40", "Department Owner")
    department = memory.create_department(owner["tenant_id"], "安全运营部", "SEC-001", owner["id"])
    assert memory.assign_user_department(owner["tenant_id"], department["id"], owner["id"], owner["id"])
    memory.record_llm_usage_event(owner["tenant_id"], owner["id"], owner["agent_id"],
                                  provider="p", model="m", prompt_tokens=10, completion_tokens=5)
    costs = memory.list_department_costs(owner["tenant_id"])
    assert costs[0]["department"] == "安全运营部"
    assert costs[0]["total_tokens"] == 15
    personal = memory.get_user_usage_summary(owner["tenant_id"], owner["id"])
    assert personal["requests"] == 1
