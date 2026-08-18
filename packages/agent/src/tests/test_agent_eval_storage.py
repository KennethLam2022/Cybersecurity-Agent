from agent_eval.trace_schema import EVAL_TRACE_SCHEMA_VERSION, normalize_trace, trace_step_names
from memory import ConversationMemory


def test_agent_eval_case_run_and_result_round_trip(tmp_path):
    memory = ConversationMemory(str(tmp_path / "agent-eval.db"))
    case_id = memory.upsert_agent_eval_case({
        "case_key": "GEN-DS-001",
        "profile": "general",
        "case_type": "retrieval",
        "domain": "数据安全",
        "query": {"text": "数据分类分级应如何开展？"},
        "expected": {"expected_sources": ["数据安全法"]},
        "risk_tags": ["source_required"],
    })
    case = memory.get_agent_eval_cases()[0]
    assert case_id == case["id"]
    assert case["profile"] == "general"
    assert case["query"]["text"] == "数据分类分级应如何开展？"

    run_id = memory.create_agent_eval_run(context={"taxonomy_version": "test"})
    trace = normalize_trace({
        "trace_id": "trace-1",
        "outcome": "answered",
        "steps": [{"step": "retrieval", "duration_s": 0.125, "returned_count": 2}],
    })
    memory.save_agent_eval_result(run_id, case, {
        "query": case["query"]["text"], "answer": "应按分类对象、分级规则开展。",
        "trace": trace, "metrics": {"source_hit": 1}, "elapsed_ms": 125,
    })
    memory.complete_agent_eval_run(run_id, {"total": 1, "passed": 1})

    run = memory.get_agent_eval_runs()[0]
    result = memory.get_agent_eval_results(run_id)[0]
    assert run["status"] == "completed"
    assert run["context"]["taxonomy_version"] == "test"
    assert result["metrics"]["source_hit"] == 1
    assert result["trace"]["schema_version"] == EVAL_TRACE_SCHEMA_VERSION
    assert result["expected"] == {"expected_sources": ["数据安全法"]}
    assert result["domain"] == "数据安全"


def test_agent_eval_trace_projection_is_stable_and_ordered():
    trace = normalize_trace({
        "trace_id": "trace-2",
        "context": {"prompt_version": "v2"},
        "outcome": "blocked",
        "outcome_detail": {"reason": "user_jailbreak"},
        "steps": [
            {"step": "jailbreak_detection", "triggered": True, "time_s": "0.02"},
            {"step": "retrieval", "duration_s": None},
        ],
    })

    assert trace["steps"][0]["latency_ms"] == 20
    assert trace["steps"][0]["status"] == "triggered"
    assert trace["steps"][1]["latency_ms"] == 0
    assert trace_step_names(trace) == ["jailbreak_detection", "retrieval"]
