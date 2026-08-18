"""Run the deterministic evaluation contract suite without external model calls."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TESTS = [
    "packages/agent/src/tests/test_prompt_test_contract.py",
    "packages/agent/src/tests/test_retrieval_eval_contract.py",
    "packages/agent/src/tests/test_e2e_eval_contract.py",
    "packages/agent/src/tests/test_agent_eval_runner.py",
    "packages/agent/src/tests/test_agent_eval_release_gate.py",
    "packages/agent/src/tests/test_agent_eval_comparison.py",
    "packages/agent/src/tests/test_agent_eval_report.py",
]


def main() -> int:
    env = os.environ.copy()
    source_paths = [str(ROOT / "packages/agent/src"), str(ROOT / "packages/preprocessor/src")]
    env["PYTHONPATH"] = os.pathsep.join(source_paths + [env.get("PYTHONPATH", "")])
    command = [sys.executable, "-m", "pytest", *TESTS, "-q", "--disable-warnings"]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
