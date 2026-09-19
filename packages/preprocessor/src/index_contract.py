"""Shared contract for all vector index writers and readers."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CONTRACT_VERSION = "1"
DEFAULT_EMBEDDING_MODEL = "quentinz/bge-small-zh-v1.5"
DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_CHROMA_COLLECTION = "cyber_security"
DEFAULT_CHROMA_SPACE = "cosine"


def index_config() -> dict[str, str]:
    return {
        "contract_version": CONTRACT_VERSION,
        "embedding_provider": os.getenv("SECURENEXUS_EMBEDDING_PROVIDER", "ollama"),
        "embedding_model": os.getenv("SECURENEXUS_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        "embedding_base_url": os.getenv("SECURENEXUS_EMBEDDING_BASE_URL", DEFAULT_OLLAMA_URL),
        "chroma_collection": os.getenv("SECURENEXUS_CHROMA_COLLECTION", DEFAULT_CHROMA_COLLECTION),
        "chroma_space": os.getenv("SECURENEXUS_CHROMA_SPACE", DEFAULT_CHROMA_SPACE),
    }


def embedding_metadata(vector_dimension: int = 0) -> dict[str, Any]:
    config = index_config()
    config["vector_dimension"] = int(vector_dimension or 0)
    config["created_at"] = datetime.now(timezone.utc).isoformat()
    config["runtime"] = platform.python_version()
    return config


def normalize_metadata(chunk: dict[str, Any]) -> dict[str, Any]:
    """Return the same metadata shape for batch, incremental and parent indexes."""
    keys = (
        "file_name", "category", "section", "clause", "chunk_type", "chunk_id", "parent_id",
        "profile", "scope", "industry", "profile_confidence", "profile_reason",
        "profile_confirmed", "profile_source", "visibility", "tenant_id", "owner_user_id",
        "agent_id", "document_id", "knowledge_base_id", "page", "page_number", "char_start",
        "char_end",
    )
    result = {}
    for key in keys:
        value = chunk.get(key, "")
        if isinstance(value, (dict, list, tuple, set)):
            value = json.dumps(value, ensure_ascii=False, sort_keys=True)
        result[key] = value
    return result


def text_fingerprint(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def manifest_for(chunks: list[dict], vector_dimension: int = 0) -> dict[str, Any]:
    config = embedding_metadata(vector_dimension)
    entries = []
    for chunk in chunks:
        metadata = normalize_metadata(chunk)
        entries.append({
            "id": str(chunk.get("chunk_id", "")),
            "text_hash": text_fingerprint(str(chunk.get("content", ""))),
            "metadata_hash": text_fingerprint(json.dumps(metadata, ensure_ascii=False, sort_keys=True)),
        })
    entries.sort(key=lambda item: item["id"])
    config.update({"chunk_count": len(entries), "chunks": entries})
    return config


def write_manifest(path: str | Path, chunks: list[dict], vector_dimension: int = 0) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(manifest_for(chunks, vector_dimension), ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def merge_manifest(path: str | Path, chunks: list[dict], vector_dimension: int = 0) -> Path:
    target = Path(path)
    current = {}
    if target.exists():
        try:
            current = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            current = {}
    incoming = manifest_for(chunks, vector_dimension)
    items = {item.get("id"): item for item in current.get("chunks", []) if item.get("id")}
    items.update({item.get("id"): item for item in incoming.get("chunks", []) if item.get("id")})
    merged = {**incoming, **{key: current.get(key, incoming.get(key)) for key in (
        "contract_version", "embedding_provider", "embedding_model", "embedding_base_url",
        "chroma_collection", "chroma_space", "vector_dimension", "created_at", "runtime",
    )}}
    merged["chunks"] = sorted(items.values(), key=lambda item: item["id"])
    merged["chunk_count"] = len(merged["chunks"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return target


def record_index_stage(store_root: str | Path, stage: str, status: str, **metrics: Any) -> None:
    """Append machine-readable embedding/index stage telemetry."""
    path = Path(store_root) / "index_events.jsonl"
    event = {"ts": time.time(), "stage": stage, "status": status, **metrics}
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def compare_manifests(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_items = {item.get("id"): item for item in left.get("chunks", [])}
    right_items = {item.get("id"): item for item in right.get("chunks", [])}
    only_left = sorted(set(left_items) - set(right_items))
    only_right = sorted(set(right_items) - set(left_items))
    changed = sorted(item_id for item_id in set(left_items) & set(right_items)
                     if left_items[item_id] != right_items[item_id])
    return {"same_contract": left.get("contract_version") == right.get("contract_version"),
            "only_left": only_left, "only_right": only_right, "changed": changed,
            "same": not only_left and not only_right and not changed}


def validate_manifest(manifest: dict[str, Any], *, model: str | None = None, dimension: int | None = None) -> list[str]:
    expected = index_config()
    errors = []
    if manifest.get("contract_version") != expected["contract_version"]:
        errors.append("index contract version mismatch")
    if model and manifest.get("embedding_model") != model:
        errors.append("embedding model mismatch")
    if dimension and int(manifest.get("vector_dimension") or 0) not in {0, int(dimension)}:
        errors.append("embedding dimension mismatch")
    if manifest.get("chroma_space") != expected["chroma_space"]:
        errors.append("chroma distance space mismatch")
    return errors
