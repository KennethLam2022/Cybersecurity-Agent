"""P5 release readiness checks.

This module intentionally separates deterministic checks from approvals that
must come from a real-model and human review. It prevents a green unit-test
run from being mistaken for production acceptance.
"""

from __future__ import annotations

from typing import Any


AUTO_CHECKS = (
    "tests_passed",
    "build_passed",
    "p5_eval_passed",
    "artifact_quality_passed",
    "http_isolation_passed",
)

MANUAL_CHECKS = (
    "real_model_baseline_approved",
    "human_judge_calibration_approved",
    "langfuse_scope_approved_or_disabled",
)

OPTIONAL_EXECUTION_CHECKS = (
    "mcp_execution_approved",
)


def evaluate_p5_release_readiness(
    evidence: dict[str, Any] | None = None,
    *,
    require_mcp_execution: bool = False,
) -> dict[str, Any]:
    """Return a transparent P5 release decision from supplied evidence."""
    evidence = evidence or {}
    checks = {
        name: bool(evidence.get(name, False))
        for name in AUTO_CHECKS + MANUAL_CHECKS
    }
    optional = {
        name: bool(evidence.get(name, False))
        for name in OPTIONAL_EXECUTION_CHECKS
    }
    blocking = [name for name, passed in checks.items() if not passed]
    if require_mcp_execution and not optional["mcp_execution_approved"]:
        blocking.append("mcp_execution_approved")
    return {
        "passed": not blocking,
        "checks": checks,
        "optional_checks": optional,
        "blocking_checks": blocking,
        "automatic_checks": list(AUTO_CHECKS),
        "manual_checks": list(MANUAL_CHECKS),
        "mcp_execution_required": require_mcp_execution,
        "status": "ready" if not blocking else "blocked",
    }
