"""Metadata-only monitoring and review-first incident reports."""
from __future__ import annotations

import json
import os
import time
from typing import Any

SEVERITIES = {"P0", "P1", "P2", "P3"}
REPORT_STATES = {"pending_review", "review_plan", "pending_approval", "approved", "rejected"}


def record_event(memory, tenant_id: str, event_type: str, severity: str = "P2",
                 module: str = "", metrics: dict | None = None, trace_id: str = "",
                 detail: str = "") -> dict:
    severity = str(severity or "P2").upper()
    if severity not in SEVERITIES:
        raise ValueError("监控事件级别不合法")
    return memory.create_monitoring_event(
        tenant_id, str(event_type or "unknown"), severity, str(module or ""),
        metrics or {}, str(trace_id or ""), str(detail or "")[:1000],
    )


def build_issue_report(event: dict, created_by: str = "system") -> dict:
    """Create a conservative report; root cause remains a hypothesis."""
    severity = event.get("severity", "P2")
    metrics = event.get("metrics") or {}
    return {
        "title": f"{severity} {event.get('event_type', '系统异常')}",
        "symptom": event.get("detail") or "监控事件触发异常阈值。",
        "timeline": [{"at": event.get("created_at", ""), "event_id": event.get("id", "")}],
        "impact": {"tenant_id": event.get("tenant_id", ""), "module": event.get("module", ""), "metrics": metrics},
        "root_cause_hypothesis": "需要管理员结合 Trace、日志和指标进一步确认，系统不自动认定根因。",
        "recommendation": "检查关联 Trace、模型调用、检索和最近配置变更；确认后再形成审阅预案。",
        "change_plan": {"change": "待管理员填写", "rollback": "待管理员填写", "validation": "运行发布检查和相关评测"},
        "created_by": created_by,
        "review_only": True,
    }


def generate_issue_diagnosis(llm, event: dict, report: dict,
                            usage_sink=None) -> dict:
    """Generate a review-only diagnosis from monitoring metadata.

    The model receives event metadata, not conversation, prompt, or answer text.
    A failed or unavailable model produces a conservative non-diagnostic result.
    """
    safe_event = {
        "event_type": event.get("event_type", ""),
        "severity": event.get("severity", ""),
        "module": event.get("module", ""),
        "metrics": event.get("metrics") or {},
        "trace_id": event.get("trace_id", ""),
        "detail": event.get("detail", "")[:1000],
    }
    safe_report = {
        "title": report.get("report", report).get("title", ""),
        "symptom": report.get("report", report).get("symptom", ""),
        "impact": report.get("report", report).get("impact", {}),
    }
    fallback = {
        "status": "unavailable",
        "root_cause_hypothesis": "AI 分析不可用，需要管理员结合 Trace、日志和指标确认根因。",
        "recommendation": "保留当前审阅预案，先人工核实事件时间线、关联配置和最近发布。",
        "confidence": 0.0,
        "review_only": True,
    }
    if llm is None:
        return fallback
    prompt = (
        "你是网络安全通用型 Agent 的运维诊断助手。仅根据以下监控元数据提出审阅草稿，"
        "不得执行变更，不得捏造日志或事实。输出 JSON，字段为 "
        "root_cause_hypothesis、recommendation、confidence（0 到 1）。\n"
        + json.dumps({"event": safe_event, "report": safe_report}, ensure_ascii=False)
    )
    try:
        result = llm.chat([
            {"role": "system", "content": "你只生成 review_only 的诊断建议。"},
            {"role": "user", "content": prompt},
        ], temperature=0.1, max_tokens=1200)
        if usage_sink:
            usage_sink(result, getattr(llm, "model", ""))
        text = str((result or {}).get("content") or "").strip()
        parsed = json.loads(text)
        if not isinstance(parsed, dict):
            return fallback
        confidence = float(parsed.get("confidence", 0.0))
        return {
            "status": "generated",
            "root_cause_hypothesis": str(parsed.get("root_cause_hypothesis") or fallback["root_cause_hypothesis"])[:4000],
            "recommendation": str(parsed.get("recommendation") or fallback["recommendation"])[:4000],
            "confidence": max(0.0, min(1.0, confidence)),
            "review_only": True,
            "model": (result or {}).get("model") or getattr(llm, "model", ""),
        }
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def scan_cost_spike(memory, tenant_id: str, multiplier: float | None = None,
                    minimum_cost: float | None = None) -> dict:
    """Create a metadata-only cost alert when the 24-hour estimate jumps."""
    multiplier = float(multiplier if multiplier is not None else os.getenv("CYBER_AGENT_COST_SPIKE_MULTIPLIER", "2"))
    minimum_cost = float(minimum_cost if minimum_cost is not None else os.getenv("CYBER_AGENT_COST_SPIKE_MIN", "10"))
    if multiplier <= 1 or minimum_cost < 0:
        raise ValueError("费用突增阈值配置不合法")
    windows = memory.get_usage_cost_windows(tenant_id)
    current = windows["current"]
    previous = windows["previous"]
    ratio = (current["cost"] / previous["cost"]) if previous["cost"] > 0 else None
    triggered = current["cost"] >= minimum_cost and (ratio is None or ratio >= multiplier)
    event = None
    if triggered:
        event = record_event(
            memory, tenant_id, "llm_cost_spike", "P2", "cost",
            {"current_24h_cost": current["cost"], "previous_24h_cost": previous["cost"],
             "ratio": round(ratio, 4) if ratio is not None else None,
             "current_requests": current["requests"], "current_total_tokens": current["total_tokens"],
             "multiplier_threshold": multiplier, "minimum_cost": minimum_cost},
            "", "24 小时估算费用达到突增阈值；仅生成告警，不自动限流或切换模型。",
        )
    return {"triggered": triggered, "windows": windows, "ratio": ratio,
            "thresholds": {"multiplier": multiplier, "minimum_cost": minimum_cost}, "event": event}
