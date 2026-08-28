"""HTTP API for the deterministic Agent Evaluation MVP."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app_state import agent, event_bus, logger, _get_backend_eval_llm, _get_llm_config_card
from notification_delivery import publish_system_event
from identity import require_permission, require_platform_permission


router = APIRouter()


def _evaluation_principal(request: Request, tenant_id: str = ""):
    return require_permission(request, agent.memory, "evaluation.manage", tenant_id)


def _owned_run(run_id: str, tenant_id: str):
    return next((item for item in agent.memory.get_agent_eval_runs(100, tenant_id)
                 if item.get("run_id") == run_id), None)


def _record_agent_eval_usage(response: dict | None, model: str = "") -> None:
    usage = (response or {}).get("usage") or {}
    agent.memory.record_llm_usage_event(
        tenant_id="local-default", module="agent_eval_judge",
        model=(response or {}).get("model") or model,
        prompt_tokens=usage.get("prompt_tokens", 0),
        completion_tokens=usage.get("completion_tokens", 0),
    )


def _langfuse_config_payload(data: dict | None = None) -> dict:
    payload = data or {}
    host = str(payload.get("host") or "https://cloud.langfuse.com").strip().rstrip("/")
    if not host.startswith("https://"):
        raise ValueError("Langfuse Host 必须使用 HTTPS")
    return {
        "enabled": bool(payload.get("enabled", False)), "host": host,
        "public_key": str(payload.get("public_key") or "").strip(),
        "secret_key": str(payload.get("secret_key") or "").strip(),
        "export_content": bool(payload.get("export_content", False)),
        "annotation_queue": str(payload.get("annotation_queue") or "").strip(),
    }


@router.get("/api/agent-eval/langfuse/config")
def get_langfuse_config(request: Request):
    require_platform_permission(request, agent.memory)
    return {"config": agent.memory.get_langfuse_config()}


@router.put("/api/agent-eval/langfuse/config")
def save_langfuse_config(request: Request, data: dict | None = None):
    try:
        require_platform_permission(request, agent.memory)
        config = _langfuse_config_payload(data)
        current = agent.memory.get_langfuse_config(include_secrets=True)
        if not config["public_key"]:
            config["public_key"] = current.get("public_key", "")
        if not config["secret_key"]:
            config["secret_key"] = current.get("secret_key", "")
        if config["enabled"] and (not config["public_key"] or not config["secret_key"]):
            return JSONResponse({"error": "启用 Langfuse 需要 Public Key 和 Secret Key"}, status_code=400)
        agent.memory.save_langfuse_config(config)
        return {"ok": True, "config": agent.memory.get_langfuse_config()}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.post("/api/agent-eval/langfuse/test")
def test_langfuse_config(request: Request):
    from agent_eval.langfuse_exporter import build_langfuse_exporter
    require_platform_permission(request, agent.memory)
    config = agent.memory.get_langfuse_config(include_secrets=True)
    exporter = build_langfuse_exporter(config)
    result = exporter.verify_connection()
    return JSONResponse(result, status_code=200 if result.get("ok") else 503)


def _case_payload(case: dict) -> dict:
    return {
        "case_key": case.get("case_key") or case.get("id"),
        "profile": case.get("profile") or "general",
        "case_type": case.get("case_type") or "answer_quality",
        "domain": case.get("domain") or "",
        "query": case.get("query") or {},
        "expected": case.get("expected") or {},
        "risk_tags": case.get("risk_tags") or [],
        "is_active": int(case.get("is_active", 1)),
    }


def _seed_builtin_cases(tenant_id: str = "local-default") -> int:
    from agent_eval.builtin_cases import load_builtin_cases

    count = 0
    for case in load_builtin_cases():
        agent.memory.upsert_agent_eval_case(case, tenant_id=tenant_id)
        count += 1
    return count


@router.get("/api/agent-eval/cases")
def get_agent_eval_cases(request: Request, profile: str | None = None, include_inactive: bool = False,
                         tenant_id: str = ""):
    principal = _evaluation_principal(request, tenant_id)
    return {"items": agent.memory.get_agent_eval_cases(profile, include_inactive, principal.tenant_id)}


@router.post("/api/agent-eval/cases")
def upsert_agent_eval_case(request: Request, data: dict):
    try:
        principal = _evaluation_principal(request, str((data or {}).get("tenant_id") or ""))
        case = _case_payload(data or {})
        case_id = agent.memory.upsert_agent_eval_case(case, principal.tenant_id)
        return {"ok": True, "id": case_id, "case": case}
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("Agent Evaluation 用例保存失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/agent-eval/cases/seed")
def seed_agent_eval_cases(request: Request, data: dict | None = None):
    try:
        principal = _evaluation_principal(request, str((data or {}).get("tenant_id") or ""))
        return {"ok": True, "count": _seed_builtin_cases(principal.tenant_id)}
    except Exception as exc:
        logger.exception("Agent Evaluation 主干测试集导入失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/agent-eval/langfuse/dataset-sync")
def sync_agent_eval_cases_to_langfuse(request: Request, data: dict | None = None):
    """Optional one-way copy of local cases to a Langfuse Dataset."""
    from agent_eval.langfuse_exporter import build_langfuse_exporter

    payload = data or {}
    require_platform_permission(request, agent.memory)
    profile = payload.get("profile") or "general"
    dataset_name = str(payload.get("dataset_name") or f"cyber-agent-eval-{profile.replace('/', '-')}").strip()
    exporter = build_langfuse_exporter(agent.memory.get_langfuse_config(include_secrets=True))
    result = exporter.export_cases_to_dataset(
        agent.memory.get_agent_eval_cases(profile=profile), dataset_name,
    )
    status_code = 200 if result.get("ok") else 503
    return JSONResponse({"profile": profile, **result}, status_code=status_code)


@router.post("/api/agent-eval/preflight")
def agent_eval_preflight(request: Request, data: dict | None = None):
    from agent_eval.preflight import build_preflight

    payload = data or {}
    principal = _evaluation_principal(request, str(payload.get("tenant_id") or ""))
    profile = payload.get("profile") or "general"
    cases = agent.memory.get_agent_eval_cases(profile=profile, tenant_id=principal.tenant_id)
    if not cases and profile == "general":
        _seed_builtin_cases(principal.tenant_id)
        cases = agent.memory.get_agent_eval_cases(profile=profile, tenant_id=principal.tenant_id)
    judge_requested = bool(payload.get("judge", False))
    judge = _get_backend_eval_llm() if judge_requested else None
    return {"ok": True, "preflight": build_preflight(
        agent, cases, profile, repetitions=payload.get("repetitions", 1),
        judge_requested=judge_requested, judge_available=judge is not None,
    )}


@router.get("/api/agent-eval/release-readiness")
def agent_eval_release_readiness(request: Request):
    from reflection_engine import PROMPT_ASSET_DEFAULTS
    from release_readiness import build_release_readiness

    require_platform_permission(request, agent.memory)
    agent.memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    return {"ok": True, "readiness": build_release_readiness(agent.memory, _get_llm_config_card)}


@router.post("/api/agent-eval/run")
def run_agent_eval(request: Request, data: dict | None = None):
    try:
        from agent_eval.runner import run_agent_evaluation
        from agent_eval.langfuse_exporter import build_langfuse_exporter

        payload = data or {}
        principal = _evaluation_principal(request, str(payload.get("tenant_id") or ""))
        profile = payload.get("profile") or "general"
        if not agent.memory.get_agent_eval_cases(profile=profile, tenant_id=principal.tenant_id):
            _seed_builtin_cases(principal.tenant_id)
        cases = agent.memory.get_agent_eval_cases(profile=profile, tenant_id=principal.tenant_id)
        limit = int(payload.get("limit") or 0)
        if limit > 0:
            cases = cases[:limit]
        if not cases:
            return JSONResponse({"error": "没有可运行的 Agent Evaluation 用例"}, status_code=400)
        repetitions = max(1, min(int(payload.get("repetitions") or 1), 5))
        from agent_eval.preflight import build_preflight

        judge = _get_backend_eval_llm() if payload.get("judge", False) else None
        preflight = build_preflight(
            agent, cases, profile, repetitions=repetitions,
            judge_requested=bool(payload.get("judge", False)), judge_available=judge is not None,
        )
        if payload.get("dry_run", False):
            return {"ok": True, "dry_run": True, "preflight": preflight}
        if not preflight["ready"]:
            return JSONResponse({"error": "评测运行前检查未通过", "preflight": preflight}, status_code=400)
        exporter = build_langfuse_exporter(agent.memory.get_langfuse_config(include_secrets=True))
        result = run_agent_evaluation(
            agent, cases, profile=profile, judge=judge, exporter=exporter,
            repetitions=repetitions, usage_sink=_record_agent_eval_usage,
            tenant_id=principal.tenant_id, agent_id=principal.agent_id, requested_by=principal.user_id,
        )
        summary = result.get("summary") or {}
        publish_system_event(agent.memory, event_bus, "eval.completed", {
            "tenant_id": principal.tenant_id,
            "run_id": result.get("run_id") or "",
            "profile": profile,
            "pass_rate": summary.get("pass_rate"),
            "notification_body": (
                f"Profile {profile} 评测完成"
                + (f"，通过率 {float(summary['pass_rate']) * 100:.1f}%。" if summary.get("pass_rate") is not None else "。")
            ),
        })
        return {"ok": True, **result}
    except Exception as exc:
        logger.exception("Agent Evaluation 运行失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/agent-eval/runs/{run_id}/judge")
def rerun_agent_eval_judge(run_id: str, request: Request, data: dict | None = None):
    """Re-run only the advisory Judge against an existing Agent run."""
    try:
        judge = _get_backend_eval_llm()
        if judge is None:
            return JSONResponse({"error": "后端评测 LLM 未配置"}, status_code=400)
        from agent_eval.runner import rerun_judge_for_results
        payload = data or {}
        principal = _evaluation_principal(request, str(payload.get("tenant_id") or ""))
        if not _owned_run(run_id, principal.tenant_id):
            return JSONResponse({"error": "评测运行不存在"}, status_code=404)
        # The browser sends a bounded replay count; keeping the limit optional
        # preserves compatibility with external clients that replay all cases.
        return {"ok": True, **rerun_judge_for_results(
            agent, run_id, judge, usage_sink=_record_agent_eval_usage,
            limit=payload.get("limit"), tenant_id=principal.tenant_id,
        )}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    except Exception as exc:
        logger.exception("Agent Evaluation Judge 重跑失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/agent-eval/runs")
def get_agent_eval_runs(request: Request, limit: int = 20, tenant_id: str = ""):
    principal = _evaluation_principal(request, tenant_id)
    return {"items": agent.memory.get_agent_eval_runs(max(1, min(limit, 100)), principal.tenant_id)}


@router.get("/api/agent-eval/runs/{run_id}/results")
def get_agent_eval_run_results(run_id: str, request: Request, tenant_id: str = ""):
    principal = _evaluation_principal(request, tenant_id)
    return {"run_id": run_id, "items": agent.memory.get_agent_eval_results(run_id, principal.tenant_id)}


@router.get("/api/agent-eval/runs/{run_id}/reviews")
def get_agent_eval_human_reviews(run_id: str, request: Request, tenant_id: str = ""):
    principal = _evaluation_principal(request, tenant_id)
    if not _owned_run(run_id, principal.tenant_id):
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)
    return {"run_id": run_id, "items": agent.memory.get_eval_human_reviews("agent", run_id)}


@router.post("/api/agent-eval/runs/{run_id}/reviews")
def upsert_agent_eval_human_review(run_id: str, request: Request, data: dict | None = None):
    payload = data or {}
    principal = _evaluation_principal(request, str(payload.get("tenant_id") or ""))
    if not _owned_run(run_id, principal.tenant_id):
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)
    result_key = str(payload.get("result_key") or "").strip()
    if not result_key:
        return JSONResponse({"error": "result_key 不能为空"}, status_code=400)
    results = agent.memory.get_agent_eval_results(run_id, principal.tenant_id)
    if not any(str(item.get("id")) == result_key for item in results):
        return JSONResponse({"error": "评测结果不存在"}, status_code=404)
    try:
        review_id = agent.memory.upsert_eval_human_review(
            "agent", run_id, result_key, payload.get("scores") or {},
            reviewer=principal.user_id, note=payload.get("note") or "",
        )
        return {"ok": True, "id": review_id}
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.get("/api/agent-eval/runs/{run_id}/calibration")
def get_agent_eval_judge_calibration(run_id: str, request: Request, agreement_tolerance: float = 0.2, tenant_id: str = ""):
    from agent_eval.judge_calibration import build_judge_calibration_report
    principal = _evaluation_principal(request, tenant_id)
    if not _owned_run(run_id, principal.tenant_id):
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)

    reviews = {
        review["result_key"]: review
        for review in agent.memory.get_eval_human_reviews("agent", run_id)
    }
    items = []
    for result in agent.memory.get_agent_eval_results(run_id, principal.tenant_id):
        review = reviews.get(str(result.get("id")))
        if not review:
            continue
        items.append({
            "result_key": result.get("id"),
            "case_key": result.get("case_key"),
            "judge_scores": (result.get("metrics") or {}).get("judge") or {},
            "human_scores": review.get("scores") or {},
            "reviewer": review.get("reviewer") or "",
            "note": review.get("note") or "",
        })
    judge_asset = agent.memory.get_active_prompt_asset("judge_faithfulness") or {}
    latest_report = agent.memory.get_latest_prompt_asset_test_report(
        "judge_faithfulness", int(judge_asset.get("version") or 0), "calibration"
    )
    return {
        "run_id": run_id,
        "review_count": len(reviews),
        "prompt_version": int(judge_asset.get("version") or 0),
        "saved_report": latest_report if latest_report and latest_report.get("run_id") == run_id else None,
        "calibration": build_judge_calibration_report(
            items, agreement_tolerance=agreement_tolerance,
        ),
    }


@router.post("/api/agent-eval/runs/{run_id}/calibration")
def save_agent_eval_judge_calibration(run_id: str, request: Request, data: dict | None = None):
    """Persist human calibration evidence for the currently active Judge Prompt."""
    from agent_eval.judge_calibration import build_judge_calibration_report

    payload = data or {}
    principal = _evaluation_principal(request, str(payload.get("tenant_id") or ""))
    run = _owned_run(run_id, principal.tenant_id)
    if not run:
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)
    reviews = {
        review["result_key"]: review
        for review in agent.memory.get_eval_human_reviews("agent", run_id)
    }
    items = []
    for result in agent.memory.get_agent_eval_results(run_id, principal.tenant_id):
        review = reviews.get(str(result.get("id")))
        if review:
            items.append({
                "result_key": result.get("id"), "case_key": result.get("case_key"),
                "judge_scores": (result.get("metrics") or {}).get("judge") or {},
                "human_scores": review.get("scores") or {},
                "reviewer": review.get("reviewer") or "", "note": review.get("note") or "",
            })
    report = build_judge_calibration_report(
        items,
        agreement_tolerance=payload.get("agreement_tolerance", 0.2),
        min_reviewed=payload.get("min_reviewed", 3),
        min_agreement_rate=payload.get("min_agreement_rate", 0.8),
        max_mean_absolute_error=payload.get("max_mean_absolute_error", 0.2),
    )
    asset = agent.memory.get_active_prompt_asset("judge_faithfulness") or {}
    judge_versions = set()
    judge_models = set()
    for item in items:
        scores = item.get("judge_scores") or {}
        if scores.get("prompt_version") is not None:
            judge_versions.add(int(scores["prompt_version"]))
        for version in (scores.get("prompt_versions") or {}).values():
            judge_versions.add(int(version))
        if scores.get("model"):
            judge_models.add(str(scores["model"]))
    report.update({
        "run_id": run_id,
        "judge_prompt_version": int(asset.get("version") or 0),
        "judge_prompt_versions_observed": sorted(judge_versions),
        "judge_models_observed": sorted(judge_models),
    })
    report["passed"] = bool(report.get("passed") and (
        not judge_versions or int(asset.get("version") or 0) in judge_versions
    ))
    report["pass_reason"] = (
        "人工样本、一致率、误差及 Judge Prompt 版本均达到发布门槛"
        if report["passed"] else "校准样本不足、评分误差过大或结果不属于当前 Judge Prompt 版本"
    )
    report_id = agent.memory.record_prompt_asset_test_run(
        "judge_faithfulness", int(asset.get("version") or 0), report,
        created_by=principal.user_id, test_type="calibration",
    )
    return {"ok": True, "run_id": run_id, "report_id": report_id, "calibration": report}


@router.post("/api/agent-eval/runs/{run_id}/gate")
def evaluate_agent_eval_release_gate(run_id: str, request: Request, data: dict | None = None):
    from agent_eval.release_gate import evaluate_release_gate

    payload = data or {}
    principal = _evaluation_principal(request, str(payload.get("tenant_id") or ""))
    run = _owned_run(run_id, principal.tenant_id)
    if not run:
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)
    return {"ok": True, "run_id": run_id, "gate": evaluate_release_gate(
        run.get("summary") or {}, (data or {}).get("thresholds")
    )}


@router.post("/api/agent-eval/compare")
def compare_agent_eval_runs(request: Request, data: dict | None = None):
    from agent_eval.comparison import compare_runs

    payload = data or {}
    principal = _evaluation_principal(request, str(payload.get("tenant_id") or ""))
    baseline_id = payload.get("baseline_run_id")
    candidate_id = payload.get("candidate_run_id")
    runs = agent.memory.get_agent_eval_runs(100, principal.tenant_id)
    baseline = next((item for item in runs if item.get("run_id") == baseline_id), None)
    candidate = next((item for item in runs if item.get("run_id") == candidate_id), None)
    if not baseline or not candidate:
        return JSONResponse({"error": "baseline 或 candidate 运行不存在"}, status_code=404)
    base_results = agent.memory.get_agent_eval_results(baseline_id, principal.tenant_id)
    candidate_results = agent.memory.get_agent_eval_results(candidate_id, principal.tenant_id)
    return {"ok": True, "comparison": compare_runs(baseline, candidate, base_results, candidate_results)}


@router.get("/api/agent-eval/runs/{run_id}/report")
def export_agent_eval_report(run_id: str, request: Request, tenant_id: str = ""):
    from agent_eval.release_gate import evaluate_release_gate
    from agent_eval.report import render_agent_eval_report

    principal = _evaluation_principal(request, tenant_id)
    run = _owned_run(run_id, principal.tenant_id)
    if not run:
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)
    run["gate"] = evaluate_release_gate(run.get("summary") or {})
    report = render_agent_eval_report(run, agent.memory.get_agent_eval_results(run_id, principal.tenant_id))
    return HTMLResponse(report, headers={"Content-Disposition": f'attachment; filename="agent_eval_{run_id}.html"'})
