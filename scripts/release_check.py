"""Run reproducible P3 release checks without invoking paid external models."""
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PNPM = "pnpm.cmd" if sys.platform == "win32" else "pnpm"
LOCAL_CHECKS = (
    ("workspace dependency install", [PNPM, "install", "--frozen-lockfile"]),
    ("full test suite", [PNPM, "test"]),
    ("deterministic evaluation smoke", [PNPM, "smoke"]),
    ("python build", [PNPM, "build"]),
    ("shared type build", [PNPM, "--filter", "@cybersec/shared", "build"]),
    ("agent-core build", [PNPM, "--filter", "@cybersec/agent-core", "build"]),
)
EXTERNAL_CONFIRMATIONS = (
    "Real-model baseline has been approved by an administrator.",
    "Human review sample is sufficient for Judge calibration.",
    "Langfuse data-transfer scope is approved when Langfuse is enabled.",
    "MCP production execution rehearsal has been approved when MCP execution is enabled.",
)


def main() -> int:
    env = os.environ.copy()
    env["CI"] = "true"
    for name, command in LOCAL_CHECKS:
        print(f"\n==> {name}", flush=True)
        completed = subprocess.run(command, cwd=ROOT, env=env, check=False)
        if completed.returncode:
            print(f"Release check failed: {name}", file=sys.stderr)
            return completed.returncode

    print("\nLocal P3 release checks passed.")
    print("Administrator confirmations required before production release:")
    for confirmation in EXTERNAL_CONFIRMATIONS:
        print(f"- {confirmation}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
