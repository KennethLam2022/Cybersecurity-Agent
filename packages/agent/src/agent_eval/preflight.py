"""No-call readiness checks for Agent Evaluation runs."""
from __future__ import annotations

from typing import Any


def build_preflight(agent: Any, cases: list[dict[str, Any]], profile: str,
                    repetitions: int = 1, judge_requested: bool = False,
                    judge_available: bool = False) -> dict[str, Any]:
    """Describe whether an evaluation can start without calling an LLM."""
    llm = getattr(agent, "llm", None)
    model = str(getattr(llm, "model", "") or "")
    base_url = str(getattr(llm, "base_url", "") or "")
    profile_cases = [case for case in cases if (case.get("profile") or "general") == profile]
    checks = {
        "agent_available": agent is not None,
        "cases_available": bool(profile_cases),
        "profile_consistent": bool(profile_cases) and all(
            (case.get("profile") or "general") == profile for case in profile_cases
        ),
        "model_configured": bool(model),
        "endpoint_configured": bool(base_url),
        "judge_available": not judge_requested or judge_available,
    }
    errors = []
    if not checks["cases_available"]:
        errors.append(f"profile={profile} 没有可运行用例")
    if not checks["model_configured"]:
        errors.append("聊天模型未配置")
    if not checks["endpoint_configured"]:
        errors.append("聊天模型地址未配置")
    if judge_requested and not judge_available:
        errors.append("独立评测 LLM 未配置")
    safe_repetitions = max(1, min(int(repetitions or 1), 5))
    return {
        "ready": not errors,
        "profile": profile,
        "case_count": len(profile_cases),
        "repetitions": safe_repetitions,
        "estimated_agent_calls": len(profile_cases) * safe_repetitions,
        "estimated_judge_calls": len(profile_cases) * safe_repetitions if judge_requested else 0,
        "model": model,
        "checks": checks,
        "errors": errors,
    }
