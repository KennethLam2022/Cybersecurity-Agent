"""Clause-aware ranking helpers for legal and policy retrieval."""
from __future__ import annotations

import re
from typing import Any


_ARTICLE_RE = re.compile(r"第[一二三四五六七八九十百千万零〇两\d]+条")
_SECTION_TERMS = (
    "附则", "法律责任", "责任", "定义", "总则", "适用范围", "施行", "生效", "发布", "通过", "修订",
)
_DATE_TERMS = ("什么时候", "何时", "日期", "颁布", "发布", "通过", "施行", "生效", "实施", "公布", "修订")


def extract_clause_signals(query: str) -> dict[str, Any]:
    text = str(query or "").strip()
    articles = list(dict.fromkeys(_ARTICLE_RE.findall(text)))
    sections = [term for term in _SECTION_TERMS if term in text]
    date_query = any(term in text for term in _DATE_TERMS)
    return {"articles": articles, "sections": sections, "date_query": date_query}


def clause_awareness_score(doc: dict[str, Any], signals: dict[str, Any]) -> float:
    if not signals.get("articles") and not signals.get("sections") and not signals.get("date_query"):
        return 0.0

    clause = str(doc.get("clause") or "")
    section = str(doc.get("section") or "")
    content = str(doc.get("content") or "")
    chunk_type = str(doc.get("chunk_type") or "")
    score = 0.0

    if any(article == clause or article in section or article in content[:500] for article in signals.get("articles", [])):
        score += 0.75
    elif signals.get("articles") and chunk_type == "clause":
        score += 0.08

    if any(term in section or term in content[:500] for term in signals.get("sections", [])):
        score += 0.35

    if signals.get("date_query"):
        date_terms = ("施行", "生效", "发布日期", "发布", "通过", "颁布", "实施", "公布", "修订")
        if any(term in section or term in content[:800] for term in date_terms):
            score += 0.45
        if "附则" in section or "附则" in content[:300]:
            score += 0.25

    return min(1.0, round(score, 4))


def exact_clause_match(doc: dict[str, Any], signals: dict[str, Any]) -> bool:
    clause = str(doc.get("clause") or "")
    section = str(doc.get("section") or "")
    content = str(doc.get("content") or "")
    return any(article == clause or article in section or article in content[:500] for article in signals.get("articles", []))


def apply_clause_awareness(docs: list[dict[str, Any]], query: str) -> list[dict[str, Any]]:
    signals = extract_clause_signals(query)
    if not signals["articles"] and not signals["sections"] and not signals["date_query"]:
        return docs

    result = []
    for doc in docs:
        item = dict(doc)
        awareness = clause_awareness_score(item, signals)
        exact_match = exact_clause_match(item, signals)
        item["clause_awareness"] = awareness
        item["clause_exact_match"] = exact_match
        if item.get("rerank_score") is not None:
            item["rerank_score"] = min(1.0, float(item["rerank_score"]) + awareness * 0.08 + (0.25 if exact_match else 0.0))
        elif item.get("score") is not None:
            item["score"] = max(0.0, float(item["score"]) - awareness * 0.12 - (0.2 if exact_match else 0.0))
        result.append(item)
    return result
