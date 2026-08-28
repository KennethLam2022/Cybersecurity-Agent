"""Evidence collection for scoped Word/PPT generation.

Generation keeps source metadata, not a hidden copy of the entire knowledge
base. The renderer can cite the selected documents while the content model
still marks unsupported claims as requiring verification.
"""

from __future__ import annotations

import json
import re
from typing import Any


_URL_PATTERN = re.compile(r"https?://[^\s<>\u3001\u3002\uFF0C\uFF1B\uFF09)]+", re.I)


def extract_explicit_urls(query: str, fields: dict[str, Any] | None = None) -> list[str]:
    """Return only URLs explicitly supplied by the user or generation form."""
    values: list[str] = []
    for item in (fields or {}).get("source_urls") or []:
        if isinstance(item, str):
            values.append(item.strip())
    if isinstance((fields or {}).get("source_url"), str):
        values.append(str((fields or {}).get("source_url")).strip())
    values.extend(_URL_PATTERN.findall(str(query or "")))
    result = []
    for value in values:
        value = value.rstrip(".,;!?)]}").strip()
        if value.startswith(("http://", "https://")) and value not in result:
            result.append(value)
    return result[:5]


def collect_generation_references(
    retriever: Any,
    query: str,
    fields: dict[str, Any] | None,
    tenant_id: str,
    user_id: str,
    agent_id: str,
    top_k: int = 5,
) -> dict[str, Any]:
    """Collect authorized source metadata without making generation claims."""
    fields = fields or {}
    profile = str(fields.get("profile") or "general").strip() or "general"
    scope = {"tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id}
    try:
        results = retriever.search_multi(
            [str(query or "").strip()], top_k=max(1, min(int(top_k), 10)),
            use_rerank=False, profiles={profile}, access_scope=scope,
        ) or []
    except Exception as exc:
        return {
            "status": "error", "profile": profile, "references": [],
            "error": str(exc)[:200], "scope": scope,
        }

    references = []
    context_blocks = []
    seen = set()
    for item in results:
        if not isinstance(item, dict):
            continue
        key = str(item.get("document_id") or item.get("file_name") or item.get("display_name") or "")
        if not key or key in seen:
            continue
        seen.add(key)
        references.append({
            "document_id": str(item.get("document_id") or ""),
            "source_name": str(item.get("display_name") or item.get("file_name") or "")[:255],
            "section": str(item.get("section") or "")[:255],
            "profile": str(item.get("profile") or profile),
            "visibility": str(item.get("visibility") or "public"),
            "score": item.get("rerank_score") if item.get("rerank_score") is not None else item.get("score"),
        })
        content = str(item.get("content") or "").strip()
        if content:
            context_blocks.append({
                "document_id": str(item.get("document_id") or ""),
                "source_name": str(item.get("display_name") or item.get("file_name") or "")[:255],
                "section": str(item.get("section") or "")[:255],
                "profile": str(item.get("profile") or profile),
                "content": content[:3500],
                "source_type": "rag",
            })
    return {
        "status": "retrieved" if references else "empty", "profile": profile,
        "references": references, "context_blocks": context_blocks, "scope": scope,
    }


def build_generation_evidence_plan(
    query: str,
    mode: str,
    fields: dict[str, Any] | None,
    rag_evidence: dict[str, Any],
) -> dict[str, Any]:
    """Build a deterministic evidence gate before spending tokens or network calls."""
    fields = fields or {}
    references = rag_evidence.get("references") or []
    context_blocks = rag_evidence.get("context_blocks") or []
    explicit_urls = extract_explicit_urls(query, fields)
    min_sources = 2 if mode == "presentation" else 1
    rag_sufficient = len(context_blocks) >= min_sources
    external_evidence_present = any(
        str(item.get("source_type") or "").startswith("mcp_")
        for item in context_blocks if isinstance(item, dict)
    )
    external_requested = bool(fields.get("allow_external_research")) or bool(explicit_urls)
    search_requested = bool(fields.get("allow_external_research")) and not explicit_urls
    return {
        "mode": mode,
        "rag_sufficient": rag_sufficient,
        "rag_source_count": len(references),
        "rag_context_count": len(context_blocks),
        "external_evidence_present": external_evidence_present,
        # A single bounded Bing call may contain several result snippets. It can
        # unblock a draft, but its references remain external and unverified.
        "generation_allowed": bool(context_blocks) and (rag_sufficient or external_evidence_present),
        "external_requested": external_requested,
        "external_allowed": external_requested,
        "search_requested": search_requested,
        "explicit_urls": explicit_urls,
        "next_step": "generate_from_rag" if rag_sufficient else (
            "fetch_explicit_urls" if external_requested and explicit_urls else (
                "search_bing" if search_requested else "clarify_or_degrade"
            )
        ),
        "token_policy": {
            "skip_external_when_rag_sufficient": True,
            "max_fetch_urls": 5,
            "max_search_calls": 1,
            "max_search_results": 5,
            "max_context_chars_per_source": 3500,
        },
    }


def append_mcp_search_evidence(
    evidence: dict[str, Any],
    query: str,
    execute_search,
    max_length: int = 3500,
) -> dict[str, Any]:
    """Append bounded Bing MCP search output as explicitly unverified evidence."""
    result = {
        **evidence,
        "references": list(evidence.get("references") or []),
        "context_blocks": list(evidence.get("context_blocks") or []),
        "mcp_calls": list(evidence.get("mcp_calls") or []),
    }
    try:
        response = execute_search(str(query or "").strip(), 5)
        output = (response or {}).get("output") or {}
        content_items = output.get("content") if isinstance(output, dict) else []
        text = "\n".join(
            str(item.get("text") or "")
            for item in (content_items or [])
            if isinstance(item, dict) and item.get("type") in {None, "text"}
        ).strip()
        if not text:
            result["mcp_calls"].append({"tool": "bing_search", "status": "empty"})
            return result
        ref_key = f"external-search:{query[:120]}"
        result["references"].append({
            "document_id": ref_key,
            "source_name": "必应中文搜索 MCP",
            "section": "bing_search",
            "profile": result.get("profile", "general"),
            "visibility": "external",
            "score": 0.25,
            "external": True,
            "external_unverified": True,
        })
        result["context_blocks"].append({
            "document_id": ref_key,
            "source_name": "必应中文搜索 MCP",
            "section": "bing_search",
            "profile": result.get("profile", "general"),
            "content": text[:max_length],
            "source_type": "mcp_search_unverified",
        })
        result["mcp_calls"].append({"tool": "bing_search", "status": "succeeded"})
        result["status"] = "retrieved"
    except Exception as exc:
        result["mcp_calls"].append({"tool": "bing_search", "status": "failed", "error_type": type(exc).__name__})
    return result


def build_generation_context(blocks: list[dict[str, Any]], max_chars: int = 18000) -> str:
    """Format bounded evidence for the generation model without leaking raw internals."""
    parts = []
    total = 0
    for index, item in enumerate(blocks or [], 1):
        content = str(item.get("content") or "").strip()[:3500]
        if not content:
            continue
        part = (
            f"[资料{index}] 来源：{item.get('source_name') or '未命名资料'}"
            f"；章节：{item.get('section') or '未定位'}"
            f"；类型：{item.get('source_type') or 'RAG'}\n{content}"
        )
        if total + len(part) > max_chars:
            break
        parts.append(part)
        total += len(part)
    return "\n\n".join(parts) or "当前没有可供引用的授权资料。"


def append_mcp_fetch_evidence(
    evidence: dict[str, Any],
    urls: list[str],
    execute_fetch,
    max_length: int = 3500,
) -> dict[str, Any]:
    """Fetch explicit URLs through the governed MCP executor and append bounded evidence."""
    result = {
        **evidence,
        "references": list(evidence.get("references") or []),
        "context_blocks": list(evidence.get("context_blocks") or []),
        "mcp_calls": [],
    }
    for url in urls[:5]:
        try:
            response = execute_fetch(url, max_length)
            output = (response or {}).get("output") or {}
            content_items = output.get("content") if isinstance(output, dict) else []
            text = "\n".join(
                str(item.get("text") or "")
                for item in (content_items or [])
                if isinstance(item, dict) and item.get("type") in {None, "text"}
            ).strip()
            if not text:
                result["mcp_calls"].append({"url": url, "status": "empty"})
                continue
            name = url.split("://", 1)[-1].split("/", 1)[0][:255]
            ref_key = f"external:{url}"
            if not any(item.get("document_id") == ref_key for item in result["references"]):
                result["references"].append({
                    "document_id": ref_key,
                    "source_name": name,
                    "section": "Fetch MCP 外部网页",
                    "profile": result.get("profile", "general"),
                    "visibility": "external",
                    "score": 0.35,
                    "external": True,
                    "external_unverified": True,
                    "source_url": url,
                })
            result["context_blocks"].append({
                "document_id": ref_key,
                "source_name": name,
                "section": "Fetch MCP 外部网页",
                "profile": result.get("profile", "general"),
                "content": text[:max_length],
                "source_type": "mcp_external_unverified",
                "source_url": url,
            })
            result["mcp_calls"].append({"url": url, "status": "succeeded"})
        except Exception as exc:
            result["mcp_calls"].append({
                "url": url, "status": "failed", "error_type": type(exc).__name__,
            })
    if result["context_blocks"]:
        result["status"] = "retrieved"
    return result


def generate_outline_from_evidence(
    memory: Any,
    llm: Any,
    mode: str,
    query: str,
    fields: dict[str, Any],
    evidence: dict[str, Any],
    usage_sink=None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Generate renderer-safe content from bounded evidence, with deterministic fallback."""
    from capability_router import build_outline
    from reflection_engine import PROMPT_ASSET_DEFAULTS

    fallback = build_outline(mode, query, fields)
    fallback["evidence_gaps"] = [
        "模型未生成结构化内容，需人工补充页面证据。",
    ]
    if llm is None:
        return fallback, {"status": "degraded", "reason": "generation_model_unconfigured"}
    slot = "generation_presentation" if mode == "presentation" else "generation_writing"
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    asset = memory.get_active_prompt_asset(slot)
    prompt = asset["template"].format(
        query=query,
        fields=json.dumps(fields or {}, ensure_ascii=False),
        sources=build_generation_context(evidence.get("context_blocks") or []),
    )
    try:
        response = llm.chat(
            [{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=4000 if mode == "presentation" else 3000,
            timeout=120,
        )
        if usage_sink:
            usage_sink(response, getattr(llm, "model", ""))
        outline = parse_structured_generation_response(response)
        if not outline or outline.get("mode") != mode:
            return fallback, {"status": "degraded", "reason": "generation_response_invalid"}
        if mode == "presentation":
            requested_pages = max(5, min(int(str(fields.get("page_count") or 8)), 30))
            pages = outline.get("pages") or []
            if len(pages) != requested_pages:
                return fallback, {"status": "degraded", "reason": "generation_page_count_invalid"}
        outline["query"] = query
        outline["fields"] = fields
        outline.setdefault("evidence_gaps", [])
        return outline, {
            "status": "generated", "prompt_version": asset.get("version", 0),
            "model": getattr(llm, "model", ""),
        }
    except Exception as exc:
        return fallback, {"status": "degraded", "reason": type(exc).__name__}
def parse_structured_generation_response(response: Any) -> dict[str, Any] | None:
    """Parse a model response while requiring a renderer-safe object."""
    text = response.get("content", "") if isinstance(response, dict) else str(response or "")
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start:end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    if data.get("mode") == "presentation" and not isinstance(data.get("pages"), list):
        return None
    if data.get("mode") == "writing" and not isinstance(data.get("sections"), list):
        return None
    return data
