from memory import ConversationMemory
from reflection_engine import PROMPT_ASSET_DEFAULTS, reflect_answer


class _FakeLlm:
    def chat(self, *_args, **_kwargs):
        return {"content": '{"decision":"REVISE","answer":"修订后的安全回答","reason":"补充边界"}'}


def test_reflection_rules_are_versioned_and_revise_answer(tmp_path):
    memory = ConversationMemory(str(tmp_path / "reflection.db"))
    rule = memory.save_reflection_rule({"name": "来源边界", "rule_text": "禁止编造来源", "severity": "high", "capability_modes": ["chat"], "status": "published"})
    assert rule["version"] == 1
    result = reflect_answer(memory, _FakeLlm(), "问题", "原始回答", [], "chat")
    assert result["decision"] == "revise"
    assert result["answer"] == "修订后的安全回答"
    assert result["rule_version"] == 1
    assert result["prompt_version"] == 1
    assert any(item["slot"] == "reflection" for item in memory.list_prompt_assets())


def test_reflection_degrades_without_model_and_keeps_original_answer(tmp_path):
    memory = ConversationMemory(str(tmp_path / "reflection.db"))
    memory.save_reflection_rule({"name": "边界", "rule_text": "限制范围", "capability_modes": ["chat"], "status": "published"})
    result = reflect_answer(memory, None, "问题", "原始回答", [], "chat")
    assert result["decision"] == "degraded"
    assert result["answer"] == "原始回答"


def test_reflection_runs_are_observable_and_rule_changes_are_audited(tmp_path):
    memory = ConversationMemory(str(tmp_path / "reflection.db"))
    rule = memory.save_reflection_rule({
        "name": "最终边界", "rule_text": "不得超出授权范围", "status": "published",
        "capability_modes": ["chat", "writing"], "created_by": "admin-a",
    })
    memory.record_reflection_run("tenant-a", "user-a", "agent-a", "conv-a", {
        "mode": "chat", "decision": "revise", "rule_version": rule["version"],
        "input_summary": "问题", "output_summary": "修订回答", "duration_ms": 12,
    })
    summary = memory.get_reflection_summary("tenant-a")
    assert summary["total"] == 1
    assert summary["decisions"]["revise"] == 1
    assert memory.list_reflection_runs("tenant-a")[0]["conversation_id"] == "conv-a"
    changed = memory.save_reflection_rule({
        "name": "最终边界", "rule_text": "必须标注依据", "status": "draft",
        "capability_modes": ["chat"], "created_by": "admin-b",
    }, rule["id"])
    assert changed["version"] == 2
    restored = memory.restore_reflection_rule_version(rule["id"], 1, "admin-c")
    assert restored["version"] == 3
    assert restored["rule_text"] == "不得超出授权范围"


def test_prompt_assets_have_draft_publish_and_rollback_lifecycle(tmp_path):
    memory = ConversationMemory(str(tmp_path / "assets.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    draft = memory.save_prompt_asset_version("reflection", "draft template", ["query"], "draft", "草稿", "admin")
    assert draft["version"] == 2
    memory.record_prompt_asset_test_run("reflection", draft["version"], {"passed": True, "total": 5, "passed_count": 5}, "admin")
    published = memory.publish_prompt_asset_version("reflection", draft["version"], "admin")
    assert published["version"] == 2
    next_draft = memory.save_prompt_asset_version("reflection", "published template", ["query"], "draft", "发布候选", "admin")
    memory.record_prompt_asset_test_run("reflection", next_draft["version"], {"passed": True, "total": 5, "passed_count": 5}, "admin")
    published = memory.publish_prompt_asset_version("reflection", next_draft["version"], "admin")
    assert published["version"] == 3
    assert memory.get_active_prompt_asset("reflection")["template"] == "published template"
    memory.record_prompt_asset_test_run("reflection", 1, {"passed": True, "total": 5, "passed_count": 5}, "admin")
    restored = memory.restore_prompt_asset_version("reflection", 1, "admin")
    assert restored["version"] == 4
    assert memory.get_active_prompt_asset("reflection")["version"] == 4


def test_prompt_asset_defaults_cover_runtime_slots_and_doc_clean(tmp_path):
    memory = ConversationMemory(str(tmp_path / "prompt-slots.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    slots = {item["slot"] for item in memory.list_prompt_assets()}
    for slot in ("query_rewrite", "self_verify", "jailbreak_detect", "semantic_scoring",
                 "memory_extract", "memory_summary", "doc_clean", "judge_faithfulness"):
        asset = memory.get_active_prompt_asset(slot)
        assert slot in slots
        assert asset["version"] == 1
        assert asset["template"]
