"""Controlled backups for the SecureNexus database and RAG state."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _files(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*") if path.is_file()) if root.exists() else []


def _manifest(root: Path, project_root: Path, db_path: Path) -> dict:
    entries = []
    for source in (project_root / "RAG_DATA" / "03_cleaned", project_root / "RAG_DATA" / "04_vector_store"):
        for path in _files(source):
            entries.append({
                "path": str(path.relative_to(project_root)).replace("\\", "/"),
                "size": path.stat().st_size,
                "sha256": _sha256(path),
            })
    entries.append({"path": "agent_data/conversations.db", "size": db_path.stat().st_size,
                    "sha256": _sha256(db_path)})
    return {
        "schema_version": "2026.08.p7.backup.v1",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": entries,
        "file_count": len(entries),
    }


def _copy_sqlite(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    source_conn = sqlite3.connect(str(source))
    target_conn = sqlite3.connect(str(target))
    try:
        source_conn.backup(target_conn)
    finally:
        target_conn.close()
        source_conn.close()


def create_backup(db_path: str, project_root: str, output_root: str = "") -> dict:
    project = Path(project_root).resolve()
    database = Path(db_path).resolve()
    if not database.is_file():
        raise FileNotFoundError(str(database))
    root = Path(output_root).resolve() if output_root else project / "agent_data" / "backups"
    backup_id = "backup-" + datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    target = root / backup_id
    target.mkdir(parents=True, exist_ok=False)
    _copy_sqlite(database, target / "agent_data" / "conversations.db")
    for relative in (Path("RAG_DATA") / "03_cleaned", Path("RAG_DATA") / "04_vector_store"):
        source = project / relative
        destination = target / relative
        if source.exists():
            shutil.copytree(source, destination)
    manifest = _manifest(target, target, target / "agent_data" / "conversations.db")
    (target / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"id": backup_id, "path": str(target), **manifest}


def verify_backup(backup_path: str) -> dict:
    root = Path(backup_path).resolve()
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        return {"valid": False, "errors": ["manifest.json 不存在"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return {"valid": False, "errors": [f"manifest 无法读取: {exc}"]}
    errors = []
    checked = 0
    for entry in manifest.get("files", []):
        path = (root / str(entry.get("path", ""))).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(f"备份路径越界: {entry.get('path', '')}")
            continue
        if not path.is_file():
            errors.append(f"文件缺失: {entry.get('path', '')}")
            continue
        checked += 1
        if path.stat().st_size != int(entry.get("size", -1)) or _sha256(path) != entry.get("sha256"):
            errors.append(f"校验失败: {entry.get('path', '')}")
    return {"valid": not errors, "errors": errors, "checked_files": checked,
            "file_count": int(manifest.get("file_count", 0)),
            "schema_version": manifest.get("schema_version", "")}


def restore_backup(backup_path: str, db_path: str, project_root: str) -> dict:
    root = Path(backup_path).resolve()
    verification = verify_backup(str(root))
    if not verification["valid"]:
        raise ValueError("备份校验失败，拒绝恢复")
    project = Path(project_root).resolve()
    database = Path(db_path).resolve()
    protection = create_backup(str(database), str(project))
    _copy_sqlite(root / "agent_data" / "conversations.db", database)
    for relative in (Path("RAG_DATA") / "03_cleaned", Path("RAG_DATA") / "04_vector_store"):
        source = root / relative
        destination = project / relative
        if destination.exists():
            shutil.rmtree(destination)
        if source.exists():
            shutil.copytree(source, destination)
    return {"restored": True, "source": str(root), "protection_backup": protection["path"], **verification}


def list_backups(output_root: str) -> list[dict]:
    root = Path(output_root).resolve()
    if not root.exists():
        return []
    items = []
    for path in sorted((item for item in root.iterdir() if item.is_dir()), reverse=True):
        result = verify_backup(str(path))
        items.append({"id": path.name, "path": str(path), "valid": result["valid"],
                      "file_count": result.get("file_count", 0),
                      "checked_files": result.get("checked_files", 0)})
    return items
