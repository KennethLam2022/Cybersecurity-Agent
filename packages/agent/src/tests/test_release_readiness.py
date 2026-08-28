from memory import ConversationMemory
from reflection_engine import PROMPT_ASSET_DEFAULTS
from release_readiness import build_release_readiness


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
    memory.complete_agent_eval_run(run_id, {"passed": True})
    requested_roles = []

    def configured(role):
        requested_roles.append(role)
        return {"model": "test", "base_url": "https://model.example.test"}

    result = build_release_readiness(memory, configured)
    assert result["ready"] is True
    assert requested_roles == ["reflection", "promptEval"]
