"""Run the complete Python suite with the project's import paths configured."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    env = os.environ.copy()
    source_paths = [
        str(ROOT / "packages" / "agent" / "src"),
        str(ROOT / "packages" / "preprocessor" / "src"),
    ]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(source_paths + ([existing] if existing else []))
    command = [sys.executable, "-m", "pytest", "packages/agent/src/tests", "-q"]
    return subprocess.run(command, cwd=ROOT, env=env, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
