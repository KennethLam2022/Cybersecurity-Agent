"""Explicit, backwards-compatible expectations for Retrieval Eval cases."""
from __future__ import annotations

import json
from typing import Any


def normalize_retrieval_expectation(expected: Any) -> dict[str, Any]:
    """Normalize legacy ``a|b`` expectations without losing their meaning."""
    if isinstance(expected, dict):
        value = dict(expected)
    elif isinstance(expected, str):
        text = expected.strip()
        try:
            parsed = json.loads(text) if text.startswith("{") else None
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed
        else:
            terms = [part.strip() for part in text.split("|") if part.strip()]
            value = {"relevant_terms": terms}
    else:
        value = {}

    return {
        "relevant_doc_ids": [str(v) for v in value.get("relevant_doc_ids") or []],
        "relevant_sources": [str(v) for v in value.get("relevant_sources") or []],
        "relevant_terms": [str(v) for v in value.get("relevant_terms") or []],
        "match_mode": value.get("match_mode", "any"),
        "expect_no_match": bool(value.get("expect_no_match") or value.get("negative")),
    }


def serialize_retrieval_expectation(expected: Any) -> str:
    return json.dumps(normalize_retrieval_expectation(expected), ensure_ascii=False)


def _doc_identifiers(doc: dict[str, Any]) -> set[str]:
    metadata = doc.get("metadata") or {}
    values = [
        doc.get("id"), doc.get("doc_id"), doc.get("source_id"),
        metadata.get("id"), metadata.get("doc_id"), metadata.get("source_id"),
    ]
    return {str(value) for value in values if value not in (None, "")}


def _doc_text(doc: dict[str, Any]) -> str:
    metadata = doc.get("metadata") or {}
    return " ".join(str(doc.get(key) or "") for key in (
        "file_name", "display_name", "source", "content"
    )) + " " + " ".join(str(metadata.get(key) or "") for key in (
        "file_name", "display_name", "source"
    ))


def match_retrieval_expectation(doc: dict[str, Any], expected: Any) -> bool:
    """Return whether one retrieved document satisfies the explicit expectation."""
    normalized = normalize_retrieval_expectation(expected)
    doc_ids = _doc_identifiers(doc)
    text = _doc_text(doc).casefold()
    id_hit = bool(set(normalized["relevant_doc_ids"]) & doc_ids)
    source_hit = any(term.casefold() in text for term in normalized["relevant_sources"])
    term_hits = [term for term in normalized["relevant_terms"] if term.casefold() in text]

    if normalized["match_mode"] == "all":
        requested = normalized["relevant_doc_ids"] + normalized["relevant_sources"] + normalized["relevant_terms"]
        return bool(requested) and all([
            (not normalized["relevant_doc_ids"] or id_hit),
            (not normalized["relevant_sources"] or source_hit),
            (not normalized["relevant_terms"] or len(term_hits) == len(normalized["relevant_terms"])),
        ])
    return id_hit or source_hit or bool(term_hits)


def expectation_evidence(doc: dict[str, Any], expected: Any) -> dict[str, Any]:
    normalized = normalize_retrieval_expectation(expected)
    return {
        "matched": match_retrieval_expectation(doc, normalized),
        "expectation": normalized,
        "doc_ids": sorted(_doc_identifiers(doc)),
    }


def is_negative_expectation(expected: Any) -> bool:
    return normalize_retrieval_expectation(expected)["expect_no_match"]
