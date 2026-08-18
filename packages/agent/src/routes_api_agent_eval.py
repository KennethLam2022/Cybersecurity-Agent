"""HTTP API for the deterministic Agent Evaluation MVP."""
from __future__ import annotations

from fastapi import APIRouter
from fastapi.responses import JSONResponse

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
