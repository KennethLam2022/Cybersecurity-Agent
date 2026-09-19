"""Batch profile confirmation tool for pending documents.

Scans pending profile records, auto-confirms high-confidence suggestions,
and supports dry-run mode to preview changes before writing.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

from profile_migration import (
    ProfileMigrationRecord,
    confirm_profile_migration,
    scan_profile_migration,
)
from profile_classifier import available_profiles

logger = logging.getLogger(__name__)

DEFAULT_MIN_CONFIDENCE = 0.85


def pending_records(scan: dict[str, Any]) -> list[ProfileMigrationRecord]:
    return [
        ProfileMigrationRecord(**rec)
        for rec in scan.get("records", [])
        if rec.get("needs_review")
    ]


def auto_confirmable(
    records: list[ProfileMigrationRecord],
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    profile_filter: str | None = None,
) -> list[ProfileMigrationRecord]:
    """Return records whose suggestion confidence meets the threshold.

    Records must have a non-general candidate profile OR a general profile
    with confidence >= threshold.  If profile_filter is given, only records
    whose *suggested* profile matches are returned.
    """
    result: list[ProfileMigrationRecord] = []
    for rec in records:
        if rec.confirmed:
            continue
        if rec.confidence < min_confidence:
            continue
        if profile_filter:
            if rec.profile != profile_filter:
                continue
        result.append(rec)
    return result


def run_batch_confirm(
    *,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
    profile_filter: str | None = None,
    dry_run: bool = True,
    update_vector_stores: bool = True,
    changed_by: str = "batch-tool",
) -> dict[str, Any]:
    scan = scan_profile_migration()
    pending = pending_records(scan)
    confirmable = auto_confirmable(
        pending,
        min_confidence=min_confidence,
        profile_filter=profile_filter,
    )
    remaining = [r for r in pending if r not in confirmable]

    # Group confirmable records by profile for batch calls
    by_profile: dict[str, list[ProfileMigrationRecord]] = {}
    for rec in confirmable:
        by_profile.setdefault(rec.profile, []).append(rec)

    results: list[dict[str, Any]] = []
    total_written = 0
    total_parent = 0
    total_faiss = 0
    total_chroma = 0

    for profile, recs in sorted(by_profile.items()):
        paths = [r.path for r in recs]
        # Use the most common category within this profile group
        categories = [r.category for r in recs if r.category]
        category = categories[0] if categories else None
        reason = (
            f"批量自动确认: confidence>= {min_confidence}，"
            f"来源: {recs[0].source or 'migration_content_suggested'}"
        )
        if dry_run:
            results.append({
                "profile": profile,
                "count": len(paths),
                "paths": paths[:10],
                "dry_run": True,
                "category": category,
            })
            total_written += len(paths)
        else:
            try:
                out = confirm_profile_migration(
                    paths=paths,
                    profile=profile,
                    category=category,
                    change_reason=reason,
                    changed_by=changed_by,
                    update_vector_stores=update_vector_stores,
                )
                total_written += out.get("written_sidecars", 0)
                total_parent += out.get("parent_updated", 0)
                total_faiss += out.get("faiss_updated", 0)
                total_chroma += out.get("chroma_updated", 0)
                results.append({
                    "profile": profile,
                    "count": out.get("total", len(paths)),
                    "dry_run": False,
                    "result": out,
                })
            except Exception as exc:
                logger.warning("批量确认 %s 失败: %s", profile, exc)
                results.append({
                    "profile": profile,
                    "count": len(paths),
                    "dry_run": False,
                    "error": str(exc),
                })

    faiss_errors = [r.get("result", {}).get("faiss_error", "") for r in results if r.get("result", {}).get("faiss_error")]
    chroma_errors = [r.get("result", {}).get("chroma_error", "") for r in results if r.get("result", {}).get("chroma_error")]
    return {
        "dry_run": dry_run,
        "min_confidence": min_confidence,
        "profile_filter": profile_filter,
        "total_pending": len(pending),
        "auto_confirmable": len(confirmable),
        "remaining_pending": len(remaining),
        "by_profile": {p: len(r) for p, r in by_profile.items()},
        "written_sidecars": total_written,
        "parent_updated": total_parent,
        "faiss_updated": total_faiss,
        "chroma_updated": total_chroma,
        "faiss_errors": faiss_errors,
        "chroma_errors": chroma_errors,
        "results": results,
        "remaining_samples": [asdict(r) for r in remaining[:5]],
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="批量确认 pending profile 资料",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例:\n"
            "  # 预览 (dry-run)\n"
            "  python -m batch_confirm_profiles --dry-run\n\n"
            "  # 确认 confidence>=0.85 且 profile=general\n"
            "  python -m batch_confirm_profiles --min-confidence 0.85 --profile general\n\n"
            "  # 实际写入并更新向量库\n"
            "  python -m batch_confirm_profiles --min-confidence 0.85 --apply"
        ),
    )
    parser.add_argument(
        "--min-confidence", type=float, default=DEFAULT_MIN_CONFIDENCE,
        help=f"最低置信度阈值 (默认 {DEFAULT_MIN_CONFIDENCE})",
    )
    parser.add_argument(
        "--profile", default=None,
        help="只处理指定 profile (如 general, industry/telecom)",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="实际写入 sidecar 和向量库 (默认 dry-run)",
    )
    parser.add_argument(
        "--no-vector-update", action="store_true",
        help="apply 时跳过向量库更新 (仅写 sidecar)",
    )
    parser.add_argument(
        "--changed-by", default="batch-tool",
        help="操作者标识",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="输出详细日志",
    )

    args = parser.parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    dry_run = not args.apply
    result = run_batch_confirm(
        min_confidence=args.min_confidence,
        profile_filter=args.profile,
        dry_run=dry_run,
        update_vector_stores=not args.no_vector_update,
        changed_by=args.changed_by,
    )

    print(json.dumps(
        {k: v for k, v in result.items() if k != "results"},
        ensure_ascii=False, indent=2,
    ))
    if not dry_run:
        for item in result.get("results", []):
            if item.get("error"):
                print(f"ERROR [{item['profile']}]: {item['error']}", file=sys.stderr)


if __name__ == "__main__":
    main()
