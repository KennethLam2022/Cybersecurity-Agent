"""Stable evaluation projection for runtime traces.

The chat trace remains the source of truth. This module projects it into a
small, versioned shape that evaluation runners can persist and compare.
"""
from __future__ import annotations

from typing import Any


EVAL_TRACE_SCHEMA_VERSION = "2026.08.phase2.agent-eval-trace.v1"


def normalize_trace(raw_trace: dict[str, Any] | None) -> dict[str, Any]:
    """Return a stable, serializable projection of a chat runtime trace."""
    raw = raw_trace or {}
    steps = []
    for raw_step in raw.get("steps", []):
        if not isinstance(raw_step, dict) or not raw_step.get("step"):
            continue
        duration_s = raw_step.get("duration_s", raw_step.get("time_s", 0))
        try:
            latency_ms = round(float(duration_s) * 1000)
        except (TypeError, ValueError):
            latency_ms = 0
        data = {
            key: value for key, value in raw_step.items()
            if key not in {"step", "duration_s", "time_s"}
        }
        steps.append({
            "step": str(raw_step["step"]),
            "latency_ms": latency_ms,
            "status": "triggered" if raw_step.get("triggered") else "completed",
            "data": data,
        })

    return {
        "schema_version": EVAL_TRACE_SCHEMA_VERSION,
        "source_trace_id": raw.get("trace_id", ""),
        "outcome": raw.get("outcome", "unknown"),
        "outcome_detail": raw.get("outcome_detail", {}),
        "context": raw.get("context", {}),
        "steps": steps,
    }


def trace_step_names(trace: dict[str, Any] | None) -> list[str]:
    """Return ordered step names for simple trajectory assertions."""
    return [
        str(step["step"])
        for step in (trace or {}).get("steps", [])
        if isinstance(step, dict) and step.get("step")
    ]
