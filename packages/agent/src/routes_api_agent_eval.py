"""HTTP API for the deterministic Agent Evaluation MVP."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import HTMLResponse, JSONResponse

from app_state import agent, logger, _get_backend_eval_llm


router = APIRouter()


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


def _seed_builtin_cases() -> int:
    from agent_eval.builtin_cases import load_builtin_cases

    count = 0
    for case in load_builtin_cases():
        agent.memory.upsert_agent_eval_case(case)
        count += 1
    return count


@router.get("/api/agent-eval/cases")
def get_agent_eval_cases(profile: str | None = None, include_inactive: bool = False):
    return {"items": agent.memory.get_agent_eval_cases(profile, include_inactive)}


@router.post("/api/agent-eval/cases")
def upsert_agent_eval_case(data: dict):
    try:
        case = _case_payload(data or {})
        case_id = agent.memory.upsert_agent_eval_case(case)
        return {"ok": True, "id": case_id, "case": case}
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception as exc:
        logger.exception("Agent Evaluation 用例保存失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/agent-eval/cases/seed")
def seed_agent_eval_cases():
    try:
        return {"ok": True, "count": _seed_builtin_cases()}
    except Exception as exc:
        logger.exception("Agent Evaluation 主干测试集导入失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.post("/api/agent-eval/langfuse/dataset-sync")
def sync_agent_eval_cases_to_langfuse(data: dict | None = None):
    """Optional one-way copy of local cases to a Langfuse Dataset."""
    from agent_eval.langfuse_exporter import build_langfuse_exporter

    payload = data or {}
    profile = payload.get("profile") or "general"
    dataset_name = str(payload.get("dataset_name") or f"cyber-agent-eval-{profile.replace('/', '-')}").strip()
    exporter = build_langfuse_exporter()
    result = exporter.export_cases_to_dataset(
        agent.memory.get_agent_eval_cases(profile=profile), dataset_name,
    )
    status_code = 200 if result.get("ok") else 503
    return JSONResponse({"profile": profile, **result}, status_code=status_code)


@router.post("/api/agent-eval/preflight")
def agent_eval_preflight(data: dict | None = None):
    from agent_eval.preflight import build_preflight

    payload = data or {}
    profile = payload.get("profile") or "general"
    cases = agent.memory.get_agent_eval_cases(profile=profile)
    if not cases and profile == "general":
        _seed_builtin_cases()
        cases = agent.memory.get_agent_eval_cases(profile=profile)
    judge_requested = bool(payload.get("judge", False))
    judge = _get_backend_eval_llm() if judge_requested else None
    return {"ok": True, "preflight": build_preflight(
        agent, cases, profile, repetitions=payload.get("repetitions", 1),
        judge_requested=judge_requested, judge_available=judge is not None,
    )}


@router.post("/api/agent-eval/run")
def run_agent_eval(data: dict | None = None):
    try:
        from agent_eval.runner import run_agent_evaluation
        from agent_eval.langfuse_exporter import build_langfuse_exporter

        payload = data or {}
        profile = payload.get("profile") or "general"
        if not agent.memory.get_agent_eval_cases(profile=profile):
            _seed_builtin_cases()
        cases = agent.memory.get_agent_eval_cases(profile=profile)
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
        exporter = build_langfuse_exporter()
        return {"ok": True, **run_agent_evaluation(
            agent, cases, profile=profile, judge=judge, exporter=exporter, repetitions=repetitions
        )}
    except Exception as exc:
        logger.exception("Agent Evaluation 运行失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


@router.get("/api/agent-eval/runs")
def get_agent_eval_runs(limit: int = 20):
    return {"items": agent.memory.get_agent_eval_runs(max(1, min(limit, 100)))}


@router.get("/api/agent-eval/runs/{run_id}/results")
def get_agent_eval_run_results(run_id: str):
    return {"run_id": run_id, "items": agent.memory.get_agent_eval_results(run_id)}


@router.get("/api/agent-eval/runs/{run_id}/reviews")
def get_agent_eval_human_reviews(run_id: str):
    return {"run_id": run_id, "items": agent.memory.get_eval_human_reviews("agent", run_id)}


@router.post("/api/agent-eval/runs/{run_id}/reviews")
def upsert_agent_eval_human_review(run_id: str, data: dict | None = None):
    payload = data or {}
    result_key = str(payload.get("result_key") or "").strip()
    if not result_key:
        return JSONResponse({"error": "result_key 不能为空"}, status_code=400)
    results = agent.memory.get_agent_eval_results(run_id)
    if not any(str(item.get("id")) == result_key for item in results):
        return JSONResponse({"error": "评测结果不存在"}, status_code=404)
    try:
        review_id = agent.memory.upsert_eval_human_review(
            "agent", run_id, result_key, payload.get("scores") or {},
            reviewer=payload.get("reviewer") or "", note=payload.get("note") or "",
        )
        return {"ok": True, "id": review_id}
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@router.get("/api/agent-eval/runs/{run_id}/calibration")
def get_agent_eval_judge_calibration(run_id: str, agreement_tolerance: float = 0.2):
    from agent_eval.judge_calibration import build_judge_calibration_report

    reviews = {
        review["result_key"]: review
        for review in agent.memory.get_eval_human_reviews("agent", run_id)
    }
    items = []
    for result in agent.memory.get_agent_eval_results(run_id):
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
    return {
        "run_id": run_id,
        "review_count": len(reviews),
        "calibration": build_judge_calibration_report(
            items, agreement_tolerance=agreement_tolerance,
        ),
    }


@router.post("/api/agent-eval/runs/{run_id}/gate")
def evaluate_agent_eval_release_gate(run_id: str, data: dict | None = None):
    from agent_eval.release_gate import evaluate_release_gate

    run = next((item for item in agent.memory.get_agent_eval_runs(100) if item.get("run_id") == run_id), None)
    if not run:
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)
    return {"ok": True, "run_id": run_id, "gate": evaluate_release_gate(
        run.get("summary") or {}, (data or {}).get("thresholds")
    )}


@router.post("/api/agent-eval/compare")
def compare_agent_eval_runs(data: dict | None = None):
    from agent_eval.comparison import compare_runs

    payload = data or {}
    baseline_id = payload.get("baseline_run_id")
    candidate_id = payload.get("candidate_run_id")
    runs = agent.memory.get_agent_eval_runs(100)
    baseline = next((item for item in runs if item.get("run_id") == baseline_id), None)
    candidate = next((item for item in runs if item.get("run_id") == candidate_id), None)
    if not baseline or not candidate:
        return JSONResponse({"error": "baseline 或 candidate 运行不存在"}, status_code=404)
    base_results = agent.memory.get_agent_eval_results(baseline_id)
    candidate_results = agent.memory.get_agent_eval_results(candidate_id)
    return {"ok": True, "comparison": compare_runs(baseline, candidate, base_results, candidate_results)}


@router.get("/api/agent-eval/runs/{run_id}/report")
def export_agent_eval_report(run_id: str):
    from agent_eval.release_gate import evaluate_release_gate
    from agent_eval.report import render_agent_eval_report

    run = next((item for item in agent.memory.get_agent_eval_runs(100) if item.get("run_id") == run_id), None)
    if not run:
        return JSONResponse({"error": "评测运行不存在"}, status_code=404)
    run["gate"] = evaluate_release_gate(run.get("summary") or {})
    report = render_agent_eval_report(run, agent.memory.get_agent_eval_results(run_id))
    return HTMLResponse(report, headers={"Content-Disposition": f'attachment; filename="agent_eval_{run_id}.html"'})
