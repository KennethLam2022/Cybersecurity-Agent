"""Metadata filtering helpers for retrieval.

This module intentionally works with the metadata fields already present in
existing FAISS/Chroma/BM25 results. It does not require rebuilding indexes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from security_taxonomy import category_aliases, known_categories, normalize_category
from retrieval_text import extract_retrieval_identifiers


@dataclass
class MetadataFilterSpec:
    doc_ids: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    file_name_contains: list[str] = field(default_factory=list)
    section_contains: list[str] = field(default_factory=list)
    article_numbers: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)
    levels: list[str] = field(default_factory=list)
    negative_terms: list[str] = field(default_factory=list)
    exclude_deprecated: bool = True
    hard_filter: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_ids": self.doc_ids,
            "categories": self.categories,
            "file_name_contains": self.file_name_contains,
            "section_contains": self.section_contains,
            "article_numbers": self.article_numbers,
            "topics": self.topics,
            "levels": self.levels,
            "negative_terms": self.negative_terms,
            "exclude_deprecated": self.exclude_deprecated,
            "hard_filter": self.hard_filter,
        }


def _uniq(items: list[Any]) -> list[str]:
    seen = set()
    result = []
    for item in items or []:
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result


def _compact(text: str) -> str:
    return re.sub(r"[\s\-/—–_]+", "", str(text or "").upper())


def normalize_filter_spec(spec: MetadataFilterSpec | dict | None) -> MetadataFilterSpec:
    if spec is None:
        return MetadataFilterSpec()
    if isinstance(spec, MetadataFilterSpec):
        return spec

    categories = _uniq(list(spec.get("categories") or []))
    exact_categories = [normalize_category(c) for c in categories]

    return MetadataFilterSpec(
        doc_ids=_uniq(list(spec.get("doc_ids") or [])),
        categories=_uniq(exact_categories),
        file_name_contains=_uniq(list(spec.get("file_name_contains") or [])),
        section_contains=_uniq(list(spec.get("section_contains") or [])),
        article_numbers=_uniq(list(spec.get("article_numbers") or [])),
        topics=_uniq(list(spec.get("topics") or [])),
        levels=_uniq(list(spec.get("levels") or [])),
        negative_terms=_uniq(list(spec.get("negative_terms") or [])),
        exclude_deprecated=bool(spec.get("exclude_deprecated", True)),
        hard_filter=bool(spec.get("hard_filter", False)),
    )


def merge_filter_specs(*specs: MetadataFilterSpec | dict | None) -> MetadataFilterSpec:
    merged = MetadataFilterSpec()
    for raw in specs:
        spec = normalize_filter_spec(raw)
        merged.doc_ids.extend(spec.doc_ids)
        merged.categories.extend(spec.categories)
        merged.file_name_contains.extend(spec.file_name_contains)
        merged.section_contains.extend(spec.section_contains)
        merged.article_numbers.extend(spec.article_numbers)
        merged.topics.extend(spec.topics)
        merged.levels.extend(spec.levels)
        merged.negative_terms.extend(spec.negative_terms)
        merged.exclude_deprecated = merged.exclude_deprecated and spec.exclude_deprecated
        merged.hard_filter = merged.hard_filter or spec.hard_filter

    merged.doc_ids = _uniq(merged.doc_ids)
    merged.categories = _uniq([normalize_category(c) for c in merged.categories])
    merged.file_name_contains = _uniq(merged.file_name_contains)
    merged.section_contains = _uniq(merged.section_contains)
    merged.article_numbers = _uniq(merged.article_numbers)
    merged.topics = _uniq(merged.topics)
    merged.levels = _uniq(merged.levels)
    merged.negative_terms = _uniq(merged.negative_terms)
    return merged


def infer_metadata_filter_from_query(query: str) -> MetadataFilterSpec:
    q = query or ""
    spec = MetadataFilterSpec()
    identifiers = extract_retrieval_identifiers(q)
    for standard in identifiers.get("standards", []):
        spec.doc_ids.append(standard)
        spec.file_name_contains.append(standard)

    doc_matches = re.findall(
        r"\b(GB/T|GB|YD/T|YD|JR/T|JR|GM/T|GM)\s*[- ]?\s*(\d{3,6})(?:[-—–](\d{4}))?",
        q,
        flags=re.IGNORECASE,
    )
    for prefix, number, year in doc_matches:
        standard = f"{prefix.upper()} {number}"
        spec.doc_ids.append(number)
        spec.file_name_contains.extend([number, standard])
        if year:
            spec.file_name_contains.append(year)

    if not spec.doc_ids:
        spec.doc_ids.extend(re.findall(r"\b\d{4,6}\b", q))

    article_numbers = re.findall(
        r"(第[一二三四五六七八九十百千万零〇两\d]+条|\d+(?:\.\d+){1,4})",
        q,
    )
    spec.article_numbers.extend(article_numbers)
    spec.section_contains.extend(article_numbers)

    for alias, category in category_aliases().items():
        if alias in q:
            spec.categories.append(category)

    topic_keywords = [
        "访问控制", "身份鉴别", "安全审计", "入侵防范", "恶意代码", "数据完整性", "数据保密性",
        "数据备份", "个人信息", "数据出境", "应急响应", "供应链", "安全管理制度",
    ]
    spec.topics.extend([kw for kw in topic_keywords if kw in q])

    level_matches = re.findall(r"(一级|二级|三级|四级|五级|第[一二三四五]级)", q)
    spec.levels.extend(level_matches)

    neg_matches = re.findall(r"(不能|不含|禁止|除外|不要|不得|不可|没有|不包含|不包括|不应|不允许)([\w\u4e00-\u9fff]{1,12})?", q)
    spec.negative_terms.extend([m[1] for m in neg_matches if m[1]])

    spec.doc_ids = _uniq(spec.doc_ids)
    spec.categories = _uniq(spec.categories)
    spec.file_name_contains = _uniq(spec.file_name_contains)
    spec.section_contains = _uniq(spec.section_contains)
    spec.article_numbers = _uniq(spec.article_numbers)
    spec.topics = _uniq(spec.topics)
    spec.levels = _uniq(spec.levels)
    spec.negative_terms = _uniq(spec.negative_terms)
    spec.hard_filter = bool(spec.doc_ids or spec.article_numbers)
    return spec


def _matches_any_contains(value: str, needles: list[str]) -> bool:
    low = str(value or "").lower()
    compact_value = _compact(value)
    return any(
        str(n).lower() in low or _compact(n) in compact_value
        for n in needles
    )


def apply_metadata_filter(docs: list[dict[str, Any]], spec: MetadataFilterSpec | dict | None) -> list[dict[str, Any]]:
    spec = normalize_filter_spec(spec)
    if not docs:
        return docs

    filtered = []
    for d in docs:
        file_name = str(d.get("file_name", ""))
        category = str(d.get("category", ""))
        section = str(d.get("section", ""))
        content = str(d.get("content", ""))

        if spec.exclude_deprecated and ("_deprecated" in file_name or "⚠️" in content[:50]):
            continue

        if spec.categories and not any(c in category or category in c for c in spec.categories):
            continue

        if spec.doc_ids:
            haystack = _compact(f"{file_name} {section} {content[:500]}")
            if not any(_compact(doc_id) in haystack for doc_id in spec.doc_ids):
                continue

        if spec.file_name_contains and not _matches_any_contains(file_name, spec.file_name_contains):
            continue

        if spec.section_contains and not _matches_any_contains(section, spec.section_contains):
            continue

        if spec.article_numbers:
            haystack = f"{section} {content[:500]}"
            if not any(a in haystack for a in spec.article_numbers):
                continue

        filtered.append(d)

    return filtered


def boost_by_metadata(docs: list[dict[str, Any]], spec: MetadataFilterSpec | dict | None) -> list[dict[str, Any]]:
    spec = normalize_filter_spec(spec)
    if not docs:
        return docs

    boosted = []
    for d in docs:
        nd = dict(d)
        file_name = str(nd.get("file_name", ""))
        category = str(nd.get("category", ""))
        section = str(nd.get("section", ""))
        content = str(nd.get("content", ""))
        boost = 0.0

        if spec.categories and any(c in category or category in c for c in spec.categories):
            boost += 0.08
        if spec.doc_ids:
            haystack = _compact(f"{file_name} {section}")
            if any(_compact(doc_id) in haystack for doc_id in spec.doc_ids):
                boost += 0.15
        if spec.article_numbers:
            haystack = f"{section} {content[:500]}"
            if any(a in haystack for a in spec.article_numbers):
                boost += 0.12
        if spec.topics:
            haystack = f"{section} {content[:800]}"
            if any(topic in haystack for topic in spec.topics):
                boost += 0.06
        if spec.levels:
            haystack = f"{section} {content[:800]}"
            if any(level in haystack for level in spec.levels):
                boost += 0.05

        if boost:
            nd["_metadata_boost"] = round(boost, 4)
            if nd.get("rerank_score") is not None:
                nd["rerank_score"] = min(1.0, float(nd["rerank_score"]) + boost)
            elif nd.get("score") is not None:
                nd["score"] = max(0.0, float(nd["score"]) - boost)
        boosted.append(nd)
    return boosted


def build_chroma_where(spec: MetadataFilterSpec | dict | None) -> dict[str, Any] | None:
    spec = normalize_filter_spec(spec)
    if len(spec.categories) != 1:
        return None
    category = spec.categories[0]
    if category not in known_categories():
        return None
    return {"category": category}
