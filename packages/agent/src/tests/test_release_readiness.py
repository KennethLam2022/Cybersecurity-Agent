import pytest

from memory import ConversationMemory
from reflection_engine import PROMPT_ASSET_DEFAULTS
from release_readiness import build_release_readiness, mcp_policy_fingerprint, validate_release_gate_evidence


def test_release_readiness_requires_real_config_contract_calibration_and_eval_run(tmp_path):
    memory = ConversationMemory(str(tmp_path / "readiness.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    result = build_release_readiness(memory, lambda _role: {})
    assert result["ready"] is False
    assert any(item["id"] == "judge.calibration" and not item["passed"] for item in result["checks"])

    for slot in ("memory_profile_proposal", "memory_conflict", "judge_faithfulness"):
        asset = memory.get_active_prompt_asset(slot)
        memory.record_prompt_asset_test_run(slot, asset["version"], {"passed": True}, "admin")
    judge = memory.get_active_prompt_asset("judge_faithfulness")
    memory.record_prompt_asset_test_run("judge_faithfulness", judge["version"], {"passed": True}, "admin", "calibration")
    run_id = memory.create_agent_eval_run()
    memory.complete_agent_eval_run(run_id, {
        "total": 3, "pass_rate": 1.0, "flaky_rate": 0.0, "errors": 0,
        "security_failures": 0, "p95_latency_ms": 10,
    })
    memory.save_release_gate_evidence("baseline.production", {
        "passed": True, "run_id": run_id, "reviewer": "admin"
    })
    memory.save_release_gate_evidence("mcp.production", {
        "passed": True, "reviewer": "admin"
    })
    memory.save_release_gate_evidence("langfuse.scope", {
        "passed": True, "mode": "disabled", "reviewer": "admin"
    })
    requested_roles = []

    def configured(role):
        requested_roles.append(role)
        return {"model": "test", "base_url": "https://model.example.test"}

    result = build_release_readiness(memory, configured, require_mcp_execution=True)
    assert result["ready"] is True
    assert requested_roles == ["reflection", "promptEval"]


def test_release_readiness_requires_explicit_production_evidence(tmp_path):
    memory = ConversationMemory(str(tmp_path / "readiness-evidence.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    for slot in ("memory_profile_proposal", "memory_conflict", "judge_faithfulness"):
        asset = memory.get_active_prompt_asset(slot)
        memory.record_prompt_asset_test_run(slot, asset["version"], {"passed": True}, "admin")
    judge = memory.get_active_prompt_asset("judge_faithfulness")
    memory.record_prompt_asset_test_run(
        "judge_faithfulness", judge["version"], {"passed": True}, "admin", "calibration"
    )
    run_id = memory.create_agent_eval_run()
    memory.complete_agent_eval_run(run_id, {
        "total": 3, "pass_rate": 1.0, "flaky_rate": 0.0, "errors": 0,
        "security_failures": 0, "p95_latency_ms": 10,
    })

    def configured(_role):
        return {"model": "production-model", "base_url": "https://model.example.test"}

    result = build_release_readiness(memory, configured, require_mcp_execution=True)
    assert result["ready"] is False
    assert {item["id"] for item in result["checks"] if not item["passed"]} >= {
        "baseline.production", "langfuse.scope", "mcp.production"
    }

    memory.save_release_gate_evidence("baseline.production", {
        "passed": True, "run_id": run_id, "reviewer": "admin"
    })
    memory.save_release_gate_evidence("mcp.production", {
        "passed": True, "reviewer": "admin"
    })
    memory.save_release_gate_evidence("langfuse.scope", {
        "passed": True, "mode": "disabled", "reviewer": "admin"
    })
    result = build_release_readiness(memory, configured)
    assert result["ready"] is True


def test_release_readiness_does_not_accept_stale_baseline_evidence(tmp_path):
    memory = ConversationMemory(str(tmp_path / "readiness-stale.db"))
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    for slot in ("memory_profile_proposal", "memory_conflict", "judge_faithfulness"):
        asset = memory.get_active_prompt_asset(slot)
        memory.record_prompt_asset_test_run(slot, asset["version"], {"passed": True}, "admin")
    judge = memory.get_active_prompt_asset("judge_faithfulness")
    memory.record_prompt_asset_test_run(
        "judge_faithfulness", judge["version"], {"passed": True}, "admin", "calibration"
    )
    first = memory.create_agent_eval_run()
    memory.complete_agent_eval_run(first, {
        "total": 3, "pass_rate": 1.0, "flaky_rate": 0.0, "errors": 0,
        "security_failures": 0, "p95_latency_ms": 10,
    })
    memory.save_release_gate_evidence("baseline.production", {
        "passed": True, "run_id": first, "reviewer": "admin"
    })
    memory.save_release_gate_evidence("mcp.production", {"passed": True})
    memory.save_release_gate_evidence("langfuse.scope", {"passed": True, "mode": "disabled"})
    second = memory.create_agent_eval_run()
    memory.complete_agent_eval_run(second, {
        "total": 3, "pass_rate": 1.0, "flaky_rate": 0.0, "errors": 0,
        "security_failures": 0, "p95_latency_ms": 10,
    })
    result = build_release_readiness(memory, lambda _role: {
        "model": "production-model", "base_url": "https://model.example.test"
    }, require_mcp_execution=True)
    baseline = next(item for item in result["checks"] if item["id"] == "baseline.production")
    assert baseline["passed"] is False


def test_release_evidence_validation_requires_real_checklist(tmp_path):
    memory = ConversationMemory(str(tmp_path / "evidence-validation.db"))
    with pytest.raises(ValueError):
        validate_release_gate_evidence("mcp.production", {"passed": True}, memory)
    with pytest.raises(ValueError):
        validate_release_gate_evidence("langfuse.scope", {
            "passed": True, "mode": "enabled"
        }, memory)


def test_release_readiness_blocks_on_low_retrieval_quality(tmp_path):
    memory = ConversationMemory(str(tmp_path / "readiness-retrieval.db"))
    memory.save_retrieval_eval(
        query="等保三级要求", expected_source="等保",
        recall_5=1, recall_10=1, mrr=1.0,
        faiss_count=10, chroma_count=10, rerank_top1_match=1,
    )
    memory.save_retrieval_eval(
        query="数据分类分级", expected_source="数据安全",
        recall_5=0, recall_10=0, mrr=0.0,
        faiss_count=10, chroma_count=10, rerank_top1_match=0,
    )
    result = build_release_readiness(memory, lambda _role: {})
    retrieval = next(item for item in result["checks"] if item["id"] == "retrieval.quality")
    assert retrieval["passed"] is False
    assert "低于 85%" in retrieval["detail"] or "MRR" in retrieval["detail"]
    assert result["ready"] is False


def test_mcp_release_evidence_is_bound_to_current_policy(tmp_path):
    memory = ConversationMemory(str(tmp_path / "readiness-mcp-fingerprint.db"))
    extension = memory.create_capability_extension(
        "mcp", "审核搜索", "1.0", "https://mcp.example.test",
        manifest={"transport": "http", "tools": ["search"]},
        permissions=["network.read"], network_scope="公开网页摘要",
    )
    first = mcp_policy_fingerprint(memory)
    assert first
    evidence = validate_release_gate_evidence("mcp.production", {
        "passed": True, "whitelist": True, "credentials": True,
        "network_egress": True, "human_approval": True,
    }, memory)
    assert evidence["policy_fingerprint"] == first
    memory.update_capability_extension(
        extension["id"], "2.0", "https://mcp.example.test",
        manifest={"transport": "http", "tools": ["search", "fetch"]},
        permissions=["network.read"], network_scope="公开网页摘要", changed_by="admin",
    )
    assert mcp_policy_fingerprint(memory) != first
