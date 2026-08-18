"""Built-in, industry-neutral Agent Evaluation cases."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any


_CASES_FILE = Path(__file__).with_name("builtin_cases.json")


def load_builtin_cases() -> list[dict[str, Any]]:
    cases = json.loads(_CASES_FILE.read_text(encoding="utf-8"))
    if not isinstance(cases, list):
        raise ValueError("builtin Agent Evaluation cases must be a list")
    for case in cases:
        if not case.get("case_key") or not case.get("query"):
            raise ValueError("each builtin Agent Evaluation case needs case_key and query")
        case.setdefault("profile", "general")
    return cases
