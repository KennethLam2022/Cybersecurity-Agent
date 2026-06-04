"""
Prompt 版本管理系统 — 支持多版本对比 + A/B 测试
=================================================
参考：掘金《Prompt多版本测试指南》+ LangSmith 理念

版本管理：
- 每个版本对应一个 system prompt 变体
- 支持版本创建、切换、对比、回滚
- 版本变更历史可追溯

评估矩阵：
- 8 个维度 + 权重配置
- 加权得分 = Σ(维度得分 × 权重)
"""

import json
import sqlite3
from pathlib import Path
from typing import Optional
from datetime import datetime

VERSIONS_DB = None  # 通过 main.py 注册


def init_versions_db(db_path: str):
    """初始化版本管理数据库"""
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS prompt_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_name TEXT UNIQUE NOT NULL,
            description TEXT,
            system_prompt TEXT NOT NULL,
            created_at TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            created_by TEXT DEFAULT 'system'
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS version_test_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            version_name TEXT NOT NULL,
            test_id TEXT NOT NULL,
            test_category TEXT NOT NULL,
            query TEXT NOT NULL,
            answer TEXT,
            scores TEXT,
            weighted_score REAL,
            total_tokens INTEGER,
            duration REAL,
            passed INTEGER,
            evaluated_at TEXT NOT NULL,
            FOREIGN KEY(version_name) REFERENCES prompt_versions(version_name)
        )
    """)
    conn.commit()
    conn.close()


def create_version(version_name: str, description: str, system_prompt: str, db_path: str) -> dict:
    """创建新 Prompt 版本"""
    conn = sqlite3.connect(db_path)
    # 先检查是否已存在
    exists = conn.execute(
        "SELECT id FROM prompt_versions WHERE version_name = ?", (version_name,)
    ).fetchone()
    if exists:
        conn.close()
        return {"ok": False, "message": f"版本 {version_name} 已存在"}

    conn.execute("UPDATE prompt_versions SET is_active = 0")
    conn.execute(
        "INSERT INTO prompt_versions (version_name, description, system_prompt, created_at, is_active) VALUES (?, ?, ?, ?, ?)",
        (version_name, description, system_prompt, datetime.now().isoformat(), 1),
    )
    conn.commit()
    conn.close()
    return {"ok": True, "version": version_name}


def list_versions(db_path: str) -> list:
    """列出所有版本（含 ID）"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, version_name, description, created_at, is_active FROM prompt_versions ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "description": r[2], "created_at": r[3], "is_active": bool(r[4])} for r in rows]


def get_active_version(db_path: str) -> Optional[str]:
    """获取当前激活版本"""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT version_name FROM prompt_versions WHERE is_active = 1 LIMIT 1"
    ).fetchone()
    conn.close()
    return row[0] if row else None


def switch_version(version_name: str, db_path: str) -> dict:
    """切换激活版本"""
    conn = sqlite3.connect(db_path)
    exists = conn.execute(
        "SELECT id FROM prompt_versions WHERE version_name = ?", (version_name,)
    ).fetchone()
    if not exists:
        conn.close()
        return {"ok": False, "message": f"版本 {version_name} 不存在"}

    conn.execute("UPDATE prompt_versions SET is_active = 0")
    conn.execute("UPDATE prompt_versions SET is_active = 1 WHERE version_name = ?", (version_name,))
    conn.commit()
    conn.close()
    return {"ok": True, "version": version_name}


def compare_versions(version_a: str, version_b: str, db_path: str) -> dict:
    """对比两个版本的测试结果"""
    conn = sqlite3.connect(db_path)

    # 获取两个版本的测试结果
    a_rows = conn.execute(
        "SELECT test_category, AVG(weighted_score) as avg_score, COUNT(*) as count, SUM(passed) as passed FROM version_test_results WHERE version_name = ? GROUP BY test_category",
        (version_a,)
    ).fetchall()

    b_rows = conn.execute(
        "SELECT test_category, AVG(weighted_score) as avg_score, COUNT(*) as count, SUM(passed) as passed FROM version_test_results WHERE version_name = ? GROUP BY test_category",
        (version_b,)
    ).fetchall()

    # 获取总体统计
    a_total = conn.execute(
        "SELECT AVG(weighted_score), COUNT(*), SUM(passed), AVG(total_tokens), AVG(duration) FROM version_test_results WHERE version_name = ?",
        (version_a,)
    ).fetchone()

    b_total = conn.execute(
        "SELECT AVG(weighted_score), COUNT(*), SUM(passed), AVG(total_tokens), AVG(duration) FROM version_test_results WHERE version_name = ?",
        (version_b,)
    ).fetchone()

    conn.close()

    return {
        "version_a": {
            "name": version_a,
            "overall_avg": round(a_total[0] * 100, 1) if a_total and a_total[0] else 0,
            "total_tests": a_total[1] if a_total else 0,
            "passed": a_total[2] if a_total else 0,
            "avg_tokens": round(a_total[3], 0) if a_total and a_total[3] else 0,
            "avg_duration": round(a_total[4], 2) if a_total and a_total[4] else 0,
            "by_category": [dict(zip(["test_category", "avg_score", "count", "passed"], r)) for r in a_rows],
        },
        "version_b": {
            "name": version_b,
            "overall_avg": round(b_total[0] * 100, 1) if b_total and b_total[0] else 0,
            "total_tests": b_total[1] if b_total else 0,
            "passed": b_total[2] if b_total else 0,
            "avg_tokens": round(b_total[3], 0) if b_total and b_total[3] else 0,
            "avg_duration": round(b_total[4], 2) if b_total and b_total[4] else 0,
            "by_category": [dict(zip(["test_category", "avg_score", "count", "passed"], r)) for r in b_rows],
        },
    }


def get_active_version_name(db_path: str) -> str | None:
    """获取当前活跃版本的名称"""
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT version_name FROM prompt_versions WHERE is_active = 1 LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None


def activate_version(version_id: int, db_path: str) -> dict:
    """设置指定版本为活跃版本"""
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE prompt_versions SET is_active = 0")
    conn.execute("UPDATE prompt_versions SET is_active = 1 WHERE id = ?", (version_id,))
    row = conn.execute("SELECT version_name, system_prompt FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    conn.commit()
    conn.close()
    if row:
        return {"ok": True, "version_name": row[0], "system_prompt": row[1]}
    return {"ok": False, "error": "版本不存在"}


def get_version_prompt(version_id: int, db_path: str) -> dict | None:
    """获取版本的 Prompt 内容"""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT id, version_name, description, system_prompt, created_at, is_active FROM prompt_versions WHERE id = ?",
        (version_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "description": row[2],
            "system_prompt": row[3], "created_at": row[4], "is_active": bool(row[5])}


def get_version_results(version_id: int, db_path: str, limit: int = 5) -> dict:
    """获取版本的最新跑分结果"""
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT version_name FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "版本不存在"}
    version_name = row[0]
    results = conn.execute("""
        SELECT id, test_id, test_category, query, scores, weighted_score, duration, passed, evaluated_at
        FROM version_test_results
        WHERE version_name = ? ORDER BY evaluated_at DESC LIMIT ?
    """, (version_name, limit)).fetchall()
    conn.close()
    return {
        "version_name": version_name,
        "results": [
            {"id": r[0], "test_id": r[1], "category": r[2], "query": r[3][:60] if r[3] else "",
             "scores": r[4], "weighted_score": r[5], "duration": r[6],
             "passed": bool(r[7]), "evaluated_at": r[8]}
            for r in results
        ]
    }


def ab_test_versions(version_a_id: int, version_b_id: int, db_path: str) -> dict:
    """A/B 测试：对比两个版本的跑分结果"""
    a_data = get_version_results(version_a_id, db_path, 999)
    b_data = get_version_results(version_b_id, db_path, 999)
    return {
        "version_a": a_data,
        "version_b": b_data,
        "comparison": {
            "a_avg_weighted": _avg_weighted(a_data.get("results", [])),
            "b_avg_weighted": _avg_weighted(b_data.get("results", [])),
        }
    }


def _avg_weighted(results: list) -> float:
    scores = [r.get("weighted_score", 0) or 0 for r in results]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


def regression_check(version_id: int, db_path: str, threshold: float = 0.1) -> dict:
    """退化检测：对比最近两次跑分，各维度下降超阈值标记退化"""
    import json
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT version_name FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "版本不存在"}
    vname = row[0]
    runs = conn.execute("""
        SELECT test_id, scores, evaluated_at FROM version_test_results
        WHERE version_name = ? ORDER BY evaluated_at DESC
    """, (vname,)).fetchall()
    conn.close()

    from collections import defaultdict
    by_test = defaultdict(list)
    for r in runs:
        by_test[r[0]].append((r[1], r[2]))

    regressions = []
    for test_id, entries in by_test.items():
        if len(entries) < 2:
            continue
        try:
            scores_new = json.loads(entries[0][0]) if isinstance(entries[0][0], str) else entries[0][0]
            scores_old = json.loads(entries[1][0]) if isinstance(entries[1][0], str) else entries[1][0]
        except (json.JSONDecodeError, TypeError):
            continue
        for dim in scores_new:
            old_val = scores_old.get(dim, 0)
            new_val = scores_new.get(dim, 0)
            if old_val > 0 and (old_val - new_val) / old_val > threshold:
                regressions.append({
                    "test_id": test_id, "dimension": dim,
                    "old_score": old_val, "new_score": new_val,
                    "drop_pct": round((old_val - new_val) / old_val * 100, 1)
                })
    return {
        "version_name": vname,
        "regression_count": len(regressions),
        "regressions": regressions,
    }
