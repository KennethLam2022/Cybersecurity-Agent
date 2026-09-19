"""Release-readiness checks that never fabricate model or human evidence."""
from __future__ import annotations

import hashlib
import json


RELEASE_EVIDENCE_KEYS = {
    "baseline.production", "langfuse.scope", "mcp.production",
}


def mcp_policy_fingerprint(memory) -> str:
    """Return a stable fingerprint for the currently registered MCP policy surface."""
    items = []
    for extension in memory.list_capability_extensions("mcp"):
        items.append({
            "id": extension.get("id", ""),
            "status": extension.get("status", ""),
            "version": extension.get("version", ""),
            "source": extension.get("source", ""),
            "manifest": extension.get("manifest") or {},
            "permissions": extension.get("permissions") or [],
            "network_scope": extension.get("network_scope", ""),
            "grants": memory.list_capability_extension_grants(extension.get("id", "")),
        })
    if not items:
        return ""
    canonical = json.dumps(sorted(items, key=lambda item: item["id"]), ensure_ascii=False,
                           sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def validate_release_gate_evidence(key: str, payload: dict, memory) -> dict:
    """Validate administrator evidence before it enters the release ledger."""
    key = str(key or "").strip()
    if key not in RELEASE_EVIDENCE_KEYS:
        raise ValueError("不支持的发布门禁证据类型")
    if not isinstance(payload, dict) or not payload.get("passed"):
        raise ValueError("只有明确通过的人工证据才能写入发布台账")
    result = {"passed": True}
    if key == "baseline.production":
        run_id = str(payload.get("run_id") or "").strip()
        run = next((item for item in memory.get_agent_eval_runs(100)
                    if item.get("run_id") == run_id), None)
        if not run or run.get("status") != "completed" or not _gate_passed(run.get("summary") or {}):
            raise ValueError("真实模型基线必须绑定已完成且通过发布阈值的评测运行")
        result.update({"run_id": run_id, "model": str(payload.get("model") or "")[:200]})
    elif key == "langfuse.scope":
        config = memory.get_langfuse_config()
        mode = str(payload.get("mode") or "").strip().lower()
        if not config.get("enabled") and mode != "disabled":
            raise ValueError("Langfuse 当前未启用，确认模式必须为 disabled")
        if config.get("enabled") and mode != "enabled":
            raise ValueError("Langfuse 当前已启用，确认模式必须为 enabled")
        result.update({
            "mode": mode,
            "config_updated_at": config.get("updated_at", ""),
            "scope_note": str(payload.get("scope_note") or "")[:1000],
        })
    else:
        required = ("whitelist", "credentials", "network_egress", "human_approval")
        if not all(bool(payload.get(item)) for item in required):
            raise ValueError("MCP 生产证据必须包含白名单、凭证、网络出口和人工审批演练")
        result["checklist"] = {item: True for item in required}
        fingerprint = mcp_policy_fingerprint(memory)
        if fingerprint:
            result["policy_fingerprint"] = fingerprint
    return result


def _gate_passed(summary: dict) -> bool:
    from agent_eval.release_gate import evaluate_release_gate

    return bool(summary and evaluate_release_gate(summary).get("passed"))


def _evidence_passed(memory, key: str, *, run_id: str = "") -> bool:
    evidence = memory.get_release_gate_evidence(key)
    if not evidence or not evidence.get("passed"):
        return False
    if run_id and str(evidence.get("run_id") or "") != str(run_id):
        return False
    return bool(str(evidence.get("reviewer") or evidence.get("updated_by") or "").strip())


def build_release_readiness(memory, config_lookup, *, require_mcp_execution: bool = False) -> dict:
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
    latest_gate = _gate_passed(latest.get("summary") or {}) if latest.get("status") == "completed" else False
    checks.append({"id": "agent_eval.latest", "label": "最近 Agent Evaluation", "passed": latest_gate,
                   "detail": f"最近运行：{latest.get('run_id', '')}" if latest_gate else
                   ("最近评测未达到发布阈值" if latest else "尚无真实评测运行")})

    checks.append({
        "id": "baseline.production", "label": "真实生产模型基线批准",
        "passed": _evidence_passed(memory, "baseline.production", run_id=latest.get("run_id", "")),
        "detail": "管理员已批准最近一次生产模型基线" if _evidence_passed(
            memory, "baseline.production", run_id=latest.get("run_id", "")
        ) else "缺少与最近一次评测绑定的管理员基线批准",
    })

    langfuse = memory.get_langfuse_config()
    langfuse_evidence = memory.get_release_gate_evidence("langfuse.scope")
    langfuse_passed = bool(
        langfuse_evidence.get("passed")
        and str(langfuse_evidence.get("reviewer") or langfuse_evidence.get("updated_by") or "").strip()
        and ((not langfuse.get("enabled") and langfuse_evidence.get("mode") == "disabled")
             or (langfuse.get("enabled") and langfuse_evidence.get("config_updated_at") == langfuse.get("updated_at")))
    )
    checks.append({"id": "langfuse.scope", "label": "Langfuse 数据范围确认",
                   "passed": langfuse_passed,
                   "detail": "Langfuse 已禁用并完成确认" if not langfuse.get("enabled") and langfuse_passed
                   else ("Langfuse 数据范围已确认" if langfuse_passed else
                         "需确认 Langfuse 已禁用，或确认当前配置的数据传输范围")})

    mcp_evidence = memory.get_release_gate_evidence("mcp.production")
    current_mcp_fingerprint = mcp_policy_fingerprint(memory)
    mcp_passed = _evidence_passed(memory, "mcp.production") if require_mcp_execution else True
    if mcp_passed and current_mcp_fingerprint:
        mcp_passed = mcp_evidence.get("policy_fingerprint") == current_mcp_fingerprint
    checks.append({"id": "mcp.production", "label": "MCP 生产执行演练",
                   "passed": mcp_passed,
                   "required": require_mcp_execution,
                   "detail": "已完成白名单、凭证、网络出口和人工审批演练" if mcp_passed and require_mcp_execution
                   else ("生产未启用 MCP 执行，当前不阻塞" if not require_mcp_execution
                         else ("MCP 授权配置已变化，需要重新演练" if mcp_evidence.get("passed") and current_mcp_fingerprint
                               else "缺少 MCP 生产执行演练证据"))})
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
    retrieval = _retrieval_readiness(memory)
    checks.append(retrieval)
    return {"ready": all(item["passed"] for item in checks), "checks": checks}


def _retrieval_readiness(memory) -> dict:
    """Block release when recent retrieval quality is degraded or below threshold."""
    retrieval_eval = memory.get_retrieval_eval(limit=100)
    summary = retrieval_eval.get("summary") or {}
    count = int(summary.get("count") or 0)
    recall_10 = float(summary.get("avg_recall_10") or 0.0)
    mrr = float(summary.get("avg_mrr") or 0.0)
    failed_items = [
        item for item in retrieval_eval.get("items", [])
        if int(item.get("recall_10") or 0) == 0
    ]
    failed_queries = [str(item.get("query") or "")[:80] for item in failed_items[:10]]

    degraded_ratio = 0.0
    pending_excluded = 0
    try:
        import json as _json
        import sqlite3 as _sqlite3
        with _sqlite3.connect(memory._db_path) as conn:
            rows = conn.execute(
                """SELECT trace_data FROM usage_logs
                   WHERE json_extract(trace_data, '$.retrieval') IS NOT NULL
                   ORDER BY id DESC LIMIT 500"""
            ).fetchall()
        degraded = 0
        sampled = 0
        for (trace_json,) in rows:
            try:
                trace = _json.loads(trace_json or "{}")
            except (TypeError, ValueError):
                continue
            retrieval = trace.get("retrieval") or {}
            if not isinstance(retrieval, dict) or not retrieval:
                continue
            sampled += 1
            if retrieval.get("retrieval_degraded"):
                degraded += 1
            pending_excluded += int(retrieval.get("pending_profile_excluded") or 0)
        if sampled:
            degraded_ratio = round(degraded / sampled, 4)
    except Exception:
        pass

    warnings = []
    hard_issues = []
    if count == 0:
        warnings.append("缺少检索质量评测记录；本次发布门禁不阻断")
    else:
        if recall_10 < 0.85:
            hard_issues.append(f"Retrieval Recall@10={recall_10:.1%} 低于 85% 发布阈值")
        if mrr and mrr < 0.75:
            hard_issues.append(f"Retrieval MRR={mrr:.3f} 低于 0.75 发布阈值")
    if degraded_ratio >= 0.1:
        hard_issues.append(f"近期检索降级率 {degraded_ratio:.0%}，建议先修复 Reranker 或向量服务")
    if pending_excluded > 0:
        hard_issues.append(f"近期检索有 {pending_excluded} 次被排除 pending profile 文档")

    passed = not hard_issues
    detail = "；".join(hard_issues + warnings)
    if failed_queries and not passed:
        detail += "；失败查询：" + "；".join(failed_queries[:5])

    return {
        "id": "retrieval.quality",
        "label": "RAG 检索质量门禁",
        "passed": passed,
        "detail": detail,
        "retrieval_eval_count": count,
        "retrieval_recall_10": recall_10,
        "retrieval_mrr": mrr,
        "degraded_ratio": degraded_ratio,
        "pending_profile_excluded": pending_excluded,
    }
