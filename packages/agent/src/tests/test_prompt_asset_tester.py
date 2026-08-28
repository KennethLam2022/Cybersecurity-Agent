from memory import ConversationMemory
from prompt_asset_tester import (compare_reflection_asset_versions, evaluate_slot_result,
                                 get_slot_golden_cases, run_reflection_golden_suite)
from reflection_engine import PROMPT_ASSET_DEFAULTS


class _GoldenLlm:
    def __init__(self):
        self.index = 0

    def chat(self, *_args, **_kwargs):
        decisions = ["PASS", "REVISE", "BLOCK", "CLARIFY"]
        decision = decisions[self.index % len(decisions)]
        self.index += 1
        return {"content": '{"decision":"' + decision + '","reason":"ok"}'}


def test_reflection_golden_cases_cover_all_expected_decisions():
    expected = {item["expected_decision"] for item in get_slot_golden_cases("reflection")}
    assert expected == {"pass", "revise", "block", "clarify", "degraded"}


def test_slot_contract_is_explicit_about_expected_and_actual_decision():
    case = {"id": "a", "name": "case", "expected_decision": "block"}
    assert evaluate_slot_result("reflection", case, {"decision": "block"})["passed"] is True
    failed = evaluate_slot_result("reflection", case, {"decision": "pass"})
    assert failed["passed"] is False
    assert "期望 block" in failed["detail"]


def test_reflection_golden_suite_executes_configured_model_and_keeps_degradation_visible(tmp_path):
    memory = ConversationMemory(str(tmp_path / "assets.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    report = run_reflection_golden_suite(memory, _GoldenLlm())
    assert report["total"] == 5
    assert report["passed_count"] == 5


def test_reflection_asset_ab_keeps_versions_separate(tmp_path):
    memory = ConversationMemory(str(tmp_path / "assets.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    first = memory.list_prompt_asset_versions("reflection")[0]
    second = memory.save_prompt_asset_version("reflection", first["template"], first["variables"], "draft", "candidate", "admin")
    report = compare_reflection_asset_versions(memory, _GoldenLlm(), first, second)
    assert report["version_a"] == first["version"]
    assert report["version_b"] == second["version"]
    assert report["summary_a"]["pass_rate"] == 100
    assert report["summary_b"]["pass_rate"] == 100


def test_reflection_prompt_cannot_publish_without_passing_latest_golden_run(tmp_path):
    memory = ConversationMemory(str(tmp_path / "assets.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    draft = memory.save_prompt_asset_version("reflection", "candidate", [], "draft", "candidate", "admin")
    try:
        memory.publish_prompt_asset_version("reflection", draft["version"], "admin")
    except ValueError as exc:
        assert "黄金回归" in str(exc)
    else:
        raise AssertionError("未通过黄金回归的反思 Prompt 不应发布")


def test_graph_relation_prompt_cannot_publish_without_passing_latest_contract(tmp_path):
    memory = ConversationMemory(str(tmp_path / "assets.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    active = memory.list_prompt_asset_versions("graph_relation_extraction")[0]
    draft = memory.save_prompt_asset_version("graph_relation_extraction", active["template"], active["variables"], "draft", "candidate", "admin")
    try:
        memory.publish_prompt_asset_version("graph_relation_extraction", draft["version"], "admin")
    except ValueError as exc:
        assert "黄金回归" in str(exc)
    else:
        raise AssertionError("未通过契约测试的图谱语义 Prompt 不应发布")
    memory.record_prompt_asset_test_run("graph_relation_extraction", draft["version"], {"passed": True}, "admin")
    assert memory.publish_prompt_asset_version("graph_relation_extraction", draft["version"], "admin")["status"] == "published"


def test_prompt_asset_catalog_marks_unmigrated_and_deterministic_slots_explicitly(tmp_path):
    memory = ConversationMemory(str(tmp_path / "assets.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    items = {item["slot"]: item for item in memory.list_prompt_assets()}
    assert items["compliance_guard"]["model_role"] == "deterministic"
    assert items["generation_writing"]["status"] == "published"
    assert items["memory_profile_proposal"]["status"] == "published"
    summary = memory.get_prompt_asset_governance_summary()
    assert summary["total"] >= 10
    assert "reflection" not in summary["missing_critical"]
    assert "query_rewrite" not in summary["missing_critical"]
    assert summary["ready_for_prompt_governance"] is True


def test_tool_prompt_assets_are_seeded_and_covered_by_governance(tmp_path):
    memory = ConversationMemory(str(tmp_path / "tool-assets.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    items = {item["slot"]: item for item in memory.list_prompt_assets()}
    expected = {
        "tool_router", "skill_call_planner", "mcp_call_planner",
        "tool_result_summarizer", "tool_failure_fallback",
        "generation_evidence_search",
    }
    assert expected <= set(items)
    assert items["tool_router"]["model_role"] == "tool_router"
    summary = memory.get_prompt_asset_governance_summary()
    assert not (expected & set(summary["missing_critical"]))


def test_generation_evidence_prompt_contract_prioritizes_rag_and_limits_fetch():
    from prompt_asset_tester import evaluate_slot_result, get_slot_golden_cases

    rag_case = get_slot_golden_cases("generation_evidence_search")[0]
    assert evaluate_slot_result("generation_evidence_search", rag_case, {
        "rag_sufficient": True, "external_needed": False,
    })["passed"] is True


def test_system_seeded_generation_search_prompt_migrates_without_overwriting_admin_prompt(tmp_path):
    memory = ConversationMemory(str(tmp_path / "prompt-migration.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    first = memory.get_active_prompt_asset("generation_evidence_search")
    memory.ensure_prompt_assets([{
        **next(item for item in PROMPT_ASSET_DEFAULTS if item["slot"] == "generation_evidence_search"),
        "template": first["template"] + "\n新增搜索约束。",
    }])
    migrated = memory.get_active_prompt_asset("generation_evidence_search")
    assert migrated["version"] == first["version"] + 1
    assert "新增搜索约束" in migrated["template"]
    admin_version = memory.save_prompt_asset_version(
        "generation_evidence_search", "管理员版本", [], "published", "admin update", "admin",
    )
    memory.ensure_prompt_assets([{
        **next(item for item in PROMPT_ASSET_DEFAULTS if item["slot"] == "generation_evidence_search"),
        "template": "系统再次更新",
    }])
    assert memory.get_active_prompt_asset("generation_evidence_search")["version"] == admin_version["version"]
    assert memory.get_active_prompt_asset("generation_evidence_search")["template"] == "管理员版本"
    fetch_case = get_slot_golden_cases("generation_evidence_search")[1]
    assert evaluate_slot_result("generation_evidence_search", fetch_case, {
        "rag_sufficient": False, "external_needed": True,
        "urls": ["https://example.com/security"],
    })["passed"] is True
    clarify_case = get_slot_golden_cases("generation_evidence_search")[2]
    assert evaluate_slot_result("generation_evidence_search", clarify_case, {
        "rag_sufficient": False, "external_needed": False,
        "clarification": "请补充明确的公开网页 URL。",
    })["passed"] is True


def test_memory_and_judge_slots_have_independent_structured_contracts():
    from prompt_asset_tester import evaluate_slot_result, get_slot_golden_cases

    profile_case = get_slot_golden_cases("memory_profile_proposal")[0]
    assert evaluate_slot_result(
        "memory_profile_proposal", profile_case,
        {"decision": "propose", "occupation": "网络安全工程师"},
    )["passed"] is True
    assert evaluate_slot_result(
        "memory_profile_proposal", profile_case,
        {"decision": "propose", "income": "高收入"},
    )["passed"] is False

    conflict_case = get_slot_golden_cases("memory_conflict")[1]
    assert evaluate_slot_result("memory_conflict", conflict_case, {"decision": "confirm"})["passed"] is True
    judge_case = get_slot_golden_cases("judge_faithfulness")[0]
    assert evaluate_slot_result("judge_faithfulness", judge_case, {"score": 0.8, "rationale": "来源支持"})["passed"] is True
    assert evaluate_slot_result("judge_faithfulness", judge_case, {"score": 2, "rationale": "越界分数"})["passed"] is False


def test_tool_prompt_contracts_cover_routing_planning_summary_and_fallback():
    from prompt_asset_tester import evaluate_slot_result, get_slot_golden_cases

    router_case = get_slot_golden_cases("tool_router")[1]
    assert evaluate_slot_result("tool_router", router_case, {
        "tool_required": True, "selected_extension_id": "ext-1", "reason": "可生成结构化提纲",
    })["passed"] is True
    assert evaluate_slot_result("tool_router", router_case, {
        "tool_required": False, "selected_extension_id": "", "reason": "未选择",
    })["passed"] is False

    mcp_case = get_slot_golden_cases("mcp_call_planner")[0]
    assert evaluate_slot_result("mcp_call_planner", mcp_case, {
        "ready": True, "tool_name": "lookup", "arguments": {"query": "访问控制"},
    })["passed"] is True
    assert evaluate_slot_result("mcp_call_planner", mcp_case, {
        "ready": True, "tool_name": "lookup", "arguments": {"token": "secret"},
    })["passed"] is False

    summary_case = get_slot_golden_cases("tool_result_summarizer")[0]
    assert evaluate_slot_result("tool_result_summarizer", summary_case, {
        "summary": "工具返回访问控制检查结果，仍需结合授权资料确认。",
    })["passed"] is True
    assert evaluate_slot_result("tool_result_summarizer", summary_case, {
        "summary": "Bearer abcdef 已用于调用 C:\\\\secret。",
    })["passed"] is False

    fallback_case = get_slot_golden_cases("tool_failure_fallback")[1]
    assert evaluate_slot_result("tool_failure_fallback", fallback_case, {
        "message": "当前工具未获得授权，请联系管理员确认授权范围。",
    })["passed"] is True


def test_non_reflection_prompt_ab_uses_the_same_contract_and_is_explicitly_offline():
    from prompt_asset_tester import compare_slot_contract_versions, get_slot_golden_cases

    cases = get_slot_golden_cases("memory_conflict")
    result_a = {cases[0]["id"]: {"decision": "update"}, cases[1]["id"]: {"decision": "confirm"}}
    result_b = {cases[0]["id"]: {"decision": "confirm"}, cases[1]["id"]: {"decision": "confirm"}}
    report = compare_slot_contract_versions("memory_conflict", 1, 2, result_a, result_b)
    assert report["contract_only"] is True
    assert report["summary_a"]["pass_rate"] == 100.0
    assert report["summary_b"]["pass_rate"] == 50.0
    assert report["winner"] == "A"


def test_tool_prompt_publish_requires_latest_contract_report(tmp_path):
    memory = ConversationMemory(str(tmp_path / "tool-gate.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    active = memory.list_prompt_asset_versions("tool_router")[0]
    draft = memory.save_prompt_asset_version(
        "tool_router", active["template"], active["variables"], "draft", "candidate", "admin",
    )
    try:
        memory.publish_prompt_asset_version("tool_router", draft["version"], "admin")
    except ValueError as exc:
        assert "黄金回归" in str(exc)
    else:
        raise AssertionError("Tool router Prompt must require a contract report before publishing")
    memory.record_prompt_asset_test_run("tool_router", draft["version"], {"passed": True}, "admin")
    assert memory.publish_prompt_asset_version("tool_router", draft["version"], "admin")["status"] == "published"


def test_structured_prompt_golden_suite_executes_supported_slots():
    from prompt_asset_tester import run_structured_prompt_golden_suite

    class FakeJudge:
        model = "prompt-eval-test"
        calls = 0
        conflict_calls = 0

        def chat(self, messages, **kwargs):
            self.calls += 1
            prompt = messages[0]["content"]
            if "用户画像候选字段" in prompt:
                if "工作很忙" in prompt:
                    return {"content": '{"decision":"hold","fields":{},"rationale":"不能从单句推断"}'}
                return {"content": '{"fields":{"occupation":"网络安全工程师"},"rationale":"用户明确表达"}'}
            if "记忆冲突" in prompt:
                self.conflict_calls += 1
                decision = "update" if self.conflict_calls == 1 else "confirm"
                return {"content": '{"decision":"' + decision + '","proposed_content":"网络安全审计员","rationale":"需要确认"}'}
            return {"content": '{"score":0.9,"rationale":"来源支持"}'}

    profile = run_structured_prompt_golden_suite(
        None, FakeJudge(), "memory_profile_proposal",
        "用户画像候选字段：{statement}", 2,
    )
    assert profile["execution"] == "real"
    assert profile["passed"] is True
    conflict = run_structured_prompt_golden_suite(
        None, FakeJudge(), "memory_conflict",
        "记忆冲突：{field}，新陈述：{statement}", 2,
    )
    assert conflict["passed"] is True


def test_judge_prompt_publish_requires_contract_and_human_calibration(tmp_path):
    memory = ConversationMemory(str(tmp_path / "judge-gate.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    active = memory.list_prompt_asset_versions("judge_faithfulness")[0]
    draft = memory.save_prompt_asset_version("judge_faithfulness", active["template"], active["variables"], "draft", "candidate", "admin")
    memory.record_prompt_asset_test_run("judge_faithfulness", draft["version"], {"passed": True}, "admin")
    try:
        memory.publish_prompt_asset_version("judge_faithfulness", draft["version"], "admin")
    except ValueError as exc:
        assert "人审校准" in str(exc)
    else:
        raise AssertionError("Judge Prompt should require calibration before publishing")
    memory.record_prompt_asset_test_run("judge_faithfulness", draft["version"], {
        "passed": True, "reviewed_count": 3, "agreement_rate": 0.8, "mean_absolute_error": 0.1,
    }, "admin", "calibration")
    assert memory.publish_prompt_asset_version("judge_faithfulness", draft["version"], "admin")["status"] == "published"
