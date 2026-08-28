"""Migrate document access metadata across cleaned files and RAG indexes."""

from __future__ import annotations

import json
import logging
import sqlite3
import shutil
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RAG = _ROOT / "RAG_DATA"
_CLEANED = _RAG / "03_cleaned"
_STORE = _RAG / "04_vector_store"
_PARENT = _STORE / "parent_texts.json"
_AUDIT = _CLEANED / ".access_assignment_audit.jsonl"


def _snapshot_state(paths: list[Path]) -> Path:
    """Create a write-ahead filesystem snapshot for a metadata migration."""
    snapshot = Path(tempfile.mkdtemp(prefix="securenexus-access-migration-"))
    sidecars = snapshot / "sidecars"
    sidecars.mkdir()
    manifest = []
    for index, path in enumerate(paths):
        meta_path = path.with_suffix(".meta.json")
        target = sidecars / f"{index}.json"
        if meta_path.exists():
            shutil.copy2(meta_path, target)
            manifest.append({"path": str(meta_path), "backup": str(target), "exists": True})
        else:
            manifest.append({"path": str(meta_path), "backup": "", "exists": False})
    for name, source in (("parent", _PARENT), ("faiss", _STORE / "faiss_index"),
                         ("chroma", _STORE / "chroma_db")):
        target = snapshot / name
        if source.is_dir():
            shutil.copytree(source, target)
            manifest.append({"path": str(source), "backup": str(target), "is_dir": True, "exists": True})
        elif source.exists():
            shutil.copy2(source, target)
            manifest.append({"path": str(source), "backup": str(target), "is_dir": False, "exists": True})
        else:
            manifest.append({"path": str(source), "backup": "", "is_dir": source.suffix == "", "exists": False})
    (snapshot / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    return snapshot


def _restore_snapshot(snapshot: Path) -> None:
    manifest = json.loads((snapshot / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest:
        target = Path(item["path"])
        if target.exists():
            if target.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()
        if item.get("exists"):
            backup = Path(item["backup"])
            target.parent.mkdir(parents=True, exist_ok=True)
            if item.get("is_dir"):
                shutil.copytree(backup, target)
            else:
                shutil.copy2(backup, target)


def _load_sidecar(path: Path) -> dict[str, Any]:
    meta = path.with_suffix(".meta.json")
    if not meta.exists():
        return {}
    try:
        value = json.loads(meta.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def _resolve(path_value: str) -> Path:
    path = Path(path_value).resolve()
    root = _CLEANED.resolve()
    if path.suffix.lower() != ".md":
        raise ValueError("only cleaned markdown documents are supported")
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("document path is outside cleaned data directory") from exc
    if not path.exists():
        raise FileNotFoundError(str(path))
    return path


def _normalize(meta: dict[str, Any]) -> dict[str, str]:
    visibility = str(meta.get("visibility") or "public").strip().lower()
    if visibility not in {"public", "tenant", "private"}:
        visibility = "public"
    result = {
        "visibility": visibility,
        "tenant_id": str(meta.get("tenant_id") or "").strip(),
        "owner_user_id": str(meta.get("owner_user_id") or "").strip(),
        "agent_id": str(meta.get("agent_id") or "").strip(),
        "document_id": str(meta.get("document_id") or "").strip(),
    }
    if visibility == "tenant" and not result["tenant_id"]:
        result["visibility"] = "public"
    if visibility == "private" and (
        not result["tenant_id"] or not result["owner_user_id"]
    ):
        result["visibility"] = "public"
    return result


def _record(path: Path) -> dict[str, Any]:
    meta = _load_sidecar(path)
    access = _normalize(meta)
    return {
        "path": str(path),
        "file_name": path.stem,
        "category": str(meta.get("category") or path.parent.name),
        "profile": str(meta.get("profile") or "general"),
        "access_source": str(meta.get("access_source") or "legacy_public"),
        "needs_review": not bool(meta.get("access_confirmed", False))
        and not bool(meta.get("visibility")),
        **access,
    }


def scan_access_migration(limit: int = 500) -> dict[str, Any]:
    files = sorted(_CLEANED.rglob("*.md")) if _CLEANED.exists() else []
    items = [_record(path) for path in files[: max(1, min(limit, 5000))]]
    return {
        "items": items,
        "total": len(items),
        "needs_review": sum(1 for item in items if item["needs_review"]),
    }


def _validate_scope(db_path: str, access: dict[str, str]) -> None:
    visibility = access["visibility"]
    if visibility == "public":
        return
    if not access["tenant_id"]:
        raise ValueError("工作区共享或私有资料必须选择工作区")
    with sqlite3.connect(db_path) as conn:
        if not conn.execute(
            "SELECT 1 FROM organizations WHERE id=? AND status='active'",
            (access["tenant_id"],),
        ).fetchone():
            raise ValueError("目标工作区不存在或已停用")
        if visibility == "private":
            if not access["owner_user_id"]:
                raise ValueError("私有资料必须选择用户")
            if not conn.execute(
                "SELECT 1 FROM users WHERE id=? AND tenant_id=? AND status='active'",
                (access["owner_user_id"], access["tenant_id"]),
            ).fetchone():
                raise ValueError("目标用户不存在或不属于该工作区")
            if access["agent_id"] and not conn.execute(
                "SELECT 1 FROM agents WHERE id=? AND tenant_id=? AND status='active'",
                (access["agent_id"], access["tenant_id"]),
            ).fetchone():
                raise ValueError("目标 Agent 不存在或不属于该工作区")


def _append_audit(event: dict[str, Any]) -> None:
    _AUDIT.parent.mkdir(parents=True, exist_ok=True)
    with _AUDIT.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def access_assignment_history(path: str, limit: int = 50) -> list[dict[str, Any]]:
    resolved = str(_resolve(path))
    if not _AUDIT.exists():
        return []
    events = []
    for line in _AUDIT.read_text(encoding="utf-8").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("path") == resolved:
            events.append(event)
    return list(reversed(events[-max(1, min(limit, 200)) :]))


def _update_indexes(records: list[tuple[Path, dict[str, str]]]) -> dict[str, int]:
    by_stem = {path.stem: access for path, access in records}
    by_document_id = {access.get("document_id"): access for _, access in records
                      if access.get("document_id")}

    def access_for(item: dict[str, Any], fallback_name: str = ""):
        document_id = str(item.get("document_id") or "")
        return by_document_id.get(document_id) or by_stem.get(
            str(item.get("file_name") or fallback_name or "")
        )
    counts = {"parent_updated": 0, "faiss_updated": 0, "chroma_updated": 0}
    if _PARENT.exists():
        data = json.loads(_PARENT.read_text(encoding="utf-8"))
        for item in data.values():
            access = access_for(item)
            if not access:
                continue
            item.update(access)
            counts["parent_updated"] += 1
        _PARENT.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    faiss_dir = _STORE / "faiss_index"
    if (faiss_dir / "index.faiss").exists():
        try:
            from langchain_community.vectorstores import FAISS
            from langchain_ollama import OllamaEmbeddings

            db = FAISS.load_local(
                str(faiss_dir), OllamaEmbeddings(
                    model="quentinz/bge-small-zh-v1.5",
                    base_url="http://localhost:11434",
                ), allow_dangerous_deserialization=True,
            )
            changed = False
            for key in db.index_to_docstore_id.values():
                doc = db.docstore.search(key)
                access = access_for(doc.metadata) if doc else None
                if access:
                    doc.metadata.update(access)
                    counts["faiss_updated"] += 1
                    changed = True
            if changed:
                db.save_local(str(faiss_dir))
        except Exception as exc:
            raise RuntimeError(f"更新 FAISS 访问 metadata 失败: {exc}") from exc

    chroma_dir = _STORE / "chroma_db"
    if chroma_dir.exists():
        try:
            import chromadb
            from chromadb.config import Settings

            client = chromadb.PersistentClient(path=str(chroma_dir), settings=Settings(anonymized_telemetry=False))
            try:
                collection = client.get_collection("cyber_security")
            except Exception:
                return counts
            all_items = collection.get(include=["metadatas"])
            ids, metas = [], []
            for item_id, metadata in zip(all_items.get("ids", []), all_items.get("metadatas", [])):
                access = access_for(metadata)
                if access:
                    updated = dict(metadata)
                    updated.update(access)
                    ids.append(item_id)
                    metas.append(updated)
            if ids:
                collection.update(ids=ids, metadatas=metas)
                counts["chroma_updated"] = len(ids)
        except Exception as exc:
            raise RuntimeError(f"更新 Chroma 访问 metadata 失败: {exc}") from exc
    return counts


def confirm_access_migration(
    paths: list[str], access: dict[str, Any], db_path: str, changed_by: str = "admin",
    change_reason: str = "",
) -> dict[str, Any]:
    normalized = _normalize(access)
    _validate_scope(db_path, normalized)
    resolved = [_resolve(path) for path in paths]
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    reason = str(change_reason or "").strip()
    previous = [(path, _record(path)) for path in resolved]
    for _, old in previous:
        if old["visibility"] != normalized["visibility"] and old.get("access_source") != "legacy_public" and not reason:
            raise ValueError("修改已确认资料权限时必须填写修改原因")
    snapshot = _snapshot_state(resolved)
    migration_id = "access-" + uuid.uuid4().hex[:12]
    try:
        records = []
        for path, old in previous:
            meta = _load_sidecar(path)
            assigned = dict(normalized)
            # Legacy documents had no stable registration ID. Generate one at the
            # first manual access classification so future rebuilds are namespaced.
            if not assigned.get("document_id"):
                assigned["document_id"] = str(meta.get("document_id") or "doc-" + uuid.uuid4().hex)
            meta.update(assigned)
            meta.update({
                "access_confirmed": True, "access_source": "manual_confirmed",
                "access_updated_at": now, "access_updated_by": str(changed_by or "admin"),
                "access_change_reason": reason,
            })
            path.with_suffix(".meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
            records.append((path, assigned))
        counts = _update_indexes(records) if records else {}
        with sqlite3.connect(db_path) as conn:
            for _, assigned in records:
                document_id = assigned.get("document_id")
                if document_id:
                    conn.execute("""
                        UPDATE documents SET visibility=?, tenant_id=?, owner_user_id=?, agent_id=?,
                            status='indexed', updated_at=CURRENT_TIMESTAMP WHERE id=?
                    """, (assigned["visibility"], assigned["tenant_id"], assigned["owner_user_id"],
                          assigned["agent_id"], document_id))
    except Exception:
        _restore_snapshot(snapshot)
        raise
    finally:
        shutil.rmtree(snapshot, ignore_errors=True)
    for (path, old), (_, assigned) in zip(previous, records):
        _append_audit({
            "action": "access_reclassified", "migration_id": migration_id, "path": str(path), "changed_at": now,
            "changed_by": str(changed_by or "admin"), "change_reason": reason,
            "old": {key: old.get(key, "") for key in assigned}, "new": assigned,
        })
    return {"migration_id": migration_id, "total": len(records), "updated_sidecars": len(records), **counts}


def rollback_access_migration(path: str, changed_at: str, db_path: str,
                              changed_by: str = "admin", change_reason: str = "") -> dict[str, Any]:
    """Restore one document's previous access scope from its immutable audit event."""
    if not str(change_reason or "").strip():
        raise ValueError("回滚访问范围必须填写原因")
    resolved = _resolve(path)
    target = None
    for event in access_assignment_history(str(resolved), limit=200):
        if event.get("changed_at") == changed_at and event.get("action") == "access_reclassified":
            target = event
            break
    if not target:
        raise ValueError("未找到可回滚的访问范围版本")
    old_access = _normalize(target.get("old") or {})
    # A rollback restores the access policy, not the stable document identity
    # allocated during the migration.
    current_meta = _load_sidecar(resolved)
    old_access["document_id"] = str(
        old_access.get("document_id") or current_meta.get("document_id") or ""
    )
    _validate_scope(db_path, old_access)
    current = _record(resolved)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    meta = current_meta
    meta.update(old_access)
    meta.update({
        "access_confirmed": True,
        "access_source": "rollback",
        "access_updated_at": now,
        "access_updated_by": str(changed_by or "admin"),
        "access_change_reason": str(change_reason).strip(),
    })
    resolved.with_suffix(".meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8",
    )
    counts = _update_indexes([(resolved, old_access)])
    if old_access.get("document_id"):
        with sqlite3.connect(db_path) as conn:
            conn.execute("""
                UPDATE documents SET visibility=?, tenant_id=?, owner_user_id=?, agent_id=?,
                    status='indexed', updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (old_access["visibility"], old_access["tenant_id"], old_access["owner_user_id"],
                  old_access["agent_id"], old_access["document_id"]))
    _append_audit({
        "action": "access_rollback", "path": str(resolved), "changed_at": now,
        "changed_by": str(changed_by or "admin"), "change_reason": str(change_reason).strip(),
        "rollback_of": changed_at,
        "old": {key: current.get(key, "") for key in old_access},
        "new": old_access,
    })
    return {"path": str(resolved), "restored_from": changed_at, "updated_sidecars": 1, **counts}
