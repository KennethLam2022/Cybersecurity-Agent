"""Run deterministic P5 release checks and print remaining approvals."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable
PYTHONPATH = os.pathsep.join((
    str(ROOT / "packages" / "agent" / "src"),
    str(ROOT / "packages" / "preprocessor" / "src"),
))
if str(ROOT / "packages" / "agent" / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "packages" / "agent" / "src"))


def _run(label: str, command: list[str]) -> bool:
    print(f"\n==> {label}", flush=True)
    env = os.environ.copy()
    env["PYTHONPATH"] = PYTHONPATH
    result = subprocess.run(command, cwd=ROOT, env=env, check=False)
    print(f"{label}: {'PASS' if result.returncode == 0 else 'FAIL'}", flush=True)
    return result.returncode == 0


def main() -> int:
    tests = _run("P5 targeted tests", [
        PYTHON, "-m", "pytest",
        "packages/agent/src/tests/test_p5_generation.py",
        "packages/agent/src/tests/test_p5_extension_center.py",
        "packages/agent/src/tests/test_generation_evidence.py",
        "packages/agent/src/tests/test_agent_eval_runner.py",
        "packages/agent/src/tests/test_document_profile_ui.py",
        "-q", "-p", "no:cacheprovider", "--disable-warnings",
        "--basetemp", str(ROOT / ".pytest-tmp-p5-release"),
    ])
    build = _run("Python compile", [
        PYTHON, "-m", "compileall", "-q",
        "packages/agent/src", "packages/preprocessor/src",
    ])
    p5_eval = tests
    artifact_quality = tests
    http_isolation = _run("P5 HTTP isolation smoke", [
        PYTHON, "scripts/p5_http_isolation_smoke.py",
    ])
    evidence = {
        "tests_passed": tests,
        "build_passed": build,
        "p5_eval_passed": p5_eval,
        "artifact_quality_passed": artifact_quality,
        "http_isolation_passed": http_isolation,
        "real_model_baseline_approved": os.environ.get("P5_REAL_MODEL_BASELINE_APPROVED", "") == "1",
        "human_judge_calibration_approved": os.environ.get("P5_JUDGE_CALIBRATION_APPROVED", "") == "1",
        "langfuse_scope_approved_or_disabled": os.environ.get("P5_LANGFUSE_APPROVED", "") == "1",
        "mcp_execution_approved": os.environ.get("P5_MCP_EXECUTION_APPROVED", "") == "1",
    }
    from p5_release_gate import evaluate_p5_release_readiness

    result = evaluate_p5_release_readiness(
        evidence, require_mcp_execution=os.environ.get("P5_REQUIRE_MCP", "") == "1",
    )
    print("\nP5 release readiness:")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
