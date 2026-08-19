"""Cost estimation and redaction helpers for evaluation evidence exports."""
from __future__ import annotations

import json
import os
import re
from typing import Any


_REDACTION_PATTERNS = (
    (re.compile(r"(?i)\b(bearer\s+)[a-z0-9._~+/=-]{8,}"), r"\1[REDACTED_TOKEN]"),
    (re.compile(r"(?i)\b(sk-[a-z0-9_-]{8,}|api[_-]?key\s*[:=]\s*)[^\s,;]+"), "[REDACTED_SECRET]"),
    (re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"), "[REDACTED_EMAIL]"),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED_PHONE]"),
    (re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)"), "[REDACTED_ID]"),
    (re.compile(r"(?<![\d.])(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}(?![\d.])"), "[REDACTED_IP]"),
)


def redact_text(value: Any, max_length: int = 4000) -> str:
    text = str(value or "")
    for pattern, replacement in _REDACTION_PATTERNS:
        text = pattern.sub(replacement, text)
    return text[:max(0, int(max_length))]


def redact_evidence(value: Any, max_length: int = 4000) -> Any:
    """Recursively redact exportable evidence while preserving its shape."""
    if isinstance(value, dict):
        return {str(key): redact_evidence(item, max_length) for key, item in value.items()}
    if isinstance(value, list):
        return [redact_evidence(item, max_length) for item in value]
    if isinstance(value, tuple):
        return [redact_evidence(item, max_length) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return redact_text(value, max_length) if isinstance(value, str) else value
    return redact_text(value, max_length)


def load_model_pricing() -> dict[str, dict[str, float]]:
    """Load administrator-provided USD per million-token rates from the environment."""
    raw = os.getenv("CYBER_AGENT_EVAL_MODEL_PRICING", "{}").strip()
    try:
        configured = json.loads(raw)
    except ValueError:
        return {}
    if not isinstance(configured, dict):
        return {}
    prices = {}
    for model, rates in configured.items():
        if not isinstance(rates, dict):
            continue
        try:
            input_rate = float(rates.get("input_per_million_usd"))
            output_rate = float(rates.get("output_per_million_usd"))
        except (TypeError, ValueError):
            continue
        if input_rate >= 0 and output_rate >= 0:
            prices[str(model)] = {
                "input_per_million_usd": input_rate,
                "output_per_million_usd": output_rate,
            }
    return prices


def estimate_usage_cost(usage: dict[str, Any] | None, model: str = "",
                        pricing: dict[str, dict[str, float]] | None = None) -> dict[str, Any]:
    """Estimate one call's cost using explicit local configuration only."""
    usage = usage or {}
    prompt_tokens = max(0, int(usage.get("prompt_tokens") or 0))
    completion_tokens = max(0, int(usage.get("completion_tokens") or 0))
    model = str(model or "")
    rates = (pricing if pricing is not None else load_model_pricing()).get(model)
    if not rates:
        return {
            "model": model,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "estimated_cost_usd": None,
            "pricing_status": "unknown_model_or_rate",
        }
    estimated = (
        prompt_tokens * rates["input_per_million_usd"]
        + completion_tokens * rates["output_per_million_usd"]
    ) / 1_000_000
    return {
        "model": model,
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "estimated_cost_usd": round(estimated, 8),
        "pricing_status": "estimated_from_local_configuration",
    }
