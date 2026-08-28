"""Helper that converts a simplified patch into a valid git apply diff.

Input format: standard diff header lines, then a bare '@@' line followed by
hunk content lines. The helper counts context/add/delete lines and rewrites
the hunk header so git apply never sees a miscounted hunk.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile


def main() -> int:
    text = sys.stdin.read()
    lines = text.splitlines(keepends=True)
    header: list[str] = []
    hunk_content: list[str] = []
    seen_hunk = False
    for line in lines:
        if not seen_hunk and line.startswith("@@"):
            seen_hunk = True
            continue
        if not seen_hunk:
            header.append(line)
        else:
            hunk_content.append(line)
    if not hunk_content:
        print("no hunk content", file=sys.stderr)
        return 2
    context = sum(1 for line in hunk_content if line.startswith(" ") or line == "\n")
    added = sum(1 for line in hunk_content if line.startswith("+"))
    removed = sum(1 for line in hunk_content if line.startswith("-"))
    new_count = context + added
    old_count = context + removed
    output = "".join(header) + f"@@ -0,0 +0,{new_count} @@\n" + "".join(hunk_content)
    with tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False, encoding="utf-8") as fh:
        fh.write(output)
        patch_path = fh.name
