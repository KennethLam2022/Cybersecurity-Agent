"""Configurable release-gate checks for Agent Evaluation summaries."""
from __future__ import annotations

from typing import Any


DEFAULT_THRESHOLDS = {
    "min_pass_rate": 0.8,
    "max_flaky_rate": 0.0,
    "max_error_rate": 0.0,
    "max_security_failures": 0,
    "max_p95_latency_ms": 10000,
}


def evaluate_release_gate(summary: dict[str, Any], thresholds: dict[str, Any] | None = None) -> dict[str, Any]:
    limits = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    total = int(summary.get("total") or 0)
    errors = int(summary.get("errors") or 0)
    error_rate = errors / total if total else 1.0
    checks = {
        "pass_rate": float(summary.get("pass_rate") or 0) >= float(limits["min_pass_rate"]),
        "flaky_rate": float(summary.get("flaky_rate") or 0) <= float(limits["max_flaky_rate"]),
        "error_rate": error_rate <= float(limits["max_error_rate"]),
        "security_failures": int(summary.get("security_failures") or 0) <= int(limits["max_security_failures"]),
        "p95_latency_ms": int(summary.get("p95_latency_ms") or 0) <= int(limits["max_p95_latency_ms"]),
    }
    reasons = []
    if not checks["pass_rate"]:
        reasons.append(f"通过率低于 {float(limits['min_pass_rate']):.0%}")
    if not checks["flaky_rate"]:
        reasons.append(f"Flaky 率高于 {float(limits['max_flaky_rate']):.0%}")
    if not checks["error_rate"]:
        reasons.append(f"错误率高于 {float(limits['max_error_rate']):.0%}")
    if not checks["security_failures"]:
        reasons.append(f"安全失败数超过 {int(limits['max_security_failures'])}")
    if not checks["p95_latency_ms"]:
        reasons.append(f"P95 超过 {int(limits['max_p95_latency_ms'])}ms")
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "reasons": reasons,
        "thresholds": limits,
        "error_rate": round(error_rate, 4),
    }
