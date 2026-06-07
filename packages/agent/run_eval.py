"""评估自动化：一键跑30题 + 生成HTML + 版本对比

用法：
  python run_eval.py                    # 跑评估 + 生成HTML
  python run_eval.py --no-run           # 仅从JSON生成HTML（不重跑）
  python run_eval.py --compare          # 跑完对比上次
  python run_eval.py --open             # 跑完自动打开浏览器
"""
import os
import sys
import json
import shutil
import subprocess
import webbrowser
import logging
import argparse
from pathlib import Path
from datetime import datetime

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_SRC = Path(__file__).parent / "src"
_EVAL_DIR = Path(__file__).parent.parent / "eval_results"
_EVAL_DIR.mkdir(parents=True, exist_ok=True)
_VERSION_DIR = _EVAL_DIR / "versions"
_VERSION_DIR.mkdir(exist_ok=True)

_VERSION_FILE = _VERSION_DIR / "eval_versions.json"


def _load_versions() -> list:
    if _VERSION_FILE.exists():
        return json.loads(_VERSION_FILE.read_text(encoding="utf-8"))
    return []


def _save_version(results_json: Path, html_path: Path, version_tag: str):
    versions = _load_versions()
    versions.append({
        "version": version_tag,
        "timestamp": datetime.now().isoformat(),
        "results_file": str(results_json.name),
        "html_file": str(html_path.name),
        "total_questions": None,
        "with_sources": None,
        "errors": None,
    })
    _VERSION_FILE.write_text(
        json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _compute_stats(results: list) -> dict:
    total = len(results)
    with_sources = sum(1 for r in results if r.get("sources"))
    errors = sum(1 for r in results if r.get("error") or r.get("auto_status") == "错误")
    total_sources = sum(len(r.get("sources", [])) for r in results)
    high = sum(1 for r in results for s in r.get("sources", []) if s.get("label") == "高")
    return {
        "total": total,
        "with_sources": with_sources,
        "errors": errors,
        "total_sources": total_sources,
        "high_confidence": high,
        "high_ratio": round(high / total_sources * 100, 1) if total_sources else 0,
    }


def _show_comparison(current_stats: dict):
    versions = _load_versions()
    if len(versions) < 2:
        logger.info("  ℹ️  只有一次记录，无法对比")
        return

    prev = versions[-2]
    prev_path = _EVAL_DIR / prev["results_file"]
    if not prev_path.exists():
        logger.info(f"  ⚠️  上次结果文件不存在: {prev_path}")
        return

    prev_results = json.loads(prev_path.read_text(encoding="utf-8"))
    prev_stats = _compute_stats(prev_results)

    logger.info(f"\n  📊 版本对比：当前 vs {prev['version']}")
    logger.info(f"  {'指标':<20} {'当前':>8} {'上次':>8} {'变化':>8}")
    logger.info(f"  {'-'*48}")
    for key, label in [("total", "总题数"), ("with_sources", "有来源"),
                       ("errors", "错误数"), ("total_sources", "来源总数"),
                       ("high_ratio", "高置信率(%)")]:
        curr_val = current_stats.get(key, 0)
        prev_val = prev_stats.get(key, 0)
        if isinstance(curr_val, (int, float)) and isinstance(prev_val, (int, float)):
            diff = curr_val - prev_val
            diff_str = f"+{diff}" if diff > 0 else str(diff)
            logger.info(f"  {label:<20} {curr_val:>8} {prev_val:>8} {diff_str:>8}")


def main():
    parser = argparse.ArgumentParser(description="Agent 评估自动化")
    parser.add_argument("--no-run", action="store_true", help="不跑评估，仅从已有JSON生成HTML")
    parser.add_argument("--compare", action="store_true", help="跑完对比上次结果")
    parser.add_argument("--open", action="store_true", help="完成后自动打开浏览器")
    args = parser.parse_args()

    version_tag = datetime.now().strftime("%Y%m%d_%H%M")
    eval_script = _SRC / "eval_30_v3.py"

    if not args.no_run:
        logger.info(f"\n  🚀 开始评估 v{version_tag} ...")
        t0 = datetime.now()

        result = subprocess.run(
            [sys.executable, str(eval_script)],
            cwd=str(_SRC),
            capture_output=True,
            text=True,
        )

        elapsed = (datetime.now() - t0).total_seconds()
        logger.info(result.stdout)
        if result.returncode != 0:
            logger.info(f"  ❌ 评估脚本报错 (exit={result.returncode})")
            logger.info(result.stderr[-2000:] if result.stderr else "")
            sys.exit(1)
        logger.info(f"  ✅ 评估完成 ({elapsed:.0f}s)")
    else:
        logger.info("  ℹ️  跳过评估，仅生成HTML")

    # 查找最新的结果JSON和HTML
    json_files = sorted(_EVAL_DIR.glob("eval30_v3_results_*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    html_files = sorted(_EVAL_DIR.glob("scorecard_30_v3_human_tone_*.html"),
                        key=lambda p: p.stat().st_mtime, reverse=True)

    if not json_files:
        logger.error("未找到结果JSON文件")
        sys.exit(1)

    latest_json = json_files[0]
    latest_html = html_files[0] if html_files else _EVAL_DIR / \
        f"scorecard_30_v3_human_tone_{version_tag}.html"

    results = json.loads(latest_json.read_text(encoding="utf-8"))
    stats = _compute_stats(results)

    logger.info(f"\n  📈 本轮指标：")
    logger.info(
        f"    总题数: {stats['total']}  |  有来源: {stats['with_sources']}  |  错误: {stats['errors']}")
    logger.info(
        f"    来源总数: {stats['total_sources']}  |  高置信: {stats['high_confidence']} ({stats['high_ratio']}%)")
    logger.info(f"    JSON: {latest_json.name}")
    logger.info(f"    HTML: {latest_html.name}")

    # ---- 版本记录 ----
    _save_version(latest_json, latest_html, version_tag)

    # ---- 版本对比 ----
    if args.compare:
        _show_comparison(stats)

    # ---- 自动打开 ----
    if args.open and html_files:
        html_path = html_files[0]
        webbrowser.open(f"file:///{html_path.resolve().as_posix()}")
        logger.info(f"  🌐 已打开浏览器: {html_path.name}")

    logger.info(f"\n  ✅ 完成。查看结果: {latest_html}")


if __name__ == "__main__":
    main()
