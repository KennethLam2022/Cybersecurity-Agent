"""Register already-indexed cleaned RAG documents without re-ingestion."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from profile_migration import scan_profile_migration

def _document_id(path: str, root: Path) -> str:
    resolved = Path(path).resolve()
    try:
        relative = resolved.relative_to(root.resolve()).as_posix()
    except ValueError:
        # Defensive fallback for isolated tests or relocated corpora.
        relative = resolved.as_posix()
    return "rag-existing-" + hashlib.sha1(relative.encode("utf-8")).hexdigest()[:24]

def scan_existing_rag_documents(limit: int | None = None) -> dict[str, Any]:
    """Return a profile-aware preview of the cleaned Markdown corpus."""
    result = scan_profile_migration(limit=limit)
    root = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "03_cleaned"
    records = []
    for item in result.get("records", []):
        path = str(item.get("path") or "")
        record = dict(item)
        record["document_id"] = _document_id(path, root)
        record["source_name"] = str(item.get("file_name") or Path(path).stem)
        record["cleaned_path"] = path
        record["knowledge_base_id"] = "kb-public-general"
        record["visibility"] = "public"
        record["tenant_id"] = ""
        record["lifecycle_status"] = "review" if item.get("needs_review") else "published"
        records.append(record)
    result["records"] = records
    result["registered_defaults"] = {"knowledge_base_id": "kb-public-general", "visibility": "public", "tenant_id": ""}
    return result

def sync_existing_rag_documents(memory, *, limit: int | None = None, changed_by: str = "admin") -> dict[str, Any]:
    """Idempotently reconcile cleaned Markdown files into documents."""
    preview = scan_existing_rag_documents(limit=limit)
    counts = {"created": 0, "updated": 0, "skipped": 0}
    review = 0
    for item in preview.get("records", []):
        path = str(item.get("cleaned_path") or "")
        sidecar_path = Path(path).with_suffix(".meta.json")
        try:
            sidecar = json.loads(sidecar_path.read_text(encoding="utf-8")) if sidecar_path.exists() else {}
        except (OSError, json.JSONDecodeError):
            sidecar = {}
        sync_metadata = {**sidecar, **item, "rag_reconciled": True}
        outcome = memory.upsert_existing_rag_document(
            document_id=str(item["document_id"]), source_name=str(item.get("source_name") or Path(path).stem),
            cleaned_path=path, category=str(item.get("category") or "通用"), profile=str(item.get("profile") or "general"),
            lifecycle_status=str(item.get("lifecycle_status") or "review"), metadata=sync_metadata, changed_by=changed_by)
        counts[outcome] = counts.get(outcome, 0) + 1
        review += int(bool(item.get("needs_review")))
    return {"ok": True, "scanned": len(preview.get("records", [])), **counts, "needs_review": review, "knowledge_base_id": "kb-public-general", "reindexed": False}
