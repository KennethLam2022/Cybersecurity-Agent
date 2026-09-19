"""Normalize searchable text without losing security and legal identifiers."""
from __future__ import annotations

import re
from typing import Any


_STANDARD_RE = re.compile(
    r"\b((?:GB/T|GB|YD/T|YD|JR/T|JR|GM/T|GM|ISO/IEC|ISO|IEC)\s*"
    r"\d{2,8}(?:\s*[-—–:]\s*\d{1,4}){0,3})\b",
    re.IGNORECASE,
)
_CLAUSE_RE = re.compile(
    r"第[一二三四五六七八九十百千万零〇两\d]+(?:\.\d+)*条"
)
_DOTTED_ID_RE = re.compile(r"\b\d+(?:\.\d+){1,5}\b")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z0-9_./-]{1,}|[0-9]+")


def _normalize_dash(value: str) -> str:
    return str(value or "").replace("—", "-").replace("–", "-").strip()


def _canonical_standard(value: str) -> str:
    text = re.sub(r"\s+", "", _normalize_dash(value)).upper()
    text = re.sub(r"^(GB/T|YD/T|JR/T|GM/T)", lambda m: m.group(1), text)
    return text


def extract_retrieval_identifiers(text: str) -> dict[str, list[str]]:
    """Extract stable identifiers used for exact retrieval constraints."""
    standards = []
    for match in _STANDARD_RE.finditer(str(text or "")):
        value = _canonical_standard(match.group(1))
        if value and value not in standards:
            standards.append(value)
    clauses = []
    for match in _CLAUSE_RE.findall(str(text or "")):
        if match not in clauses:
            clauses.append(match)
    dotted = []
    for match in _DOTTED_ID_RE.findall(str(text or "")):
        if match not in dotted:
            dotted.append(match)
    return {"standards": standards, "clauses": clauses, "dotted_ids": dotted}


def build_bm25_text(metadata: dict[str, Any]) -> str:
    """Build a field-aware BM25 document representation.

    Metadata is repeated before the body intentionally: BM25 treats the
    representation as a bag of words, so repetition provides a modest field
    boost without changing the persisted source document.
    """
    aliases = metadata.get("aliases") or metadata.get("keywords") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    fields = [
        metadata.get("title", ""), metadata.get("file_name", ""),
        metadata.get("standard_name", ""), " ".join(str(item) for item in aliases),
        metadata.get("section", ""), metadata.get("clause", ""),
    ]
    body = metadata.get("text", metadata.get("content", ""))
    return "\n".join(str(value).strip() for value in fields if str(value or "").strip()) + "\n" + str(body or "").strip()


def tokenize_for_retrieval(text: str) -> list[str]:
    """Tokenize Chinese text while retaining exact identifier variants."""
    raw = _normalize_dash(text)
    tokens: list[str] = []

    def add(value: str) -> None:
        value = str(value or "").strip()
        if value and value not in tokens:
            tokens.append(value)

    identifiers = extract_retrieval_identifiers(raw)
    for standard in identifiers["standards"]:
        prefix, number = standard.rsplit("/", 1) if "/" in standard else ("", standard)
        display = standard
        if prefix:
            display = prefix + "/" + number
        add(display)
        add(re.sub(r"(GB/T|YD/T|JR/T|GM/T)(?=\d)", r"\1 ", display))
        add(re.sub(r"[-/\s]", "", display))
    for clause in identifiers["clauses"]:
        add(clause)
    for dotted in identifiers["dotted_ids"]:
        add(dotted)
        add(dotted.replace(".", ""))

    try:
        from jieba_compat import load_jieba
        segments = load_jieba().cut(raw)
    except Exception:  # pragma: no cover - compatibility fallback
        segments = re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_./-]+", raw)
    for segment in segments:
        segment = str(segment).strip()
        if len(segment) > 1 and not re.fullmatch(r"[\W_]+", segment, re.UNICODE):
            add(segment)
    for word in _WORD_RE.findall(raw):
        add(word.upper() if word.isascii() else word)
    return tokens
