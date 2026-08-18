"""Document profile suggestion helpers.

This is the first-stage classifier for uploaded documents. It uses the file
name, category hints, and optional text preview to suggest a profile and
category for manual confirmation in the backend upload cards.
"""

from __future__ import annotations

import json
import os
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


_REGISTRY_PATH = Path(__file__).with_name("profile_registry.json")


@lru_cache(maxsize=1)
def load_profile_registry() -> dict[str, Any]:
    registry = json.loads(_REGISTRY_PATH.read_text(encoding="utf-8"))
    try:
        from profile_extensions import load_profile_extensions
        existing = {item.get("profile") for item in registry.get("profiles", [])}
        registry["profiles"].extend(
            item for item in load_profile_extensions() if item.get("profile") not in existing
        )
    except (ImportError, OSError, ValueError):
        pass
    return registry


def available_profiles() -> list[dict[str, Any]]:
    return list(load_profile_registry().get("profiles", []))


def profile_options() -> list[dict[str, str]]:
    return [
        {
            "profile": str(p.get("profile", "")),
            "label": str(p.get("label", p.get("profile", ""))),
            "category": str(p.get("category", "")),
            "scope": str(p.get("scope", "")),
            "industry": str(p.get("industry", "")),
            "description": str(p.get("description", "")),
        }
        for p in available_profiles()
    ]


def profile_for_metadata(profile: str = "", category: str = "") -> str:
    """Resolve a stored document profile, including legacy category-only data."""
    known_profiles = {str(p.get("profile", "")) for p in available_profiles()}
    explicit_profile = str(profile or "").strip()
    if explicit_profile in known_profiles:
        return explicit_profile

    normalized_category = str(category or "").strip()
    for item in available_profiles():
        aliases = [item.get("category", ""), *(item.get("category_aliases") or [])]
        if normalized_category and normalized_category in {str(a).strip() for a in aliases}:
            return str(item.get("profile", "general"))
    return "pending"


def enabled_retrieval_profiles() -> set[str]:
    """Return explicitly enabled retrieval profiles; the default is the general core."""
    raw = os.getenv(
        "CYBER_AGENT_RETRIEVAL_PROFILES",
        os.getenv("CYBER_AGENT_SOURCE_PROFILES", "general"),
    )
    profiles = {item.strip() for item in raw.split(",") if item.strip()}
    return profiles or {"general"}


def _normalize_text(*parts: str) -> str:
    return " ".join(p for p in (str(x or "").strip() for x in parts) if p).lower()


def _score_profile(text: str, profile: dict[str, Any]) -> tuple[int, list[str]]:
    score = 0
    hits: list[str] = []
    for kw in profile.get("keywords", []):
        keyword = str(kw or "").strip()
        if not keyword:
            continue
        if keyword.lower() in text:
            score += 2
            hits.append(keyword)
    # 行业特征补充词也由 profile 注册表维护，避免新增行业时修改主流程。
    for alias in profile.get("classifier_aliases", []):
        alias = str(alias or "").strip().lower()
        if not alias:
            continue
        if alias in text:
            score += 1
            hits.append(alias)
    return score, hits


def suggest_document_profile(
    *,
    filename: str = "",
    category_hint: str = "",
    text_preview: str = "",
) -> dict[str, Any]:
    """Return a suggested profile for manual confirmation."""
    text = _normalize_text(filename, category_hint, text_preview)
    best: dict[str, Any] | None = None
    best_score = 0
    best_hits: list[str] = []

    for profile in available_profiles():
        if profile.get("profile") == "general":
            continue
        score, hits = _score_profile(text, profile)
        if score > best_score:
            best = profile
            best_score = score
            best_hits = hits

    if best and best_score > 0:
        confidence = min(0.55 + best_score * 0.12, 0.98)
        return {
            "profile": best.get("profile", "general"),
            "scope": best.get("scope", "industry"),
            "industry": best.get("industry", ""),
            "category": best.get("category", "通用"),
            "label": best.get("label", best.get("profile", "通用")),
            "confidence": round(confidence, 2),
            "reason": f"命中关键词: {', '.join(best_hits[:5])}",
            "review_required": confidence < 0.9,
            "registry_version": load_profile_registry().get("version", ""),
        }

    general = next((p for p in available_profiles() if p.get("profile") == "general"), {})
    fallback_category = str(category_hint or general.get("category") or "通用")
    # 通用资料默认给出建议，但保留待确认，以便管理员最后拍板。
    return {
        "profile": "general",
        "scope": "general",
        "industry": "",
        "category": fallback_category,
        "label": general.get("label", "网络安全通用主干"),
        "confidence": 0.58,
        "reason": "未识别到明显行业特征，按通用主干建议",
        "review_required": True,
        "registry_version": load_profile_registry().get("version", ""),
    }
