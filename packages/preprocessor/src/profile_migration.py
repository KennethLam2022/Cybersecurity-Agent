"""Stored document profile migration helpers.

This module scans existing cleaned markdown files, infers a profile for each
document, and can write `.meta.json` sidecars plus best-effort metadata updates
into parent_texts / FAISS / Chroma.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from profile_classifier import available_profiles, suggest_document_profile


logger = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_RAG = _ROOT / "RAG_DATA"
_CLEANED_DIR = _RAG / "03_cleaned"
_STORE_DIR = _RAG / "04_vector_store"
_PARENT_FILE = _STORE_DIR / "parent_texts.json"
_AUDIT_FILE_NAME = ".profile_assignment_audit.jsonl"


@dataclass
class ProfileMigrationRecord:
    path: str
    file_name: str
    category: str
    profile: str
    scope: str
    industry: str
    confidence: float
    reason: str
    confirmed: bool
    source: str
    needs_review: bool
    changed_at: str = ""
    changed_by: str = ""
    change_reason: str = ""


def _load_sidecar(meta_path: Path) -> dict[str, Any]:
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_sidecar(md_path: Path, record: ProfileMigrationRecord) -> None:
    meta_path = md_path.with_suffix(".meta.json")
    # Preserve ingestion metadata that is unrelated to Profile assignment.
    meta_payload = _load_sidecar(meta_path)
    meta_payload.update({
        "file_name": record.file_name,
        "category": record.category,
        "scope": record.scope,
        "profile": record.profile,
        "industry": record.industry,
        "profile_confidence": record.confidence,
        "profile_reason": record.reason,
        "profile_confirmed": record.confirmed,
        "profile_source": record.source,
        "needs_review": record.needs_review,
        "migration_path": record.path,
        "profile_updated_at": record.changed_at,
        "profile_updated_by": record.changed_by,
        "profile_change_reason": record.change_reason,
    })
    meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _audit_file() -> Path:
    return _CLEANED_DIR / _AUDIT_FILE_NAME


def _append_assignment_audit(event: dict[str, Any]) -> None:
    audit_file = _audit_file()
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def profile_assignment_history(path: str, limit: int = 50) -> list[dict[str, Any]]:
    """Return newest Profile-assignment audit events for one cleaned document."""
    resolved_path = str(_resolve_cleaned_md_path(path))
    audit_file = _audit_file()
    if not audit_file.exists():
        return []

    events: list[dict[str, Any]] = []
    try:
        for line in audit_file.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and event.get("path") == resolved_path:
                events.append(event)
    except OSError as exc:
        logger.warning(f"读取 Profile 变更审计失败: {exc}")
        return []
    return list(reversed(events[-max(1, min(limit, 200)):]))


def _profile_config(profile: str) -> dict[str, Any]:
    for item in available_profiles():
        if item.get("profile") == profile:
            return item
    raise ValueError(f"unknown profile: {profile}")


def _resolve_cleaned_md_path(path_value: str) -> Path:
    candidate = Path(path_value).resolve()
    cleaned = _CLEANED_DIR.resolve()
    if candidate.suffix.lower() != ".md":
        raise ValueError("only markdown cleaned documents can be calibrated")
    try:
        candidate.relative_to(cleaned)
    except ValueError as exc:
        raise ValueError("document path is outside cleaned data directory") from exc
    if not candidate.exists():
        raise FileNotFoundError(str(candidate))
    return candidate


def _known_general_dir(name: str) -> bool:
    return name in {"01-国家法律", "02-等保国标", "03-CII关基"}


def _known_unknown_dir(name: str) -> bool:
    return name in {"上传文档", "10-项目测试文档", "_archive", "_deprecated"}


def _infer_profile(md_path: Path) -> ProfileMigrationRecord:
    sidecar = _load_sidecar(md_path.with_suffix(".meta.json"))
    category = str(sidecar.get("category") or md_path.parent.name or "").strip()
    file_name = md_path.stem

    if sidecar.get("profile"):
        sidecar_profile = str(sidecar.get("profile", "general"))
        sidecar_confirmed = bool(sidecar.get("profile_confirmed", False))
        # When the sidecar marks profile as pending and not confirmed, fall
        # through to content-based suggestion so high-confidence inferences
        # can be surfaced for batch confirmation.  Confirmed sidecars are
        # still authoritative and returned as-is.
        if sidecar_profile == "pending" and not sidecar_confirmed:
            pass  # fall through to content preview below
        else:
            return ProfileMigrationRecord(
                path=str(md_path),
                file_name=file_name,
                category=category,
                profile=sidecar_profile,
                scope=str(sidecar.get("scope", "general")),
                industry=str(sidecar.get("industry", "")),
                confidence=float(sidecar.get("profile_confidence", sidecar.get("confidence", 1.0)) or 0.0),
                reason=str(sidecar.get("profile_reason", "existing sidecar metadata")),
                confirmed=sidecar_confirmed,
                source=str(sidecar.get("profile_source", "existing")),
                needs_review=not sidecar_confirmed,
                changed_at=str(sidecar.get("profile_updated_at", "")),
                changed_by=str(sidecar.get("profile_updated_by", "")),
                change_reason=str(sidecar.get("profile_change_reason", "")),
            )

    if _known_general_dir(category):
        return ProfileMigrationRecord(
            path=str(md_path),
            file_name=file_name,
            category=category,
            profile="general",
            scope="general",
            industry="",
            confidence=0.95,
            reason=f"目录命中通用主干: {category}",
            confirmed=True,
            source="migration_rule",
            needs_review=False,
        )

    if category == "04-通信行业标准-运营商":
        return ProfileMigrationRecord(
            path=str(md_path),
            file_name=file_name,
            category=category,
            profile="industry/telecom",
            scope="industry",
            industry="telecom",
            confidence=0.95,
            reason=f"目录命中通信行业扩展: {category}",
            confirmed=True,
            source="migration_rule",
            needs_review=False,
        )

    if _known_unknown_dir(category):
        # Use a bounded content preview to produce an auditable suggestion for
        # uploaded material. Suggestions remain unconfirmed and therefore do
        # not enter default retrieval until an administrator approves them.
        try:
            text_preview = md_path.read_text(encoding="utf-8", errors="ignore")[:6000]
        except OSError:
            text_preview = ""
        suggestion = suggest_document_profile(
            filename=file_name,
            category_hint=category,
            text_preview=text_preview,
        )
        suggested_profile = str(suggestion.get("profile") or "general")
        if suggested_profile != "general":
            return ProfileMigrationRecord(
                path=str(md_path),
                file_name=file_name,
                category=str(suggestion.get("category") or category),
                profile=suggested_profile,
                scope=str(suggestion.get("scope") or "industry"),
                industry=str(suggestion.get("industry") or ""),
                confidence=float(suggestion.get("confidence") or 0.0),
                reason=str(suggestion.get("reason") or "基于文件名和正文摘要的候选归类"),
                confirmed=False,
                source="migration_content_suggested",
                needs_review=True,
            )
        suggested_reason = str(suggestion.get("reason") or "目录需要人工确认")
        suggested_confidence = float(suggestion.get("confidence") or 0.4)
        return ProfileMigrationRecord(
            path=str(md_path),
            file_name=file_name,
            category=category,
            profile="pending",
            scope="unknown",
            industry="",
            confidence=suggested_confidence,
            reason=f"{suggested_reason}；目录仍需人工确认: {category}",
            confirmed=False,
            source="migration_content_suggested",
            needs_review=True,
        )

    suggestion = suggest_document_profile(filename=file_name, category_hint=category)
    profile = str(suggestion.get("profile", "general"))
    if profile == "general":
        return ProfileMigrationRecord(
            path=str(md_path),
            file_name=file_name,
            category=category,
            profile="general",
            scope="general",
            industry="",
            confidence=float(suggestion.get("confidence", 0.58) or 0.58),
            reason=str(suggestion.get("reason", "未识别到明确行业特征")),
            confirmed=False,
            source="migration_suggested",
            needs_review=True,
        )

    return ProfileMigrationRecord(
        path=str(md_path),
        file_name=file_name,
        category=str(suggestion.get("category", category) or category),
        profile=profile,
        scope=str(suggestion.get("scope", "industry")),
        industry=str(suggestion.get("industry", "")),
        confidence=float(suggestion.get("confidence", 0.0) or 0.0),
        reason=str(suggestion.get("reason", "自动识别为行业扩展")),
        confirmed=False,
        source="migration_suggested",
        needs_review=bool(suggestion.get("review_required", True)),
    )


def scan_profile_migration(limit: int | None = None) -> dict[str, Any]:
    if not _CLEANED_DIR.exists():
        return {"total": 0, "records": [], "summary": {}}

    records: list[ProfileMigrationRecord] = []
    for md_path in sorted(_CLEANED_DIR.rglob("*.md")):
        if limit is not None and len(records) >= limit:
            break
        records.append(_infer_profile(md_path))

    summary: dict[str, int] = {}
    for rec in records:
        key = rec.profile
        summary[key] = summary.get(key, 0) + 1

    pending = sum(1 for rec in records if rec.needs_review)
    return {
        "total": len(records),
        "pending": pending,
        "summary": summary,
        "records": [asdict(r) for r in records],
        "profiles": [p.get("profile") for p in available_profiles()],
    }


def apply_profile_migration(limit: int | None = None, update_vector_stores: bool = True) -> dict[str, Any]:
    scan = scan_profile_migration(limit=limit)
    records = [ProfileMigrationRecord(**r) for r in scan["records"]]

    written = 0
    for rec in records:
        md_path = Path(rec.path)
        try:
            _write_sidecar(md_path, rec)
            written += 1
        except Exception as e:
            logger.warning(f"写入 sidecar 失败: {md_path}: {e}")

    parent_updated = 0
    faiss_updated = 0
    chroma_updated = 0
    faiss_error = ""
    chroma_error = ""
    if update_vector_stores and records:
        parent_updated, faiss_updated, chroma_updated, faiss_error, chroma_error = _update_vector_store_metadata(records)

    return {
        "total": len(records),
        "written_sidecars": written,
        "parent_updated": parent_updated,
        "faiss_updated": faiss_updated,
        "chroma_updated": chroma_updated,
        "faiss_error": faiss_error,
        "chroma_error": chroma_error,
        "pending": sum(1 for rec in records if rec.needs_review),
    }


def confirm_profile_migration(
    *,
    paths: list[str],
    profile: str,
    category: str | None = None,
    change_reason: str | None = None,
    changed_by: str = "admin",
    update_vector_stores: bool = True,
) -> dict[str, Any]:
    profile_cfg = _profile_config(profile)
    resolved_paths = [_resolve_cleaned_md_path(p) for p in paths]
    final_category = str(category or profile_cfg.get("category") or "通用").strip()

    now = datetime.now().astimezone().isoformat(timespec="seconds")
    reason = str(change_reason or "").strip()
    previous_records = {str(path): _infer_profile(path) for path in resolved_paths}
    reclassified = sum(
        1 for path in resolved_paths
        if previous_records[str(path)].confirmed and (
            previous_records[str(path)].profile != profile
            or previous_records[str(path)].category != final_category
        )
    )
    if reclassified and not reason:
        raise ValueError("修改已确认资料的 profile 时必须填写修改原因")

    records = [
        ProfileMigrationRecord(
            path=str(md_path),
            file_name=md_path.stem,
            category=final_category,
            profile=profile,
            scope=str(profile_cfg.get("scope", "general")),
            industry=str(profile_cfg.get("industry", "")),
            confidence=1.0,
            reason=(
                f"人工重新归属: {profile_cfg.get('label', profile)}"
                if previous_records[str(md_path)].confirmed else
                f"人工确认: {profile_cfg.get('label', profile)}"
            ),
            confirmed=True,
            source="manual_confirmed",
            needs_review=False,
            changed_at=now,
            changed_by=str(changed_by or "admin").strip() or "admin",
            change_reason=reason,
        )
        for md_path in resolved_paths
    ]

    written = 0
    for rec in records:
        _write_sidecar(Path(rec.path), rec)
        previous = previous_records[rec.path]
        _append_assignment_audit({
            "action": "profile_reclassified" if previous.confirmed else "profile_confirmed",
            "path": rec.path,
            "file_name": rec.file_name,
            "changed_at": now,
            "changed_by": rec.changed_by,
            "change_reason": reason,
            "old": {
                "profile": previous.profile,
                "category": previous.category,
                "scope": previous.scope,
                "industry": previous.industry,
                "confirmed": previous.confirmed,
            },
            "new": {
                "profile": rec.profile,
                "category": rec.category,
                "scope": rec.scope,
                "industry": rec.industry,
                "confirmed": rec.confirmed,
            },
        })
        written += 1

    parent_updated = faiss_updated = chroma_updated = 0
    faiss_error = chroma_error = ""
    if update_vector_stores and records:
        parent_updated, faiss_updated, chroma_updated, faiss_error, chroma_error = _update_vector_store_metadata(records)

    return {
        "total": len(records),
        "written_sidecars": written,
        "parent_updated": parent_updated,
        "faiss_updated": faiss_updated,
        "chroma_updated": chroma_updated,
        "faiss_error": faiss_error,
        "chroma_error": chroma_error,
        "reclassified": reclassified,
    }


def _update_vector_store_metadata(records: list[ProfileMigrationRecord]) -> tuple[int, int, int, str, str]:
    profile_by_stem = {Path(rec.path).stem: rec for rec in records}
    parent_updated = 0
    faiss_updated = 0
    chroma_updated = 0
    faiss_error = ""
    chroma_error = ""

    if _PARENT_FILE.exists():
        try:
            parent_data = json.loads(_PARENT_FILE.read_text(encoding="utf-8"))
            for key, item in parent_data.items():
                stem = str(item.get("file_name", ""))
                rec = profile_by_stem.get(stem)
                if not rec:
                    continue
                item["category"] = rec.category
                item["profile"] = rec.profile
                item["scope"] = rec.scope
                item["industry"] = rec.industry
                item["profile_confidence"] = rec.confidence
                item["profile_reason"] = rec.reason
                item["profile_confirmed"] = rec.confirmed
                item["profile_source"] = rec.source
                parent_updated += 1
            _PARENT_FILE.write_text(json.dumps(parent_data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning(f"更新 parent_texts 失败: {e}")

    try:
        import faiss
        import pickle as _pickle
        import numpy as _np

        faiss_dir = _STORE_DIR / "faiss_index"
        faiss_file = faiss_dir / "index.faiss"
        pkl_file = faiss_dir / "index.pkl"
        if faiss_file.exists() and pkl_file.exists():
            # FAISS C++ on Windows cannot open paths with non-ASCII characters.
            # Copy to an ASCII-safe temp dir, update, then copy back.
            import shutil, tempfile
            with tempfile.TemporaryDirectory(prefix="faiss_meta_") as tmp_dir:
                tmp_path = Path(tmp_dir)
                tmp_faiss = tmp_path / "index.faiss"
                tmp_pkl = tmp_path / "index.pkl"
                shutil.copy2(faiss_file, tmp_faiss)
                shutil.copy2(pkl_file, tmp_pkl)
                with open(tmp_pkl, "rb") as _f:
                    # pkl is (docstore, index_to_docstore_id) as saved by langchain FAISS
                    pkl_data = _pickle.load(_f)
                docstore, index_to_docstore_id = pkl_data
                changed = False
                for key in index_to_docstore_id.values():
                    doc = docstore.search(key)
                    if not doc:
                        continue
                    rec = profile_by_stem.get(str(doc.metadata.get("file_name", "")))
                    if not rec:
                        continue
                    doc.metadata.update({
                        "category": rec.category,
                        "profile": rec.profile,
                        "scope": rec.scope,
                        "industry": rec.industry,
                        "profile_confidence": rec.confidence,
                        "profile_reason": rec.reason,
                        "profile_confirmed": rec.confirmed,
                        "profile_source": rec.source,
                    })
                    changed = True
                    faiss_updated += 1
                if changed:
                    with open(tmp_pkl, "wb") as _f:
                        _pickle.dump((docstore, index_to_docstore_id), _f)
                    shutil.copy2(tmp_pkl, pkl_file)
                    logger.info(f"FAISS metadata 已更新 {faiss_updated} 条，写入 index.pkl")
    except Exception as e:
        logger.warning(f"更新 FAISS metadata 失败: {e}")
        faiss_error = str(e)

    try:
        import chromadb
        from chromadb.config import Settings

        chroma_dir = str(_STORE_DIR / "chroma_db")
        client = chromadb.PersistentClient(path=chroma_dir, settings=Settings(anonymized_telemetry=False))
        collection = client.get_collection("cyber_security")
        all_items = collection.get(include=["metadatas"])
        ids_to_update = []
        metas_to_update = []
        for item_id, meta in zip(all_items.get("ids", []), all_items.get("metadatas", [])):
            rec = profile_by_stem.get(str(meta.get("file_name", "")))
            if not rec:
                continue
            new_meta = dict(meta)
            new_meta.update({
                "category": rec.category,
                "profile": rec.profile,
                "scope": rec.scope,
                "industry": rec.industry,
                "profile_confidence": rec.confidence,
                "profile_reason": rec.reason,
                "profile_confirmed": rec.confirmed,
                "profile_source": rec.source,
            })
            ids_to_update.append(item_id)
            metas_to_update.append(new_meta)
        # chromadb limits update batch size; split into chunks of 1000
        chunk_size = 1000
        for i in range(0, len(ids_to_update), chunk_size):
            chunk_ids = ids_to_update[i:i + chunk_size]
            chunk_metas = metas_to_update[i:i + chunk_size]
            collection.update(ids=chunk_ids, metadatas=chunk_metas)
            chroma_updated += len(chunk_ids)
    except Exception as e:
        logger.warning(f"更新 Chroma metadata 失败: {e}")
        chroma_error = str(e)

    return parent_updated, faiss_updated, chroma_updated, faiss_error, chroma_error
