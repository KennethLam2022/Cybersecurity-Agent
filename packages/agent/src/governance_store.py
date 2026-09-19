"""Tenant-scoped governance storage for SecureNexus.

Centralizes production secrets, MCP tool policies, skill installation review,
tenant-scoped evaluation metadata, saved searches, operations todos,
notification policies, workflow template sharing, custom roles, cost routing
decisions, and industry security profiles.

All credential values are encrypted at rest and never returned in plain text
to admin list APIs. Caller-level permission checks remain the responsibility
of route handlers; this store enforces tenant scope and audit trails.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

try:
    from cryptography.fernet import Fernet
    _CRYPTO_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    _CRYPTO_AVAILABLE = False

from llm_config_manager import get_fernet

logger = logging.getLogger(__name__)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _hash_value(value: str) -> str:
    return hashlib.sha256((value or "").encode("utf-8")).hexdigest()[:24]


def mask_secret(value: str, keep: int = 4) -> str:
    """Return a masked credential suitable for logs and list views."""
    if not value:
        return ""
    text = str(value)
    if len(text) <= keep + 2:
        return "*" * len(text)
    return text[:keep] + "*" * 6 + text[-2:]


def _encrypt(value: str) -> str:
    fernet = get_fernet() if _CRYPTO_AVAILABLE else None
    if fernet is None:
        raise RuntimeError("加密组件未初始化，无法安全保存凭证")
    return fernet.encrypt(str(value).encode("utf-8")).decode("ascii")


def _decrypt(value: str) -> str:
    fernet = get_fernet() if _CRYPTO_AVAILABLE else None
    if fernet is None:
        raise RuntimeError("加密组件未初始化，无法读取凭证")
    return fernet.decrypt(value.encode("ascii")).decode("utf-8")


def _project_root() -> Path:
    p = Path(__file__).resolve()
    for _ in range(10):
        if (p / "packages").is_dir() and (p / "RAG_DATA").is_dir():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent.parent.parent


_DEFAULT_DB_PATH = str(_project_root() / "agent_data" / "governance.db")


class GovernanceStore:
    """SQLite-backed governance store with tenant-scoped query helpers."""

    def __init__(self, db_path: Optional[str] = None):
        self._db_path = db_path or _DEFAULT_DB_PATH
        Path(self._db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @staticmethod
    def _validate_active_tenant_users(conn, tenant_id: str, user_ids):
        """Reject assignments and notification targets outside the organization."""
        normalized = {str(user_id or "").strip() for user_id in (user_ids or []) if str(user_id or "").strip()}
        if not normalized:
            return
        placeholders = ",".join("?" for _ in normalized)
        rows = conn.execute(
            f"SELECT user_id FROM organization_memberships WHERE tenant_id=? AND status='active' AND user_id IN ({placeholders})",
            [tenant_id, *sorted(normalized)],
        ).fetchall()
        valid = {str(row[0]) for row in rows}
        if normalized - valid:
            raise ValueError("指定的用户不存在、已停用或不属于当前工作区")

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
            CREATE TABLE IF NOT EXISTS p8_secret_refs (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                description TEXT DEFAULT '',
                encrypted_value TEXT NOT NULL,
                value_hash TEXT NOT NULL,
                current_version INTEGER NOT NULL DEFAULT 1,
                status TEXT NOT NULL DEFAULT 'active',
                expires_at TEXT,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                revoked_at TEXT,
                rotated_from TEXT,
                UNIQUE(tenant_id, name, category)
            );
            CREATE TABLE IF NOT EXISTS p8_secret_versions (
                id TEXT PRIMARY KEY,
                secret_id TEXT NOT NULL,
                version INTEGER NOT NULL,
                encrypted_value TEXT NOT NULL,
                value_hash TEXT NOT NULL,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                reason TEXT DEFAULT '',
                revoked_at TEXT,
                UNIQUE(secret_id, version)
            );
            CREATE TABLE IF NOT EXISTS p8_secret_access_audit (
                id TEXT PRIMARY KEY,
                secret_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT DEFAULT '',
                allowed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS p8_mcp_servers (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                auth_type TEXT DEFAULT 'none',
                secret_ref_id TEXT,
                status TEXT NOT NULL DEFAULT 'registered',
                health TEXT DEFAULT 'unknown',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, name)
            );
            CREATE TABLE IF NOT EXISTS p8_mcp_tools (
                id TEXT PRIMARY KEY,
                server_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                description TEXT DEFAULT '',
                input_schema TEXT DEFAULT '{}',
                high_risk INTEGER NOT NULL DEFAULT 0,
                UNIQUE(server_id, tool_name)
            );
            CREATE TABLE IF NOT EXISTS p8_mcp_tool_policies (
                id TEXT PRIMARY KEY,
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                allowed_roles TEXT NOT NULL DEFAULT '[]',
                allowed_agents TEXT NOT NULL DEFAULT '[]',
                param_allowlist TEXT NOT NULL DEFAULT '[]',
                param_denylist TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 0,
                updated_by TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(server_id, tool_name)
            );
            CREATE TABLE IF NOT EXISTS p8_mcp_call_audit (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                agent_id TEXT NOT NULL,
                server_id TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                param_summary TEXT DEFAULT '{}',
                result_status TEXT NOT NULL,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS p8_gov_audit (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                actor_user_id TEXT NOT NULL,
                actor_role TEXT NOT NULL,
                action TEXT NOT NULL,
                resource_type TEXT NOT NULL,
                resource_id TEXT DEFAULT '',
                detail TEXT DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS saved_searches (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                owner_user_id TEXT NOT NULL,
                name TEXT NOT NULL,
                query_text TEXT NOT NULL,
                resource_types TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, owner_user_id, name)
            );
            CREATE TABLE IF NOT EXISTS search_synonyms (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                term TEXT NOT NULL,
                alternatives TEXT NOT NULL DEFAULT '[]',
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, term)
            );
            CREATE TABLE IF NOT EXISTS search_ranking_configs (
                tenant_id TEXT PRIMARY KEY,
                weights_json TEXT NOT NULL DEFAULT '{}',
                updated_by TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS operations_tasks (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_id TEXT NOT NULL DEFAULT '',
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                priority TEXT NOT NULL DEFAULT 'medium',
                assignee_user_id TEXT NOT NULL DEFAULT '',
                collaborator_user_ids TEXT NOT NULL DEFAULT '[]',
                department TEXT NOT NULL DEFAULT '',
                due_at TEXT,
                sla_hours INTEGER,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_operations_tasks_scope ON operations_tasks(tenant_id, status, priority, due_at);
            CREATE TABLE IF NOT EXISTS operations_task_comments (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                author_user_id TEXT NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_operations_task_comments_scope ON operations_task_comments(task_id, tenant_id, created_at);
            CREATE TABLE IF NOT EXISTS operations_task_attachments (
                id TEXT PRIMARY KEY,
                task_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                resource_type TEXT NOT NULL DEFAULT 'reference',
                resource_id TEXT NOT NULL DEFAULT '',
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_operations_task_attachments_scope ON operations_task_attachments(task_id, tenant_id, created_at);
            CREATE TABLE IF NOT EXISTS notification_policies (
                id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                name TEXT NOT NULL,
                event_types TEXT NOT NULL DEFAULT '[]',
                channels TEXT NOT NULL DEFAULT '["in_app"]',
                recipient_user_ids TEXT NOT NULL DEFAULT '[]',
                min_severity TEXT NOT NULL DEFAULT 'info',
                quiet_start_hour INTEGER,
                quiet_end_hour INTEGER,
                cooldown_minutes INTEGER NOT NULL DEFAULT 30,
                enabled INTEGER NOT NULL DEFAULT 1,
                created_by TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(tenant_id, name)
            );
            CREATE TABLE IF NOT EXISTS industry_profile_grants (
                tenant_id TEXT NOT NULL,
                profile TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0,
                profile_version TEXT NOT NULL DEFAULT '',
                changed_by TEXT NOT NULL DEFAULT '',
                change_note TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (tenant_id, profile)
            );
            CREATE INDEX IF NOT EXISTS idx_industry_profile_grants_scope
                ON industry_profile_grants(tenant_id, enabled, updated_at DESC);
            CREATE TABLE IF NOT EXISTS notification_policy_deliveries (
                id TEXT PRIMARY KEY,
                policy_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                channel TEXT NOT NULL,
                decision TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                notification_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_notification_policy_deliveries_scope ON notification_policy_deliveries(tenant_id, policy_id, event_type, created_at DESC);
            """)

    def _write_audit(self, conn, tenant_id, actor_user_id, actor_role, action,
                     resource_type, resource_id="", detail=None):
        conn.execute(
            "INSERT INTO p8_gov_audit (id, tenant_id, actor_user_id, actor_role, action, resource_type, resource_id, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_new_id("aud"), tenant_id, actor_user_id, actor_role, action,
             resource_type, resource_id, json.dumps(detail or {}, ensure_ascii=False), _utc_now()),
        )

    def list_audit(self, tenant_id, resource_type=None, limit=100):
        sql = "SELECT * FROM p8_gov_audit WHERE tenant_id = ?"
        params = [tenant_id]
        if resource_type:
            sql += " AND resource_type = ?"
            params.append(resource_type)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
            return [dict(row) for row in rows]

    def save_search(self, tenant_id: str, owner_user_id: str, name: str, query_text: str,
                    resource_types: list[str] | None = None) -> dict:
        name = str(name or "").strip()[:120]
        query_text = str(query_text or "").strip()[:120]
        if not name or not query_text:
            raise ValueError("保存搜索需要名称和查询条件")
        now = _utc_now()
        item_id = _new_id("search")
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO saved_searches (id, tenant_id, owner_user_id, name, query_text, resource_types, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, owner_user_id, name) DO UPDATE SET query_text=excluded.query_text, resource_types=excluded.resource_types, updated_at=excluded.updated_at",
                (item_id, tenant_id, owner_user_id, name, query_text,
                 json.dumps(resource_types or [], ensure_ascii=False), now, now),
            )
            row = conn.execute("SELECT * FROM saved_searches WHERE tenant_id=? AND owner_user_id=? AND name=?", (tenant_id, owner_user_id, name)).fetchone()
            self._write_audit(conn, tenant_id, owner_user_id, "admin", "search.save", "saved_search", row["id"], {"name": name})
            return self._saved_search_dict(row)

    @staticmethod
    def _saved_search_dict(row):
        item = dict(row)
        item["resource_types"] = json.loads(item.pop("resource_types") or "[]")
        return item

    def list_saved_searches(self, tenant_id: str, owner_user_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM saved_searches WHERE tenant_id=? AND owner_user_id=? ORDER BY updated_at DESC", (tenant_id, owner_user_id)).fetchall()
            return [self._saved_search_dict(row) for row in rows]

    def delete_saved_search(self, tenant_id: str, owner_user_id: str, search_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM saved_searches WHERE id=? AND tenant_id=? AND owner_user_id=?", (search_id, tenant_id, owner_user_id))
            if cur.rowcount:
                self._write_audit(conn, tenant_id, owner_user_id, "admin", "search.delete", "saved_search", search_id)
            return bool(cur.rowcount)

    def upsert_search_synonym(self, tenant_id: str, term: str, alternatives: list[str], actor_user_id: str) -> dict:
        term = str(term or "").strip()[:80]
        values = sorted({str(item).strip()[:80] for item in alternatives if str(item).strip() and str(item).strip() != term})
        if not term or not values:
            raise ValueError("同义词需要原词和至少一个替代表达")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("INSERT INTO search_synonyms (id, tenant_id, term, alternatives, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, term) DO UPDATE SET alternatives=excluded.alternatives, enabled=1, updated_at=excluded.updated_at",
                         (_new_id("syn"), tenant_id, term, json.dumps(values, ensure_ascii=False), actor_user_id, now, now))
            row = conn.execute("SELECT * FROM search_synonyms WHERE tenant_id=? AND term=?", (tenant_id, term)).fetchone()
            self._write_audit(conn, tenant_id, actor_user_id, "admin", "search.synonym.upsert", "search_synonym", row["id"], {"term": term})
            return {**dict(row), "alternatives": json.loads(row["alternatives"])}

    def expand_search_terms(self, tenant_id: str, query_text: str) -> list[str]:
        query = str(query_text or "").strip()[:120]
        terms = [query]
        with self._connect() as conn:
            rows = conn.execute("SELECT term, alternatives FROM search_synonyms WHERE tenant_id=? AND enabled=1", (tenant_id,)).fetchall()
        for row in rows:
            if row["term"] in query:
                terms.extend(query.replace(row["term"], alt) for alt in json.loads(row["alternatives"] or "[]"))
        return list(dict.fromkeys(term for term in terms if term))[:8]

    def record_search_execution(self, tenant_id: str, actor_user_id: str, query_text: str,
                                expanded_count: int, result_count: int) -> None:
        with self._connect() as conn:
            self._write_audit(conn, tenant_id, actor_user_id, "admin", "search.execute",
                              "search", "", {"query": str(query_text or "")[:120],
                                                 "expanded_count": max(1, int(expanded_count)),
                                                 "result_count": max(0, int(result_count))})

    @staticmethod
    def _default_search_weights() -> dict[str, int]:
        return {
            "document": 100, "knowledge_base": 95, "knowledge_gap": 90,
            "agent_eval": 85, "monitoring_report": 80, "workflow": 75,
            "conversation": 70, "trace": 65, "audit": 60, "user": 55,
            "agent": 55, "prompt_asset": 50,
        }

    def get_search_ranking(self, tenant_id: str) -> dict[str, int]:
        with self._connect() as conn:
            row = conn.execute("SELECT weights_json FROM search_ranking_configs WHERE tenant_id=?", (tenant_id,)).fetchone()
        configured = json.loads(row["weights_json"] or "{}") if row else {}
        defaults = self._default_search_weights()
        return {key: int(configured.get(key, value)) for key, value in defaults.items()}

    def save_search_ranking(self, tenant_id: str, weights: dict, actor_user_id: str) -> dict[str, int]:
        if not isinstance(weights, dict):
            raise ValueError("排序权重必须是对象")
        defaults = self._default_search_weights()
        merged = dict(defaults)
        for key, value in weights.items():
            if key not in defaults:
                continue
            try:
                numeric = int(value)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 的权重必须是整数") from exc
            if numeric < 0 or numeric > 1000:
                raise ValueError("排序权重必须在 0-1000 之间")
            merged[key] = numeric
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("INSERT INTO search_ranking_configs (tenant_id, weights_json, updated_by, updated_at) VALUES (?, ?, ?, ?) ON CONFLICT(tenant_id) DO UPDATE SET weights_json=excluded.weights_json, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                         (tenant_id, json.dumps(merged, ensure_ascii=False), actor_user_id, now))
            self._write_audit(conn, tenant_id, actor_user_id, "admin", "search.ranking.update", "search_ranking", tenant_id, {"weights": merged})
        return merged

    def rank_search_results(self, tenant_id: str, items: list[dict]) -> list[dict]:
        weights = self.get_search_ranking(tenant_id)
        ranked = []
        for index, item in enumerate(items):
            scored = dict(item)
            scored["ranking_score"] = weights.get(str(item.get("type") or ""), 0)
            scored["ranking_policy"] = "tenant-configured"
            ranked.append((scored, index))
        ranked.sort(key=lambda pair: (-pair[0]["ranking_score"], pair[1]))
        return [item for item, _ in ranked]

    def create_operations_task(self, tenant_id: str, created_by: str, *, source_type: str,
                               title: str, description: str = "", source_id: str = "",
                               priority: str = "medium", assignee_user_id: str = "",
                               collaborator_user_ids: list[str] | None = None, department: str = "",
                               due_at: str | None = None, sla_hours: int | None = None) -> dict:
        if priority not in {"low", "medium", "high", "critical"}:
            raise ValueError("优先级不合法")
        if not str(source_type).strip() or not str(title).strip():
            raise ValueError("待办来源和标题不能为空")
        if sla_hours is not None and (int(sla_hours) < 1 or int(sla_hours) > 8760):
            raise ValueError("SLA 时长必须在 1-8760 小时之间")
        task_id, now = _new_id("task"), _utc_now()
        with self._connect() as conn:
            self._validate_active_tenant_users(
                conn, tenant_id, [assignee_user_id, *(collaborator_user_ids or [])],
            )
            conn.execute("INSERT INTO operations_tasks (id, tenant_id, source_type, source_id, title, description, priority, assignee_user_id, collaborator_user_ids, department, due_at, sla_hours, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (task_id, tenant_id, str(source_type)[:80], str(source_id)[:160], str(title)[:200], str(description)[:2000], priority, str(assignee_user_id)[:120], json.dumps(collaborator_user_ids or []), str(department)[:120], due_at, sla_hours, created_by, now, now))
            self._write_audit(conn, tenant_id, created_by, "admin", "task.create", "operations_task", task_id, {"source_type": source_type, "priority": priority})
        return self.get_operations_task(tenant_id, task_id) or {}

    def ensure_operations_task(self, tenant_id: str, created_by: str, *, source_type: str,
                               source_id: str, title: str, description: str = "",
                               priority: str = "medium") -> tuple[dict, bool]:
        """Return an open task for an actionable source without duplicating it."""
        with self._connect() as conn:
            row = conn.execute("SELECT id FROM operations_tasks WHERE tenant_id=? AND source_type=? AND source_id=? AND status NOT IN ('done','cancelled') ORDER BY created_at DESC LIMIT 1", (tenant_id, source_type, source_id)).fetchone()
        if row:
            return self.get_operations_task(tenant_id, row["id"]) or {}, False
        return self.create_operations_task(tenant_id, created_by, source_type=source_type,
                                           source_id=source_id, title=title,
                                           description=description, priority=priority), True

    def get_operations_task(self, tenant_id: str, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM operations_tasks WHERE id=? AND tenant_id=?", (task_id, tenant_id)).fetchone()
        if not row:
            return None
        item = dict(row); item["collaborator_user_ids"] = json.loads(item["collaborator_user_ids"] or "[]")
        item.update(self._operations_task_timing(item))
        return item

    @staticmethod
    def _operations_task_timing(item: dict) -> dict:
        if item.get("status") in {"done", "cancelled"}:
            return {"sla_state": "closed", "is_overdue": False}
        deadline = item.get("due_at")
        if not deadline and item.get("sla_hours"):
            try:
                started = datetime.fromisoformat(str(item.get("created_at") or "").replace("Z", "+00:00"))
                deadline = (started + timedelta(hours=int(item["sla_hours"]))).isoformat(timespec="seconds")
            except (TypeError, ValueError):
                deadline = ""
        if not deadline:
            return {"sla_state": "unspecified", "is_overdue": False}
        try:
            target = datetime.fromisoformat(str(deadline).replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            overdue = target.astimezone(timezone.utc) < now
            return {"sla_state": "overdue" if overdue else "on_track", "is_overdue": overdue,
                    "effective_due_at": target.isoformat(timespec="seconds")}
        except (TypeError, ValueError):
            return {"sla_state": "invalid_due_at", "is_overdue": False}

    def list_operations_tasks(self, tenant_id: str, status: str = "", assignee_user_id: str = "",
                              sla_state: str = "") -> list[dict]:
        sql, params = "SELECT id FROM operations_tasks WHERE tenant_id=?", [tenant_id]
        if status: sql += " AND status=?"; params.append(status)
        if assignee_user_id: sql += " AND assignee_user_id=?"; params.append(assignee_user_id)
        sql += " ORDER BY CASE priority WHEN 'critical' THEN 4 WHEN 'high' THEN 3 WHEN 'medium' THEN 2 ELSE 1 END DESC, due_at ASC, updated_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        items = [item for row in rows if (item := self.get_operations_task(tenant_id, row["id"]))]
        return [item for item in items if not sla_state or item.get("sla_state") == sla_state]

    def update_operations_task(self, tenant_id: str, task_id: str, actor_user_id: str, changes: dict) -> dict | None:
        allowed = {"status", "priority", "assignee_user_id", "collaborator_user_ids", "department", "due_at", "sla_hours", "title", "description"}
        updates = {key: value for key, value in (changes or {}).items() if key in allowed}
        if not updates: raise ValueError("没有可更新的字段")
        if "status" in updates and updates["status"] not in {"open", "in_progress", "blocked", "done", "cancelled"}: raise ValueError("待办状态不合法")
        if "priority" in updates and updates["priority"] not in {"low", "medium", "high", "critical"}: raise ValueError("优先级不合法")
        if "collaborator_user_ids" in updates: updates["collaborator_user_ids"] = json.dumps(updates["collaborator_user_ids"] or [])
        fields, params = [], []
        for key, value in updates.items(): fields.append(f"{key}=?"); params.append(value)
        if updates.get("status") in {"done", "cancelled"}: fields.append("closed_at=?"); params.append(_utc_now())
        fields.append("updated_at=?"); params.append(_utc_now()); params.extend([task_id, tenant_id])
        with self._connect() as conn:
            self._validate_active_tenant_users(
                conn, tenant_id,
                [updates.get("assignee_user_id", ""), *(changes.get("collaborator_user_ids") or [])],
            )
            cur = conn.execute(f"UPDATE operations_tasks SET {', '.join(fields)} WHERE id=? AND tenant_id=?", params)
            if not cur.rowcount: return None
            self._write_audit(conn, tenant_id, actor_user_id, "admin", "task.update", "operations_task", task_id, {"fields": sorted(updates)})
        return self.get_operations_task(tenant_id, task_id)

    def add_operations_task_comment(self, tenant_id: str, task_id: str, author_user_id: str, content: str) -> dict:
        if not self.get_operations_task(tenant_id, task_id): raise ValueError("待办不存在")
        text = str(content or "").strip()
        if not text: raise ValueError("评论不能为空")
        item = {"id": _new_id("comment"), "task_id": task_id, "tenant_id": tenant_id, "author_user_id": author_user_id, "content": text[:2000], "created_at": _utc_now()}
        with self._connect() as conn:
            conn.execute("INSERT INTO operations_task_comments (id, task_id, tenant_id, author_user_id, content, created_at) VALUES (?, ?, ?, ?, ?, ?)", tuple(item.values()))
            self._write_audit(conn, tenant_id, author_user_id, "admin", "task.comment", "operations_task", task_id)
        return item

    def list_operations_task_comments(self, tenant_id: str, task_id: str) -> list[dict]:
        if not self.get_operations_task(tenant_id, task_id): return []
        with self._connect() as conn:
            rows = conn.execute("SELECT id, task_id, tenant_id, author_user_id, content, created_at FROM operations_task_comments WHERE task_id=? AND tenant_id=? ORDER BY created_at ASC", (task_id, tenant_id)).fetchall()
        return [dict(row) for row in rows]

    def add_operations_task_attachment(self, tenant_id: str, task_id: str, author_user_id: str,
                                       name: str, resource_type: str = "reference", resource_id: str = "") -> dict:
        if not self.get_operations_task(tenant_id, task_id): raise ValueError("待办不存在")
        name = str(name or "").strip()[:200]
        if not name: raise ValueError("附件名称不能为空")
        item = {"id": _new_id("attachment"), "task_id": task_id, "tenant_id": tenant_id,
                "name": name, "resource_type": str(resource_type or "reference")[:80],
                "resource_id": str(resource_id or "")[:200], "created_by": author_user_id,
                "created_at": _utc_now()}
        with self._connect() as conn:
            conn.execute("INSERT INTO operations_task_attachments (id, task_id, tenant_id, name, resource_type, resource_id, created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", tuple(item.values()))
            self._write_audit(conn, tenant_id, author_user_id, "admin", "task.attachment", "operations_task", task_id, {"resource_type": item["resource_type"]})
        return item

    def list_operations_task_attachments(self, tenant_id: str, task_id: str) -> list[dict]:
        if not self.get_operations_task(tenant_id, task_id): return []
        with self._connect() as conn:
            rows = conn.execute("SELECT id, task_id, tenant_id, name, resource_type, resource_id, created_by, created_at FROM operations_task_attachments WHERE task_id=? AND tenant_id=? ORDER BY created_at ASC", (task_id, tenant_id)).fetchall()
        return [dict(row) for row in rows]

    def upsert_notification_policy(self, tenant_id: str, actor_user_id: str, data: dict) -> dict:
        name = str(data.get("name") or "").strip()[:120]
        events = [str(item)[:120] for item in data.get("event_types", []) if str(item).strip()]
        channels = [str(item) for item in data.get("channels", ["in_app"]) if str(item) in {"in_app", "email", "webhook"}]
        if not name or not events or not channels: raise ValueError("策略需要名称、事件类型和至少一个通道")
        quiet_start, quiet_end = data.get("quiet_start_hour"), data.get("quiet_end_hour")
        for value in (quiet_start, quiet_end):
            if value is not None and not 0 <= int(value) <= 23: raise ValueError("静默时间必须是 0-23")
        cooldown = max(0, min(int(data.get("cooldown_minutes", 30)), 10080))
        now = _utc_now()
        with self._connect() as conn:
            self._validate_active_tenant_users(conn, tenant_id, data.get("recipient_user_ids") or [])
            conn.execute("INSERT INTO notification_policies (id, tenant_id, name, event_types, channels, recipient_user_ids, min_severity, quiet_start_hour, quiet_end_hour, cooldown_minutes, enabled, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(tenant_id, name) DO UPDATE SET event_types=excluded.event_types, channels=excluded.channels, recipient_user_ids=excluded.recipient_user_ids, min_severity=excluded.min_severity, quiet_start_hour=excluded.quiet_start_hour, quiet_end_hour=excluded.quiet_end_hour, cooldown_minutes=excluded.cooldown_minutes, enabled=excluded.enabled, updated_at=excluded.updated_at",
                         (_new_id("npol"), tenant_id, name, json.dumps(events), json.dumps(channels), json.dumps(data.get("recipient_user_ids") or []), str(data.get("min_severity") or "info"), quiet_start, quiet_end, cooldown, int(bool(data.get("enabled", True))), actor_user_id, now, now))
            row = conn.execute("SELECT * FROM notification_policies WHERE tenant_id=? AND name=?", (tenant_id, name)).fetchone()
            self._write_audit(conn, tenant_id, actor_user_id, "admin", "notification.policy.upsert", "notification_policy", row["id"], {"name": name})
            return self._notification_policy_dict(row)

    @staticmethod
    def _notification_policy_dict(row):
        item = dict(row)
        for key in ("event_types", "channels", "recipient_user_ids"): item[key] = json.loads(item[key] or "[]")
        item["enabled"] = bool(item["enabled"])
        return item

    def list_notification_policies(self, tenant_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM notification_policies WHERE tenant_id=? ORDER BY updated_at DESC", (tenant_id,)).fetchall()
        return [self._notification_policy_dict(row) for row in rows]

    def notification_decision(self, tenant_id: str, event_type: str, severity: str) -> dict:
        policies = [item for item in self.list_notification_policies(tenant_id) if item["enabled"] and event_type in item["event_types"]]
        if not policies: return {"channels": ["in_app", "email", "webhook"], "policy_ids": [], "recipient_user_ids": [], "suppressed": False}
        levels = {"info": 1, "success": 1, "warning": 2, "error": 3, "critical": 4}
        hour = datetime.now().hour; channels, policy_ids, recipients, decisions = set(), [], set(), []
        for policy in policies:
            if levels.get(severity, 1) < levels.get(policy.get("min_severity"), 1):
                decisions.append((policy, "suppressed", "below_min_severity")); continue
            start, end = policy.get("quiet_start_hour"), policy.get("quiet_end_hour")
            quiet = start is not None and end is not None and ((start <= hour < end) if start < end else (hour >= start or hour < end))
            if quiet:
                decisions.append((policy, "suppressed", "quiet_hours")); continue
            with self._connect() as conn:
                last = conn.execute("SELECT created_at FROM notification_policy_deliveries WHERE policy_id=? AND event_type=? AND decision='sent' ORDER BY created_at DESC LIMIT 1", (policy["id"], event_type)).fetchone()
            cooldown = int(policy.get("cooldown_minutes") or 0)
            if last and cooldown:
                try:
                    elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last["created_at"].replace("Z", "+00:00"))).total_seconds()
                    if elapsed < cooldown * 60:
                        decisions.append((policy, "suppressed", "cooldown")); continue
                except (TypeError, ValueError):
                    pass
            channels.update(policy["channels"]); recipients.update(policy.get("recipient_user_ids") or []); decisions.append((policy, "sent", "matched"))
            policy_ids.append(policy["id"])
        with self._connect() as conn:
            for policy, decision, reason in decisions:
                conn.execute("INSERT INTO notification_policy_deliveries (id, policy_id, tenant_id, event_type, channel, decision, reason, created_at) VALUES (?, ?, ?, ?, '', ?, ?, ?)",
                             (_new_id("npd"), policy["id"], tenant_id, event_type, decision, reason, _utc_now()))
        return {"channels": sorted(channels), "policy_ids": policy_ids,
                "recipient_user_ids": sorted(recipients), "suppressed": not bool(channels)}

    def list_notification_policy_deliveries(self, tenant_id: str, policy_id: str = "", limit: int = 100) -> list[dict]:
        sql = "SELECT id, policy_id, tenant_id, event_type, channel, decision, reason, notification_id, created_at FROM notification_policy_deliveries WHERE tenant_id=?"
        params: list[object] = [tenant_id]
        if policy_id:
            sql += " AND policy_id=?"; params.append(policy_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"; params.append(max(1, min(int(limit), 500)))
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def list_industry_profile_grants(self, tenant_id: str) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""SELECT tenant_id, profile, enabled, profile_version, changed_by,
                change_note, created_at, updated_at FROM industry_profile_grants
                WHERE tenant_id=? ORDER BY profile""", (tenant_id,)).fetchall()
        return [{**dict(row), "enabled": bool(row["enabled"])} for row in rows]

    def enabled_profiles(self, tenant_id: str) -> set[str]:
        return {"general", *(item["profile"] for item in self.list_industry_profile_grants(tenant_id)
                              if item["enabled"])}

    def set_industry_profile_grant(self, tenant_id: str, profile: str, enabled: bool,
                                   profile_version: str, actor_user_id: str,
                                   change_note: str = "") -> dict:
        profile = str(profile or "").strip()
        if not profile.startswith("industry/"):
            raise ValueError("只能管理 industry/ 前缀的网络安全行业扩展包")
        now = _utc_now()
        with self._connect() as conn:
            conn.execute("""INSERT INTO industry_profile_grants
                (tenant_id, profile, enabled, profile_version, changed_by, change_note, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(tenant_id, profile) DO UPDATE SET enabled=excluded.enabled,
                profile_version=excluded.profile_version, changed_by=excluded.changed_by,
                change_note=excluded.change_note, updated_at=excluded.updated_at""",
                (tenant_id, profile, int(bool(enabled)), str(profile_version or "")[:120], actor_user_id,
                 str(change_note or "")[:1000], now, now))
            self._write_audit(conn, tenant_id, actor_user_id, "admin", "industry_profile.grant",
                              "industry_profile", profile,
                              {"enabled": bool(enabled), "profile_version": profile_version})
            row = conn.execute("SELECT tenant_id, profile, enabled, profile_version, changed_by, change_note, created_at, updated_at FROM industry_profile_grants WHERE tenant_id=? AND profile=?", (tenant_id, profile)).fetchone()
        return {**dict(row), "enabled": bool(row["enabled"])}

    def create_secret(self, tenant_id, name, category, value, created_by,
                      actor_role, description="", expires_at=None):
        if not name.strip() or not category.strip() or value is None:
            raise ValueError("密钥名称、类别和值不能为空")
        secret_id = _new_id("sec")
        encrypted = _encrypt(value)
        value_hash = _hash_value(value)
        now = _utc_now()
        with self._connect() as conn:
            try:
                conn.execute(
                    "INSERT INTO p8_secret_refs (id, tenant_id, name, category, description, encrypted_value, value_hash, "
                    "current_version, status, expires_at, created_by, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, 1, 'active', ?, ?, ?, ?)",
                    (secret_id, tenant_id, name.strip(), category.strip(), description.strip(),
                     encrypted, value_hash, expires_at, created_by, now, now),
                )
                conn.execute(
                    "INSERT INTO p8_secret_versions (id, secret_id, version, encrypted_value, value_hash, created_by, created_at, reason) "
                    "VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                    (_new_id("sev"), secret_id, encrypted, value_hash, created_by, now, "initial"),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("同名密钥已存在") from exc
            self._write_audit(conn, tenant_id, created_by, actor_role, "secret.create",
                              "secret", secret_id, {"category": category, "name": name})
        return self.get_secret(tenant_id, secret_id, created_by, actor_role,
                               reason="create return", include_value=False)

    def list_secrets(self, tenant_id, category=None):
        sql = ("SELECT id, tenant_id, name, category, description, current_version, status, "
               "expires_at, created_by, created_at, updated_at, revoked_at, rotated_from "
               "FROM p8_secret_refs WHERE tenant_id = ?")
        params = [tenant_id]
        if category:
            sql += " AND category = ?"
            params.append(category)
        sql += " ORDER BY created_at DESC"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()

            return [dict(row) for row in rows]

    def _secret_row(self, conn, tenant_id: str, secret_id: str):
        row = conn.execute(
            "SELECT * FROM p8_secret_refs WHERE id = ? AND tenant_id = ?",
            (secret_id, tenant_id),
        ).fetchone()
        if not row:
            raise ValueError("密钥不存在或不属于当前租户")
        return row

    def get_secret(self, tenant_id, secret_id, actor_user_id, actor_role,
                   reason="", include_value=False):
        """Return secret metadata, optionally resolving its value for server-side use.

        HTTP handlers must always call this with ``include_value=False``.  The
        value path exists only for trusted service adapters (MCP/SSO/model
        clients) and records an explicit read audit entry.
        """
        with self._connect() as conn:
            row = self._secret_row(conn, tenant_id, secret_id)
            if row["status"] != "active":
                raise ValueError("密钥已撤销，不能使用")
            result = dict(row)
            result.pop("encrypted_value", None)
            result.pop("value_hash", None)
            if include_value:
                result["value"] = _decrypt(row["encrypted_value"])
                conn.execute(
                    "INSERT INTO p8_secret_access_audit (id, secret_id, tenant_id, actor_user_id, actor_role, action, reason, allowed, created_at) VALUES (?, ?, ?, ?, ?, 'secret.resolve', ?, 1, ?)",
                    (_new_id("saa"), secret_id, tenant_id, actor_user_id, actor_role, reason[:500], _utc_now()),
                )
                self._write_audit(conn, tenant_id, actor_user_id, actor_role,
                                  "secret.resolve", "secret", secret_id,
                                  {"reason": reason[:500]})
            return result

    def rotate_secret(self, tenant_id, secret_id, value, actor_user_id,
                      actor_role, reason=""):
        if value is None or not str(value):
            raise ValueError("新密钥不能为空")
        encrypted = _encrypt(str(value))
        value_hash = _hash_value(str(value))
        now = _utc_now()
        with self._connect() as conn:
            row = self._secret_row(conn, tenant_id, secret_id)
            if row["status"] != "active":
                raise ValueError("已撤销的密钥不能轮换")
            next_version = int(row["current_version"]) + 1
            conn.execute(
                "UPDATE p8_secret_refs SET encrypted_value=?, value_hash=?, current_version=?, updated_at=? WHERE id=? AND tenant_id=?",
                (encrypted, value_hash, next_version, now, secret_id, tenant_id),
            )
            conn.execute(
                "INSERT INTO p8_secret_versions (id, secret_id, version, encrypted_value, value_hash, created_by, created_at, reason) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (_new_id("sev"), secret_id, next_version, encrypted, value_hash,
                 actor_user_id, now, str(reason)[:500]),
            )
            self._write_audit(conn, tenant_id, actor_user_id, actor_role,
                              "secret.rotate", "secret", secret_id,
                              {"version": next_version, "reason": str(reason)[:500]})
        return self.get_secret(tenant_id, secret_id, actor_user_id, actor_role,
                               reason="rotate return", include_value=False)

    def revoke_secret(self, tenant_id, secret_id, actor_user_id, actor_role,
                      reason=""):
        now = _utc_now()
        with self._connect() as conn:
            self._secret_row(conn, tenant_id, secret_id)
            conn.execute(
                "UPDATE p8_secret_refs SET status='revoked', revoked_at=?, updated_at=? WHERE id=? AND tenant_id=?",
                (now, now, secret_id, tenant_id),
            )
            self._write_audit(conn, tenant_id, actor_user_id, actor_role,
                              "secret.revoke", "secret", secret_id,
                              {"reason": str(reason)[:500]})
        return {"id": secret_id, "status": "revoked", "revoked_at": now}

    def secret_versions(self, tenant_id, secret_id):
        with self._connect() as conn:
            self._secret_row(conn, tenant_id, secret_id)
            rows = conn.execute(
                "SELECT v.id, v.secret_id, v.version, v.value_hash, v.created_by, v.created_at, v.reason, v.revoked_at "
                "FROM p8_secret_versions v "
                "JOIN p8_secret_refs s ON s.id = v.secret_id "
                "WHERE v.secret_id=? AND s.tenant_id=? ORDER BY v.version DESC",
                (secret_id, tenant_id),
            ).fetchall()
            return [dict(row) for row in rows]

    def register_mcp_server(self, tenant_id, name, endpoint, created_by, *,
                            secret_ref_id="", auth_type="none"):
        from urllib.parse import urlparse
        parsed = urlparse(str(endpoint))
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("MCP Server 必须使用 HTTPS 地址")
        if auth_type not in {"none", "bearer", "api_key", "oauth2"}:
            raise ValueError("不支持的 MCP 认证类型")
        if auth_type != "none" and not secret_ref_id:
            raise ValueError("需要认证的 MCP Server 必须引用受控密钥")
        now = _utc_now()
        server_id = _new_id("mcp")
        with self._connect() as conn:
            if secret_ref_id:
                secret = self._secret_row(conn, tenant_id, secret_ref_id)
                if secret["status"] != "active":
                    raise ValueError("引用的密钥不可用")
            try:
                conn.execute(
                    "INSERT INTO p8_mcp_servers (id, tenant_id, name, endpoint, auth_type, secret_ref_id, status, created_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, 'registered', ?, ?, ?)",
                    (server_id, tenant_id, name.strip(), endpoint.strip(), auth_type,
                     secret_ref_id or None, created_by, now, now),
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError("同名 MCP Server 已存在") from exc
            self._write_audit(conn, tenant_id, created_by, "system", "mcp.register",
                              "mcp_server", server_id, {"name": name, "endpoint": endpoint})
        return self.get_mcp_server(tenant_id, server_id)

    def get_mcp_server(self, tenant_id, server_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM p8_mcp_servers WHERE id=? AND tenant_id=?", (server_id, tenant_id)).fetchone()
            if not row:
                raise ValueError("MCP Server 不存在或不属于当前租户")
            return dict(row)

    def list_mcp_servers(self, tenant_id):
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM p8_mcp_servers WHERE tenant_id=? ORDER BY created_at DESC", (tenant_id,)).fetchall()
            return [dict(row) for row in rows]

    def set_mcp_status(self, tenant_id, server_id, status, actor_user_id, actor_role):
        if status not in {"registered", "enabled", "disabled"}:
            raise ValueError("MCP 状态不合法")
        with self._connect() as conn:
            self.get_mcp_server(tenant_id, server_id)
            conn.execute("UPDATE p8_mcp_servers SET status=?, updated_at=? WHERE id=? AND tenant_id=?",
                         (status, _utc_now(), server_id, tenant_id))
            self._write_audit(conn, tenant_id, actor_user_id, actor_role, "mcp.status.update",
                              "mcp_server", server_id, {"status": status})
        return self.get_mcp_server(tenant_id, server_id)

    def upsert_mcp_tool(self, tenant_id, server_id, tool_name, description,
                        input_schema, high_risk, actor_user_id, actor_role):
        if not tool_name.strip():
            raise ValueError("工具名称不能为空")
        try:
            schema_json = json.dumps(input_schema or {}, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            raise ValueError("工具参数契约必须是 JSON 对象") from exc
        with self._connect() as conn:
            self.get_mcp_server(tenant_id, server_id)
            tool_id = _new_id("mto")
            conn.execute(
                "INSERT INTO p8_mcp_tools (id, server_id, tenant_id, tool_name, description, input_schema, high_risk) VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(server_id, tool_name) DO UPDATE SET description=excluded.description, input_schema=excluded.input_schema, high_risk=excluded.high_risk",
                (tool_id, server_id, tenant_id, tool_name.strip(), str(description or ""), schema_json, int(bool(high_risk))),
            )
            self._write_audit(conn, tenant_id, actor_user_id, actor_role, "mcp.tool.upsert",
                              "mcp_tool", f"{server_id}:{tool_name}", {"high_risk": bool(high_risk)})
        return self.list_mcp_tools(tenant_id, server_id)

    def list_mcp_tools(self, tenant_id, server_id):
        with self._connect() as conn:
            self.get_mcp_server(tenant_id, server_id)
            rows = conn.execute("SELECT * FROM p8_mcp_tools WHERE tenant_id=? AND server_id=? ORDER BY tool_name", (tenant_id, server_id)).fetchall()
            result = []
            for row in rows:
                item = dict(row)
                item["input_schema"] = json.loads(item["input_schema"] or "{}")
                result.append(item)
            return result

    def set_mcp_tool_policy(self, tenant_id, server_id, tool_name, *, allowed_roles,
                            allowed_agents, param_allowlist, param_denylist, enabled,
                            actor_user_id, actor_role):
        with self._connect() as conn:
            tool = conn.execute("SELECT 1 FROM p8_mcp_tools WHERE tenant_id=? AND server_id=? AND tool_name=?", (tenant_id, server_id, tool_name)).fetchone()
            if not tool:
                raise ValueError("MCP 工具不存在")
            if bool(enabled) and not allowed_roles:
                raise ValueError("启用工具前至少需要一个授权角色")
            conn.execute(
                "INSERT INTO p8_mcp_tool_policies (id, server_id, tool_name, tenant_id, allowed_roles, allowed_agents, param_allowlist, param_denylist, enabled, updated_by, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(server_id, tool_name) DO UPDATE SET allowed_roles=excluded.allowed_roles, allowed_agents=excluded.allowed_agents, param_allowlist=excluded.param_allowlist, param_denylist=excluded.param_denylist, enabled=excluded.enabled, updated_by=excluded.updated_by, updated_at=excluded.updated_at",
                (_new_id("mtp"), server_id, tool_name, tenant_id,
                 json.dumps(allowed_roles or []), json.dumps(allowed_agents or []),
                 json.dumps(param_allowlist or []), json.dumps(param_denylist or []),
                 int(bool(enabled)), actor_user_id, _utc_now()),
            )
            self._write_audit(conn, tenant_id, actor_user_id, actor_role, "mcp.policy.update",
                              "mcp_tool", f"{server_id}:{tool_name}", {"enabled": bool(enabled)})

    def authorize_mcp_call(self, tenant_id, server_id, tool_name, *, user_id,
                           role, agent_id, params):
        with self._connect() as conn:
            server = conn.execute("SELECT status FROM p8_mcp_servers WHERE id=? AND tenant_id=?", (server_id, tenant_id)).fetchone()
            policy = conn.execute("SELECT * FROM p8_mcp_tool_policies WHERE server_id=? AND tenant_id=? AND tool_name=?", (server_id, tenant_id, tool_name)).fetchone()
            agent = conn.execute(
                "SELECT 1 FROM agents WHERE id=? AND tenant_id=? AND status='active'",
                (str(agent_id or ""), tenant_id),
            ).fetchone()
            allowed = bool(agent and server and server["status"] == "enabled" and policy and policy["enabled"])
            deny = set(json.loads(policy["param_denylist"] or "[]")) if policy else set()
            allow = set(json.loads(policy["param_allowlist"] or "[]")) if policy else set()
            roles = set(json.loads(policy["allowed_roles"] or "[]")) if policy else set()
            agents = set(json.loads(policy["allowed_agents"] or "[]")) if policy else set()
            keys = set((params or {}).keys())
            allowed = allowed and role in roles and (not agents or agent_id in agents) and not (keys & deny) and (not allow or keys <= allow)
            return bool(allowed)

    def record_mcp_call(self, tenant_id, server_id, tool_name, user_id, agent_id,
                        params, result_status, duration_ms):
        summary = {key: "[redacted]" for key in (params or {}).keys()}
        with self._connect() as conn:
            conn.execute("INSERT INTO p8_mcp_call_audit (id, tenant_id, user_id, agent_id, server_id, tool_name, param_summary, result_status, duration_ms, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                         (_new_id("mca"), tenant_id, user_id, agent_id, server_id, tool_name,
                          json.dumps(summary), str(result_status)[:64], max(0, int(duration_ms)), _utc_now()))
