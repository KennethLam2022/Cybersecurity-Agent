"""Controlled one-shot external retrieval for approved sources."""
from __future__ import annotations

import hashlib
import re
from html.parser import HTMLParser
from urllib.parse import urlparse

from data_source_reader import read_url


class _TextParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


def _tokens(text: str) -> set[str]:
    return {x for x in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{3,}", str(text or "").lower())}


def _relevant_excerpt(text: str, query: str, max_chars: int = 4000) -> str:
    lines = [line.strip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    terms = _tokens(query)
    ranked = sorted(lines, key=lambda line: len(terms & _tokens(line)), reverse=True)
    selected = ranked[:20] if terms else lines[:20]
    return "\n".join(selected)[:max_chars]


def fetch_external_evidence(query: str, sources: list[dict], max_sources: int = 3,
                            timeout_seconds: int = 10, max_bytes: int = 2_000_000) -> list[dict]:
    """Fetch approved sources only; returned records are evidence, never RAG documents."""
    results = []
    for source in sources[:max(0, int(max_sources))]:
        endpoint = str(source.get("endpoint") or "").strip()
        if not endpoint:
            continue
        try:
            result = read_url(endpoint, {
                "timeout_seconds": max(1, min(int(timeout_seconds), 60)),
                "max_bytes": max(1024, min(int(max_bytes), 50 * 1024 * 1024)),
            })
            excerpt = _relevant_excerpt(result.get("content", ""), query)
            if not excerpt:
                continue
            digest = hashlib.sha256(excerpt.encode("utf-8")).hexdigest()
            host = (urlparse(endpoint).hostname or "").lower()
            results.append({
                "file_name": source.get("name") or host or endpoint,
                "display_name": source.get("name") or host or endpoint,
                "category": "外部实时信息",
                "section": "单次检索证据",
                "content": excerpt,
                "source_url": endpoint,
                "source_id": source.get("id", ""),
                "content_hash": digest,
                "external": True,
                "external_unverified": True,
                "source_type": source.get("source_type", "url"),
                "confidence": 0.35,
                "label": "外部待核验",
            })
        except Exception as exc:
            results.append({
                "file_name": source.get("name") or endpoint,
                "display_name": source.get("name") or endpoint,
                "source_url": endpoint, "source_id": source.get("id", ""),
                "external": True, "external_unverified": True,
                "fetch_error": str(exc),
            })
    return results
