"""Trace envelope helpers for Agent observability.

The Agent already emits step-level details in several places. This module keeps
the top-level runtime context consistent so evaluation reports can attribute a
run to the prompt, model, taxonomy, and knowledge-base state used at the time.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memory import get_llm_config_card


_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent


def _safe_count_json_items(path: Path) -> int:
    try:
        if not path.exists():
            return 0
        data = json.loads(path.read_text(encoding="utf-8"))
        return len(data) if hasattr(data, "__len__") else 0
    except Exception:
        return 0


def _knowledge_base_snapshot() -> dict[str, Any]:
    rag = _PROJECT_ROOT / "RAG_DATA"
    cleaned = rag / "03_cleaned"
    store = rag / "04_vector_store"
    return {
        "cleaned_docs": sum(1 for p in cleaned.rglob("*.md")) if cleaned.exists() else 0,
        "parent_sections": _safe_count_json_items(store / "parent_texts.json"),
        "faiss_index_exists": (store / "faiss_index" / "index.faiss").exists(),
        "chroma_exists": (store / "chroma_db").exists(),
    }


def _taxonomy_version() -> str:
    try:
        from security_taxonomy import load_taxonomy

        return str(load_taxonomy().get("version", "unknown"))
    except Exception:
        return "unknown"


def _active_prompt_version(db_path: str | None) -> str:
    if not db_path:
        return ""
    try:
        from prompt_versions import get_active_version_name

        return get_active_version_name(db_path) or ""
    except Exception:
        return ""


def _llm_summary(module_id: str) -> dict[str, str]:
    cfg = get_llm_config_card(module_id)
    return {
        "provider": cfg.get("provider", ""),
        "model": cfg.get("model", ""),
        "base_url": cfg.get("base_url", ""),
    }


def build_runtime_context(db_path: str | None = None) -> dict[str, Any]:
    """Return the versioned runtime context shared by chat traces and evals."""
    return {
        "prompt_version": _active_prompt_version(db_path),
        "taxonomy_version": _taxonomy_version(),
        "llm": {
            "chat": _llm_summary("chat"),
            "reranker": _llm_summary("reranker"),
            "embedding": _llm_summary("embedding"),
        },
        "knowledge_base": _knowledge_base_snapshot(),
    }


def build_trace_envelope(
    *,
    original_query: str,
    rewrite_enabled: bool,
    conversation_id: str = "",
    conversation_category: str = "user",
    path: str = "chat",
    db_path: str | None = None,
) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": "2026.08.phase2.trace.v1",
        "trace_id": f"trace_{now.strftime('%Y%m%d%H%M%S%f')}",
        "created_at": now.isoformat(timespec="milliseconds").replace("+00:00", "Z"),
        "original_query": original_query,
        "rewrite_enabled": rewrite_enabled,
        "conversation": {
            "id": conversation_id,
            "category": conversation_category,
            "path": path,
        },
        "context": build_runtime_context(db_path),
        "steps": [],
        "outcome": "started",
    }


def add_trace_step(trace_data: dict[str, Any], step: str, **payload: Any) -> dict[str, Any]:
    """Append one step while allowing ``trace`` in the step payload."""
    trace_data.setdefault("steps", []).append({"step": step, **payload})
    return trace_data


def finish_trace(trace: dict[str, Any], outcome: str, **payload: Any) -> dict[str, Any]:
    trace["outcome"] = outcome
    if payload:
        trace.setdefault("outcome_detail", {}).update(payload)
    return trace


def retrieval_counts(trace: dict[str, Any] | None) -> dict[str, int]:
    data = trace or {}
    retrieval = data.get("retrieval", data)
    if "branches" in retrieval:
        counts = {"faiss": 0, "chroma": 0, "bm25": 0, "final": 0, "returned": 0}
        for branch in retrieval.get("branches", []):
            bc = branch.get("counts", {})
            counts["faiss"] += int(bc.get("faiss") or 0)
            counts["chroma"] += int(bc.get("chroma") or 0)
            counts["bm25"] += int(bc.get("bm25") or 0)
        mc = retrieval.get("merged_counts", {})
        counts["final"] = int(mc.get("final") or mc.get("after_dedupe") or 0)
        counts["returned"] = int(mc.get("returned") or 0)
        return counts

    counts = retrieval.get("counts", {})
    return {
        "faiss": int(counts.get("faiss") or 0),
        "chroma": int(counts.get("chroma") or 0),
        "bm25": int(counts.get("bm25") or 0),
        "final": int(counts.get("after_rerank") or counts.get("after_metadata_filter") or counts.get("merged") or 0),
        "returned": int(counts.get("returned") or 0),
    }
