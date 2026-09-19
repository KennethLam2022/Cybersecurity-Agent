"""Document profile suggestion helpers.

This is the first-stage classifier for uploaded documents. It uses the file
name, category hints, and optional text preview to suggest a profile and
category for manual confirmation in the backend upload cards.
"""

from __future__ import annotations

import json
import os
import re
import hashlib
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


def profile_version_snapshot(profile: str = "") -> dict[str, str]:
    """Return a reproducible registry snapshot for an evaluation profile.

    The registry version identifies the configured catalog release.  The
    definition hash additionally captures administrator-confirmed local
    extensions, whose content can change without editing the bundled registry.
    """
    registry = load_profile_registry()
    requested_profile = str(profile or "general").strip() or "general"
    selected = next(
        (item for item in registry.get("profiles", []) if item.get("profile") == requested_profile),
        None,
    )
    registry_version = str(registry.get("version") or "unknown")
    if selected is None:
        return {
            "profile": requested_profile,
            "registry_version": registry_version,
            "profile_version": "unknown",
            "definition_hash": "",
            "status": "unknown",
        }

    # Source paths are operational metadata, not part of the retrieval policy.
    definition = {key: value for key, value in selected.items() if key != "source_paths"}
    serialized = json.dumps(definition, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    definition_hash = hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:16]
    return {
        "profile": requested_profile,
        "registry_version": registry_version,
        "profile_version": f"{registry_version}:{definition_hash}",
        "definition_hash": definition_hash,
        "status": "known",
    }


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


# Industry-specific standard prefixes (e.g. YD/T for telecom, JR/T for finance).
_INDUSTRY_STANDARD_PREFIXES: dict[str, str] = {
    "yd": "industry/telecom",
    "jr": "industry/finance",
    "ga": "industry/government",
    "ws": "industry/healthcare",
    "dl": "industry/energy",
}
# National / international general standards default to the general profile
# regardless of body-content keyword matches.
_GENERAL_STANDARD_PATTERN = re.compile(
    r"(?:GB[\s_/]*[TZ]?|ISO[\s_/]*(?:IEC)?|IEC|TC260)[\s_/]*\d{3,6}",
    re.IGNORECASE,
)
_INDUSTRY_STANDARD_PATTERN = re.compile(
    r"(?:YD|JR|GA|WS|DL)(?:\s*/\s*T)?[\s_/]*\d{3,6}",
    re.IGNORECASE,
)


def _detect_standard_profile(filename: str) -> str | None:
    """Return an industry profile if the filename carries an industry standard prefix."""
    normalized = filename.strip().lower()
    for prefix, profile_id in _INDUSTRY_STANDARD_PREFIXES.items():
        if normalized.startswith(prefix) or ("/" + prefix + "/") in normalized:
            return profile_id
    return None


def _score_profile(text: str, profile: dict[str, Any], *, weight: int = 2) -> tuple[int, list[str]]:
    score = 0
    hits: list[str] = []
    for kw in profile.get("keywords", []):
        keyword = str(kw or "").strip()
        if not keyword:
            continue
        if keyword.lower() in text:
            score += weight
            hits.append(keyword)
    for alias in profile.get("classifier_aliases", []):
        alias = str(alias or "").strip().lower()
        if not alias:
            continue
        if alias in text:
            score += max(1, weight // 2)
            hits.append(alias)
    return score, hits


def suggest_document_profile(
    *,
    filename: str = "",
    category_hint: str = "",
    text_preview: str = "",
) -> dict[str, Any]:
    """Return a suggested profile for manual confirmation."""
    # 1. Industry standard prefix (YD/T, JR/T etc.) is the strongest signal.
    industry_from_prefix = _detect_standard_profile(filename)
    if industry_from_prefix:
        for profile in available_profiles():
            if profile.get("profile") == industry_from_prefix:
                return {
                    "profile": industry_from_prefix,
                    "scope": profile.get("scope", "industry"),
                    "industry": profile.get("industry", ""),
                    "category": profile.get("category", "通用"),
                    "label": profile.get("label", industry_from_prefix),
                    "confidence": 0.95,
                    "reason": f"行业标准前缀命中: {filename[:20]}",
                    "review_required": False,
                    "registry_version": load_profile_registry().get("version", ""),
                }

    # 2. General national/international standard numbers default to general;
    #    body-content keywords in universal standards are coincidental, not
    #    evidence of industry specificity.
    if _GENERAL_STANDARD_PATTERN.search(filename):
        general = next((p for p in available_profiles() if p.get("profile") == "general"), {})
        fallback_category = str(category_hint or general.get("category") or "通用")
        return {
            "profile": "general",
            "scope": "general",
            "industry": "",
            "category": fallback_category,
            "label": general.get("label", "网络安全通用主干"),
            "confidence": 0.92,
            "reason": "GB/T / ISO / TC260 国标或国际标准，默认归通用主干",
            "review_required": False,
            "registry_version": load_profile_registry().get("version", ""),
        }

    # 3. For non-standard documents, score filename and content separately.
    #    A keyword in the filename is a strong signal; the same keyword in a
    #    6000-char preview is weak (universal standards discuss every domain).
    filename_text = _normalize_text(filename, category_hint)
    content_text = _normalize_text(text_preview)
    best: dict[str, Any] | None = None
    best_score = 0
    best_fn_score = 0
    best_hits: list[str] = []

    for profile in available_profiles():
        if profile.get("profile") == "general":
            continue
        fn_score, fn_hits = _score_profile(filename_text, profile, weight=3)
        ct_score, ct_hits = _score_profile(content_text, profile, weight=1)
        total = fn_score + ct_score
        if total > best_score:
            best = profile
            best_score = total
            best_fn_score = fn_score
            best_hits = fn_hits[:3] + ct_hits[:2]

    # Require at least one filename-level hit OR three content-level hits
    # before classifying as an industry-specific document.
    if best and best_score >= 3:
        # Filename hits indicate the document identity; content hits alone
        # are weak evidence.  Weight them separately for confidence.
        confidence = min(0.55 + best_fn_score * 0.15 + (best_score - best_fn_score) * 0.05, 0.95)
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
