"""Run the complete Python suite with the project's import paths configured."""
from __future__ import annotations

import os
import subprocess
import sys
import argparse
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _environment() -> dict[str, str]:
    env = os.environ.copy()
    source_paths = [
        str(ROOT / "packages" / "agent" / "src"),
        str(ROOT / "packages" / "preprocessor" / "src"),
    ]
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = os.pathsep.join(source_paths + ([existing] if existing else []))
    return env


def main() -> int:
    parser = argparse.ArgumentParser(description="Run SecureNexus Python tests.")
    parser.add_argument(
        "scope",
        nargs="?",
        choices=("current", "root", "legacy"),
        default="current",
        help="current release suite, root pytest entry, or explicit legacy experiments",
    )
    args = parser.parse_args()

    if args.scope == "current":
        basetemp = ROOT / ".pytest-tmp-current"
        command = [sys.executable, "-m", "pytest", "packages/agent/src/tests", "-q", "-p", "no:cacheprovider", "--basetemp", str(basetemp)]
    elif args.scope == "root":
        basetemp = ROOT / ".pytest-tmp-root"
        command = [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--basetemp", str(basetemp)]
    else:
        basetemp = ROOT / ".pytest-tmp-legacy"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "archive/03-preprocessor历史测试/test_vector_pipeline.py",
            "-q",
            "-p",
            "no:cacheprovider",
            "--basetemp",
            str(basetemp),
        ]
    print(f"Running {args.scope} test scope...", flush=True)
    return subprocess.run(command, cwd=ROOT, env=_environment(), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
