"""Controlled storage for administrator-confirmed industry profile extensions."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any


def _extensions_path() -> Path:
    configured = os.getenv("CYBER_AGENT_PROFILE_EXTENSIONS_PATH")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[3] / "agent_data" / "profile_extensions.json"


def _slug(value: str) -> str:
    ascii_slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    if ascii_slug:
        return ascii_slug[:40]
    return "custom-" + hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]


def load_profile_extensions() -> list[dict[str, Any]]:
    path = _extensions_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def propose_profile_extension(*, industry: str, label: str = "", category: str = "",
                              keywords: list[str] | None = None,
                              classifier_aliases: list[str] | None = None,
                              description: str = "") -> dict[str, Any]:
    """Build a proposal without changing the active registry."""
    industry = str(industry or "").strip()
    if not industry:
        raise ValueError("industry 不能为空")
    profile = f"industry/{_slug(industry)}"
    clean_keywords = list(dict.fromkeys(str(item).strip() for item in (keywords or []) if str(item).strip()))
    clean_aliases = list(dict.fromkeys(str(item).strip() for item in (classifier_aliases or []) if str(item).strip()))
    return {
        "profile": profile,
        "scope": "industry",
        "industry": industry,
        "category": str(category or f"{industry}行业安全").strip(),
        "label": str(label or f"{industry}行业扩展").strip(),
        "keywords": clean_keywords,
        "classifier_aliases": clean_aliases,
        "description": str(description or f"{industry}行业扩展资料包").strip(),
        "requires_manual_confirmation": True,
    }


def confirm_profile_extension(proposal: dict[str, Any]) -> dict[str, Any]:
    """Persist a confirmed extension and return the stored definition."""
    required = ("profile", "industry", "category", "label")
    if any(not str(proposal.get(key) or "").strip() for key in required):
        raise ValueError("Profile 扩展缺少必要字段")
    profile = str(proposal["profile"]).strip()
    if not profile.startswith("industry/"):
        raise ValueError("扩展 Profile 必须使用 industry/ 前缀")
    stored = {**proposal, "requires_manual_confirmation": False, "confirmed": True}
    extensions = [item for item in load_profile_extensions() if item.get("profile") != profile]
    extensions.append(stored)
    path = _extensions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(extensions, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stored
