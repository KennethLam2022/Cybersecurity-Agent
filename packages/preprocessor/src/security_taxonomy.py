"""Config-backed network security taxonomy helpers.

The taxonomy keeps domain aliases, labels, icons, and industry-extension flags
outside the Agent and retriever code. Existing category names are preserved so
current FAISS/Chroma metadata remains compatible.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any


_TAXONOMY_PATH = Path(__file__).with_name("security_taxonomy.json")


@lru_cache(maxsize=1)
def load_taxonomy() -> dict[str, Any]:
    return json.loads(_TAXONOMY_PATH.read_text(encoding="utf-8"))


def domains() -> list[dict[str, Any]]:
    return list(load_taxonomy().get("domains", []))


def category_aliases(include_industry: bool = True) -> dict[str, str]:
    aliases: dict[str, str] = {}
    for domain in domains():
        if not include_industry and domain.get("scope") == "industry":
            continue
        category = str(domain.get("category", "")).strip()
        if not category:
            continue
        aliases[category] = category
        aliases[str(domain.get("label", "")).strip()] = category
        for alias in domain.get("aliases", []):
            alias = str(alias).strip()
            if alias:
                aliases[alias] = category
    return {k: v for k, v in aliases.items() if k}


def known_categories(include_industry: bool = True) -> set[str]:
    return set(category_aliases(include_industry=include_industry).values())


def normalize_category(value: str, include_industry: bool = True) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    aliases = category_aliases(include_industry=include_industry)
    return aliases.get(text, text)


def infer_categories_from_text(text: str, include_industry: bool = True) -> list[str]:
    query = str(text or "")
    hits: list[str] = []
    for alias, category in category_aliases(include_industry=include_industry).items():
        if alias and alias in query and category not in hits:
            hits.append(category)
    return hits


def category_icon(category: str, default: str = "file-text") -> str:
    normalized = normalize_category(category)
    for domain in domains():
        if domain.get("category") == normalized:
            return str(domain.get("icon") or default)
    return default


def fallback_queries(domain_id: str = "default") -> list[str]:
    data = load_taxonomy().get("fallback_queries", {})
    queries = data.get(domain_id) or data.get("default") or []
    return [str(q).strip() for q in queries if str(q).strip()]
