import json

from agent_eval.runner import rerun_judge_for_results
from memory import ConversationMemory


class FakeJudge:
    def chat(self, messages):
        return {"content": json.dumps({
            "answer_completeness": 0.9,
            "faithfulness": 0.8,
            "relevancy": 0.7,
            "safety_pass": True,
            "reason": "可复核",
        }, ensure_ascii=False)}


class FakeAgent:
    def __init__(self, memory):
        self.memory = memory


def test_judge_replay_updates_existing_result_without_agent_call(tmp_path):
    memory = ConversationMemory(str(tmp_path / "agent-eval.db"))
    memory.upsert_agent_eval_case({
        "case_key": "GEN-JUDGE-001",
        "profile": "general",
        "query": {"text": "什么是风险评估？"},
        "expected": {"expected_points": ["识别风险"]},
    })
    case = memory.get_agent_eval_cases()[0]
    run_id = memory.create_agent_eval_run()
    result_id = memory.save_agent_eval_result(run_id, case, {
        "query": "什么是风险评估？",
        "answer": "风险评估需要识别风险。",
        "trace": {"steps": []},
        "metrics": {"task_success": True},
    })

    replay = rerun_judge_for_results(FakeAgent(memory), run_id, FakeJudge())

    assert replay["updated"] == 1
    assert replay["errors"] == []
    saved = memory.get_agent_eval_results(run_id)[0]
    assert saved["id"] == result_id
    assert saved["metrics"]["judge"]["faithfulness"] == 0.8
    assert saved["metrics"]["judge"]["prompt_version"] == 1


def test_judge_replay_reports_missing_run(tmp_path):
    memory = ConversationMemory(str(tmp_path / "agent-eval.db"))
    try:
        rerun_judge_for_results(FakeAgent(memory), "missing", FakeJudge())
    except ValueError as exc:
        assert "没有结果" in str(exc)
    else:
        raise AssertionError("missing run should fail")


def test_judge_replay_honors_bounded_limit(tmp_path):
    memory = ConversationMemory(str(tmp_path / "agent-eval-limit.db"))
    run_id = memory.create_agent_eval_run()
    for index in range(3):
        memory.save_agent_eval_result(run_id, {
            "case_key": f"CASE-{index}", "profile": "general", "query": {"text": "问题"},
            "expected": {},
        }, {"query": "问题", "answer": "回答", "trace": {}, "metrics": {}})
    replay = rerun_judge_for_results(FakeAgent(memory), run_id, FakeJudge(), limit=2)
    assert replay["updated"] == 2
    assert replay["total"] == 2
    assert replay["available"] == 3
