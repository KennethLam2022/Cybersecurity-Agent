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


def _db(db_path: str):
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def init_versions_db(db_path: str):
    """初始化版本管理数据库"""
    conn = _db(db_path)
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
def list_versions(db_path: str) -> list:
    """列出所有版本（含 ID）"""
    conn = _db(db_path)
    rows = conn.execute(
        "SELECT id, version_name, description, created_at, is_active FROM prompt_versions ORDER BY id DESC"
    ).fetchall()
    conn.close()
    return [{"id": r[0], "name": r[1], "description": r[2], "created_at": r[3], "is_active": bool(r[4])} for r in rows]
def switch_version(version_name: str, db_path: str) -> dict:
    """切换激活版本"""
    conn = _db(db_path)
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
def get_active_version_name(db_path: str) -> str | None:
    """获取当前活跃版本的名称"""
    conn = _db(db_path)
    row = conn.execute(
        "SELECT version_name FROM prompt_versions WHERE is_active = 1 LIMIT 1").fetchone()
    conn.close()
    return row[0] if row else None
def get_version_prompt(version_id: int, db_path: str) -> dict | None:
    """获取版本的 Prompt 内容"""
    conn = _db(db_path)
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
    conn = _db(db_path)
    row = conn.execute("SELECT version_name FROM prompt_versions WHERE id = ?",
                       (version_id,)).fetchone()
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
