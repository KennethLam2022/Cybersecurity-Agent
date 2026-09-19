from memory import ConversationMemory


def test_model_pricing_cost_aggregation_and_route_recommendation(tmp_path):
    memory = ConversationMemory(str(tmp_path / "cost.db"))
    memory.update_model_pricing_status("price-default-deepseek-v4-flash", False)
    user = memory.register_user("cost-owner@example.com", "Correct-Horse-30", "Cost Owner")
    memory.upsert_model_pricing({
        "provider": "test", "model": "cheap", "input_price_per_million": 1,
        "output_price_per_million": 2, "quality_score": 0.7,
    })
    memory.upsert_model_pricing({
        "provider": "test", "model": "quality", "input_price_per_million": 3,
        "output_price_per_million": 5, "quality_score": 0.95,
    })
    conversation = memory.create_conversation(
        tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"],
    )
    message_id = memory.add_message(conversation["id"], "assistant", "answer")
    memory.log_usage(
        conversation["id"], message_id, "query", prompt_tokens=1_000_000,
        completion_tokens=500_000, trace_data={"context": {"llm": {"chat": {
            "provider": "test", "model": "cheap",
        }}}},
    )
    summary = memory.get_usage_cost_summary(user["tenant_id"])
    assert summary["total"]["cost"] == 2.0
    assert summary["by_model"][0]["model"] == "cheap"
    assert summary["by_user"][0]["id"] == user["id"]
    assert memory.recommend_model_route("writing")["recommended"]["model"] == "quality"
    assert memory.recommend_model_route("chat")["recommended"]["model"] == "cheap"


def test_unified_llm_usage_event_snapshots_model_price_and_unknown_history(tmp_path):
    memory = ConversationMemory(str(tmp_path / "events.db"))
    memory.upsert_model_pricing({
        "provider": "test", "model": "judge", "input_price_per_million": 4,
        "output_price_per_million": 6, "quality_score": 0.8,
    })
    priced = memory.record_llm_usage_event(
        "tenant-a", "user-a", "agent-a", module="evaluation", provider="test", model="judge",
        prompt_tokens=1_000_000, completion_tokens=500_000,
    )
    unknown = memory.record_llm_usage_event(
        "tenant-a", "user-a", "agent-a", module="external_api", provider="old", model="missing",
        prompt_tokens=10, completion_tokens=20,
    )
    assert priced["estimated_cost"] == 7.0
    assert priced["pricing_status"] == "estimated_from_local_configuration"
    assert unknown["estimated_cost"] is None
    assert unknown["pricing_status"] == "unknown_model_or_rate"
    events = memory.list_llm_usage_events("tenant-a")
    assert {item["module"] for item in events} == {"evaluation", "external_api"}


def test_chat_usage_writes_unified_event_with_conversation_scope(tmp_path):
    memory = ConversationMemory(str(tmp_path / "chat-events.db"))
    user = memory.register_user("chat-events@example.com", "Correct-Horse-30", "Chat Events")
    conversation = memory.create_conversation(tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"])
    message_id = memory.add_message(conversation["id"], "assistant", "answer")
    memory.log_usage(conversation["id"], message_id, "query", prompt_tokens=12, completion_tokens=8,
                     trace_data={"context": {"llm": {"chat": {"provider": "test", "model": "chat-model"}}}})
    events = memory.list_llm_usage_events(user["tenant_id"], module="chat")
    assert len(events) == 1
    assert events[0]["user_id"] == user["id"]
    assert events[0]["total_tokens"] == 20


def test_auxiliary_evaluation_modules_keep_separate_usage_rows(tmp_path):
    memory = ConversationMemory(str(tmp_path / "aux-events.db"))
    memory.record_llm_usage_event("tenant-a", module="prompt_test", model="m1", prompt_tokens=3, completion_tokens=4)
    memory.record_llm_usage_event("tenant-a", module="e2e_evaluation", model="m2", prompt_tokens=5, completion_tokens=6)
    assert {item["module"] for item in memory.list_llm_usage_events("tenant-a")} == {"prompt_test", "e2e_evaluation"}


def test_predictive_route_returns_projected_cost_budget_and_lock_decision(tmp_path):
    memory = ConversationMemory(str(tmp_path / "predictive-route.db"))
    memory.upsert_model_pricing({
        "provider": "test", "model": "cheap", "input_price_per_million": 1,
        "output_price_per_million": 2, "quality_score": 0.75,
    })
    memory.upsert_model_pricing({
        "provider": "test", "model": "quality", "input_price_per_million": 3,
        "output_price_per_million": 5, "quality_score": 0.95,
    })
    decision = memory.recommend_model_route(
        "writing", quality_floor=0.8, prompt_tokens=1_000_000, completion_tokens=500_000,
        budget_limit=6.0, budget_used=0.0,
    )
    assert decision["recommended"]["model"] == "quality"
    assert decision["recommended"]["projected_cost"] == 5.5
    assert decision["recommended"]["projected_budget_after"] == 5.5
    assert decision["decision"] == "recommended"

    locked = memory.recommend_model_route(
        "chat", quality_floor=0.0, prompt_tokens=1_000_000, completion_tokens=500_000,
        budget_limit=2.0, locked_provider="test", locked_model="quality",
    )
    assert locked["decision"] == "blocked"
    assert locked["recommended"] is None
    assert "锁定模型" in locked["reason"]
