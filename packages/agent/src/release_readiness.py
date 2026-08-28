"""Release-readiness checks that never fabricate model or human evidence."""
from __future__ import annotations


def build_release_readiness(memory, config_lookup) -> dict:
    checks = []
    # Judge uses the promptEval card in _get_backend_eval_llm(), not scoring.
    for role in ("reflection", "promptEval"):
        config = config_lookup(role) or {}
        ready = bool(config.get("model") and config.get("base_url"))
        checks.append({"id": f"model.{role}", "label": f"{role} 模型配置", "passed": ready,
                       "detail": "已配置模型与服务地址" if ready else "缺少真实模型或服务地址"})
    for slot in ("memory_profile_proposal", "memory_conflict", "judge_faithfulness"):
        asset = memory.get_active_prompt_asset(slot)
        report = memory.get_latest_prompt_asset_test_report(slot, int(asset.get("version") or 0)) if asset else None
        checks.append({"id": f"prompt.{slot}", "label": f"{slot} 契约测试", "passed": bool(report and report.get("passed")),
                       "detail": "当前发布版本已通过测试" if report and report.get("passed") else "当前发布版本缺少通过的测试报告"})
    judge = memory.get_active_prompt_asset("judge_faithfulness")
    calibration = memory.get_latest_prompt_asset_test_report("judge_faithfulness", int(judge.get("version") or 0), "calibration") if judge else None
    checks.append({"id": "judge.calibration", "label": "Judge 人工校准", "passed": bool(calibration and calibration.get("passed")),
                   "detail": "已满足人工样本、一致率和误差门槛" if calibration and calibration.get("passed") else "缺少通过的人审校准报告"})
    runs = memory.get_agent_eval_runs(1)
    latest = runs[0] if runs else {}
    checks.append({"id": "agent_eval.latest", "label": "最近 Agent Evaluation", "passed": latest.get("status") == "completed",
                   "detail": f"最近运行：{latest.get('run_id', '')}" if latest else "尚无真实评测运行"})
    # A calibration report is only valid for the latest completed run.  This
    # prevents a Judge replay from silently reusing evidence for older scores.
    calibration_check = next((item for item in checks if item["id"] == "judge.calibration"), None)
    calibration = memory.get_latest_prompt_asset_test_report(
        "judge_faithfulness", int(judge.get("version") or 0), "calibration"
    ) if judge else None
    if calibration_check and calibration_check["passed"]:
        calibration_check["passed"] = bool(
            latest.get("status") == "completed"
            and calibration
            and (not calibration.get("run_id") or calibration.get("run_id") == latest.get("run_id"))
        )
        if not calibration_check["passed"]:
            calibration_check["detail"] = "校准报告不属于最近一次已完成的 Agent Evaluation"
    return {"ready": all(item["passed"] for item in checks), "checks": checks}
