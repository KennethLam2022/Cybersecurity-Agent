"""SQLite 对话记忆 — 多轮问答上下文管理

支持：
  - 基础历史存储（SQLite）
  - 滑动窗口 + 上下文压缩（第 6 轮起压缩为摘要）
  - 跨会话关键记忆（用户角色、提及的标准、偏好）

用法：
  memory = ConversationMemory()
  conv = memory.create_conversation()
  memory.add_message(conv["id"], "user", "你好")
  history = memory.get_compressed_history(conv["id"], llm_provider=None)
"""
import re
import sqlite3
import json
import uuid
from difflib import SequenceMatcher
import logging
import os
import hashlib
import hmac
import secrets
import urllib.parse
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional
from data_source_security import validate_data_source_config

logger = logging.getLogger(__name__)


def _to_utc_iso(dt_str):
    """SQLite CURRENT_TIMESTAMP 存的是 UTC，直接加 Z 标记"""
    if not dt_str:
        return dt_str
    s = dt_str.strip()
    if s.endswith("Z"):
        return s
    if "T" not in s:
        s = s.replace(" ", "T")
    return s + "Z"


# ---- 项目根目录：优先使用环境变量，回退到相对路径 ----
# memory.py 在 packages/agent/src/，向上4层到项目根目录
# （项目根目录 agent_data/conversations.db 才有完整的400+对话记录）
_PROJECT_ROOT = os.environ.get("PROJECT_ROOT")
if not _PROJECT_ROOT:
    _PROJECT_ROOT = str(Path(__file__).parent.parent.parent.parent)
    logger.info(f"PROJECT_ROOT 未设置，自动推断为: {_PROJECT_ROOT}")

_DB_DIR = Path(_PROJECT_ROOT) / "agent_data"
_DB_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = str(_DB_DIR / "conversations.db")

# ---- 加密工具（与 main.py 共享同一 Fernet key）----
try:
    from cryptography.fernet import Fernet
    import hashlib
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False


def _load_encryption_key() -> str:
    key = os.environ.get("LLM_KEY_ENCRYPTION_KEY")
    if key:
        return key
    key_file = Path(__file__).parent.parent / "agent_data" / ".encryption_key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip()
    return ""


_ENCRYPTION_KEY = _load_encryption_key()
_fernet = Fernet(_ENCRYPTION_KEY.encode()) if (_CRYPTO_AVAILABLE and _ENCRYPTION_KEY) else None


def get_llm_config_card(module_id: str) -> dict:
    """从 llm_configs 表读取单个 LLM 配置卡片（含解密后 Key）

    可供 agent.py / llm_provider.py / retriever.py 等模块调用。
    返回: {provider, model, base_url, api_key} 或 {}(无配置时)
    """
    try:
        with sqlite3.connect(_DB_PATH) as conn:
            row = conn.execute(
                "SELECT provider, model, base_url, api_key_enc, api_key_hash FROM llm_configs WHERE module_id = ?",
                (module_id,),
            ).fetchone()
        if not row:
            return {}
        provider, model, base_url, api_key_enc, stored_hash = row
        api_key = ""
        if api_key_enc and _fernet is not None:
            try:
                decrypted = _fernet.decrypt(api_key_enc.encode("utf-8")).decode()
                if hashlib.sha256(decrypted.encode()).hexdigest()[:16] == stored_hash:
                    api_key = decrypted
            except Exception:
                pass
        return {
            "provider": provider or "",
            "model": model or "",
            "base_url": base_url or "",
            "api_key": api_key,
        }
    except sqlite3.OperationalError:
        return {}


_SUMMARY_PROMPT = """压缩以下对话轮次为一段话（不超过50字），保留关键信息：

{conversation}

压缩摘要："""


class ConversationMemory:
    def __init__(self, db_path: str = _DB_PATH):
        self._db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self._db_path) as conn:
            # 启用 WAL 模式提升并发读写性能
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS organizations (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    email TEXT NOT NULL UNIQUE,
                    display_name TEXT NOT NULL DEFAULT '',
                    password_hash TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_users_tenant ON users(tenant_id, status);
                CREATE TABLE IF NOT EXISTS custom_roles (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    permissions_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id, name),
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_custom_roles_scope
                    ON custom_roles(tenant_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS organization_departments (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    cost_center TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id, name)
                );
                CREATE TABLE IF NOT EXISTS user_department_memberships (
                    tenant_id TEXT NOT NULL,
                    department_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(tenant_id, department_id, user_id),
                    FOREIGN KEY(department_id) REFERENCES organization_departments(id),
                    FOREIGN KEY(user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_user_department_scope
                    ON user_department_memberships(tenant_id, user_id, status);
                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_agents_tenant ON agents(tenant_id, status);
                CREATE TABLE IF NOT EXISTS organization_memberships (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, user_id),
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_org_memberships_user
                    ON organization_memberships(user_id, status);
                CREATE TABLE IF NOT EXISTS agent_memberships (
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (agent_id, user_id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id),
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_memberships_user
                    ON agent_memberships(user_id, tenant_id, status);
                CREATE TABLE IF NOT EXISTS auth_sessions (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    user_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    expires_at TIMESTAMP NOT NULL,
                    revoked_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users(id)
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_token ON auth_sessions(token_hash, expires_at);
                CREATE TABLE IF NOT EXISTS workspace_invitations (
                    id TEXT PRIMARY KEY,
                    token_hash TEXT NOT NULL UNIQUE,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT '',
                    email TEXT NOT NULL,
                    role TEXT NOT NULL DEFAULT 'user',
                    invited_by TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    expires_at TIMESTAMP NOT NULL,
                    accepted_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_workspace_invitations_scope
                    ON workspace_invitations(tenant_id, email, status, expires_at);
                CREATE TABLE IF NOT EXISTS audit_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT,
                    agent_id TEXT,
                    action TEXT NOT NULL,
                    resource_type TEXT NOT NULL DEFAULT '',
                    resource_id TEXT NOT NULL DEFAULT '',
                    detail_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_audit_logs_scope ON audit_logs(tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS long_term_memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    memory_type TEXT NOT NULL,
                    content TEXT NOT NULL,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    source_conversation_id TEXT,
                    importance REAL NOT NULL DEFAULT 0.5,
                    confidence REAL NOT NULL DEFAULT 0.7,
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id),
                    FOREIGN KEY (user_id) REFERENCES users(id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_long_term_memory_scope
                    ON long_term_memories(tenant_id, user_id, agent_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS user_memory_preferences (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, user_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS memory_profile_proposals (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    source_conversation_id TEXT NOT NULL DEFAULT '',
                    fields_json TEXT NOT NULL DEFAULT '{}',
                    rationale TEXT NOT NULL DEFAULT '',
                    prompt_version INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_memory_profile_proposals_scope
                    ON memory_profile_proposals(tenant_id, user_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS memory_conflict_proposals (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    existing_memory_id INTEGER NOT NULL,
                    proposed_content TEXT NOT NULL,
                    rationale TEXT NOT NULL DEFAULT '',
                    prompt_version INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_memory_conflict_proposals_scope
                    ON memory_conflict_proposals(tenant_id, user_id, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS user_language_preferences (
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    language TEXT NOT NULL DEFAULT 'zh-CN',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (tenant_id, user_id, agent_id)
                );
                CREATE TABLE IF NOT EXISTS documents (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    knowledge_base_id TEXT NOT NULL DEFAULT '',
                    visibility TEXT NOT NULL DEFAULT 'public',
                    source_name TEXT NOT NULL,
                    cleaned_path TEXT NOT NULL DEFAULT '',
                    category TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL DEFAULT 'general',
                    status TEXT NOT NULL DEFAULT 'staged',
                    version INTEGER NOT NULL DEFAULT 1,
                    effective_date TIMESTAMP,
                    expiry_date TIMESTAMP,
                    lifecycle_status TEXT NOT NULL DEFAULT 'review',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_documents_scope
                    ON documents(tenant_id, owner_user_id, agent_id, visibility, status);
                CREATE TABLE IF NOT EXISTS knowledge_bases (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    profile TEXT NOT NULL DEFAULT 'general',
                    visibility TEXT NOT NULL DEFAULT 'tenant',
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id, name),
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id)
                );
                CREATE TABLE IF NOT EXISTS knowledge_base_retrieval_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    knowledge_base_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'active',
                    changed_by TEXT NOT NULL DEFAULT '',
                    change_reason TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(knowledge_base_id, version),
                    FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)
                );
                CREATE INDEX IF NOT EXISTS idx_kb_retrieval_config
                    ON knowledge_base_retrieval_configs(knowledge_base_id, status, version DESC);
                CREATE INDEX IF NOT EXISTS idx_knowledge_bases_scope
                    ON knowledge_bases(tenant_id, status, visibility);
                CREATE TABLE IF NOT EXISTS knowledge_base_grants (
                    knowledge_base_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    role TEXT NOT NULL DEFAULT 'viewer',
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (knowledge_base_id, user_id, agent_id),
                    FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_base_grants_scope
                    ON knowledge_base_grants(tenant_id, user_id, agent_id, status);
                CREATE TABLE IF NOT EXISTS document_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    cleaned_path TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    lifecycle_status TEXT NOT NULL DEFAULT 'review',
                    change_reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(document_id, version),
                    FOREIGN KEY (document_id) REFERENCES documents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_document_versions_doc
                    ON document_versions(document_id, version DESC);
                CREATE TABLE IF NOT EXISTS ingestion_jobs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'processing',
                    total_files INTEGER NOT NULL DEFAULT 0,
                    success_count INTEGER NOT NULL DEFAULT 0,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ingestion_job_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    document_id TEXT NOT NULL,
                    source_name TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL DEFAULT 'starting',
                    status TEXT NOT NULL DEFAULT 'pending',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(job_id, document_id),
                    FOREIGN KEY (job_id) REFERENCES ingestion_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_ingestion_items_job
                    ON ingestion_job_items(job_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS ingestion_stage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    document_id TEXT NOT NULL DEFAULT '',
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    error_type TEXT NOT NULL DEFAULT '',
                    error_message TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (job_id) REFERENCES ingestion_jobs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_ingestion_stage_events_job
                    ON ingestion_stage_events(job_id, document_id, id);
                CREATE TABLE IF NOT EXISTS data_sources (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    owner_user_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL,
                    endpoint TEXT NOT NULL DEFAULT '',
                    config_json TEXT NOT NULL DEFAULT '{}',
                    knowledge_base_id TEXT NOT NULL DEFAULT '',
                    sync_mode TEXT NOT NULL DEFAULT 'manual',
                    schedule TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    last_synced_at TIMESTAMP,
                    last_content_hash TEXT NOT NULL DEFAULT '',
                    last_error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (knowledge_base_id) REFERENCES knowledge_bases(id)
                );
                CREATE INDEX IF NOT EXISTS idx_data_sources_scope
                    ON data_sources(tenant_id, knowledge_base_id, status, updated_at);
                CREATE TABLE IF NOT EXISTS data_source_sync_runs (
                    id TEXT PRIMARY KEY,
                    data_source_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'queued',
                    attempt INTEGER NOT NULL DEFAULT 1,
                    trigger TEXT NOT NULL DEFAULT 'manual',
                    content_hash TEXT NOT NULL DEFAULT '',
                    documents_found INTEGER NOT NULL DEFAULT 0,
                    documents_ingested INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    started_at TIMESTAMP,
                    finished_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (data_source_id) REFERENCES data_sources(id)
                );
                CREATE INDEX IF NOT EXISTS idx_data_source_sync_runs_source
                    ON data_source_sync_runs(data_source_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS external_retrieval_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    trigger_mode TEXT NOT NULL DEFAULT 'empty_only',
                    max_sources INTEGER NOT NULL DEFAULT 3,
                    timeout_seconds INTEGER NOT NULL DEFAULT 10,
                    max_bytes INTEGER NOT NULL DEFAULT 2000000,
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS external_retrieval_sources (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    source_type TEXT NOT NULL DEFAULT 'url',
                    endpoint TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    approved INTEGER NOT NULL DEFAULT 0,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_external_sources_status
                    ON external_retrieval_sources(enabled, approved, updated_at DESC);
                CREATE TABLE IF NOT EXISTS external_retrieval_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    trace_id TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    tenant_id TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    query TEXT NOT NULL DEFAULT '',
                    source_id TEXT NOT NULL DEFAULT '',
                    source_url TEXT NOT NULL DEFAULT '',
                    content_hash TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    result_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_external_events_scope
                    ON external_retrieval_events(tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS external_apps (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    app_key TEXT NOT NULL UNIQUE,
                    app_secret_hash TEXT NOT NULL,
                    scopes_json TEXT NOT NULL DEFAULT '["chat"]',
                    rate_limit_per_minute INTEGER NOT NULL DEFAULT 60,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_external_apps_tenant
                    ON external_apps(tenant_id, enabled, updated_at DESC);
                CREATE TABLE IF NOT EXISTS external_app_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    app_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_ref TEXT NOT NULL DEFAULT '',
                    endpoint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    request_id TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (app_id) REFERENCES external_apps(id)
                );
                CREATE INDEX IF NOT EXISTS idx_external_app_usage_scope
                    ON external_app_usage(app_id, tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS embed_tokens (
                    id TEXT PRIMARY KEY,
                    app_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    origin TEXT NOT NULL DEFAULT '',
                    expires_at TIMESTAMP NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_used_at TIMESTAMP,
                    FOREIGN KEY (app_id) REFERENCES external_apps(id)
                );
                CREATE INDEX IF NOT EXISTS idx_embed_tokens_lookup
                    ON embed_tokens(app_id, enabled, expires_at);
                CREATE TABLE IF NOT EXISTS webhook_subscriptions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    name TEXT NOT NULL,
                    url TEXT NOT NULL,
                    event_types_json TEXT NOT NULL DEFAULT '[]',
                    signing_secret_enc TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    timeout_seconds INTEGER NOT NULL DEFAULT 10,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_delivered_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_webhook_subscriptions_scope
                    ON webhook_subscriptions(tenant_id, enabled, updated_at DESC);
                CREATE TABLE IF NOT EXISTS webhook_deliveries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    subscription_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL DEFAULT '',
                    event_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    status_code INTEGER,
                    error TEXT NOT NULL DEFAULT '',
                    delivered INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    delivered_at TIMESTAMP,
                    UNIQUE(subscription_id, event_id, attempt),
                    FOREIGN KEY (subscription_id) REFERENCES webhook_subscriptions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_scope
                    ON webhook_deliveries(tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS login_attempts (
                    email TEXT PRIMARY KEY,
                    failed_count INTEGER NOT NULL DEFAULT 0,
                    window_started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    locked_until TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS account_deletion_requests (
                    user_id TEXT PRIMARY KEY,
                    requested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    purge_after TIMESTAMP NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    reason TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS generation_requests (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'outline_ready',
                    query_text TEXT NOT NULL DEFAULT '',
                    fields_json TEXT NOT NULL DEFAULT '{}',
                    outline_json TEXT NOT NULL DEFAULT '{}',
                    trace_json TEXT NOT NULL DEFAULT '{}',
                    references_json TEXT NOT NULL DEFAULT '[]',
                    artifact_path TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_generation_scope
                    ON generation_requests(tenant_id, user_id, agent_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS generation_artifacts (
                    id TEXT PRIMARY KEY,
                    generation_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    artifact_path TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(generation_id, version),
                    FOREIGN KEY (generation_id) REFERENCES generation_requests(id)
                );
                CREATE INDEX IF NOT EXISTS idx_generation_artifacts_scope
                    ON generation_artifacts(generation_id, tenant_id, user_id, agent_id, version DESC);
                CREATE TABLE IF NOT EXISTS capability_extensions (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    version TEXT NOT NULL DEFAULT '',
                    source TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    permissions_json TEXT NOT NULL DEFAULT '[]',
                    network_scope TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_capability_extensions_kind
                    ON capability_extensions(kind, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS capability_extension_versions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extension_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source TEXT NOT NULL DEFAULT '',
                    description TEXT NOT NULL DEFAULT '',
                    manifest_json TEXT NOT NULL DEFAULT '{}',
                    permissions_json TEXT NOT NULL DEFAULT '[]',
                    network_scope TEXT NOT NULL DEFAULT '',
                    changed_by TEXT NOT NULL DEFAULT '',
                    change_reason TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(extension_id, version),
                    FOREIGN KEY (extension_id) REFERENCES capability_extensions(id)
                );
                CREATE TABLE IF NOT EXISTS capability_extension_grants (
                    extension_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 0,
                    approved_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY (extension_id, tenant_id, agent_id),
                    FOREIGN KEY (extension_id) REFERENCES capability_extensions(id),
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_extension_grants_scope
                    ON capability_extension_grants(tenant_id, agent_id, enabled);
                CREATE TABLE IF NOT EXISTS capability_extension_calls (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    extension_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (extension_id) REFERENCES capability_extensions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_extension_calls_usage
                    ON capability_extension_calls(extension_id, tenant_id, agent_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '新对话',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    jailbreak_status TEXT DEFAULT NULL,
                    jailbreak_reason TEXT DEFAULT NULL,
                    jailbreak_message_id INTEGER DEFAULT NULL
                );
                CREATE TABLE IF NOT EXISTS semantic_cache (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cache_key TEXT NOT NULL UNIQUE,
                    normalized_query TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    sources_json TEXT NOT NULL DEFAULT '[]',
                    model TEXT NOT NULL DEFAULT '',
                    knowledge_base_id TEXT NOT NULL DEFAULT '',
                    profile_scope TEXT NOT NULL DEFAULT '',
                    prompt_version TEXT NOT NULL DEFAULT '',
                    hit_count INTEGER NOT NULL DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_hit_at TIMESTAMP,
                    expires_at TIMESTAMP NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_semantic_cache_expiry ON semantic_cache(expires_at);
                CREATE TABLE IF NOT EXISTS messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    content TEXT NOT NULL,
                    sources TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
                CREATE TABLE IF NOT EXISTS session_memory (
                    conversation_id TEXT PRIMARY KEY,
                    memory_data TEXT NOT NULL DEFAULT '{}',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_messages_conv
                    ON messages(conversation_id, id);
                CREATE INDEX IF NOT EXISTS idx_messages_created_at
                    ON messages(created_at);
                CREATE TABLE IF NOT EXISTS usage_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL,
                    message_id INTEGER,
                    query TEXT NOT NULL,
                    rewrite_time REAL DEFAULT 0,
                    faiss_time REAL DEFAULT 0,
                    chroma_time REAL DEFAULT 0,
                    rerank_time REAL DEFAULT 0,
                    llm_time REAL DEFAULT 0,
                    total_time REAL DEFAULT 0,
                    faiss_count INTEGER DEFAULT 0,
                    chroma_count INTEGER DEFAULT 0,
                    bm25_count INTEGER DEFAULT 0,
                    final_count INTEGER DEFAULT 0,
                    returned_count INTEGER DEFAULT 0,
                    prompt_tokens INTEGER DEFAULT 0,
                    completion_tokens INTEGER DEFAULT 0,
                    llm_success INTEGER DEFAULT 1,
                    was_circuit_break INTEGER DEFAULT 0,
                    circuit_provider TEXT,
                    was_truncated INTEGER DEFAULT 0,
                    off_topic INTEGER DEFAULT 0,
                    answer_jailbreak INTEGER DEFAULT 0,
                    user_rating INTEGER,
                    semantic_rating INTEGER,
                    documents TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
                CREATE TABLE IF NOT EXISTS feedback_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    message_id INTEGER,
                    conversation_id TEXT NOT NULL,
                    query TEXT NOT NULL DEFAULT '',
                    feedback_type TEXT NOT NULL,
                    feedback_text TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_feedback_items_message
                    ON feedback_items(message_id);
                CREATE INDEX IF NOT EXISTS idx_feedback_items_conv
                    ON feedback_items(conversation_id, created_at);
                CREATE TABLE IF NOT EXISTS knowledge_gaps (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    cluster_key TEXT NOT NULL UNIQUE,
                    canonical_question TEXT NOT NULL,
                    profile TEXT NOT NULL DEFAULT 'general',
                    knowledge_base_id TEXT NOT NULL DEFAULT '',
                    knowledge_base_ids TEXT NOT NULL DEFAULT '[]',
                    reason_tags TEXT NOT NULL DEFAULT '[]',
                    occurrence_count INTEGER NOT NULL DEFAULT 0,
                    low_rating_count INTEGER NOT NULL DEFAULT 0,
                    refresh_count INTEGER NOT NULL DEFAULT 0,
                    correction_count INTEGER NOT NULL DEFAULT 0,
                    no_source_count INTEGER NOT NULL DEFAULT 0,
                    related_queries TEXT NOT NULL DEFAULT '[]',
                    related_message_ids TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'open',
                    note TEXT NOT NULL DEFAULT '',
                    first_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_status
                    ON knowledge_gaps(status, occurrence_count DESC);
                CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_profile
                    ON knowledge_gaps(profile, status);
                CREATE TABLE IF NOT EXISTS gap_supply_tasks (
                    id TEXT PRIMARY KEY,
                    gap_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (gap_id) REFERENCES knowledge_gaps(id)
                );
                CREATE INDEX IF NOT EXISTS idx_gap_supply_tasks_status
                    ON gap_supply_tasks(status, created_at);
                -- LLM 提供商 API Key 安全存储表（加密存储）
                CREATE TABLE IF NOT EXISTS llm_provider_keys (
                    provider TEXT PRIMARY KEY,
                    api_key_hash TEXT NOT NULL,
                    api_key_enc TEXT,
                    api_key_mask TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN deleted INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN knowledge_base_id TEXT DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE documents ADD COLUMN knowledge_base_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE external_apps ADD COLUMN agent_id TEXT NOT NULL DEFAULT ''")
            except sqlite3.OperationalError:
                pass
            for column, definition in (
                ("ip_address", "TEXT NOT NULL DEFAULT ''"),
                ("user_agent", "TEXT NOT NULL DEFAULT ''"),
                ("last_seen_at", "TIMESTAMP"),
            ):
                try:
                    conn.execute(f"ALTER TABLE auth_sessions ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            for column, definition in (
                ("user_id", "TEXT"),
                ("approved_by", "TEXT"),
                ("approved_at", "TIMESTAMP"),
            ):
                try:
                    conn.execute(f"ALTER TABLE workspace_invitations ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            for column, definition in (
                ("version", "INTEGER NOT NULL DEFAULT 1"),
                ("effective_date", "TIMESTAMP"),
                ("expiry_date", "TIMESTAMP"),
                ("lifecycle_status", "TEXT NOT NULL DEFAULT 'review'"),
                ("metadata_json", "TEXT NOT NULL DEFAULT '{}'"),
            ):
                try:
                    conn.execute(f"ALTER TABLE documents ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("""
                INSERT OR IGNORE INTO document_versions
                    (document_id, version, cleaned_path, lifecycle_status, created_by)
                SELECT id, COALESCE(version, 1), cleaned_path,
                       CASE WHEN status='indexed' THEN 'published' ELSE 'review' END,
                       'migration'
                FROM documents
            """)
            conn.execute("""
                UPDATE documents SET lifecycle_status='published'
                WHERE lifecycle_status='review' AND status='indexed'
                  AND (metadata_json IS NULL OR metadata_json='{}')
            """)
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN category TEXT DEFAULT 'user'")
            except sqlite3.OperationalError:
                pass
            for column, default_value in (
                ("tenant_id", "local-default"),
                ("user_id", "local-owner"),
                ("agent_id", "default-agent"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE conversations ADD COLUMN {column} TEXT DEFAULT '{default_value}'"
                    )
                except sqlite3.OperationalError:
                    pass
                conn.execute(
                    f"UPDATE conversations SET {column}=? WHERE {column} IS NULL OR {column}=''",
                    (default_value,),
                )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_scope ON conversations(tenant_id, user_id, agent_id, updated_at DESC)"
            )
            # Existing local users predate membership tables. Preserve their current access.
            conn.execute("""
                INSERT OR IGNORE INTO organization_memberships (tenant_id, user_id, role)
                SELECT tenant_id, id, role FROM users WHERE status='active'
            """)
            conn.execute("""
                INSERT OR IGNORE INTO agent_memberships (tenant_id, agent_id, user_id, role)
                SELECT a.tenant_id, a.id, u.id, u.role
                FROM agents a JOIN users u ON u.tenant_id=a.tenant_id
                WHERE u.status='active' AND u.role IN ('org_admin', 'platform_admin')
            """)
            conn.execute("""
                INSERT OR IGNORE INTO knowledge_bases
                    (id, tenant_id, name, description, profile, visibility, owner_user_id)
                VALUES ('kb-public-general', 'local-default', '公共网络安全知识库',
                        '二期存量资料默认归属库', 'general', 'public', '')
            """)
            conn.execute("""
                UPDATE documents
                SET knowledge_base_id='kb-public-general'
                WHERE (knowledge_base_id IS NULL OR knowledge_base_id='')
                  AND (visibility='public' OR visibility='')
            """)
            legacy_scopes = conn.execute("""
                SELECT DISTINCT tenant_id, visibility, owner_user_id, agent_id
                FROM documents
                WHERE (knowledge_base_id IS NULL OR knowledge_base_id='')
                  AND visibility IN ('tenant', 'private')
            """).fetchall()
            for legacy_tenant, legacy_visibility, legacy_owner, legacy_agent in legacy_scopes:
                scope_key = f"{legacy_tenant}:{legacy_visibility}:{legacy_owner if legacy_visibility == 'private' else ''}:{legacy_agent if legacy_visibility == 'private' else ''}"
                legacy_kb_id = "kb-migrated-" + hashlib.sha1(scope_key.encode("utf-8")).hexdigest()[:16]
                legacy_name = "存量工作区知识库" if legacy_visibility == "tenant" else "存量私有知识库"
                conn.execute("""
                    INSERT OR IGNORE INTO knowledge_bases
                        (id, tenant_id, name, description, profile, visibility, owner_user_id)
                    VALUES (?, ?, ?, '二期存量资料迁移库', 'general', ?, ?)
                """, (legacy_kb_id, legacy_tenant or 'local-default', legacy_name,
                      legacy_visibility, legacy_owner if legacy_visibility == 'private' else ''))
                if legacy_visibility == 'private':
                    conn.execute("""
                        UPDATE documents SET knowledge_base_id=?
                        WHERE (knowledge_base_id IS NULL OR knowledge_base_id='')
                          AND tenant_id=? AND visibility=? AND owner_user_id=? AND agent_id=?
                    """, (legacy_kb_id, legacy_tenant, legacy_visibility, legacy_owner, legacy_agent))
                else:
                    conn.execute("""
                        UPDATE documents SET knowledge_base_id=?
                        WHERE (knowledge_base_id IS NULL OR knowledge_base_id='')
                          AND tenant_id=? AND visibility=?
                    """, (legacy_kb_id, legacy_tenant, legacy_visibility))
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN jailbreak_status TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN jailbreak_reason TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute(
                    "ALTER TABLE conversations ADD COLUMN jailbreak_message_id INTEGER DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE messages ADD COLUMN jailbreak_flagged INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE usage_logs ADD COLUMN answer_jailbreak INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE usage_logs ADD COLUMN trace_data TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            for column, default_value in (
                ("trace_json", "'{}'"),
                ("references_json", "'[]'"),
            ):
                try:
                    conn.execute(
                        f"ALTER TABLE generation_requests ADD COLUMN {column} TEXT DEFAULT {default_value}"
                    )
                except sqlite3.OperationalError:
                    pass
            # ---- LLM 配置卡片表（后端模型配置持久化）----
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS llm_configs (
                    module_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    base_url TEXT NOT NULL DEFAULT '',
                    api_key_enc TEXT,
                    api_key_hash TEXT,
                    api_key_mask TEXT,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            # ---- Prompt 测试集表 ----
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS prompt_test_sets (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    keyword_input TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    is_active INTEGER DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS prompt_test_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    set_id TEXT NOT NULL,
                    seq INTEGER NOT NULL,
                    query TEXT NOT NULL,
                    category TEXT,
                    difficulty TEXT DEFAULT 'medium',
                    expected TEXT,
                    is_active INTEGER DEFAULT 1
                );
            """)
            # ---- Prompt A/B 离线对照评测记录 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS prompt_ab_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    version_a_id INTEGER NOT NULL,
                    version_b_id INTEGER NOT NULL,
                    version_a_name TEXT NOT NULL,
                    version_b_name TEXT NOT NULL,
                    set_id TEXT NOT NULL,
                    report_json TEXT NOT NULL DEFAULT '{}',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- P6-H Prompt asset registry (all non-chat prompts converge here) ----
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS prompt_assets (
                    slot TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    model_role TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS prompt_asset_versions (
                    id TEXT PRIMARY KEY,
                    slot TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    template TEXT NOT NULL,
                    variables_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'draft',
                    change_note TEXT NOT NULL DEFAULT '',
                    changed_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(slot, version)
                );
                CREATE INDEX IF NOT EXISTS idx_prompt_asset_versions_active
                    ON prompt_asset_versions(slot, status, version DESC);
                CREATE TABLE IF NOT EXISTS prompt_asset_test_runs (
                    id TEXT PRIMARY KEY,
                    slot TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    test_type TEXT NOT NULL DEFAULT 'contract',
                    report_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS langfuse_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    host TEXT NOT NULL DEFAULT 'https://cloud.langfuse.com',
                    public_key_enc TEXT NOT NULL DEFAULT '',
                    public_key_mask TEXT NOT NULL DEFAULT '',
                    secret_key_enc TEXT NOT NULL DEFAULT '',
                    secret_key_mask TEXT NOT NULL DEFAULT '',
                    export_content INTEGER NOT NULL DEFAULT 0,
                    annotation_queue TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- pipeline_stats 表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS pipeline_stats (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    total_files INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    fail_count INTEGER DEFAULT 0,
                    dedup_l1 INTEGER DEFAULT 0,
                    dedup_l2 INTEGER DEFAULT 0,
                    dedup_l3 INTEGER DEFAULT 0,
                    faiss_after INTEGER DEFAULT 0,
                    chroma_after INTEGER DEFAULT 0,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- retrieval_eval 表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retrieval_eval (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    expected_source TEXT DEFAULT '',
                    profile TEXT DEFAULT 'general',
                    context_json TEXT DEFAULT '{}',
                    recall_5 INTEGER DEFAULT 0,
                    recall_10 INTEGER DEFAULT 0,
                    mrr REAL DEFAULT 0,
                    faiss_count INTEGER DEFAULT 0,
                    chroma_count INTEGER DEFAULT 0,
                    rerank_top1_match INTEGER DEFAULT 0,
                    eval_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- eval_comparison 表（4种检索模式对比）----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS eval_comparison (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    expected_source TEXT DEFAULT '',
                    profile TEXT DEFAULT 'general',
                    context_json TEXT DEFAULT '{}',
                    faiss_only_recall_5 INTEGER DEFAULT 0,
                    faiss_only_mrr REAL DEFAULT 0,
                    bm25_only_recall_5 INTEGER DEFAULT 0,
                    bm25_only_mrr REAL DEFAULT 0,
                    hybrid_no_rerank_recall_5 INTEGER DEFAULT 0,
                    hybrid_no_rerank_mrr REAL DEFAULT 0,
                    hybrid_rerank_recall_5 INTEGER DEFAULT 0,
                    hybrid_rerank_mrr REAL DEFAULT 0,
                    eval_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- misclassification_candidates 评测反馈闭环表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS misclassification_candidates (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    file_name TEXT NOT NULL,
                    current_profile TEXT,
                    suggested_profile TEXT,
                    reason TEXT,
                    detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    resolved INTEGER DEFAULT 0,
                    resolved_profile TEXT,
                    UNIQUE(query, file_name)
                )
            """)
            # ---- retrieval_eval_items 测试集表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retrieval_eval_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    profile TEXT DEFAULT 'general',
                    category TEXT DEFAULT '',
                    difficulty TEXT DEFAULT 'medium',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # Feedback-to-eval provenance is additive so existing installations remain readable.
            for column, definition in (
                ("failure_class", "TEXT NOT NULL DEFAULT 'answer_quality'"),
                ("eval_status", "TEXT NOT NULL DEFAULT 'unlabeled'"),
            ):
                try:
                    conn.execute(f"ALTER TABLE feedback_items ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            for column, definition in (
                ("source_feedback_id", "INTEGER"),
                ("label_status", "TEXT NOT NULL DEFAULT 'pending'"),
                ("failure_class", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    conn.execute(f"ALTER TABLE retrieval_eval_items ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            # ---- profile 字段迁移：历史评测记录默认属于通用主干 ----
            for table in ("retrieval_eval", "eval_comparison", "retrieval_eval_items"):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN profile TEXT DEFAULT 'general'")
                except sqlite3.OperationalError:
                    pass
            for table in ("retrieval_eval", "eval_comparison"):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN context_json TEXT DEFAULT '{{}}'")
                except sqlite3.OperationalError:
                    pass
            # ---- e2e_eval_items 综合质量评测测试集表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS e2e_eval_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    domain TEXT DEFAULT '',
                    profile TEXT DEFAULT 'general',
                    difficulty TEXT DEFAULT '中等',
                    style TEXT DEFAULT 'plain',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- prompt_versions 扩展字段 ----
            for col in ("changed_by", "change_log", "prompt_diff"):
                try:
                    conn.execute(f"ALTER TABLE prompt_versions ADD COLUMN {col} TEXT DEFAULT ''")
                except sqlite3.OperationalError:
                    pass
            try:
                conn.execute("ALTER TABLE e2e_eval_items ADD COLUMN profile TEXT DEFAULT 'general'")
            except sqlite3.OperationalError:
                pass
            # Legacy evaluation and knowledge-gap records predate organizations.
            # Keep them visible only to the local default tenant after migration.
            for table, column, definition in (
                ("knowledge_gaps", "tenant_id", "TEXT NOT NULL DEFAULT 'local-default'"),
                ("knowledge_gaps", "agent_id", "TEXT NOT NULL DEFAULT ''"),
                ("knowledge_gaps", "owner_user_id", "TEXT NOT NULL DEFAULT ''"),
                ("gap_supply_tasks", "tenant_id", "TEXT NOT NULL DEFAULT 'local-default'"),
                ("gap_supply_tasks", "agent_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_gaps_tenant ON knowledge_gaps(tenant_id, status, occurrence_count DESC)")
            # ---- Agent Evaluation：通用测试用例、运行快照与逐例结果 ----
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS agent_eval_cases (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    case_key TEXT NOT NULL UNIQUE,
                    profile TEXT NOT NULL DEFAULT 'general',
                    case_type TEXT NOT NULL DEFAULT 'answer_quality',
                    domain TEXT DEFAULT '',
                    query_json TEXT NOT NULL DEFAULT '{}',
                    expected_json TEXT NOT NULL DEFAULT '{}',
                    risk_tags_json TEXT NOT NULL DEFAULT '[]',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_agent_eval_cases_profile
                    ON agent_eval_cases(profile, is_active, id);
                CREATE TABLE IF NOT EXISTS agent_eval_runs (
                    run_id TEXT PRIMARY KEY,
                    profile TEXT NOT NULL DEFAULT 'general',
                    status TEXT NOT NULL DEFAULT 'running',
                    context_json TEXT NOT NULL DEFAULT '{}',
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS agent_eval_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    case_id INTEGER,
                    case_key TEXT NOT NULL,
                    profile TEXT NOT NULL DEFAULT 'general',
                    case_type TEXT NOT NULL DEFAULT 'answer_quality',
                    query_text TEXT NOT NULL DEFAULT '',
                    answer_text TEXT NOT NULL DEFAULT '',
                    trace_json TEXT NOT NULL DEFAULT '{}',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'completed',
                    elapsed_ms INTEGER DEFAULT 0,
                    error TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (run_id) REFERENCES agent_eval_runs(run_id),
                    FOREIGN KEY (case_id) REFERENCES agent_eval_cases(id)
                );
                CREATE INDEX IF NOT EXISTS idx_agent_eval_results_run
                    ON agent_eval_results(run_id, id);
                CREATE TABLE IF NOT EXISTS eval_human_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    evaluation_type TEXT NOT NULL,
                    run_id TEXT NOT NULL,
                    result_key TEXT NOT NULL,
                    reviewer TEXT DEFAULT '',
                    scores_json TEXT NOT NULL DEFAULT '{}',
                    note TEXT DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(evaluation_type, run_id, result_key)
                );
                CREATE INDEX IF NOT EXISTS idx_eval_human_reviews_run
                    ON eval_human_reviews(evaluation_type, run_id, result_key);
                CREATE TABLE IF NOT EXISTS release_gate_evidence (
                    evidence_key TEXT PRIMARY KEY,
                    evidence_json TEXT NOT NULL DEFAULT '{}',
                    updated_by TEXT NOT NULL DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );

                CREATE TABLE IF NOT EXISTS sso_providers (
                    id TEXT PRIMARY KEY,
                    provider_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    config_json TEXT NOT NULL DEFAULT '{}',
                    secrets_json_enc TEXT NOT NULL DEFAULT '',
                    enabled INTEGER NOT NULL DEFAULT 0,
                    auto_provision INTEGER NOT NULL DEFAULT 0,
                    default_role TEXT NOT NULL DEFAULT 'user',
                    default_tenant_id TEXT NOT NULL DEFAULT '',
                    display_order INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL DEFAULT 'admin',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_sso_providers_scope
                    ON sso_providers(enabled, provider_type, display_order, name);
                CREATE TABLE IF NOT EXISTS sso_identity_links (
                    id TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    email TEXT NOT NULL,
                    display_name TEXT NOT NULL DEFAULT '',
                    user_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    last_login_at TIMESTAMP,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider_id, subject),
                    FOREIGN KEY (provider_id) REFERENCES sso_providers(id)
                );
                CREATE INDEX IF NOT EXISTS idx_sso_identity_links_scope
                    ON sso_identity_links(provider_id, status, email);
                CREATE INDEX IF NOT EXISTS idx_sso_identity_links_user
                    ON sso_identity_links(user_id, status);
                CREATE TABLE IF NOT EXISTS sso_oauth_states (
                    state TEXT PRIMARY KEY,
                    provider_id TEXT NOT NULL,
                    redirect_uri TEXT NOT NULL DEFAULT '',
                    expires_at TIMESTAMP NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (provider_id) REFERENCES sso_providers(id)
                );
                CREATE INDEX IF NOT EXISTS idx_sso_oauth_states_expires
                    ON sso_oauth_states(expires_at);
                CREATE TABLE IF NOT EXISTS notification_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'local-default',
                    user_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'info',
                    title TEXT NOT NULL,
                    body TEXT NOT NULL DEFAULT '',
                    link_path TEXT NOT NULL DEFAULT '',
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'unread',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    read_at TIMESTAMP,
                    archived_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_notification_events_user
                    ON notification_events(tenant_id, user_id, status, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_notification_events_tenant
                    ON notification_events(tenant_id, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS email_notification_config (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    enabled INTEGER NOT NULL DEFAULT 0,
                    host TEXT NOT NULL DEFAULT '',
                    port INTEGER NOT NULL DEFAULT 465,
                    security TEXT NOT NULL DEFAULT 'ssl',
                    sender TEXT NOT NULL DEFAULT '',
                    username TEXT NOT NULL DEFAULT '',
                    password_enc TEXT NOT NULL DEFAULT '',
                    recipients_json TEXT NOT NULL DEFAULT '[]',
                    events_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS email_notification_deliveries (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'local-default',
                    event_type TEXT NOT NULL,
                    notification_id TEXT NOT NULL DEFAULT '',
                    recipient_count INTEGER NOT NULL DEFAULT 0,
                    attempt INTEGER NOT NULL DEFAULT 1,
                    sent INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_email_notification_deliveries_scope
                    ON email_notification_deliveries(tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS conversation_shares (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL DEFAULT '',
                    allow_copy INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    expires_at TIMESTAMP NOT NULL,
                    access_count INTEGER NOT NULL DEFAULT 0,
                    last_accessed_at TIMESTAMP,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
                CREATE TABLE IF NOT EXISTS model_pricing (
                    id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL,
                    input_price_per_million REAL NOT NULL DEFAULT 0,
                    output_price_per_million REAL NOT NULL DEFAULT 0,
                    quality_score REAL NOT NULL DEFAULT 0.5,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    notes TEXT NOT NULL DEFAULT '',
                    updated_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(provider, model)
                );
                CREATE INDEX IF NOT EXISTS idx_model_pricing_enabled
                    ON model_pricing(enabled, provider, model);
                CREATE TABLE IF NOT EXISTS llm_usage_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL DEFAULT 'local-default',
                    user_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    message_id INTEGER,
                    module TEXT NOT NULL DEFAULT 'chat',
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    estimated_cost REAL,
                    pricing_status TEXT NOT NULL DEFAULT 'unknown_model_or_rate',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_llm_usage_events_scope
                    ON llm_usage_events(tenant_id, user_id, agent_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS quota_budgets (
                    id TEXT PRIMARY KEY,
                    scope_type TEXT NOT NULL,
                    scope_id TEXT NOT NULL DEFAULT '',
                    tenant_id TEXT NOT NULL DEFAULT '',
                    period TEXT NOT NULL DEFAULT 'monthly',
                    token_limit INTEGER,
                    cost_limit REAL,
                    warn_percent REAL NOT NULL DEFAULT 80,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(scope_type, scope_id, period)
                );
                CREATE INDEX IF NOT EXISTS idx_quota_budgets_scope
                    ON quota_budgets(tenant_id, scope_type, enabled);
                CREATE TABLE IF NOT EXISTS reflection_rules (
                    id TEXT PRIMARY KEY,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    severity TEXT NOT NULL DEFAULT 'medium',
                    rule_text TEXT NOT NULL,
                    capability_modes_json TEXT NOT NULL DEFAULT '["chat","writing","presentation"]',
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'draft',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_rules_status
                    ON reflection_rules(status, category, version DESC);
                CREATE TABLE IF NOT EXISTS reflection_rule_versions (
                    id TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    category TEXT NOT NULL DEFAULT 'general',
                    severity TEXT NOT NULL DEFAULT 'medium',
                    rule_text TEXT NOT NULL,
                    capability_modes_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'draft',
                    changed_by TEXT NOT NULL DEFAULT '',
                    change_type TEXT NOT NULL DEFAULT 'save',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(rule_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_rule_versions_rule
                    ON reflection_rule_versions(rule_id, version DESC);
                CREATE TABLE IF NOT EXISTS reflection_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    user_id TEXT NOT NULL DEFAULT '',
                    agent_id TEXT NOT NULL DEFAULT '',
                    conversation_id TEXT NOT NULL DEFAULT '',
                    message_id INTEGER,
                    mode TEXT NOT NULL DEFAULT 'chat',
                    model TEXT NOT NULL DEFAULT '',
                    rule_version INTEGER NOT NULL DEFAULT 0,
                    decision TEXT NOT NULL DEFAULT 'skipped',
                    input_summary TEXT NOT NULL DEFAULT '',
                    output_summary TEXT NOT NULL DEFAULT '',
                    revision_diff TEXT NOT NULL DEFAULT '',
                    rounds INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_reflection_runs_scope
                    ON reflection_runs(tenant_id, agent_id, created_at DESC);
                CREATE INDEX IF NOT EXISTS idx_conversation_shares_scope
                    ON conversation_shares(conversation_id, tenant_id, status, expires_at);
                CREATE TABLE IF NOT EXISTS conversation_notes (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    author_id TEXT NOT NULL DEFAULT '',
                    author_role TEXT NOT NULL DEFAULT 'admin',
                    content TEXT NOT NULL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
                CREATE INDEX IF NOT EXISTS idx_conversation_notes_scope
                    ON conversation_notes(conversation_id, tenant_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS answer_revisions (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    original_content TEXT NOT NULL,
                    revised_content TEXT NOT NULL,
                    correction_note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(message_id, version),
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY (message_id) REFERENCES messages(id)
                );
                CREATE INDEX IF NOT EXISTS idx_answer_revisions_scope
                    ON answer_revisions(conversation_id, message_id, tenant_id, version DESC);
                CREATE TABLE IF NOT EXISTS answer_revision_drafts (
                    id TEXT PRIMARY KEY,
                    conversation_id TEXT NOT NULL,
                    message_id INTEGER NOT NULL,
                    tenant_id TEXT NOT NULL,
                    original_content TEXT NOT NULL,
                    proposed_content TEXT NOT NULL,
                    correction_note TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending',
                    model TEXT NOT NULL DEFAULT '',
                    prompt_version INTEGER NOT NULL DEFAULT 0,
                    created_by TEXT NOT NULL DEFAULT '',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    reviewed_at TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id),
                    FOREIGN KEY (message_id) REFERENCES messages(id)
                );
                CREATE INDEX IF NOT EXISTS idx_answer_revision_drafts_scope
                    ON answer_revision_drafts(tenant_id, conversation_id, message_id, status, created_at DESC);
                CREATE TABLE IF NOT EXISTS workflow_definitions (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'draft',
                    current_version INTEGER NOT NULL DEFAULT 1,
                    published_version INTEGER,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (tenant_id) REFERENCES organizations(id),
                    FOREIGN KEY (agent_id) REFERENCES agents(id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_definitions_scope
                    ON workflow_definitions(tenant_id, agent_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS workflow_templates (
                    id TEXT PRIMARY KEY,
                    template_key TEXT NOT NULL,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    nodes_json TEXT NOT NULL DEFAULT '[]',
                    edges_json TEXT NOT NULL DEFAULT '[]',
                    visibility TEXT NOT NULL DEFAULT 'private',
                    tenant_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'active',
                    source_template_key TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(template_key, version, tenant_id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_templates_scope
                    ON workflow_templates(tenant_id, visibility, status, updated_at DESC);
                CREATE TABLE IF NOT EXISTS workflow_versions (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    nodes_json TEXT NOT NULL DEFAULT '[]',
                    edges_json TEXT NOT NULL DEFAULT '[]',
                    change_note TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(workflow_id, version),
                    FOREIGN KEY (workflow_id) REFERENCES workflow_definitions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_versions_scope
                    ON workflow_versions(workflow_id, version DESC);
                CREATE TABLE IF NOT EXISTS workflow_runs (
                    id TEXT PRIMARY KEY,
                    workflow_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'running',
                    input_json TEXT NOT NULL DEFAULT '{}',
                    output_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL DEFAULT '',
                    error TEXT NOT NULL DEFAULT '',
                    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP,
                    FOREIGN KEY (workflow_id) REFERENCES workflow_definitions(id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_runs_scope
                    ON workflow_runs(tenant_id, agent_id, started_at DESC);
                CREATE TABLE IF NOT EXISTS workflow_run_traces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    node_id TEXT NOT NULL,
                    node_type TEXT NOT NULL,
                    sequence INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    input_summary TEXT NOT NULL DEFAULT '',
                    output_summary TEXT NOT NULL DEFAULT '',
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(run_id, sequence),
                    FOREIGN KEY (run_id) REFERENCES workflow_runs(id)
                );
                CREATE INDEX IF NOT EXISTS idx_workflow_trace_run
                    ON workflow_run_traces(run_id, sequence);
                CREATE TABLE IF NOT EXISTS knowledge_graph_entities (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL DEFAULT '',
                    entity_type TEXT NOT NULL,
                    name TEXT NOT NULL,
                    normalized_name TEXT NOT NULL,
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    source_document_id TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id, knowledge_base_id, entity_type, normalized_name)
                );
                CREATE INDEX IF NOT EXISTS idx_graph_entities_scope
                    ON knowledge_graph_entities(tenant_id, knowledge_base_id, status, entity_type);
                CREATE TABLE IF NOT EXISTS knowledge_graph_relations (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL DEFAULT '',
                    subject_id TEXT NOT NULL,
                    predicate TEXT NOT NULL,
                    object_id TEXT NOT NULL,
                    properties_json TEXT NOT NULL DEFAULT '{}',
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    source_document_id TEXT NOT NULL DEFAULT '',
                    confidence REAL NOT NULL DEFAULT 0.5,
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(tenant_id, knowledge_base_id, subject_id, predicate, object_id)
                );
                CREATE INDEX IF NOT EXISTS idx_graph_relations_scope
                    ON knowledge_graph_relations(tenant_id, knowledge_base_id, status, predicate);
                CREATE TABLE IF NOT EXISTS knowledge_graph_extraction_runs (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    knowledge_base_id TEXT NOT NULL DEFAULT '',
                    document_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'pending_review',
                    entity_count INTEGER NOT NULL DEFAULT 0,
                    relation_count INTEGER NOT NULL DEFAULT 0,
                    error TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    finished_at TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_graph_runs_scope
                    ON knowledge_graph_extraction_runs(tenant_id, knowledge_base_id, created_at DESC);
                CREATE TABLE IF NOT EXISTS system_monitoring_events (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    severity TEXT NOT NULL DEFAULT 'P2',
                    module TEXT NOT NULL DEFAULT '',
                    metrics_json TEXT NOT NULL DEFAULT '{}',
                    trace_id TEXT NOT NULL DEFAULT '',
                    detail TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_monitoring_events_scope
                    ON system_monitoring_events(tenant_id, severity, created_at DESC);
                CREATE TABLE IF NOT EXISTS monitoring_issue_reports (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    event_id TEXT NOT NULL,
                    state TEXT NOT NULL DEFAULT 'pending_review',
                    report_json TEXT NOT NULL DEFAULT '{}',
                    created_by TEXT NOT NULL DEFAULT '',
                    reviewed_by TEXT NOT NULL DEFAULT '',
                    review_note TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_monitoring_reports_scope
                    ON monitoring_issue_reports(tenant_id, state, updated_at DESC);
                CREATE TABLE IF NOT EXISTS monitoring_report_verifications (
                    id TEXT PRIMARY KEY,
                    tenant_id TEXT NOT NULL,
                    report_id TEXT NOT NULL,
                    result TEXT NOT NULL,
                    checks_json TEXT NOT NULL DEFAULT '[]',
                    note TEXT NOT NULL DEFAULT '',
                    executed_by TEXT NOT NULL DEFAULT '',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_monitoring_verifications_scope
                    ON monitoring_report_verifications(tenant_id, report_id, created_at DESC);
            """)
            # New databases now have the evaluation tables; old databases get
            # the same scope fields through the idempotent migration below.
            for table, column, definition in (
                ("agent_eval_cases", "tenant_id", "TEXT NOT NULL DEFAULT 'local-default'"),
                ("agent_eval_runs", "tenant_id", "TEXT NOT NULL DEFAULT 'local-default'"),
                ("agent_eval_runs", "agent_id", "TEXT NOT NULL DEFAULT ''"),
                ("agent_eval_runs", "requested_by", "TEXT NOT NULL DEFAULT ''"),
                ("agent_eval_results", "tenant_id", "TEXT NOT NULL DEFAULT 'local-default'"),
                ("agent_eval_results", "agent_id", "TEXT NOT NULL DEFAULT ''"),
            ):
                try:
                    conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
                except sqlite3.OperationalError:
                    pass
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_runs_tenant ON agent_eval_runs(tenant_id, started_at DESC)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_eval_results_tenant ON agent_eval_results(tenant_id, run_id, id)")
            self._ensure_default_workspace(conn)
            self._ensure_workflow_templates(conn)
            self._ensure_default_model_pricing(conn)

    @staticmethod
    def _ensure_default_workspace(conn) -> None:
        """Seed the legacy local workspace used when authentication is not enabled."""
        conn.execute(
            "INSERT OR IGNORE INTO organizations (id, name) VALUES ('local-default', '安枢平台工作区')"
        )
        conn.execute("UPDATE organizations SET name='安枢平台工作区' WHERE id='local-default' AND name='本地默认工作区'")
        conn.execute("""
            INSERT OR IGNORE INTO users (id, tenant_id, email, display_name, password_hash, role)
            VALUES ('local-owner', 'local-default', 'local-owner@localhost', '本地管理员', '', 'platform_admin')
        """)
        conn.execute("""
            INSERT OR IGNORE INTO agents (id, tenant_id, name)
            VALUES ('default-agent', 'local-default', '安枢默认 Agent')
        """)

    @staticmethod
    def _ensure_default_model_pricing(conn) -> None:
        """Seed one editable default so cost analysis is not empty on first run."""
        conn.execute("""
            INSERT OR IGNORE INTO model_pricing
            (id, provider, model, input_price_per_million, output_price_per_million,
             quality_score, enabled, notes, updated_by)
            VALUES ('price-default-deepseek-v4-flash', 'DeepSeek', 'deepseek-v4-flash',
                    0.27, 1.10, 0.78, 1,
                    '内置参考价；上线前请按供应商合同价复核。', 'system')
        """)

    @staticmethod
    def _ensure_workflow_templates(conn) -> None:
        """Seed platform templates without overwriting administrator changes."""
        from workflow_engine import workflow_templates
        for item in workflow_templates():
            conn.execute("""INSERT OR IGNORE INTO workflow_templates
                (id, template_key, name, description, nodes_json, edges_json, visibility,
                 source_template_key, version, created_by)
                VALUES (?, ?, ?, ?, ?, ?, 'platform', ?, 1, 'system')""", (
                "builtin-" + str(item["key"]), str(item["key"]), str(item.get("name") or ""),
                str(item.get("description") or ""), json.dumps(item.get("nodes") or [], ensure_ascii=False),
                json.dumps(item.get("edges") or [], ensure_ascii=False), str(item["key"]),
            ))

    # ==================== P6-F Monitoring and review-first reports ====================

    def get_operations_overview(self, tenant_id: str = "local-default") -> dict:
        """Aggregate actionable management work from existing governed tables."""
        tenant_id = str(tenant_id or "local-default")

        def count(sql: str, params: tuple = ()) -> int:
            try:
                with sqlite3.connect(self._db_path) as conn:
                    row = conn.execute(sql, params).fetchone()
                return int((row or [0])[0] or 0)
            except sqlite3.Error:
                return 0

        return {
            "tenant_id": tenant_id,
            "items": {
                "unread_notifications": self.notification_summary(tenant_id).get("unread", 0),
                "pending_invitations": count("SELECT COUNT(*) FROM workspace_invitations WHERE tenant_id=? AND status IN ('pending','accepted_pending')", (tenant_id,)),
                "documents_pending_review": count("SELECT COUNT(*) FROM documents WHERE (tenant_id=? OR tenant_id='') AND lifecycle_status IN ('review','staged')", (tenant_id,)),
                "graph_entities_pending_review": count("SELECT COUNT(*) FROM knowledge_graph_entities WHERE tenant_id=? AND status='pending_review'", (tenant_id,)),
                "graph_relations_pending_review": count("SELECT COUNT(*) FROM knowledge_graph_relations WHERE tenant_id=? AND status='pending_review'", (tenant_id,)),
                "knowledge_gaps_open": count("SELECT COUNT(*) FROM knowledge_gaps WHERE status='open'"),
                "monitoring_reports_pending": count("SELECT COUNT(*) FROM monitoring_issue_reports WHERE tenant_id=? AND state IN ('pending_review','review_plan','pending_approval')", (tenant_id,)),
                "extensions_pending_review": count("SELECT COUNT(*) FROM capability_extensions WHERE status='pending_review'"),
                "extensions_pending_approval": count("SELECT COUNT(*) FROM capability_extension_grants WHERE tenant_id=? AND enabled=0 AND approved_by=''", (tenant_id,)),
                "agent_eval_failed_runs": count("SELECT COUNT(*) FROM agent_eval_runs WHERE status='error'"),
            },
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    def global_admin_search(self, query: str, tenant_id: str = "", include_platform_assets: bool = False) -> list[dict]:
        """Search governed resource metadata only; never search sensitive bodies."""
        query = str(query or "").strip()[:120]
        if not query:
            return []
        like = f"%{query}%"
        scope = " AND tenant_id=?" if tenant_id else ""
        suffix = (tenant_id,) if tenant_id else ()
        results = []

        with sqlite3.connect(self._db_path) as conn:
            for r in conn.execute("SELECT id, tenant_id, email, display_name, role, status FROM users WHERE (email LIKE ? OR display_name LIKE ? OR id LIKE ?)" + scope + " LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"user","id":r[0],"tenant_id":r[1],"title":r[3] or r[2],"detail":f"{r[2]} · {r[4]} · {r[5]}","tab":"workspaces"})
            for r in conn.execute("SELECT id, tenant_id, name, status FROM agents WHERE (name LIKE ? OR id LIKE ?)" + scope + " LIMIT 20", (like, like, *suffix)).fetchall():
                results.append({"type":"agent","id":r[0],"tenant_id":r[1],"title":r[2],"detail":r[3],"tab":"workspaces"})
            for r in conn.execute("SELECT id, tenant_id, source_name, profile, lifecycle_status FROM documents WHERE (source_name LIKE ? OR id LIKE ? OR profile LIKE ?)" + scope + " ORDER BY updated_at DESC LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"document","id":r[0],"tenant_id":r[1],"title":r[2],"detail":f"{r[3]} · {r[4]}","tab":"documents"})
            for r in conn.execute("SELECT id, tenant_id, name, profile, status FROM knowledge_bases WHERE (name LIKE ? OR id LIKE ? OR profile LIKE ?)" + scope + " ORDER BY updated_at DESC LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"knowledge_base","id":r[0],"tenant_id":r[1],"title":r[2],"detail":f"{r[3]} · {r[4]}","tab":"documents"})
            for r in conn.execute("SELECT id, tenant_id, title, updated_at FROM conversations WHERE (id LIKE ? OR title LIKE ?)" + scope + " ORDER BY updated_at DESC LIMIT 20", (like, like, *suffix)).fetchall():
                results.append({"type":"conversation","id":r[0],"tenant_id":r[1],"title":r[2] or r[0],"detail":f"会话 · {r[3] or ''}","tab":"conversations","deep_link":f"/admin#conversations?conversation_id={r[0]}"})
            for r in conn.execute("SELECT run_id, tenant_id, profile, status, started_at FROM agent_eval_runs WHERE (run_id LIKE ? OR profile LIKE ? OR status LIKE ?)" + scope + " ORDER BY started_at DESC LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"agent_eval","id":r[0],"tenant_id":r[1],"title":r[0],"detail":f"{r[2]} · {r[3]} · {r[4] or ''}","tab":"agentEval","deep_link":f"/admin#agentEval?run_id={r[0]}"})
            for r in conn.execute("SELECT id, tenant_id, canonical_question, profile, status, updated_at FROM knowledge_gaps WHERE (canonical_question LIKE ? OR profile LIKE ? OR status LIKE ?)" + scope + " ORDER BY updated_at DESC LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"knowledge_gap","id":str(r[0]),"tenant_id":r[1],"title":r[2][:120],"detail":f"{r[3]} · {r[4]} · {r[5] or ''}","tab":"knowledgeGaps"})
            for r in conn.execute("SELECT id, tenant_id, name, status, updated_at FROM workflow_definitions WHERE (id LIKE ? OR name LIKE ? OR status LIKE ?)" + scope + " ORDER BY updated_at DESC LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"workflow","id":r[0],"tenant_id":r[1],"title":r[2],"detail":f"{r[3]} · {r[4] or ''}","tab":"workflows","deep_link":f"/admin#workflows?workflow_id={r[0]}"})
            for r in conn.execute("SELECT id, tenant_id, state, created_at FROM monitoring_issue_reports WHERE (id LIKE ? OR state LIKE ?)" + scope + " ORDER BY updated_at DESC LIMIT 20", (like, like, *suffix)).fetchall():
                results.append({"type":"monitoring_report","id":r[0],"tenant_id":r[1],"title":f"监控预案 {r[0]}","detail":f"{r[2]} · {r[3] or ''}","tab":"monitoring"})
            for r in conn.execute("SELECT id, trace_id, tenant_id, source_id, status, created_at FROM external_retrieval_events WHERE (trace_id LIKE ? OR source_id LIKE ? OR status LIKE ?)" + scope + " ORDER BY created_at DESC LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"trace","id":str(r[0]),"tenant_id":r[2],"title":r[1] or f"trace-{r[0]}","detail":f"{r[3]} · {r[4]} · {r[5] or ''}","tab":"monitoring"})
            if include_platform_assets:
                for r in conn.execute("SELECT slot, name, model_role, enabled, updated_at FROM prompt_assets WHERE (slot LIKE ? OR name LIKE ? OR model_role LIKE ?) ORDER BY updated_at DESC LIMIT 20", (like, like, like)).fetchall():
                    results.append({"type":"prompt_asset","id":r[0],"tenant_id":"platform","title":r[1],"detail":f"{r[2]} · {'enabled' if r[3] else 'disabled'} · {r[4] or ''}","tab":"promptAssets"})
            for r in conn.execute("SELECT id, tenant_id, action, resource_type, resource_id, created_at FROM audit_logs WHERE (action LIKE ? OR resource_type LIKE ? OR resource_id LIKE ?)" + scope + " ORDER BY created_at DESC LIMIT 20", (like, like, like, *suffix)).fetchall():
                results.append({"type":"audit","id":str(r[0]),"tenant_id":r[1],"title":r[2],"detail":f"{r[3]}:{r[4]} · {r[5] or ''}","tab":"workspaces"})
        return results[:100]

    @staticmethod
    def _monitoring_json(value, fallback):
        try:
            parsed = json.loads(value or json.dumps(fallback, ensure_ascii=False))
            return parsed if isinstance(parsed, type(fallback)) else fallback
        except (TypeError, ValueError):
            return fallback

    def create_monitoring_event(self, tenant_id: str, event_type: str, severity: str,
                                module: str = "", metrics: dict | None = None,
                                trace_id: str = "", detail: str = "") -> dict:
        if severity not in {"P0", "P1", "P2", "P3"}:
            raise ValueError("监控事件级别不合法")
        event_id = "me-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO system_monitoring_events
                (id, tenant_id, event_type, severity, module, metrics_json, trace_id, detail)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (
                    event_id, tenant_id, str(event_type or "unknown")[:120], severity,
                    str(module or "")[:120], json.dumps(metrics or {}, ensure_ascii=False),
                    str(trace_id or "")[:160], str(detail or "")[:1000],
                ))
        return self.get_monitoring_event(event_id, tenant_id) or {}

    def get_monitoring_event(self, event_id: str, tenant_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT id, tenant_id, event_type, severity, module,
                metrics_json, trace_id, detail, created_at FROM system_monitoring_events
                WHERE id=? AND tenant_id=?""", (event_id, tenant_id)).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "tenant_id", "event_type", "severity", "module",
                         "metrics_json", "trace_id", "detail", "created_at"), row))
        item["metrics"] = self._monitoring_json(item.pop("metrics_json"), {})
        item["created_at"] = _to_utc_iso(item["created_at"])
        return item

    def list_monitoring_events(self, tenant_id: str, severity: str = "", limit: int = 100) -> list[dict]:
        sql = "SELECT id FROM system_monitoring_events WHERE tenant_id=?"
        params: list[object] = [tenant_id]
        if severity:
            sql += " AND severity=?"; params.append(severity)
        sql += " ORDER BY created_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 500)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self.get_monitoring_event(row[0], tenant_id))]

    def summarize_monitoring_events(self, tenant_id: str, limit: int = 1000) -> dict:
        items = self.list_monitoring_events(tenant_id, limit=limit)
        by_severity = {level: 0 for level in ("P0", "P1", "P2", "P3")}
        by_type: dict[str, int] = {}
        for item in items:
            level = str(item.get("severity") or "P2")
            if level in by_severity:
                by_severity[level] += 1
            event_type = str(item.get("event_type") or "unknown")
            by_type[event_type] = by_type.get(event_type, 0) + 1
        return {
            "total": len(items),
            "by_severity": by_severity,
            "by_type": dict(sorted(by_type.items(), key=lambda pair: (-pair[1], pair[0]))[:20]),
            "latest_at": items[0].get("created_at", "") if items else "",
        }

    def create_monitoring_issue_report(self, tenant_id: str, event_id: str,
                                       report: dict, created_by: str = "system") -> dict:
        if not self.get_monitoring_event(event_id, tenant_id):
            raise ValueError("监控事件不存在或不属于当前工作区")
        report_id = "mir-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO monitoring_issue_reports
                (id, tenant_id, event_id, report_json, created_by)
                VALUES (?, ?, ?, ?, ?)""", (
                    report_id, tenant_id, event_id, json.dumps(report or {}, ensure_ascii=False), created_by,
                ))
        self.log_audit(tenant_id, created_by, "", "monitoring.report.create", "issue_report", report_id, {"event_id": event_id})
        return self.get_monitoring_issue_report(report_id, tenant_id) or {}

    def get_monitoring_issue_report(self, report_id: str, tenant_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT id, tenant_id, event_id, state, report_json,
                created_by, reviewed_by, review_note, created_at, updated_at
                FROM monitoring_issue_reports WHERE id=? AND tenant_id=?""", (report_id, tenant_id)).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "tenant_id", "event_id", "state", "report_json", "created_by",
                         "reviewed_by", "review_note", "created_at", "updated_at"), row))
        item["report"] = self._monitoring_json(item.pop("report_json"), {})
        item["created_at"] = _to_utc_iso(item["created_at"])
        item["updated_at"] = _to_utc_iso(item["updated_at"])
        return item

    def list_monitoring_issue_reports(self, tenant_id: str, state: str = "", limit: int = 100) -> list[dict]:
        sql = "SELECT id FROM monitoring_issue_reports WHERE tenant_id=?"
        params: list[object] = [tenant_id]
        if state:
            sql += " AND state=?"; params.append(state)
        sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 500)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self.get_monitoring_issue_report(row[0], tenant_id))]

    def update_monitoring_issue_report(self, report_id: str, tenant_id: str, state: str,
                                       changed_by: str = "admin", review_note: str = "") -> dict:
        if state not in {"pending_review", "review_plan", "pending_approval", "approved", "rejected"}:
            raise ValueError("问题报告状态不合法")
        current = self.get_monitoring_issue_report(report_id, tenant_id)
        if not current:
            raise ValueError("问题报告不存在或不属于当前工作区")
        allowed_transitions = {
            "pending_review": {"review_plan", "rejected"},
            "review_plan": {"pending_review", "pending_approval", "rejected"},
            "pending_approval": {"review_plan", "approved", "rejected"},
            "approved": {"review_plan"},
            "rejected": {"review_plan"},
        }
        if state != current["state"] and state not in allowed_transitions.get(current["state"], set()):
            raise ValueError(f"不允许从 {current['state']} 直接变更为 {state}")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""UPDATE monitoring_issue_reports SET state=?, reviewed_by=?,
                review_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?""",
                (state, changed_by, str(review_note or "")[:1000], report_id, tenant_id))
        if not cur.rowcount:
            raise ValueError("问题报告不存在或不属于当前工作区")
        self.log_audit(tenant_id, changed_by, "", "monitoring.report.state.update", "issue_report", report_id, {"state": state})
        return self.get_monitoring_issue_report(report_id, tenant_id) or {}

    def update_monitoring_issue_report_content(self, report_id: str, tenant_id: str,
                                               report: dict, changed_by: str = "admin") -> dict:
        current = self.get_monitoring_issue_report(report_id, tenant_id)
        if not current:
            raise ValueError("问题报告不存在或不属于当前工作区")
        if not isinstance(report, dict):
            raise ValueError("问题报告内容必须是对象")
        merged = dict(current.get("report") or {})
        for key in ("title", "symptom", "root_cause_hypothesis", "recommendation", "impact", "change_plan", "diagnosis"):
            if key in report:
                merged[key] = report[key]
        change_plan = merged.get("change_plan")
        if not isinstance(change_plan, dict):
            raise ValueError("变更预案必须是对象")
        for key in ("change", "rollback", "validation"):
            if key in change_plan:
                change_plan[key] = str(change_plan[key] or "")[:4000]
        merged["change_plan"] = change_plan
        merged["review_only"] = True
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""UPDATE monitoring_issue_reports SET report_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=?""", (json.dumps(merged, ensure_ascii=False), report_id, tenant_id))
        self.log_audit(tenant_id, changed_by, "", "monitoring.report.content.update", "issue_report", report_id, {"review_only": True})
        return self.get_monitoring_issue_report(report_id, tenant_id) or {}

    def create_monitoring_report_verification(self, report_id: str, tenant_id: str,
                                              result: str, checks: list | None = None,
                                              note: str = "", executed_by: str = "admin") -> dict:
        if result not in {"passed", "failed", "blocked"}:
            raise ValueError("验证结果必须是 passed、failed 或 blocked")
        report = self.get_monitoring_issue_report(report_id, tenant_id)
        if not report:
            raise ValueError("问题报告不存在或不属于当前工作区")
        if report["state"] != "approved":
            raise ValueError("只有已通过的问题报告才能提交验证结果")
        normalized_checks = []
        for item in checks or []:
            if not isinstance(item, dict):
                continue
            normalized_checks.append({
                "name": str(item.get("name") or "")[:200],
                "status": str(item.get("status") or "")[:40],
                "detail": str(item.get("detail") or "")[:1000],
            })
        verification_id = "mv-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO monitoring_report_verifications
                (id, tenant_id, report_id, result, checks_json, note, executed_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (
                    verification_id, tenant_id, report_id, result,
                    json.dumps(normalized_checks, ensure_ascii=False), str(note or "")[:2000], executed_by,
                ))
        self.log_audit(tenant_id, executed_by, "", "monitoring.report.verification.create",
                       "issue_report", report_id, {"verification_id": verification_id, "result": result})
        return self.get_monitoring_report_verification(verification_id, tenant_id) or {}

    def get_monitoring_report_verification(self, verification_id: str, tenant_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("""SELECT id, tenant_id, report_id, result, checks_json,
                note, executed_by, created_at FROM monitoring_report_verifications
                WHERE id=? AND tenant_id=?""", (verification_id, tenant_id)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["checks"] = self._monitoring_json(item.pop("checks_json"), [])
        return item

    def list_monitoring_report_verifications(self, report_id: str, tenant_id: str,
                                             limit: int = 50) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""SELECT id FROM monitoring_report_verifications
                WHERE report_id=? AND tenant_id=? ORDER BY created_at DESC LIMIT ?""",
                (report_id, tenant_id, max(1, min(int(limit), 100)))).fetchall()
        return [item for row in rows if (item := self.get_monitoring_report_verification(row[0], tenant_id))]

    def create_conversation(
        self,
        title: str = "新对话",
        category: str = "user",
        tenant_id: str = "local-default",
        user_id: str = "local-owner",
        agent_id: str = "default-agent",
        knowledge_base_id: str = "",
    ) -> dict:
        conv_id = str(uuid.uuid4())[:8]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO conversations (id, title, category, tenant_id, user_id, agent_id, knowledge_base_id)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (conv_id, title, category, tenant_id, user_id, agent_id, knowledge_base_id),
            )
            conn.execute(
                "INSERT INTO session_memory (conversation_id, memory_data) VALUES (?, '{}')",
                (conv_id,),
            )
        return {
            "id": conv_id, "title": title, "category": category,
            "tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id,
            "knowledge_base_id": knowledge_base_id,
        }

    def get_conversation_knowledge_base(self, conversation_id: str) -> str:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT knowledge_base_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return str(row[0] or "") if row else ""

    @staticmethod
    def normalize_cache_query(query: str) -> str:
        return " ".join(str(query or "").strip().lower().split())

    def get_semantic_cache(self, cache_key: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT normalized_query, answer, sources_json, model, knowledge_base_id,
                       profile_scope, prompt_version, hit_count, expires_at
                FROM semantic_cache WHERE cache_key=? AND expires_at > CURRENT_TIMESTAMP
            """, (cache_key,)).fetchone()
            if not row:
                conn.execute("DELETE FROM semantic_cache WHERE cache_key=?", (cache_key,))
                return None
            conn.execute("""
                UPDATE semantic_cache SET hit_count=hit_count+1, last_hit_at=CURRENT_TIMESTAMP
                WHERE cache_key=?
            """, (cache_key,))
        try:
            sources = json.loads(row[2] or "[]")
        except (TypeError, json.JSONDecodeError):
            sources = []
        return {"normalized_query": row[0], "answer": row[1], "sources": sources,
                "model": row[3], "knowledge_base_id": row[4], "profile_scope": row[5],
                "prompt_version": row[6], "hit_count": int(row[7] or 0) + 1,
                "expires_at": row[8]}

    def put_semantic_cache(self, cache_key: str, normalized_query: str, answer: str,
                           sources: list, model: str, knowledge_base_id: str = "",
                           profile_scope: str = "", prompt_version: str = "",
                           ttl_seconds: int = 3600) -> None:
        ttl = max(60, min(int(ttl_seconds), 7 * 24 * 3600))
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO semantic_cache (
                    cache_key, normalized_query, answer, sources_json, model,
                    knowledge_base_id, profile_scope, prompt_version, expires_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, datetime('now', ?))
                ON CONFLICT(cache_key) DO UPDATE SET
                    answer=excluded.answer, sources_json=excluded.sources_json,
                    model=excluded.model, knowledge_base_id=excluded.knowledge_base_id,
                    profile_scope=excluded.profile_scope, prompt_version=excluded.prompt_version,
                    expires_at=excluded.expires_at, hit_count=0, last_hit_at=NULL
            """, (cache_key, normalized_query, str(answer or ""),
                  json.dumps(sources or [], ensure_ascii=False), str(model or ""),
                  str(knowledge_base_id or ""), str(profile_scope or ""),
                  str(prompt_version or ""), f"+{ttl} seconds"))

    def clear_semantic_cache(self, knowledge_base_id: str | None = None) -> int:
        with sqlite3.connect(self._db_path) as conn:
            if knowledge_base_id:
                cur = conn.execute("DELETE FROM semantic_cache WHERE knowledge_base_id=?", (knowledge_base_id,))
            else:
                cur = conn.execute("DELETE FROM semantic_cache")
        return cur.rowcount

    def semantic_cache_stats(self) -> dict:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(hit_count), 0)
                FROM semantic_cache WHERE expires_at > CURRENT_TIMESTAMP
            """).fetchone()
        return {"entries": int(row[0] or 0), "hits": int(row[1] or 0)}

    @staticmethod
    def _password_hash(password: str, salt: str | None = None) -> str:
        salt = salt or secrets.token_hex(16)
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 310_000)
        return f"pbkdf2_sha256$310000${salt}${digest.hex()}"

    @staticmethod
    def _validate_account_password(password: str, field_name: str = "密码") -> None:
        value = str(password or "")
        has_upper = any("A" <= char <= "Z" for char in value)
        has_lower = any("a" <= char <= "z" for char in value)
        has_digit = any("0" <= char <= "9" for char in value)
        if len(value) < 8 or not (has_upper and has_lower and has_digit):
            raise ValueError(f"{field_name}至少需要 8 位，且包含大写字母、小写字母和数字")

    @staticmethod
    def _normalize_account_email(email: str, field_name: str = "邮箱") -> str:
        """Normalize and validate complete account email addresses."""
        value = str(email or "").strip().lower()
        valid = (
            len(value) <= 254
            and "*" not in value
            and not any(char.isspace() for char in value)
            and re.fullmatch(
                r"[^@<>()\[\]\\,;:\s\"]+@[^@<>()\[\]\\,;:\s\"]+\.[^@<>()\[\]\\,;:\s\".]+",
                value,
            )
        )
        if not valid:
            raise ValueError(f"{field_name}格式不正确，请输入完整邮箱地址")
        return value

    @staticmethod
    def _password_matches(password: str, encoded: str) -> bool:
        try:
            scheme, rounds, salt, expected = encoded.split("$", 3)
            if scheme != "pbkdf2_sha256":
                return False
            actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), int(rounds)).hex()
            return hmac.compare_digest(actual, expected)
        except (TypeError, ValueError):
            return False

    def register_user(self, email: str, password: str, display_name: str = "") -> dict:
        email = self._normalize_account_email(email)
        display_name = str(display_name or "").strip()[:80]
        self._validate_account_password(password)
        tenant_id = "org-" + uuid.uuid4().hex[:12]
        user_id = "usr-" + uuid.uuid4().hex[:12]
        agent_id = "agt-" + uuid.uuid4().hex[:12]
        organization_name = f"{display_name or email.split('@')[0]} 的工作区"
        with sqlite3.connect(self._db_path) as conn:
            try:
                conn.execute("INSERT INTO organizations (id, name) VALUES (?, ?)", (tenant_id, organization_name))
                conn.execute("""
                    INSERT INTO users (id, tenant_id, email, display_name, password_hash, role)
                    VALUES (?, ?, ?, ?, ?, 'org_admin')
                """, (user_id, tenant_id, email, display_name, self._password_hash(password)))
                conn.execute("INSERT INTO agents (id, tenant_id, name) VALUES (?, ?, ?)",
                             (agent_id, tenant_id, "安枢默认 Agent"))
                conn.execute("""
                    INSERT INTO organization_memberships (tenant_id, user_id, role)
                    VALUES (?, ?, 'org_admin')
                """, (tenant_id, user_id))
                conn.execute("""
                    INSERT INTO agent_memberships (tenant_id, agent_id, user_id, role)
                    VALUES (?, ?, ?, 'org_admin')
                """, (tenant_id, agent_id, user_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该邮箱已注册") from exc
        self.log_audit(tenant_id, user_id, agent_id, "user.register", "user", user_id, {"email": email})
        return {"id": user_id, "tenant_id": tenant_id, "agent_id": agent_id, "email": email,
                "display_name": display_name, "role": "org_admin"}

    def submit_user_registration(self, email: str, password: str, display_name: str = "") -> dict:
        """Create a self-registration application that remains unusable until approval."""
        email = self._normalize_account_email(email)
        display_name = str(display_name or "").strip()[:80]
        self._validate_account_password(password)
        tenant_id = "org-" + uuid.uuid4().hex[:12]
        user_id = "usr-" + uuid.uuid4().hex[:12]
        agent_id = "agt-" + uuid.uuid4().hex[:12]
        with sqlite3.connect(self._db_path) as conn:
            try:
                conn.execute("INSERT INTO organizations (id, name) VALUES (?, ?)",
                             (tenant_id, f"{display_name or email.split('@')[0]} 的工作区"))
                conn.execute("""INSERT INTO users
                    (id, tenant_id, email, display_name, password_hash, role, status)
                    VALUES (?, ?, ?, ?, ?, 'org_admin', 'pending_approval')""",
                             (user_id, tenant_id, email, display_name, self._password_hash(password)))
                conn.execute("INSERT INTO agents (id, tenant_id, name) VALUES (?, ?, ?)",
                             (agent_id, tenant_id, "安枢默认 Agent"))
                conn.execute("""INSERT INTO organization_memberships
                    (tenant_id, user_id, role, status) VALUES (?, ?, 'org_admin', 'disabled')""",
                             (tenant_id, user_id))
                conn.execute("""INSERT INTO agent_memberships
                    (tenant_id, agent_id, user_id, role, status)
                    VALUES (?, ?, ?, 'org_admin', 'disabled')""", (tenant_id, agent_id, user_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该邮箱已注册或已有待审核申请") from exc
        self.log_audit(tenant_id, user_id, agent_id, "user.registration.submit", "user", user_id,
                       {"email": email})
        return {"id": user_id, "tenant_id": tenant_id, "agent_id": agent_id, "email": email,
                "display_name": display_name, "role": "org_admin", "status": "pending_approval"}

    def list_pending_user_registrations(self) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""SELECT u.id, u.tenant_id, u.email, u.display_name,
                u.role, u.created_at, o.name FROM users u JOIN organizations o ON o.id=u.tenant_id
                WHERE u.status='pending_approval' ORDER BY u.created_at DESC""").fetchall()
        return [{"id": r[0], "tenant_id": r[1], "email": r[2], "display_name": r[3],
                 "role": r[4], "created_at": _to_utc_iso(r[5]), "workspace_name": r[6]}
                for r in rows]

    def approve_user_registration(self, user_id: str, approved_by: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT tenant_id, email, display_name, role FROM users WHERE id=? AND status='pending_approval'", (user_id,)).fetchone()
            if not row:
                return None
            tenant_id, email, display_name, role = row
            conn.execute("UPDATE users SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=?", (user_id,))
            conn.execute("UPDATE organization_memberships SET status='active' WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
            conn.execute("UPDATE agent_memberships SET status='active' WHERE tenant_id=? AND user_id=?", (tenant_id, user_id))
        self.log_audit(tenant_id, approved_by, "", "user.registration.approve", "user", user_id, {"email": email})
        return {"id": user_id, "tenant_id": tenant_id, "email": email, "display_name": display_name, "role": role, "status": "active"}

    def reject_user_registration(self, user_id: str, rejected_by: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT tenant_id, email FROM users WHERE id=? AND status='pending_approval'", (user_id,)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE users SET status='deactivated', updated_at=CURRENT_TIMESTAMP WHERE id=?", (user_id,))
        self.log_audit(row[0], rejected_by, "", "user.registration.reject", "user", user_id, {"email": row[1]})
        return True

    def platform_admin_setup_required(self) -> bool:
        """Return whether the built-in platform administrator has not been activated yet."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT 1 FROM users
                WHERE role='platform_admin' AND status='active' AND password_hash<>''
                LIMIT 1
            """).fetchone()
        return row is None

    def bootstrap_platform_admin(self, email: str, password: str, display_name: str = "") -> dict:
        """Activate the one built-in local administrator during guarded first-run setup."""
        email = self._normalize_account_email(email)
        display_name = str(display_name or "").strip()[:80]
        self._validate_account_password(password)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            configured = conn.execute("""
                SELECT 1 FROM users
                WHERE role='platform_admin' AND status='active' AND password_hash<>''
                LIMIT 1
            """).fetchone()
            if configured:
                raise ValueError("平台管理员已初始化")
            duplicate = conn.execute(
                "SELECT id FROM users WHERE email=? AND id<>'local-owner'", (email,),
            ).fetchone()
            if duplicate:
                raise ValueError("该邮箱已注册，不能用于首次平台管理员初始化")
            conn.execute("""
                UPDATE users
                SET email=?, display_name=?, password_hash=?, role='platform_admin', status='active',
                    updated_at=CURRENT_TIMESTAMP
                WHERE id='local-owner' AND tenant_id='local-default'
            """, (email, display_name, self._password_hash(password)))
            conn.execute("""
                INSERT OR IGNORE INTO organization_memberships (tenant_id, user_id, role)
                VALUES ('local-default', 'local-owner', 'platform_admin')
            """)
            conn.execute("""
                INSERT OR IGNORE INTO agent_memberships (tenant_id, agent_id, user_id, role)
                VALUES ('local-default', 'default-agent', 'local-owner', 'platform_admin')
            """)
        self.log_audit("local-default", "local-owner", "default-agent", "platform.bootstrap",
                       "user", "local-owner", {"email": email})
        return {"id": "local-owner", "tenant_id": "local-default", "agent_id": "default-agent",
                "email": email, "display_name": display_name, "role": "platform_admin"}

    def authenticate_user(self, email: str, password: str) -> dict | None:
        email = str(email or "").strip().lower()
        with sqlite3.connect(self._db_path) as conn:
            locked = conn.execute("""
                SELECT 1 FROM login_attempts WHERE email=? AND locked_until > CURRENT_TIMESTAMP
            """, (email,)).fetchone()
        if locked:
            return None
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT u.id, u.tenant_id, u.email, u.display_name, u.password_hash,
                       m.role, a.id, m.tenant_id
                FROM users u
                JOIN organization_memberships m ON m.user_id=u.id AND m.status='active'
                JOIN agent_memberships am ON am.user_id=u.id AND am.tenant_id=m.tenant_id
                    AND am.status='active'
                JOIN agents a ON a.id=am.agent_id AND a.tenant_id=m.tenant_id
                    AND a.status='active'
                WHERE u.email=? AND u.status='active'
                ORDER BY CASE WHEN m.tenant_id=u.tenant_id THEN 0 ELSE 1 END,
                         m.created_at, a.created_at LIMIT 1
            """, (email,)).fetchone()
        if not row or not row[4] or not self._password_matches(password or "", row[4]):
            self._record_login_failure(email)
            return None
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM login_attempts WHERE email=?", (email,))
        return {"id": row[0], "tenant_id": row[7], "email": row[2], "display_name": row[3],
                "role": row[5], "agent_id": row[6]}

    def record_login_event(self, email: str, success: bool, reason: str = "") -> None:
        """Record authentication outcomes without storing passwords or raw credentials."""
        email = str(email or "").strip().lower()[:254]
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT id, tenant_id FROM users WHERE email=?""", (email,)).fetchone()
        tenant_id, user_id = (row[1], row[0]) if row else ("local-default", "")
        self.log_audit(
            tenant_id, user_id, "",
            "auth.login.success" if success else "auth.login.failure",
            "auth", email,
            {"reason": str(reason or "")[:120]} if reason else {},
        )

    def _record_login_failure(self, email: str) -> None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT failed_count, window_started_at, locked_until FROM login_attempts WHERE email=?", (email,),
            ).fetchone()
            if not row:
                conn.execute("INSERT INTO login_attempts (email, failed_count) VALUES (?, 1)", (email,))
                return
            if row[1] and conn.execute(
                "SELECT datetime(?) <= datetime('now', '-15 minutes')", (row[1],)
            ).fetchone()[0]:
                conn.execute("UPDATE login_attempts SET failed_count=1, window_started_at=CURRENT_TIMESTAMP, locked_until=NULL, updated_at=CURRENT_TIMESTAMP WHERE email=?", (email,))
                return
            failed_count = int(row[0]) + 1
            conn.execute("""
                UPDATE login_attempts
                SET failed_count=?, locked_until=CASE WHEN ? >= 5 THEN datetime('now', '+15 minutes') ELSE NULL END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE email=?
            """, (failed_count, failed_count, email))

    def list_login_locks(self, include_expired: bool = False) -> list[dict]:
        sql = """SELECT l.email, l.failed_count, l.window_started_at, l.locked_until,
                         l.updated_at, u.id, u.tenant_id, u.display_name, u.status
                  FROM login_attempts l LEFT JOIN users u ON u.email=l.email"""
        if not include_expired:
            sql += " WHERE l.locked_until IS NOT NULL AND l.locked_until > CURRENT_TIMESTAMP"
        sql += " ORDER BY l.locked_until DESC, l.updated_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql).fetchall()
        return [{"email": r[0], "failed_count": r[1], "window_started_at": _to_utc_iso(r[2]),
                 "locked_until": _to_utc_iso(r[3]) if r[3] else "", "updated_at": _to_utc_iso(r[4]),
                 "user_id": r[5] or "", "tenant_id": r[6] or "", "display_name": r[7] or "",
                 "user_status": r[8] or "unknown"} for r in rows]

    def unlock_login(self, email: str, unlocked_by: str) -> bool:
        email = str(email or "").strip().lower()
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("DELETE FROM login_attempts WHERE email=?", (email,))
            row = conn.execute("SELECT tenant_id, id FROM users WHERE email=?", (email,)).fetchone()
        if cur.rowcount:
            tenant_id, user_id = row if row else ("local-default", "")
            self.log_audit(tenant_id, unlocked_by, "", "auth.login.unlock", "auth", email)
        return bool(cur.rowcount)

    def change_password(self, user_id: str, current_password: str, new_password: str) -> bool:
        self._validate_account_password(new_password, "新密码")
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT password_hash, tenant_id FROM users WHERE id=? AND status='active'", (user_id,)).fetchone()
            if not row or not self._password_matches(current_password or "", row[0]):
                return False
            conn.execute("UPDATE users SET password_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                         (self._password_hash(new_password), user_id))
            conn.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked_at IS NULL", (user_id,))
        self.log_audit(row[1], user_id, action="user.password.change", resource_type="user", resource_id=user_id)
        return True

    def request_account_deletion(self, user_id: str, reason: str = "", days: int = 14) -> None:
        days = max(7, min(int(days), 90))
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT tenant_id FROM users WHERE id=?", (user_id,)).fetchone()
            if not row:
                raise ValueError("用户不存在")
            conn.execute("""
                INSERT INTO account_deletion_requests (user_id, purge_after, reason)
                VALUES (?, datetime('now', ?), ?)
                ON CONFLICT(user_id) DO UPDATE SET purge_after=excluded.purge_after,
                    reason=excluded.reason, status='pending', requested_at=CURRENT_TIMESTAMP
            """, (user_id, f"+{days} days", str(reason or "")[:500]))
            conn.execute("UPDATE users SET status='deactivated', updated_at=CURRENT_TIMESTAMP WHERE id=?", (user_id,))
            conn.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND revoked_at IS NULL", (user_id,))
        self.log_audit(row[0], user_id, action="user.deletion.request", resource_type="user", resource_id=user_id,
                       detail={"purge_after_days": days})

    def export_user_data(self, tenant_id: str, user_id: str) -> dict:
        return {
            "conversations": self.get_conversations(tenant_id=tenant_id, user_id=user_id, agent_id=None),
            "long_term_memories": self.list_long_term_memories(tenant_id, user_id, agent_id=None),
        }

    def list_account_deletion_requests(self, tenant_id: str, status: str = "pending") -> list[dict]:
        sql = """SELECT d.user_id, u.email, u.display_name, u.status, d.requested_at,
                         d.purge_after, d.status, d.reason
                  FROM account_deletion_requests d JOIN users u ON u.id=d.user_id
                  WHERE u.tenant_id=?"""
        params: list[object] = [tenant_id]
        if status:
            sql += " AND d.status=?"
            params.append(status)
        sql += " ORDER BY d.requested_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"user_id": row[0], "email": row[1], "display_name": row[2], "user_status": row[3],
                 "requested_at": self._utc_to_local(row[4] or ""),
                 "purge_after": self._utc_to_local(row[5] or ""), "status": row[6], "reason": row[7] or ""}
                for row in rows]

    def cancel_account_deletion(self, tenant_id: str, user_id: str, reviewed_by: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT status FROM account_deletion_requests
                                WHERE user_id=? AND status='pending'
                                  AND EXISTS (SELECT 1 FROM users WHERE id=? AND tenant_id=?)""",
                              (user_id, user_id, tenant_id)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE account_deletion_requests SET status='cancelled' WHERE user_id=?", (user_id,))
            conn.execute("UPDATE users SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                         (user_id, tenant_id))
        self.log_audit(tenant_id, reviewed_by, action="user.deletion.cancel", resource_type="user", resource_id=user_id)
        return True

    def reset_user_password(self, tenant_id: str, user_id: str, new_password: str, reset_by: str) -> bool:
        self._validate_account_password(new_password, "新密码")
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT email FROM users WHERE id=? AND tenant_id=? AND status!='deactivated'", (user_id, tenant_id)).fetchone()
            if not row:
                return False
            conn.execute("UPDATE users SET password_hash=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                         (self._password_hash(new_password), user_id, tenant_id))
            conn.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND tenant_id=? AND revoked_at IS NULL", (user_id, tenant_id))
            # A password reset is an administrative recovery action. Clear the
            # old failed-login lock so the newly issued credential can be used.
            conn.execute("DELETE FROM login_attempts WHERE email=?", (row[0],))
        self.log_audit(tenant_id, reset_by, "", "user.password.reset", "user", user_id, {"email": row[0]})
        return True

    def purge_due_deleted_accounts(self) -> list[dict]:
        """Purge due personal data and return private document IDs for index cleanup."""
        with sqlite3.connect(self._db_path) as conn:
            due = conn.execute("""
                SELECT d.user_id, u.tenant_id FROM account_deletion_requests d
                JOIN users u ON u.id=d.user_id
                WHERE d.status='pending' AND d.purge_after <= CURRENT_TIMESTAMP
            """).fetchall()
            results = []
            for user_id, tenant_id in due:
                document_ids = [row[0] for row in conn.execute("""
                    SELECT id FROM documents WHERE owner_user_id=? AND visibility='private'
                """, (user_id,)).fetchall()]
                conversation_ids = [row[0] for row in conn.execute(
                    "SELECT id FROM conversations WHERE user_id=?", (user_id,)
                ).fetchall()]
                for conversation_id in conversation_ids:
                    conn.execute("DELETE FROM messages WHERE conversation_id=?", (conversation_id,))
                    conn.execute("DELETE FROM session_memory WHERE conversation_id=?", (conversation_id,))
                conn.execute("DELETE FROM conversations WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM long_term_memories WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM user_memory_preferences WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM auth_sessions WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM agent_memberships WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM organization_memberships WHERE user_id=?", (user_id,))
                conn.execute("UPDATE documents SET status='purge_pending', updated_at=CURRENT_TIMESTAMP WHERE owner_user_id=?", (user_id,))
                conn.execute("UPDATE account_deletion_requests SET status='purged' WHERE user_id=?", (user_id,))
                conn.execute("DELETE FROM users WHERE id=?", (user_id,))
                results.append({"tenant_id": tenant_id, "user_id": user_id, "document_ids": document_ids})
        for item in results:
            self.log_audit(item["tenant_id"], item["user_id"], action="user.deletion.purge",
                           resource_type="user", resource_id=item["user_id"],
                           detail={"private_documents": len(item["document_ids"])})
        return results

    def list_workspaces(self) -> list[dict]:
        """Return the platform-admin workspace inventory without credentials."""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT o.id, o.name, o.status, o.created_at,
                       COUNT(DISTINCT CASE WHEN om.status='active' THEN om.user_id END),
                       COUNT(DISTINCT CASE WHEN a.status='active' THEN a.id END),
                       COUNT(DISTINCT CASE WHEN kb.status='active' THEN kb.id END),
                       COALESCE((SELECT u.display_name FROM organization_memberships om2
                                 JOIN users u ON u.id=om2.user_id
                                 WHERE om2.tenant_id=o.id AND om2.status='active'
                                   AND om2.role IN ('platform_admin','org_admin')
                                 ORDER BY CASE WHEN om2.role='platform_admin' THEN 0 ELSE 1 END,
                                          om2.created_at LIMIT 1), '')
                FROM organizations o
                LEFT JOIN organization_memberships om ON om.tenant_id=o.id AND om.status='active'
                LEFT JOIN agents a ON a.tenant_id=o.id AND a.status='active'
                LEFT JOIN knowledge_bases kb ON kb.tenant_id=o.id AND kb.status='active'
                GROUP BY o.id
                ORDER BY o.created_at DESC
            """).fetchall()
            users = conn.execute("""
                SELECT om.tenant_id, u.id, u.email, u.display_name, om.role, om.status, om.created_at
                FROM organization_memberships om JOIN users u ON u.id=om.user_id
                ORDER BY om.created_at DESC
            """).fetchall()
            memberships = conn.execute("""
                SELECT tenant_id, agent_id, user_id, role, status
                FROM agent_memberships
            """).fetchall()
            agents = conn.execute("""
                SELECT id, tenant_id, name, status, created_at
                FROM agents ORDER BY created_at ASC
            """).fetchall()

        by_tenant = {
            row[0]: {
                "id": row[0], "name": row[1], "status": row[2],
                "created_at": _to_utc_iso(row[3]), "user_count": row[4],
                "agent_count": row[5], "knowledge_base_count": row[6],
                "owner_name": row[7] or ("平台管理员" if row[0] == "local-default" else "未设置负责人"),
                "users": [], "agents": [],
            }
            for row in rows
        }
        for row in users:
            if row[0] in by_tenant:
                by_tenant[row[0]]["users"].append({
                    "id": row[1], "email": row[2], "display_name": row[3],
                    "role": row[4], "status": row[5], "created_at": _to_utc_iso(row[6]),
                    "agent_memberships": [],
                })
        for row in agents:
            if row[1] in by_tenant:
                by_tenant[row[1]]["agents"].append({
                    "id": row[0], "name": row[2], "status": row[3],
                    "created_at": _to_utc_iso(row[4]),
                })
        users_by_id = {
            user["id"]: user
            for workspace in by_tenant.values()
            for user in workspace["users"]
        }
        for tenant_id, agent_id, user_id, role, status in memberships:
            user = users_by_id.get(user_id)
            if user and tenant_id in by_tenant:
                user["agent_memberships"].append({
                    "agent_id": agent_id, "role": role, "status": status,
                })
        return list(by_tenant.values())

    def create_organization(self, name: str, created_by: str = "") -> dict:
        """Create a formal organization with its own default Agent."""
        name = str(name or "").strip()[:120]
        if not name:
            raise ValueError("组织名称不能为空")
        tenant_id = "org-" + uuid.uuid4().hex[:12]
        agent_id = "agt-" + uuid.uuid4().hex[:12]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("INSERT INTO organizations (id, name) VALUES (?, ?)", (tenant_id, name))
            conn.execute("INSERT INTO agents (id, tenant_id, name) VALUES (?, ?, ?)",
                         (agent_id, tenant_id, "安枢默认 Agent"))
        self.log_audit(tenant_id, created_by, "", "organization.create", "organization", tenant_id,
                       {"name": name})
        return {"id": tenant_id, "name": name, "status": "active", "agent_id": agent_id,
                "user_count": 0, "agent_count": 1, "knowledge_base_count": 0,
                "owner_name": "未设置负责人"}

    def list_permission_catalog(self) -> list[dict]:
        from identity import ROLE_PERMISSIONS, CUSTOM_ROLE_DENIED_PERMISSIONS
        permissions = sorted({permission for values in ROLE_PERMISSIONS.values() for permission in values})
        return [{"key": permission, "label": permission.replace(".", " / ")} for permission in permissions]

    def list_custom_roles(self, tenant_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""SELECT id, tenant_id, name, description, permissions_json,
                status, created_by, created_at, updated_at FROM custom_roles
                WHERE tenant_id=? ORDER BY status, name""", (tenant_id,)).fetchall()
        items = []
        for row in rows:
            item = dict(zip(("id", "tenant_id", "name", "description", "permissions_json",
                             "status", "created_by", "created_at", "updated_at"), row))
            item["permissions"] = self._workflow_json(item.pop("permissions_json"), [])
            item["created_at"] = _to_utc_iso(item["created_at"])
            item["updated_at"] = _to_utc_iso(item["updated_at"])
            items.append(item)
        return items

    def get_custom_role(self, role_id: str, tenant_id: str) -> dict | None:
        return next((item for item in self.list_custom_roles(tenant_id) if item["id"] == str(role_id)), None)

    def get_custom_role_permissions(self, role_id: str, tenant_id: str) -> list[str]:
        item = self.get_custom_role(role_id, tenant_id)
        return list(item.get("permissions") or []) if item and item.get("status") == "active" else []

    def save_custom_role(self, tenant_id: str, name: str, description: str, permissions: list,
                         changed_by: str = "", role_id: str = "") -> dict:
        from identity import ROLE_PERMISSIONS, CUSTOM_ROLE_DENIED_PERMISSIONS
        name = str(name or "").strip()[:80]
        if not name:
            raise ValueError("角色名称不能为空")
        if name in ROLE_PERMISSIONS:
            raise ValueError("不能覆盖系统角色，请使用自定义角色名称")
        if not isinstance(permissions, list) or not all(isinstance(item, str) and item.strip() for item in permissions):
            raise ValueError("角色权限必须是非空字符串列表")
        allowed = {item["key"] for item in self.list_permission_catalog()}
        normalized = sorted({item.strip() for item in permissions})
        denied = sorted(set(normalized) & CUSTOM_ROLE_DENIED_PERMISSIONS)
        if denied:
            raise ValueError("自定义角色不能授予平台安全权限: " + ", ".join(denied))
        unknown = [item for item in normalized if item not in allowed]
        if unknown:
            raise ValueError("包含未注册权限: " + ", ".join(unknown))
        role_id = str(role_id or "").strip() or "role-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            duplicate = conn.execute("SELECT id FROM custom_roles WHERE tenant_id=? AND name=? AND id<>?",
                                     (tenant_id, name, role_id)).fetchone()
            if duplicate:
                raise ValueError("当前工作区已存在同名角色")
            if conn.execute("SELECT 1 FROM custom_roles WHERE id=? AND tenant_id=?", (role_id, tenant_id)).fetchone():
                conn.execute("""UPDATE custom_roles SET name=?, description=?, permissions_json=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?""",
                    (name, str(description or "")[:1000], json.dumps(normalized, ensure_ascii=False), role_id, tenant_id))
            else:
                conn.execute("""INSERT INTO custom_roles
                    (id, tenant_id, name, description, permissions_json, created_by)
                    VALUES (?, ?, ?, ?, ?, ?)""",
                    (role_id, tenant_id, name, str(description or "")[:1000], json.dumps(normalized, ensure_ascii=False), changed_by))
        self.log_audit(tenant_id, changed_by, "", "role.save", "custom_role", role_id,
                       {"name": name, "permissions": normalized})
        return self.get_custom_role(role_id, tenant_id) or {}

    def set_custom_role_status(self, role_id: str, tenant_id: str, status: str, changed_by: str = "") -> bool:
        if status not in {"active", "disabled"}:
            raise ValueError("角色状态仅支持 active 或 disabled")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("UPDATE custom_roles SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                               (status, role_id, tenant_id))
        if cur.rowcount:
            self.log_audit(tenant_id, changed_by, "", "role.status", "custom_role", role_id, {"status": status})
        return bool(cur.rowcount)

    def assign_custom_role(self, tenant_id: str, user_id: str, role_id: str, changed_by: str = "") -> bool:
        role = self.get_custom_role(role_id, tenant_id)
        if not role or role.get("status") != "active":
            raise ValueError("自定义角色不存在或已停用")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE organization_memberships SET role=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE tenant_id=? AND user_id=? AND status='active'",
                               (role_id, tenant_id, user_id))
            if cur.rowcount:
                conn.execute("UPDATE users SET role=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                             (role_id, user_id, tenant_id))
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP "
                    "WHERE tenant_id=? AND user_id=? AND revoked_at IS NULL", (tenant_id, user_id)
                )
        if cur.rowcount:
            self.log_audit(tenant_id, changed_by, "", "role.assign", "user", user_id, {"role_id": role_id})
        return bool(cur.rowcount)

    def create_agent(self, tenant_id: str, name: str) -> dict:
        name = str(name or "").strip()[:80]
        if not name:
            raise ValueError("Agent 名称不能为空")
        with sqlite3.connect(self._db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM organizations WHERE id=? AND status='active'", (tenant_id,),
            ).fetchone()
            if not exists:
                raise ValueError("工作区不存在或已停用")
            agent_id = "agt-" + uuid.uuid4().hex[:12]
            conn.execute(
                "INSERT INTO agents (id, tenant_id, name) VALUES (?, ?, ?)",
                (agent_id, tenant_id, name),
            )
            conn.execute("""
                INSERT OR IGNORE INTO agent_memberships (tenant_id, agent_id, user_id, role)
                SELECT om.tenant_id, ?, om.user_id, 'agent_admin'
                FROM organization_memberships om JOIN users u ON u.id=om.user_id
                WHERE om.tenant_id=? AND om.status='active' AND u.status='active'
                  AND om.role IN ('org_admin', 'platform_admin')
            """, (agent_id, tenant_id))
        self.log_audit(tenant_id, action="agent.create", resource_type="agent",
                       resource_id=agent_id, detail={"name": name})
        return {"id": agent_id, "tenant_id": tenant_id, "name": name, "status": "active"}

    def update_agent_status(self, tenant_id: str, agent_id: str, status: str) -> bool:
        if status not in {"active", "disabled"}:
            raise ValueError("Agent 状态仅支持 active 或 disabled")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE agents SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                (status, agent_id, tenant_id),
            )
        if cur.rowcount:
            self.log_audit(tenant_id, action="agent.status.update", resource_type="agent",
                           resource_id=agent_id, detail={"status": status})
        return cur.rowcount > 0

    def update_user_status(self, tenant_id: str, user_id: str, status: str) -> bool:
        if status not in {"active", "suspended", "deactivated"}:
            raise ValueError("用户状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE users SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                (status, user_id, tenant_id),
            )
            if status != "active":
                conn.execute(
                    "UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE user_id=? AND tenant_id=? AND revoked_at IS NULL",
                    (user_id, tenant_id),
                )
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, action="user.status.update", resource_type="user",
                           resource_id=user_id, detail={"status": status})
        return cur.rowcount > 0

    def update_workspace_member_status(self, tenant_id: str, user_id: str, status: str,
                                       return_revoked_count: bool = False) -> bool | int | None:
        """Disable membership in one workspace without disabling the user's other workspaces."""
        if status not in {"active", "disabled"}:
            raise ValueError("成员状态仅支持 active 或 disabled")
        revoked_count = 0
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE organization_memberships SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=? AND user_id=?
            """, (status, tenant_id, user_id))
            if status != "active":
                revoked_cur = conn.execute("""
                    UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=? AND user_id=? AND revoked_at IS NULL
                """, (tenant_id, user_id))
                revoked_count = max(0, int(revoked_cur.rowcount))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, action="workspace.member.status.update",
                           resource_type="user", resource_id=user_id,
                           detail={"status": status, "revoked_sessions": revoked_count})
        if not cur.rowcount:
            return None
        return revoked_count if return_revoked_count else True

    def create_workspace_user(self, tenant_id: str, email: str, password: str = "",
                              display_name: str = "", role: str = "user",
                              agent_id: str = "", created_by: str = "") -> dict:
        """Create a credentialed user directly inside an existing workspace.

        A blank password is allowed only for administrator provisioning; a strong
        one-time password is generated and returned once to the caller.
        """
        email = self._normalize_account_email(email)
        display_name = str(display_name or "").strip()[:80]
        role = str(role or "user").strip()
        if role not in {"user", "org_admin"}:
            raise ValueError("成员角色仅支持 user 或 org_admin")
        generated_password = ""
        if not password:
            generated_password = "Secure-" + secrets.token_urlsafe(8) + "9A"
            password = generated_password
        self._validate_account_password(password, "初始密码")
        user_id = "usr-" + uuid.uuid4().hex[:12]
        with sqlite3.connect(self._db_path) as conn:
            if not conn.execute("SELECT 1 FROM organizations WHERE id=? AND status='active'", (tenant_id,)).fetchone():
                raise ValueError("工作区不存在或已停用")
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise ValueError("该邮箱已注册")
            conn.execute("""
                INSERT INTO users (id, tenant_id, email, display_name, password_hash, role)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (user_id, tenant_id, email, display_name, self._password_hash(password), role))
            conn.execute("""
                INSERT INTO organization_memberships (tenant_id, user_id, role, status)
                VALUES (?, ?, ?, 'active')
            """, (tenant_id, user_id, role))
            if agent_id:
                agent = conn.execute(
                    "SELECT id FROM agents WHERE id=? AND tenant_id=? AND status='active'",
                    (agent_id, tenant_id),
                ).fetchone()
                if not agent:
                    raise ValueError("Agent 不存在或不属于当前工作区")
                agent_ids = [agent_id]
            else:
                agent_ids = [row[0] for row in conn.execute(
                    "SELECT id FROM agents WHERE tenant_id=? AND status='active'", (tenant_id,)
                ).fetchall()]
                if role != "org_admin":
                    agent_ids = agent_ids[:1]
            if not agent_ids:
                raise ValueError("当前工作区没有可用 Agent")
            for current_agent_id in agent_ids:
                conn.execute("""
                    INSERT INTO agent_memberships (tenant_id, agent_id, user_id, role, status)
                    VALUES (?, ?, ?, ?, 'active')
                """, (tenant_id, current_agent_id, user_id, "agent_admin" if role == "org_admin" else "user"))
        self.log_audit(tenant_id, created_by, "", "workspace.user.create", "user", user_id,
                       {"email": email, "role": role, "agent_id": agent_id or "all_active"})
        result = {"id": user_id, "tenant_id": tenant_id, "email": email,
                  "display_name": display_name, "role": role, "status": "active",
                  "agent_id": agent_ids[0] if len(agent_ids) == 1 else ""}
        if generated_password:
            result["temporary_password"] = generated_password
        return result

    def add_workspace_member(self, tenant_id: str, email: str, role: str = "user") -> dict:
        email = self._normalize_account_email(email)
        role = str(role or "user").strip()
        if role not in {"user", "org_admin"}:
            raise ValueError("成员角色仅支持 user 或 org_admin")
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT id, email, display_name, status FROM users WHERE email=?", (email,)
            ).fetchone()
            if not row:
                raise ValueError("该邮箱尚未注册，请先完成账号注册")
            if row[3] != "active":
                raise ValueError("该用户账号未激活")
            if not conn.execute(
                "SELECT 1 FROM organizations WHERE id=? AND status='active'", (tenant_id,)
            ).fetchone():
                raise ValueError("工作区不存在或已停用")
            conn.execute("""
                INSERT INTO organization_memberships (tenant_id, user_id, role, status)
                VALUES (?, ?, ?, 'active')
                ON CONFLICT(tenant_id, user_id) DO UPDATE SET
                    role=excluded.role, status='active', updated_at=CURRENT_TIMESTAMP
            """, (tenant_id, row[0], role))
            if role == "org_admin":
                for (agent_id,) in conn.execute(
                    "SELECT id FROM agents WHERE tenant_id=? AND status='active'", (tenant_id,)
                ).fetchall():
                    conn.execute("""
                        INSERT OR IGNORE INTO agent_memberships (tenant_id, agent_id, user_id, role)
                        VALUES (?, ?, ?, 'agent_admin')
                    """, (tenant_id, agent_id, row[0]))
        self.log_audit(tenant_id, row[0], action="workspace.member.add", resource_type="user",
                       resource_id=row[0], detail={"email": email, "role": role})
        return {"id": row[0], "email": row[1], "display_name": row[2], "role": role, "status": "active"}

    def update_membership(self, tenant_id: str, user_id: str, role: str = "user",
                          status: str = "active") -> bool:
        if role not in {"user", "org_admin"}:
            raise ValueError("成员角色仅支持 user 或 org_admin")
        if status not in {"active", "disabled"}:
            raise ValueError("成员状态仅支持 active 或 disabled")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE organization_memberships
                SET role=?, status=?, updated_at=CURRENT_TIMESTAMP
                WHERE tenant_id=? AND user_id=?
            """, (role, status, tenant_id, user_id))
            if cur.rowcount and role == "org_admin" and status == "active":
                conn.execute("""
                    INSERT OR IGNORE INTO agent_memberships (tenant_id, agent_id, user_id, role)
                    SELECT tenant_id, id, ?, 'agent_admin' FROM agents
                    WHERE tenant_id=? AND status='active'
                """, (user_id, tenant_id))
            if status != "active":
                conn.execute("""
                    UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP
                    WHERE tenant_id=? AND user_id=? AND revoked_at IS NULL
                """, (tenant_id, user_id))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, action="workspace.member.update", resource_type="user",
                           resource_id=user_id, detail={"role": role, "status": status})
        return cur.rowcount > 0

    def update_agent_membership(self, tenant_id: str, agent_id: str, user_id: str,
                                status: str = "active", role: str = "user") -> bool:
        if status not in {"active", "disabled"}:
            raise ValueError("Agent 授权状态仅支持 active 或 disabled")
        if role not in {"user", "agent_admin"}:
            raise ValueError("Agent 授权角色仅支持 user 或 agent_admin")
        with sqlite3.connect(self._db_path) as conn:
            exists = conn.execute("""
                SELECT 1 FROM agents a JOIN users u ON u.id=?
                JOIN organization_memberships om ON om.tenant_id=a.tenant_id AND om.user_id=u.id
                WHERE a.id=? AND a.tenant_id=? AND a.status='active'
                  AND u.status='active' AND om.status='active'
            """, (user_id, agent_id, tenant_id)).fetchone()
            if not exists:
                return False
            cur = conn.execute("""
                INSERT INTO agent_memberships (tenant_id, agent_id, user_id, role, status)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, user_id) DO UPDATE SET
                    role=excluded.role, status=excluded.status, updated_at=CURRENT_TIMESTAMP
            """, (tenant_id, agent_id, user_id, role, status))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, agent_id, "agent.membership.update", "agent", agent_id,
                           {"status": status, "role": role})
        return cur.rowcount > 0

    def create_auth_session(self, user: dict, days: int = 7, ip_address: str = "", user_agent: str = "") -> str:
        token = secrets.token_urlsafe(40)
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        session_id = "ses-" + uuid.uuid4().hex[:16]
        expires_at = (datetime.now(timezone.utc) + timedelta(days=max(1, min(days, 30)))).strftime("%Y-%m-%d %H:%M:%S")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO auth_sessions
                    (id, token_hash, user_id, tenant_id, agent_id, expires_at, ip_address, user_agent, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            """, (session_id, token_hash, user["id"], user["tenant_id"], user["agent_id"], expires_at,
                  str(ip_address or "")[:128], str(user_agent or "")[:500]))
        self.log_audit(user["tenant_id"], user["id"], user["agent_id"], "session.create", "session", session_id)
        return token

    def create_knowledge_base(self, tenant_id: str, name: str, description: str = "",
                               profile: str = "general", visibility: str = "tenant",
                               owner_user_id: str = "", knowledge_base_id: str | None = None) -> dict:
        """Create a logical knowledge base; documents remain indexed in shared stores."""
        if visibility not in {"public", "tenant", "private"}:
            raise ValueError("知识库可见范围不合法")
        name = str(name or "").strip()[:120]
        if not name:
            raise ValueError("知识库名称不能为空")
        kb_id = knowledge_base_id or "kb-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            try:
                conn.execute("""
                    INSERT INTO knowledge_bases
                        (id, tenant_id, name, description, profile, visibility, owner_user_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (kb_id, tenant_id, name, str(description or "")[:500],
                      str(profile or "general")[:120], visibility, owner_user_id))
            except sqlite3.IntegrityError as exc:
                raise ValueError("知识库名称已存在或归属工作区无效") from exc
        self.log_audit(tenant_id, owner_user_id or "local-owner", "", "knowledge_base.create",
                       "knowledge_base", kb_id, {"name": name, "profile": profile})
        return self.get_knowledge_base(kb_id, tenant_id) or {"id": kb_id, "tenant_id": tenant_id, "name": name}

    @staticmethod
    def default_retrieval_config() -> dict:
        return {
            "top_k": 10, "candidate_multiplier": 5, "rrf_k": 60,
            "vector_weight": 1.0, "bm25_weight": 1.0,
            "use_hybrid": True, "use_rerank": True, "use_parent": True,
            "chunk_size": 300, "chunk_overlap": 50,
        }

    @classmethod
    def validate_retrieval_config(cls, config: dict | None) -> dict:
        merged = cls.default_retrieval_config()
        if isinstance(config, dict):
            merged.update(config)
        try:
            merged["top_k"] = max(1, min(int(merged["top_k"]), 50))
            merged["candidate_multiplier"] = max(1, min(int(merged["candidate_multiplier"]), 20))
            merged["rrf_k"] = max(1, min(int(merged["rrf_k"]), 200))
            merged["chunk_size"] = max(100, min(int(merged["chunk_size"]), 2000))
            merged["chunk_overlap"] = max(0, min(int(merged["chunk_overlap"]), 500))
        except (TypeError, ValueError) as exc:
            raise ValueError("检索整数参数格式不正确") from exc
        if merged["chunk_overlap"] >= merged["chunk_size"]:
            raise ValueError("切片重叠必须小于切片大小")
        for key in ("vector_weight", "bm25_weight"):
            try:
                merged[key] = max(0.0, min(float(merged[key]), 10.0))
            except (TypeError, ValueError) as exc:
                raise ValueError(f"{key} 必须是数字") from exc
        if merged["vector_weight"] == 0 and merged["bm25_weight"] == 0:
            raise ValueError("向量权重和 BM25 权重不能同时为 0")
        for key in ("use_hybrid", "use_rerank", "use_parent"):
            merged[key] = bool(merged[key])
        return merged

    def get_knowledge_base_retrieval_config(self, knowledge_base_id: str,
                                             tenant_id: str | None = None) -> dict | None:
        kb = self.get_knowledge_base(knowledge_base_id, tenant_id)
        if not kb:
            return None
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT version, config_json, changed_by, change_reason, created_at
                FROM knowledge_base_retrieval_configs
                WHERE knowledge_base_id=? AND status='active'
                ORDER BY version DESC LIMIT 1
            """, (knowledge_base_id,)).fetchone()
        if not row:
            return {"knowledge_base_id": knowledge_base_id, "version": 0,
                    "config": self.default_retrieval_config(), "changed_by": "system",
                    "change_reason": "默认配置", "created_at": ""}
        try:
            config = self.validate_retrieval_config(json.loads(row[1] or "{}"))
        except (TypeError, json.JSONDecodeError, ValueError):
            config = self.default_retrieval_config()
        return {"knowledge_base_id": knowledge_base_id, "version": row[0], "config": config,
                "changed_by": row[2], "change_reason": row[3], "created_at": row[4]}

    def update_knowledge_base_retrieval_config(self, knowledge_base_id: str, config: dict,
                                               tenant_id: str | None = None,
                                               changed_by: str = "admin",
                                               change_reason: str = "") -> dict:
        kb = self.get_knowledge_base(knowledge_base_id, tenant_id)
        if not kb:
            raise ValueError("知识库不存在")
        normalized = self.validate_retrieval_config(config)
        current = self.get_knowledge_base_retrieval_config(knowledge_base_id, tenant_id)
        version = int(current.get("version", 0)) + 1
        old = current.get("config", self.default_retrieval_config())
        rebuild_required = any(old.get(key) != normalized.get(key) for key in ("chunk_size", "chunk_overlap"))
        normalized["rebuild_required"] = rebuild_required
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("UPDATE knowledge_base_retrieval_configs SET status='archived' WHERE knowledge_base_id=?", (knowledge_base_id,))
            conn.execute("""
                INSERT INTO knowledge_base_retrieval_configs
                    (knowledge_base_id, version, config_json, status, changed_by, change_reason)
                VALUES (?, ?, ?, 'active', ?, ?)
            """, (knowledge_base_id, version, json.dumps(normalized, ensure_ascii=False),
                  str(changed_by or "admin")[:120], str(change_reason or "")[:500]))
        self.log_audit(tenant_id or kb["tenant_id"], changed_by, "",
                       "knowledge_base.retrieval_config.update", "knowledge_base",
                       knowledge_base_id, {"version": version, "rebuild_required": rebuild_required})
        return self.get_knowledge_base_retrieval_config(knowledge_base_id, tenant_id) or {}

    def list_knowledge_base_retrieval_config_versions(self, knowledge_base_id: str,
                                                      tenant_id: str | None = None,
                                                      limit: int = 20) -> list[dict]:
        if not self.get_knowledge_base(knowledge_base_id, tenant_id):
            return []
        limit = max(1, min(int(limit), 100))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT version, config_json, status, changed_by, change_reason, created_at
                FROM knowledge_base_retrieval_configs
                WHERE knowledge_base_id=? ORDER BY version DESC LIMIT ?
            """, (knowledge_base_id, limit)).fetchall()
        result = []
        for row in rows:
            try:
                config = self.validate_retrieval_config(json.loads(row[1] or "{}"))
            except (TypeError, json.JSONDecodeError, ValueError):
                config = self.default_retrieval_config()
            result.append({"version": row[0], "config": config, "status": row[2],
                           "changed_by": row[3], "change_reason": row[4], "created_at": row[5]})
        return result

    def create_ingestion_job(self, job_id: str, tenant_id: str, user_id: str,
                             agent_id: str, documents: list[dict]) -> dict:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO ingestion_jobs (id, tenant_id, user_id, agent_id, total_files)
                VALUES (?, ?, ?, ?, ?)
            """, (job_id, tenant_id, user_id, agent_id, len(documents)))
            for item in documents:
                conn.execute("""
                    INSERT INTO ingestion_job_items (job_id, document_id, source_name)
                    VALUES (?, ?, ?)
                """, (job_id, str(item.get("document_id") or ""),
                      str(item.get("source_name") or "")[:255]))
        return {"id": job_id, "total_files": len(documents), "status": "processing"}

    def update_ingestion_item(self, job_id: str, document_id: str, stage: str,
                              status: str, error: str = "") -> bool:
        allowed_stages = {"starting", "parsing", "cleaning", "incremental_index", "done"}
        allowed_statuses = {"pending", "processing", "completed", "failed"}
        if stage not in allowed_stages or status not in allowed_statuses:
            raise ValueError("入库管线状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE ingestion_job_items
                SET stage=?, status=?, error=?,
                    started_at=CASE WHEN started_at IS NULL AND status='processing'
                                    THEN CURRENT_TIMESTAMP ELSE started_at END,
                    finished_at=CASE WHEN status IN ('completed', 'failed')
                                     THEN CURRENT_TIMESTAMP ELSE finished_at END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE job_id=? AND document_id=?
            """, (stage, status, str(error or "")[:500], job_id, document_id))
        return cur.rowcount > 0

    def finish_ingestion_job(self, job_id: str, status: str, success_count: int,
                             fail_count: int, error: str = "") -> bool:
        if status not in {"completed", "error"}:
            raise ValueError("入库任务状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE ingestion_jobs
                SET status=?, success_count=?, fail_count=?, error=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (status, max(0, int(success_count)), max(0, int(fail_count)),
                  str(error or "")[:500], job_id))
        return cur.rowcount > 0

    def get_ingestion_job(self, job_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            job = conn.execute("""
                SELECT id, tenant_id, user_id, agent_id, status, total_files,
                       success_count, fail_count, error, created_at, updated_at
                FROM ingestion_jobs WHERE id=?
            """, (job_id,)).fetchone()
            if not job:
                return None
            items = conn.execute("""
                SELECT document_id, source_name, stage, status, error,
                       started_at, finished_at, updated_at
                FROM ingestion_job_items WHERE job_id=? ORDER BY id
            """, (job_id,)).fetchall()
        trace = self.list_ingestion_stage_events(job_id, job[1])
        by_document: dict[str, list[dict]] = {}
        for event in trace:
            by_document.setdefault(event["document_id"], []).append(event)
        return {"id": job[0], "tenant_id": job[1], "user_id": job[2], "agent_id": job[3],
                "status": job[4], "total_files": job[5], "success_count": job[6],
                "fail_count": job[7], "error": job[8], "created_at": job[9], "updated_at": job[10],
                "trace": trace,
                "items": [{"document_id": item[0], "source_name": item[1], "stage": item[2],
                           "status": item[3], "error": item[4], "started_at": item[5],
                           "finished_at": item[6], "updated_at": item[7],
                           "trace": by_document.get(item[0], [])} for item in items]}

    def list_ingestion_jobs(self, tenant_id: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        sql = "SELECT id FROM ingestion_jobs"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY updated_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self._db_path) as conn:
            ids = [row[0] for row in conn.execute(sql, params).fetchall()]
        return [item for job_id in ids if (item := self.get_ingestion_job(job_id))]

    def list_ingestion_failures(self, tenant_id: str | None = None, limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit), 200))
        sql = """
            SELECT j.id, j.tenant_id, j.status, j.updated_at,
                   i.document_id, i.source_name, i.stage, i.error, i.updated_at
            FROM ingestion_job_items i
            JOIN ingestion_jobs j ON j.id=i.job_id
            WHERE i.status='failed'
        """
        params: list[object] = []
        if tenant_id is not None:
            sql += " AND j.tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY i.updated_at DESC, i.id DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            error = str(row[7] or "")
            lower = error.lower()
            if "解析" in error or "parse" in lower:
                failure_type = "parsing"
            elif "清洗" in error or "clean" in lower:
                failure_type = "cleaning"
            elif "索引" in error or "index" in lower:
                failure_type = "incremental_index"
            elif "重复" in error or "duplicate" in lower:
                failure_type = "deduplication"
            else:
                failure_type = "unknown"
            items.append({
                "job_id": row[0], "tenant_id": row[1], "job_status": row[2],
                "job_updated_at": row[3], "document_id": row[4],
                "source_name": row[5], "stage": row[6], "error": error,
                "failure_type": failure_type, "updated_at": row[8],
            })
        return items

    def create_data_source(self, tenant_id: str, name: str, source_type: str,
                           endpoint: str = "", config: dict | None = None,
                           knowledge_base_id: str = "", owner_user_id: str = "",
                           sync_mode: str = "manual", schedule: str = "") -> dict:
        allowed = {"local_upload", "url", "web_directory", "database_readonly", "excel_csv"}
        if source_type not in allowed:
            raise ValueError("数据源类型不支持")
        if sync_mode not in {"manual", "scheduled"}:
            raise ValueError("同步模式不支持")
        name = str(name or "").strip()[:120]
        if not name:
            raise ValueError("数据源名称不能为空")
        validation = validate_data_source_config(source_type, endpoint, config)
        if not validation["valid"]:
            raise ValueError("；".join(validation["errors"]))
        if knowledge_base_id and not self.get_knowledge_base(knowledge_base_id, tenant_id):
            raise ValueError("数据源绑定的知识库不存在")
        source_id = "src-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO data_sources
                    (id, tenant_id, owner_user_id, name, source_type, endpoint,
                     config_json, knowledge_base_id, sync_mode, schedule)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (source_id, tenant_id, owner_user_id, name, source_type,
                  str(endpoint or "")[:1000], json.dumps(config or {}, ensure_ascii=False),
                  knowledge_base_id, sync_mode, str(schedule or "")[:120]))
        self.log_audit(tenant_id, owner_user_id or "local-owner", "", "data_source.create",
                       "data_source", source_id, {"source_type": source_type, "knowledge_base_id": knowledge_base_id})
        return self.get_data_source(source_id, tenant_id) or {"id": source_id, "name": name}

    def get_data_source(self, source_id: str, tenant_id: str | None = None) -> dict | None:
        sql = """
            SELECT id, tenant_id, owner_user_id, name, source_type, endpoint, config_json,
                   knowledge_base_id, sync_mode, schedule, status, last_synced_at,
                   last_content_hash, last_error, created_at, updated_at
            FROM data_sources WHERE id=?
        """
        params: list[object] = [source_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        try:
            config = json.loads(row[6] or "{}")
        except (TypeError, json.JSONDecodeError):
            config = {}
        return {"id": row[0], "tenant_id": row[1], "owner_user_id": row[2], "name": row[3],
                "source_type": row[4], "endpoint": row[5], "config": config,
                "knowledge_base_id": row[7], "sync_mode": row[8], "schedule": row[9],
                "status": row[10], "last_synced_at": row[11], "last_content_hash": row[12],
                "last_error": row[13], "created_at": row[14], "updated_at": row[15]}

    def list_data_sources(self, tenant_id: str | None = None) -> list[dict]:
        sql = "SELECT id FROM data_sources"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY updated_at DESC, id"
        with sqlite3.connect(self._db_path) as conn:
            ids = [r[0] for r in conn.execute(sql, params).fetchall()]
        return [item for source_id in ids if (item := self.get_data_source(source_id, tenant_id))]

    def update_data_source_status(self, source_id: str, status: str,
                                  tenant_id: str | None = None, error: str = "") -> bool:
        if status not in {"draft", "active", "paused", "error"}:
            raise ValueError("数据源状态不合法")
        sql = "UPDATE data_sources SET status=?, last_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"
        params: list[object] = [status, str(error or "")[:500], source_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
        return cur.rowcount > 0

    def mark_data_source_synced(self, source_id: str, content_hash: str,
                                tenant_id: str | None = None) -> bool:
        sql = """
            UPDATE data_sources
            SET status='active', last_synced_at=CURRENT_TIMESTAMP,
                last_content_hash=?, last_error='', updated_at=CURRENT_TIMESTAMP
            WHERE id=?
        """
        params: list[object] = [str(content_hash or "")[:128], source_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
        return cur.rowcount > 0

    def create_data_source_sync_run(self, source_id: str, tenant_id: str | None = None,
                                    trigger: str = "manual") -> dict:
        source = self.get_data_source(source_id, tenant_id)
        if not source:
            raise ValueError("数据源不存在")
        if source["status"] == "paused":
            raise ValueError("数据源已暂停")
        run_id = "sync-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO data_source_sync_runs
                    (id, data_source_id, tenant_id, trigger)
                VALUES (?, ?, ?, ?)
            """, (run_id, source_id, source["tenant_id"], str(trigger or "manual")[:40]))
        return self.get_data_source_sync_run(run_id, tenant_id) or {"id": run_id, "status": "queued"}

    def update_data_source_sync_run(self, run_id: str, status: str, error: str = "",
                                    content_hash: str = "", documents_found: int = 0,
                                    documents_ingested: int = 0) -> bool:
        if status not in {"queued", "running", "succeeded", "failed", "cancelled"}:
            raise ValueError("同步任务状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE data_source_sync_runs
                SET status=?, error=?, content_hash=?, documents_found=?, documents_ingested=?,
                    started_at=CASE WHEN status='running' AND started_at IS NULL
                                    THEN CURRENT_TIMESTAMP ELSE started_at END,
                    finished_at=CASE WHEN status IN ('succeeded','failed','cancelled')
                                     THEN CURRENT_TIMESTAMP ELSE finished_at END,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (status, str(error or "")[:500], str(content_hash or "")[:128],
                  max(0, int(documents_found)), max(0, int(documents_ingested)), run_id))
        return cur.rowcount > 0

    def retry_data_source_sync_run(self, run_id: str, tenant_id: str | None = None) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT data_source_id, tenant_id, attempt, status
                FROM data_source_sync_runs WHERE id=?
            """, (run_id,)).fetchone()
        if not row or (tenant_id is not None and row[1] != tenant_id):
            return None
        if row[3] not in {"failed", "cancelled"}:
            raise ValueError("只有失败或已取消的同步任务可以重试")
        retry_id = "sync-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO data_source_sync_runs
                    (id, data_source_id, tenant_id, status, attempt, trigger)
                VALUES (?, ?, ?, 'queued', ?, 'retry')
            """, (retry_id, row[0], row[1], int(row[2] or 1) + 1))
        return self.get_data_source_sync_run(retry_id, tenant_id)

    def get_data_source_sync_run(self, run_id: str, tenant_id: str | None = None) -> dict | None:
        sql = """
            SELECT id, data_source_id, tenant_id, status, attempt, trigger, content_hash,
                   documents_found, documents_ingested, error, started_at, finished_at,
                   created_at, updated_at
            FROM data_source_sync_runs WHERE id=?
        """
        params: list[object] = [run_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        return {"id": row[0], "data_source_id": row[1], "tenant_id": row[2], "status": row[3],
                "attempt": row[4], "trigger": row[5], "content_hash": row[6],
                "documents_found": row[7], "documents_ingested": row[8], "error": row[9],
                "started_at": row[10], "finished_at": row[11], "created_at": row[12],
                "updated_at": row[13]}

    def list_data_source_sync_runs(self, source_id: str, tenant_id: str | None = None,
                                   limit: int = 20) -> list[dict]:
        limit = max(1, min(int(limit), 100))
        sql = "SELECT id FROM data_source_sync_runs WHERE data_source_id=?"
        params: list[object] = [source_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self._db_path) as conn:
            ids = [r[0] for r in conn.execute(sql, params).fetchall()]
        return [item for run_id in ids if (item := self.get_data_source_sync_run(run_id, tenant_id))]

    def get_knowledge_base(self, knowledge_base_id: str, tenant_id: str | None = None) -> dict | None:
        sql = """
            SELECT id, tenant_id, name, description, profile, visibility, owner_user_id,
                   status, created_at, updated_at
            FROM knowledge_bases WHERE id=?
        """
        params: list[object] = [knowledge_base_id]
        if tenant_id is not None:
            sql += " AND (tenant_id=? OR visibility='public')"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        return {"id": row[0], "tenant_id": row[1], "name": row[2], "description": row[3],
                "profile": row[4], "visibility": row[5], "owner_user_id": row[6],
                "status": row[7], "created_at": row[8], "updated_at": row[9]}

    def can_access_knowledge_base(self, knowledge_base_id: str, tenant_id: str,
                                  user_id: str = "", agent_id: str = "") -> bool:
        """Resolve end-user KB grants; public/workspace defaults remain backward compatible."""
        kb = self.get_knowledge_base(knowledge_base_id, tenant_id)
        if not kb or kb["status"] == "archived":
            return False
        if kb["visibility"] == "public":
            return True
        if kb["tenant_id"] != tenant_id:
            return False
        if kb["visibility"] == "tenant":
            # Workspace knowledge bases are shared unless explicit grants exist.
            with sqlite3.connect(self._db_path) as conn:
                grant_count = conn.execute(
                    "SELECT COUNT(*) FROM knowledge_base_grants WHERE knowledge_base_id=? AND status='active'",
                    (knowledge_base_id,),
                ).fetchone()[0]
                if not grant_count:
                    return True
                return bool(conn.execute("""SELECT 1 FROM knowledge_base_grants
                    WHERE knowledge_base_id=? AND tenant_id=? AND status='active'
                      AND (user_id='' OR user_id=?) AND (agent_id='' OR agent_id=?)
                    LIMIT 1""", (knowledge_base_id, tenant_id, user_id, agent_id)).fetchone())
        if kb["owner_user_id"] and kb["owner_user_id"] == user_id:
            return True
        with sqlite3.connect(self._db_path) as conn:
            return bool(conn.execute("""SELECT 1 FROM knowledge_base_grants
                WHERE knowledge_base_id=? AND tenant_id=? AND status='active'
                  AND (user_id='' OR user_id=?) AND (agent_id='' OR agent_id=?)
                LIMIT 1""", (knowledge_base_id, tenant_id, user_id, agent_id)).fetchone())

    def grant_knowledge_base_access(self, knowledge_base_id: str, tenant_id: str,
                                    user_id: str = "", agent_id: str = "",
                                    role: str = "viewer", changed_by: str = "admin") -> bool:
        if role not in {"viewer", "editor"}:
            raise ValueError("知识库授权角色不合法")
        kb = self.get_knowledge_base(knowledge_base_id, tenant_id)
        if not kb or kb["tenant_id"] != tenant_id:
            return False
        if not user_id and not agent_id:
            raise ValueError("至少需要指定用户或 Agent")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO knowledge_base_grants
                (knowledge_base_id, tenant_id, user_id, agent_id, role, status)
                VALUES (?, ?, ?, ?, ?, 'active')
                ON CONFLICT(knowledge_base_id, user_id, agent_id) DO UPDATE SET
                    tenant_id=excluded.tenant_id, role=excluded.role, status='active'""",
                (knowledge_base_id, tenant_id, str(user_id or ""), str(agent_id or ""), role))
        self.log_audit(tenant_id, changed_by, agent_id, "knowledge_base.grant", "knowledge_base",
                       knowledge_base_id, {"user_id": user_id, "agent_id": agent_id, "role": role})
        return True

    def revoke_knowledge_base_access(self, knowledge_base_id: str, tenant_id: str,
                                     user_id: str = "", agent_id: str = "",
                                     changed_by: str = "admin") -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""UPDATE knowledge_base_grants SET status='revoked'
                WHERE knowledge_base_id=? AND tenant_id=? AND user_id=? AND agent_id=?""",
                (knowledge_base_id, tenant_id, str(user_id or ""), str(agent_id or "")))
        if cur.rowcount:
            self.log_audit(tenant_id, changed_by, agent_id, "knowledge_base.grant.revoke", "knowledge_base",
                           knowledge_base_id, {"user_id": user_id, "agent_id": agent_id})
        return cur.rowcount > 0

    def list_knowledge_base_grants(self, knowledge_base_id: str, tenant_id: str,
                                   include_revoked: bool = False) -> list[dict]:
        if not self.get_knowledge_base(knowledge_base_id, tenant_id):
            return []
        sql = """SELECT user_id, agent_id, role, status, created_at
                 FROM knowledge_base_grants WHERE knowledge_base_id=? AND tenant_id=?"""
        params: list[object] = [knowledge_base_id, tenant_id]
        if not include_revoked:
            sql += " AND status='active'"
        sql += " ORDER BY created_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"user_id": row[0], "agent_id": row[1], "role": row[2],
                 "status": row[3], "created_at": row[4]} for row in rows]

    def list_knowledge_bases(self, tenant_id: str | None = None, include_archived: bool = False) -> list[dict]:
        sql = """
            SELECT id, tenant_id, name, description, profile, visibility, owner_user_id,
                   status, created_at, updated_at,
                   (SELECT COUNT(*) FROM documents d WHERE d.knowledge_base_id=kb.id) AS document_count
            FROM knowledge_bases kb
        """
        clauses: list[str] = []
        params: list[object] = []
        if tenant_id is not None:
            clauses.append("tenant_id=?")
            params.append(tenant_id)
        if not include_archived:
            clauses.append("status != 'archived'")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY updated_at DESC, id"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "tenant_id": r[1], "name": r[2], "description": r[3],
                 "profile": r[4], "visibility": r[5], "owner_user_id": r[6],
                 "status": r[7], "created_at": r[8], "updated_at": r[9],
                 "document_count": r[10]} for r in rows]

    def update_knowledge_base_status(self, knowledge_base_id: str, status: str,
                                     tenant_id: str | None = None) -> bool:
        if status not in {"active", "disabled", "archived"}:
            raise ValueError("知识库状态不合法")
        sql = "UPDATE knowledge_bases SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"
        params: list[object] = [status, knowledge_base_id]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
        if cur.rowcount:
            self.log_audit(tenant_id or "", "local-owner", "", "knowledge_base.status.update",
                           "knowledge_base", knowledge_base_id, {"status": status})
        return cur.rowcount > 0

    def get_document(self, document_id: str, tenant_id: str | None = None) -> dict | None:
        sql = """
            SELECT id, tenant_id, owner_user_id, agent_id, knowledge_base_id, visibility,
                   source_name, cleaned_path, category, profile, status, version,
                   effective_date, expiry_date, lifecycle_status, metadata_json, created_at, updated_at
            FROM documents WHERE id=?
        """
        params: list[object] = [document_id]
        if tenant_id is not None:
            sql += " AND (tenant_id=? OR (tenant_id='' AND visibility='public'))"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        try:
            metadata = json.loads(row[15] or "{}")
        except (TypeError, json.JSONDecodeError):
            metadata = {}
        return {"id": row[0], "tenant_id": row[1], "owner_user_id": row[2], "agent_id": row[3],
                "knowledge_base_id": row[4], "visibility": row[5], "source_name": row[6],
                "cleaned_path": row[7], "category": row[8], "profile": row[9], "status": row[10],
                "version": row[11], "effective_date": row[12], "expiry_date": row[13],
                "lifecycle_status": row[14], "metadata": metadata, "created_at": row[16],
                "updated_at": row[17]}

    def update_document_lifecycle(self, document_id: str, lifecycle_status: str,
                                  tenant_id: str | None = None, changed_by: str = "admin",
                                  change_reason: str = "") -> bool:
        if lifecycle_status not in {"draft", "review", "published", "expired", "archived"}:
            raise ValueError("文档生命周期状态不合法")
        doc = self.get_document(document_id, tenant_id)
        if not doc:
            return False
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE documents SET lifecycle_status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (lifecycle_status, document_id))
            conn.execute("""
                UPDATE document_versions SET lifecycle_status=?
                WHERE document_id=? AND version=?
            """, (lifecycle_status, document_id, doc["version"]))
        if cur.rowcount:
            self.log_audit(tenant_id or doc["tenant_id"], changed_by, doc["agent_id"],
                           "document.lifecycle.update", "document", document_id,
                           {"status": lifecycle_status, "reason": change_reason})
        return cur.rowcount > 0

    def list_document_versions(self, document_id: str, tenant_id: str | None = None) -> list[dict]:
        if not self.get_document(document_id, tenant_id):
            return []
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, document_id, version, cleaned_path, content_hash,
                       lifecycle_status, change_reason, created_by, created_at
                FROM document_versions WHERE document_id=? ORDER BY version DESC
            """, (document_id,)).fetchall()
        return [{"id": r[0], "document_id": r[1], "version": r[2], "cleaned_path": r[3],
                 "content_hash": r[4], "lifecycle_status": r[5], "change_reason": r[6],
                 "created_by": r[7], "created_at": r[8]} for r in rows]

    def create_document_version(self, document_id: str, cleaned_path: str,
                                tenant_id: str | None = None, lifecycle_status: str = "review",
                                change_reason: str = "文档替换", created_by: str = "admin",
                                status: str = "staged") -> dict | None:
        """Create an immutable version and move the document pointer to it."""
        if lifecycle_status not in {"draft", "review", "published", "expired", "archived"}:
            raise ValueError("文档生命周期状态不合法")
        document = self.get_document(document_id, tenant_id)
        if not document:
            return None
        path = str(cleaned_path or "")
        content_hash = ""
        try:
            content_hash = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        except (OSError, ValueError):
            pass
        next_version = int(document.get("version") or 0) + 1
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO document_versions
                (document_id, version, cleaned_path, content_hash, lifecycle_status,
                 change_reason, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (document_id, next_version, path, content_hash, lifecycle_status,
                 str(change_reason or "")[:255], str(created_by or "")[:120]))
            conn.execute("""UPDATE documents
                SET cleaned_path=?, version=?, status=?, lifecycle_status=?,
                    updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (path, next_version, str(status or "staged")[:40], lifecycle_status, document_id))
        self.log_audit(tenant_id or document["tenant_id"], created_by, document.get("agent_id", ""),
                       "document.version.create", "document", document_id,
                       {"version": next_version, "reason": str(change_reason or "")[:120]})
        return self.get_document(document_id, tenant_id)

    def rollback_document_version(self, document_id: str, version: int,
                                  tenant_id: str | None = None, changed_by: str = "admin",
                                  change_reason: str = "") -> dict | None:
        """Rollback by creating a new version pointing to the selected immutable content."""
        document = self.get_document(document_id, tenant_id)
        if not document:
            return None
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT cleaned_path FROM document_versions
                WHERE document_id=? AND version=?""", (document_id, int(version))).fetchone()
        if not row:
            raise ValueError("目标文档版本不存在")
        return self.create_document_version(
            document_id, row[0], tenant_id, lifecycle_status="review",
            change_reason=change_reason or f"回滚到 v{int(version)}",
            created_by=changed_by, status="staged",
        )

    def expire_documents(self, tenant_id: str | None = None,
                         changed_by: str = "system") -> int:
        """Mark published documents past expiry as expired, preserving auditability."""
        sql = """UPDATE documents SET lifecycle_status='expired', updated_at=CURRENT_TIMESTAMP
                 WHERE lifecycle_status='published' AND expiry_date IS NOT NULL
                   AND expiry_date != '' AND date(expiry_date) < date('now')"""
        params: list[object] = []
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
            count = cur.rowcount
        if count:
            self.log_audit(tenant_id or "", changed_by, "", "document.expire.scan", "document", "",
                           {"expired_count": count})
        return max(0, count)

    def assign_document_knowledge_base(self, document_id: str, knowledge_base_id: str,
                                       tenant_id: str | None = None) -> bool:
        kb = self.get_knowledge_base(knowledge_base_id, tenant_id)
        if not kb or kb["status"] == "archived":
            return False
        sql = "UPDATE documents SET knowledge_base_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"
        params: list[object] = [knowledge_base_id, document_id]
        if tenant_id is not None:
            sql += " AND (tenant_id=? OR (tenant_id='' AND visibility='public'))"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
        if cur.rowcount:
            self.log_audit(tenant_id or "", "local-owner", "", "document.knowledge_base.update",
                           "document", document_id, {"knowledge_base_id": knowledge_base_id})
        return cur.rowcount > 0

    def list_knowledge_base_documents(self, knowledge_base_id: str, tenant_id: str | None = None) -> list[dict]:
        sql = """
            SELECT id, tenant_id, owner_user_id, agent_id, knowledge_base_id, visibility,
                   source_name, cleaned_path, category, profile, status, version,
                   lifecycle_status, metadata_json, created_at, updated_at
            FROM documents WHERE knowledge_base_id=?
        """
        params: list[object] = [knowledge_base_id]
        if tenant_id is not None:
            sql += " AND (tenant_id=? OR (tenant_id='' AND visibility='public'))"
            params.append(tenant_id)
        sql += " ORDER BY updated_at DESC, id"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for r in rows:
            try:
                metadata = json.loads(r[13] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            items.append({"id": r[0], "tenant_id": r[1], "owner_user_id": r[2], "agent_id": r[3],
                 "knowledge_base_id": r[4], "visibility": r[5], "source_name": r[6],
                 "cleaned_path": r[7], "category": r[8], "profile": r[9], "status": r[10],
                 "version": r[11], "lifecycle_status": r[12], "metadata": metadata,
                 "created_at": r[14], "updated_at": r[15]})
        return items

    def register_document(self, document_id: str, source_name: str, category: str, profile: str,
                          visibility: str = "public", tenant_id: str = "",
                          owner_user_id: str = "", agent_id: str = "",
                          knowledge_base_id: str = "") -> None:
        if visibility not in {"public", "tenant", "private"}:
            raise ValueError("资料可见范围不合法")
        if not knowledge_base_id:
            if visibility == "public":
                knowledge_base_id = "kb-public-general"
                kb_tenant = "local-default"
                kb_name = "公共网络安全知识库"
                kb_visibility = "public"
            else:
                kb_tenant = tenant_id or "local-default"
                knowledge_base_id = "kb-" + hashlib.sha1(
                    f"{kb_tenant}:{visibility}".encode("utf-8")
                ).hexdigest()[:16]
                kb_name = "工作区网络安全知识库" if visibility == "tenant" else "私有网络安全知识库"
                kb_visibility = visibility
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT OR IGNORE INTO knowledge_bases
                        (id, tenant_id, name, description, profile, visibility, owner_user_id)
                    VALUES (?, ?, ?, '', ?, ?, ?)
                """, (knowledge_base_id, kb_tenant, kb_name, str(profile or "general")[:120],
                      kb_visibility, owner_user_id if visibility == "private" else ""))
        knowledge_base = self.get_knowledge_base(knowledge_base_id)
        if not knowledge_base or knowledge_base["status"] == "archived":
            raise ValueError("知识库不存在")
        if visibility == "public" and knowledge_base["visibility"] != "public":
            raise ValueError("公共资料只能归属公共知识库")
        if visibility in {"tenant", "private"} and knowledge_base["tenant_id"] != (tenant_id or "local-default"):
            raise ValueError("资料不能归属其他工作区知识库")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO documents (
                    id, tenant_id, owner_user_id, agent_id, knowledge_base_id, visibility,
                    source_name, category, profile
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (document_id, tenant_id, owner_user_id, agent_id, knowledge_base_id, visibility,
                  str(source_name or "")[:255], str(category or "")[:120],
                  str(profile or "general")[:120]))

    def upsert_existing_rag_document(self, document_id: str, source_name: str, cleaned_path: str,
                                     category: str, profile: str, lifecycle_status: str,
                                     metadata: dict | None = None, changed_by: str = "admin") -> str:
        """Register a cleaned corpus file without parsing or re-indexing it."""
        if lifecycle_status not in {"review", "published"}: lifecycle_status = "review"
        payload = json.dumps(metadata or {}, ensure_ascii=False)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT metadata_json FROM documents WHERE id=?", (document_id,)).fetchone()
            if not row:
                row = conn.execute(
                    "SELECT metadata_json FROM documents WHERE cleaned_path=? ORDER BY updated_at DESC LIMIT 1",
                    (cleaned_path,),
                ).fetchone()
            if row:
                try: existing = json.loads(row[0] or "{}")
                except (TypeError, json.JSONDecodeError): existing = {}
                merged = {**(metadata or {}), **existing}
                conn.execute("UPDATE documents SET cleaned_path=COALESCE(NULLIF(cleaned_path,''),?), metadata_json=?, updated_at=CURRENT_TIMESTAMP WHERE id=?", (cleaned_path, json.dumps(merged, ensure_ascii=False), document_id))
                if existing.get("rag_reconciled") or existing.get("document_id") == document_id:
                    conn.execute("UPDATE documents SET lifecycle_status=? WHERE id=?", (lifecycle_status, document_id))
                return "skipped"
            conn.execute("""
                INSERT OR IGNORE INTO documents
                  (id, tenant_id, owner_user_id, agent_id, knowledge_base_id, visibility, source_name, cleaned_path, category, profile, status, lifecycle_status, metadata_json)
                VALUES (?, '', '', '', 'kb-public-general', 'public', ?, ?, ?, ?, 'indexed', ?, ?)
            """, (document_id, str(source_name or "")[:255], cleaned_path, str(category or "")[:120], str(profile or "general")[:120], lifecycle_status, payload))
        self.log_audit("", changed_by, "", "document.rag_reconcile", "document", document_id, {"cleaned_path": cleaned_path, "reindexed": False})
        return "created"

    def create_staged_document_from_source(self, document_id: str, source_name: str,
                                           staged_path: str, metadata: dict,
                                           category: str = "通用", profile: str = "general",
                                           visibility: str = "tenant", tenant_id: str = "",
                                           owner_user_id: str = "", agent_id: str = "",
                                           knowledge_base_id: str = "") -> dict:
        """Register externally-read content without marking it indexed or published."""
        self.register_document(
            document_id=document_id, source_name=source_name, category=category,
            profile=profile, visibility=visibility, tenant_id=tenant_id,
            owner_user_id=owner_user_id, agent_id=agent_id,
            knowledge_base_id=knowledge_base_id,
        )
        payload = dict(metadata or {})
        payload.update({
            "source_name": str(source_name or "")[:255], "document_id": document_id,
            "staged_path": str(staged_path or ""), "category": str(category or "")[:120],
            "profile": str(profile or "general")[:120], "visibility": visibility,
            "tenant_id": tenant_id, "owner_user_id": owner_user_id,
            "agent_id": agent_id, "knowledge_base_id": knowledge_base_id,
        })
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                UPDATE documents
                SET cleaned_path=?, metadata_json=?, status='staged',
                    lifecycle_status='review', updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (str(staged_path or ""), json.dumps(payload, ensure_ascii=False), document_id))
        self.log_audit(tenant_id, owner_user_id or "local-owner", agent_id,
                       "document.source_stage", "document", document_id,
                       {"source_name": source_name, "content_hash": payload.get("content_hash", "")})
        return self.get_document(document_id, tenant_id) or {
            "id": document_id, "status": "staged", "lifecycle_status": "review",
            "metadata": payload,
        }

    def mark_document_indexed(self, document_id: str, cleaned_path: str, status: str = "indexed",
                              change_reason: str = "首次入库", created_by: str = "system") -> None:
        cleaned_path = str(cleaned_path or "")
        content_hash = ""
        try:
            content_hash = hashlib.sha256(Path(cleaned_path).read_bytes()).hexdigest()
        except (OSError, ValueError):
            pass
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT version, lifecycle_status FROM documents WHERE id=?", (document_id,)
            ).fetchone()
            if not row:
                return
            version, lifecycle_status = int(row[0] or 1), row[1] or "review"
            conn.execute("""
                UPDATE documents SET cleaned_path=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE id=?
            """, (cleaned_path, status, document_id))
            conn.execute("""
                INSERT INTO document_versions
                    (document_id, version, cleaned_path, content_hash, lifecycle_status,
                     change_reason, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(document_id, version) DO UPDATE SET
                    cleaned_path=excluded.cleaned_path,
                    content_hash=excluded.content_hash,
                    lifecycle_status=excluded.lifecycle_status,
                    change_reason=excluded.change_reason,
                    created_by=excluded.created_by
            """, (document_id, version, cleaned_path, content_hash, lifecycle_status,
                  change_reason, created_by))

    def create_generation_request(self, tenant_id: str, user_id: str, agent_id: str, mode: str,
                                  query_text: str, fields: dict, outline: dict) -> dict:
        if mode not in {"writing", "presentation"}:
            raise ValueError("不支持的生成类型")
        request_id = "gen-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO generation_requests (
                    id, tenant_id, user_id, agent_id, mode, query_text, fields_json, outline_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (request_id, tenant_id, user_id, agent_id, mode, str(query_text or "")[:4000],
                  json.dumps(fields or {}, ensure_ascii=False), json.dumps(outline or {}, ensure_ascii=False)))
        self.log_audit(tenant_id, user_id, agent_id, "generation.outline.create", "generation", request_id,
                       {"mode": mode})
        return self.get_generation_request(request_id, tenant_id, user_id, agent_id)

    def get_generation_request(self, request_id: str, tenant_id: str, user_id: str,
                               agent_id: str | None = None) -> dict | None:
        sql = """
            SELECT id, mode, status, query_text, fields_json, outline_json,
                   trace_json, references_json, artifact_path, created_at, updated_at
            FROM generation_requests WHERE id=? AND tenant_id=? AND user_id=?
        """
        params: list[object] = [request_id, tenant_id, user_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        item = {
            "id": row[0], "mode": row[1], "status": row[2], "query": row[3],
            "fields": json.loads(row[4] or "{}"), "outline": json.loads(row[5] or "{}"),
            "trace": json.loads(row[6] or "{}"), "references": json.loads(row[7] or "[]"),
            "artifact_path": row[8], "created_at": _to_utc_iso(row[9]), "updated_at": _to_utc_iso(row[10]),
        }
        item["artifacts"] = self.list_generation_artifacts(request_id, tenant_id, user_id, agent_id)
        return item

    def update_generation_observability(self, request_id: str, tenant_id: str, user_id: str,
                                         agent_id: str, trace: dict, references: list) -> bool:
        if not isinstance(trace, dict) or not isinstance(references, list):
            raise ValueError("生成观测数据格式不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE generation_requests SET trace_json=?, references_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (json.dumps(trace, ensure_ascii=False), json.dumps(references, ensure_ascii=False),
                  request_id, tenant_id, user_id, agent_id))
        return cur.rowcount > 0

    def list_generation_requests(self, tenant_id: str, user_id: str, agent_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            ids = conn.execute("""
                SELECT id FROM generation_requests
                WHERE tenant_id=? AND user_id=? AND agent_id=? ORDER BY updated_at DESC LIMIT 100
            """, (tenant_id, user_id, agent_id)).fetchall()
        return [
            item for row in ids
            if (item := self.get_generation_request(row[0], tenant_id, user_id, agent_id))
        ]

    def complete_generation_request(self, request_id: str, tenant_id: str, user_id: str,
                                    agent_id: str, artifact_path: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE generation_requests SET status='generated', artifact_path=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (artifact_path, request_id, tenant_id, user_id, agent_id))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, agent_id, "generation.artifact.create", "generation",
                           request_id, {"artifact_path": artifact_path})
        return cur.rowcount > 0

    def add_generation_artifact(self, request_id: str, tenant_id: str, user_id: str,
                                agent_id: str, artifact_path: str) -> dict | None:
        item = self.get_generation_request(request_id, tenant_id, user_id, agent_id)
        if not item:
            return None
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT COALESCE(MAX(version), 0) + 1 FROM generation_artifacts
                WHERE generation_id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (request_id, tenant_id, user_id, agent_id)).fetchone()
            version = int(row[0] or 1)
            artifact_id = "gar-" + uuid.uuid4().hex[:16]
            conn.execute("""
                INSERT INTO generation_artifacts (
                    id, generation_id, tenant_id, user_id, agent_id, version, artifact_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (artifact_id, request_id, tenant_id, user_id, agent_id, version, artifact_path))
            conn.execute("""
                UPDATE generation_requests SET status='generated', artifact_path=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (artifact_path, request_id, tenant_id, user_id, agent_id))
        self.log_audit(tenant_id, user_id, agent_id, "generation.artifact.create", "generation",
                       request_id, {"artifact_id": artifact_id, "version": version})
        return {"id": artifact_id, "version": version, "artifact_path": artifact_path}

    def list_generation_artifacts(self, request_id: str, tenant_id: str, user_id: str,
                                  agent_id: str | None = None) -> list[dict]:
        sql = """
            SELECT id, version, artifact_path, created_at
            FROM generation_artifacts
            WHERE generation_id=? AND tenant_id=? AND user_id=?
        """
        params: list[object] = [request_id, tenant_id, user_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        sql += " ORDER BY version DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "id": row[0], "version": int(row[1]), "artifact_path": row[2],
            "created_at": _to_utc_iso(row[3]),
        } for row in rows]

    def get_generation_artifact(self, request_id: str, artifact_id: str, tenant_id: str,
                                user_id: str, agent_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT id, version, artifact_path, created_at FROM generation_artifacts
                WHERE id=? AND generation_id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (artifact_id, request_id, tenant_id, user_id, agent_id)).fetchone()
        if not row:
            return None
        return {"id": row[0], "version": int(row[1]), "artifact_path": row[2],
                "created_at": _to_utc_iso(row[3])}

    def update_generation_outline(self, request_id: str, tenant_id: str, user_id: str,
                                  agent_id: str, outline: dict) -> bool:
        if not isinstance(outline, dict) or not str(outline.get("title") or "").strip():
            raise ValueError("提纲必须包含标题")
        item = self.get_generation_request(request_id, tenant_id, user_id, agent_id)
        if not item:
            return False
        if item["status"] == "generated":
            raise ValueError("生成后的文件不可直接覆盖，请新建生成任务")
        if item["mode"] == "writing" and not isinstance(outline.get("sections"), list):
            raise ValueError("文档提纲必须包含章节")
        if item["mode"] == "presentation" and not isinstance(outline.get("pages"), list):
            raise ValueError("PPT 提纲必须包含页面")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE generation_requests SET outline_json=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (json.dumps(outline, ensure_ascii=False), request_id, tenant_id, user_id, agent_id))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, agent_id, "generation.outline.update", "generation",
                           request_id, {"mode": item["mode"]})
        return cur.rowcount > 0

    def delete_generation_request(self, request_id: str, tenant_id: str, user_id: str,
                                  agent_id: str) -> str | None:
        item = self.get_generation_request(request_id, tenant_id, user_id, agent_id)
        if not item:
            return None
        paths = [entry["artifact_path"] for entry in self.list_generation_artifacts(
            request_id, tenant_id, user_id, agent_id,
        )]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                DELETE FROM generation_artifacts WHERE generation_id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (request_id, tenant_id, user_id, agent_id))
            conn.execute("""
                DELETE FROM generation_requests WHERE id=? AND tenant_id=? AND user_id=? AND agent_id=?
            """, (request_id, tenant_id, user_id, agent_id))
        self.log_audit(tenant_id, user_id, agent_id, "generation.delete", "generation", request_id)
        return json.dumps(paths or [str(item.get("artifact_path") or "")], ensure_ascii=False)

    def create_capability_extension(self, kind: str, name: str, version: str = "",
                                    source: str = "", description: str = "",
                                    manifest: dict | None = None,
                                    permissions: list | None = None,
                                    network_scope: str = "") -> dict:
        if kind not in {"skill", "mcp"}:
            raise ValueError("扩展类型仅支持 skill 或 mcp")
        name = str(name or "").strip()[:120]
        if not name:
            raise ValueError("扩展名称不能为空")
        source = str(source or "").strip()[:500]
        if not source:
            raise ValueError("请填写受控来源或服务地址")
        if kind == "mcp":
            parsed = urllib.parse.urlparse(source)
            manifest = manifest if isinstance(manifest, dict) else {}
            transport = str(manifest.get("transport") or "http").strip().lower()
            local_dev_endpoint = (
                parsed.scheme == "http"
                and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
                and os.environ.get("APP_ENV", "development").strip().lower()
                not in {"production", "prod"}
            )
            local_stdio = transport == "stdio" and parsed.scheme == "stdio"
            if ((parsed.scheme != "https" and not local_dev_endpoint and not local_stdio)
                    or (not parsed.netloc and not local_stdio)):
                raise ValueError("MCP 服务地址必须使用 HTTPS；开发环境仅允许本机测试地址")
            if local_stdio and (not manifest.get("command") or not isinstance(manifest.get("tools"), list) or not manifest.get("tools")):
                raise ValueError("MCP stdio 必须声明 command 和 tools")
            forbidden_manifest_keys = {"api_key", "secret", "secret_key", "token", "password", "credential"}
            if forbidden_manifest_keys.intersection(str(key).lower() for key in manifest):
                raise ValueError("MCP manifest 不得包含真实凭证，请使用密钥引用")
            if not str(network_scope or "").strip():
                raise ValueError("MCP 必须声明数据传输范围")
            try:
                timeout = int(manifest.get("timeout_seconds", 15))
                if timeout < 1 or timeout > 60:
                    raise ValueError
            except (TypeError, ValueError):
                raise ValueError("MCP timeout_seconds 必须在 1-60 秒之间")
        if permissions is not None and (not isinstance(permissions, list) or
                                        not all(isinstance(item, str) and item.strip() for item in permissions)):
            raise ValueError("扩展 permissions 必须是非空字符串列表")
        extension_id = "ext-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO capability_extensions (
                    id, kind, name, version, source, description, manifest_json,
                    permissions_json, network_scope
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                extension_id, kind, name, str(version or "")[:80], source,
                str(description or "")[:2000],
                json.dumps(manifest or {}, ensure_ascii=False),
                json.dumps(permissions or [], ensure_ascii=False),
                str(network_scope or "")[:1000],
            ))
            conn.execute("""
                INSERT INTO capability_extension_versions (
                    extension_id, version, source, description, manifest_json,
                    permissions_json, network_scope, change_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'initial registration')
            """, (
                extension_id, str(version or "")[:80], source,
                str(description or "")[:2000], json.dumps(manifest or {}, ensure_ascii=False),
                json.dumps(permissions or [], ensure_ascii=False), str(network_scope or "")[:1000],
            ))
        self.log_audit("platform", action="extension.register", resource_type="extension",
                       resource_id=extension_id, detail={"kind": kind, "name": name, "source": source})
        return self.get_capability_extension(extension_id) or {}

    def ensure_builtin_capability_extensions(self) -> None:
        """Seed safe built-in capabilities without granting them to any Agent."""
        builtins = [
            ("builtin-skill-security-ppt", "skill", "网络安全 PPT 故事线 Skill",
             "builtin://security-ppt", "通用网络安全 PPT 的受众、目标、叙事、证据和页面结构设计。",
             {"runtime": "builtin", "operation": "security_ppt_storyboard", "signature": "built-in-reviewed"},
             "不出网；只生成结构化 PPT 大纲"),
            ("builtin-skill-critical-infrastructure-ppt", "skill", "关键信息基础设施安全 PPT Skill",
             "builtin://critical-infrastructure-ppt", "参数化生成关基安全专题 PPT 故事线，法规依据和组织现状必须人工核验。",
             {"runtime": "builtin", "operation": "critical_infrastructure_security_ppt", "signature": "built-in-reviewed"},
             "不出网；只生成结构化 PPT 大纲"),
            ("builtin-mcp-fetch", "mcp", "网页内容 Fetch MCP",
             "stdio://modelcontextprotocol-fetch",
             "受控抓取公开网页内容并转换为 Markdown；默认禁止访问本机和内网地址。",
             {
                 "transport": "stdio",
                 "command": "uvx",
                 "args": ["mcp-server-fetch"],
                 "tools": ["fetch"],
                 "timeout_seconds": 30,
                 "block_private_network": True,
                 "auth_mode": "none",
             },
             "仅传输管理员或用户明确提交的公网 URL；不允许访问本机、内网和私有地址"),
            ("builtin-mcp-bing-search", "mcp", "必应中文搜索 MCP",
             "stdio://bing-cn-mcp",
             "受控调用必应中文搜索，补充授权 RAG 未覆盖的公开网络资料；结果必须标记为外部待核验。",
             {
                 "transport": "stdio",
                 "command": "npx",
                 "args": ["-y", "bing-cn-mcp"],
                 "tools": ["bing_search", "crawl_webpage"],
                 "timeout_seconds": 30,
                 "max_results": 5,
                 "block_private_network": True,
                 "auth_mode": "none",
             },
             "仅传输最小化搜索关键词；搜索结果为外部待核验资料，不传输租户私有内容"),
        ]
        with sqlite3.connect(self._db_path) as conn:
            for extension_id, kind, name, source, description, manifest, network_scope in builtins:
                conn.execute("""
                    INSERT OR IGNORE INTO capability_extensions (
                        id, kind, name, version, source, description, manifest_json,
                        permissions_json, network_scope, status
                    ) VALUES (?, ?, ?, '1.0', ?, ?, ?, '[]', ?, 'approved')
                """, (extension_id, kind, name, source, description,
                      json.dumps(manifest, ensure_ascii=False), network_scope))
                conn.execute("""
                    INSERT OR IGNORE INTO capability_extension_versions (
                        extension_id, version, source, description, manifest_json,
                        permissions_json, network_scope, change_reason
                    ) VALUES (?, '1.0', ?, ?, ?, '[]', ?, 'built-in seed')
                """, (extension_id, source, description,
                      json.dumps(manifest, ensure_ascii=False), network_scope))

    def capability_extension_health(self, extension_id: str) -> dict | None:
        extension = self.get_capability_extension(extension_id)
        if not extension:
            return None
        errors = []
        if extension["kind"] == "mcp":
            parsed = urllib.parse.urlparse(extension["source"])
            manifest = extension.get("manifest") or {}
            if manifest.get("transport") == "stdio":
                if parsed.scheme != "stdio":
                    errors.append("MCP stdio 来源必须使用 stdio:// 标识")
                if not manifest.get("command"):
                    errors.append("MCP stdio 未声明 command")
            elif parsed.scheme != "https" or not parsed.netloc:
                errors.append("MCP 地址不是 HTTPS")
            if not extension.get("network_scope"):
                errors.append("未声明数据传输范围")
            if not isinstance(manifest.get("tools"), list) or not manifest.get("tools"):
                errors.append("未声明 MCP tools")
        else:
            if not extension.get("source"):
                errors.append("未声明 Skill 来源")
            if not (extension.get("manifest") or {}).get("signature"):
                errors.append("未声明 Skill 签名/校验信息")
        result = {"ok": not errors, "mode": "metadata_only", "errors": errors,
                  "status": "healthy" if not errors else "needs_review"}
        self.log_audit("platform", "platform_admin", "", "extension.health_check",
                       "extension", extension_id, result)
        return result

    def get_capability_extension(self, extension_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT id, kind, name, version, source, description, manifest_json,
                       permissions_json, network_scope, status, created_at, updated_at
                FROM capability_extensions WHERE id=?
            """, (extension_id,)).fetchone()
        if not row:
            return None
        return {
            "id": row[0], "kind": row[1], "name": row[2], "version": row[3],
            "source": row[4], "description": row[5],
            "manifest": json.loads(row[6] or "{}"),
            "permissions": json.loads(row[7] or "[]"),
            "network_scope": row[8], "status": row[9],
            "created_at": _to_utc_iso(row[10]), "updated_at": _to_utc_iso(row[11]),
        }

    def list_capability_extensions(self, kind: str = "") -> list[dict]:
        if kind and kind not in {"skill", "mcp"}:
            raise ValueError("扩展类型仅支持 skill 或 mcp")
        sql = "SELECT id FROM capability_extensions"
        params: list[object] = []
        if kind:
            sql += " WHERE kind=?"
            params.append(kind)
        sql += " ORDER BY updated_at DESC, created_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self.get_capability_extension(row[0]))]

    def review_capability_extension(self, extension_id: str, status: str,
                                    reviewer: str = "") -> bool:
        if status not in {"approved", "disabled", "rejected"}:
            raise ValueError("审核状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE capability_extensions SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (status, extension_id))
        if cur.rowcount:
            self.log_audit("platform", reviewer, action="extension.review", resource_type="extension",
                           resource_id=extension_id, detail={"status": status})
        return cur.rowcount > 0

    def set_capability_extension_grant(self, extension_id: str, tenant_id: str, agent_id: str,
                                       enabled: bool, approved_by: str = "") -> bool:
        extension = self.get_capability_extension(extension_id)
        if not extension:
            return False
        if enabled and extension["status"] != "approved":
            raise ValueError("仅已审核通过的扩展可以授权")
        with sqlite3.connect(self._db_path) as conn:
            agent_exists = conn.execute("""
                SELECT 1 FROM agents WHERE id=? AND tenant_id=? AND status='active'
            """, (agent_id, tenant_id)).fetchone()
            if not agent_exists:
                return False
            conn.execute("""
                INSERT INTO capability_extension_grants (
                    extension_id, tenant_id, agent_id, enabled, approved_by
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(extension_id, tenant_id, agent_id) DO UPDATE SET
                    enabled=excluded.enabled, approved_by=excluded.approved_by,
                    updated_at=CURRENT_TIMESTAMP
            """, (extension_id, tenant_id, agent_id, int(bool(enabled)), str(approved_by or "")[:120]))
        self.log_audit(tenant_id, approved_by, agent_id, "extension.grant.update", "extension",
                       extension_id, {"enabled": bool(enabled), "kind": extension["kind"]})
        return True

    def list_capability_extension_grants(self, extension_id: str = "",
                                         tenant_id: str | None = None) -> list[dict]:
        sql = """
            SELECT extension_id, tenant_id, agent_id, enabled, approved_by, created_at, updated_at
            FROM capability_extension_grants
        """
        params: list[object] = []
        conditions = []
        if extension_id:
            conditions.append("extension_id=?")
            params.append(extension_id)
        if tenant_id is not None:
            conditions.append("tenant_id=?")
            params.append(tenant_id)
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY updated_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "extension_id": row[0], "tenant_id": row[1], "agent_id": row[2],
            "enabled": bool(row[3]), "approved_by": row[4],
            "created_at": _to_utc_iso(row[5]), "updated_at": _to_utc_iso(row[6]),
        } for row in rows]

    def list_enabled_capability_extensions(self, tenant_id: str, agent_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT e.id, e.kind, e.name, e.version, e.permissions_json, e.network_scope
                FROM capability_extension_grants g
                JOIN capability_extensions e ON e.id=g.extension_id
                WHERE g.tenant_id=? AND g.agent_id=? AND g.enabled=1 AND e.status='approved'
                ORDER BY e.kind, e.name
            """, (tenant_id, agent_id)).fetchall()
        return [{
            "id": row[0], "kind": row[1], "name": row[2], "version": row[3],
            "permissions": json.loads(row[4] or "[]"), "network_scope": row[5],
        } for row in rows]

    def record_capability_extension_call(self, extension_id: str, tenant_id: str, agent_id: str,
                                         status: str, duration_ms: int = 0, error: str = "") -> bool:
        if status not in {"success", "error", "denied", "timeout", "skipped"}:
            raise ValueError("调用状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            allowed = conn.execute("""
                SELECT 1 FROM capability_extension_grants g
                JOIN capability_extensions e ON e.id=g.extension_id
                WHERE g.extension_id=? AND g.tenant_id=? AND g.agent_id=?
                  AND g.enabled=1 AND e.status='approved'
            """, (extension_id, tenant_id, agent_id)).fetchone()
            if not allowed:
                return False
            conn.execute("""
                INSERT INTO capability_extension_calls (
                    extension_id, tenant_id, agent_id, status, duration_ms, error
                ) VALUES (?, ?, ?, ?, ?, ?)
            """, (extension_id, tenant_id, agent_id, status, max(0, int(duration_ms or 0)),
                  str(error or "")[:1000]))
        return True

    def capability_extension_usage(self, extension_id: str, tenant_id: str | None = None) -> dict | None:
        if not self.get_capability_extension(extension_id):
            return None
        conditions = ["extension_id=?"]
        params: list[object] = [extension_id]
        if tenant_id is not None:
            conditions.append("tenant_id=?")
            params.append(tenant_id)
        where = " WHERE " + " AND ".join(conditions)
        with sqlite3.connect(self._db_path) as conn:
            summary_sql = (
                "SELECT COUNT(*), "
                "COALESCE(SUM(CASE WHEN status='success' THEN 1 ELSE 0 END), 0), "
                "COALESCE(SUM(CASE WHEN status IN ('error', 'timeout') THEN 1 ELSE 0 END), 0), "
                "COALESCE(AVG(duration_ms), 0) "
                "FROM capability_extension_calls" + where
            )
            row = conn.execute(summary_sql, params).fetchone()
            durations = conn.execute(
                "SELECT duration_ms FROM capability_extension_calls" + where + " ORDER BY duration_ms",
                params,
            ).fetchall()
        count = int(row[0] or 0)
        p95 = 0
        if durations:
            p95 = int(durations[max(0, (len(durations) * 95 + 99) // 100 - 1)][0])
        return {
            "calls": count, "successes": int(row[1] or 0), "failures": int(row[2] or 0),
            "success_rate": round((int(row[1] or 0) / count) * 100, 1) if count else None,
            "avg_duration_ms": round(float(row[3] or 0), 1), "p95_duration_ms": p95,
            "grants": self.list_capability_extension_grants(extension_id, tenant_id),
            "recent_calls": self.list_capability_extension_calls(extension_id, tenant_id=tenant_id),
        }

    def list_capability_extension_calls(self, extension_id: str, limit: int = 10,
                                        tenant_id: str | None = None) -> list[dict]:
        conditions = ["extension_id=?"]
        params: list[object] = [extension_id]
        if tenant_id is not None:
            conditions.append("tenant_id=?")
            params.append(tenant_id)
        params.append(max(1, min(int(limit), 100)))
        with sqlite3.connect(self._db_path) as conn:
            sql = (
                "SELECT tenant_id, agent_id, status, duration_ms, error, created_at "
                "FROM capability_extension_calls WHERE " + " AND ".join(conditions) +
                " ORDER BY created_at DESC, id DESC LIMIT ?"
            )
            rows = conn.execute(sql, params).fetchall()
        return [{
            "tenant_id": row[0], "agent_id": row[1], "status": row[2],
            "duration_ms": int(row[3] or 0), "error": row[4],
            "created_at": _to_utc_iso(row[5]),
        } for row in rows]

    def list_user_agents(self, tenant_id: str, user_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT a.id, a.name, a.status, am.role
                FROM agent_memberships am
                JOIN agents a ON a.id=am.agent_id AND a.tenant_id=am.tenant_id
                WHERE am.tenant_id=? AND am.user_id=? AND am.status='active'
                  AND a.status='active'
                ORDER BY a.created_at
            """, (tenant_id, user_id)).fetchall()
        return [{"id": row[0], "name": row[1], "status": row[2], "role": row[3]} for row in rows]

    def list_user_workspaces(self, user_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT o.id, o.name, om.role
                FROM organization_memberships om JOIN organizations o ON o.id=om.tenant_id
                WHERE om.user_id=? AND om.status='active' AND o.status='active'
                ORDER BY om.created_at
            """, (user_id,)).fetchall()
        return [{"id": row[0], "name": row[1], "role": row[2]} for row in rows]

    def switch_auth_session_workspace(self, token: str, tenant_id: str) -> dict | None:
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            session = conn.execute("""
                SELECT user_id FROM auth_sessions
                WHERE token_hash=? AND revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP
            """, (token_hash,)).fetchone()
            if not session:
                return None
            row = conn.execute("""
                SELECT om.role, a.id, a.name
                FROM organization_memberships om
                JOIN agent_memberships am ON am.tenant_id=om.tenant_id AND am.user_id=om.user_id
                    AND am.status='active'
                JOIN agents a ON a.id=am.agent_id AND a.tenant_id=om.tenant_id AND a.status='active'
                WHERE om.tenant_id=? AND om.user_id=? AND om.status='active'
                ORDER BY a.created_at LIMIT 1
            """, (tenant_id, session[0])).fetchone()
            if not row:
                return None
            conn.execute(
                "UPDATE auth_sessions SET tenant_id=?, agent_id=? WHERE token_hash=?",
                (tenant_id, row[1], token_hash),
            )
        self.log_audit(tenant_id, session[0], row[1], "session.workspace.switch", "workspace", tenant_id)
        return {"id": tenant_id, "role": row[0], "agent_id": row[1], "agent_name": row[2]}

    def switch_auth_session_agent(self, token: str, agent_id: str) -> dict | None:
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            session = conn.execute("""
                SELECT user_id, tenant_id FROM auth_sessions
                WHERE token_hash=? AND revoked_at IS NULL AND expires_at > CURRENT_TIMESTAMP
            """, (token_hash,)).fetchone()
            if not session:
                return None
            allowed = conn.execute("""
                SELECT a.id, a.name
                FROM agent_memberships am JOIN agents a ON a.id=am.agent_id
                WHERE am.tenant_id=? AND am.user_id=? AND am.agent_id=?
                  AND am.status='active' AND a.status='active'
            """, (session[1], session[0], agent_id)).fetchone()
            if not allowed:
                return None
            conn.execute(
                "UPDATE auth_sessions SET agent_id=? WHERE token_hash=?", (agent_id, token_hash),
            )
        self.log_audit(session[1], session[0], agent_id, "session.agent.switch", "agent", agent_id)
        return {"id": allowed[0], "name": allowed[1]}

    def get_auth_session(self, token: str) -> dict | None:
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT s.id, s.user_id, s.tenant_id, s.agent_id, om.role, u.email, u.display_name,
                       s.ip_address, s.user_agent, s.last_seen_at
                FROM auth_sessions s
                JOIN users u ON u.id=s.user_id
                JOIN organization_memberships om ON om.user_id=s.user_id AND om.tenant_id=s.tenant_id
                JOIN agent_memberships am ON am.user_id=s.user_id AND am.tenant_id=s.tenant_id
                    AND am.agent_id=s.agent_id
                JOIN agents a ON a.id=s.agent_id AND a.tenant_id=s.tenant_id
                WHERE s.token_hash=? AND s.revoked_at IS NULL AND s.expires_at > CURRENT_TIMESTAMP
                  AND u.status='active' AND om.status='active' AND am.status='active'
                  AND a.status='active'
            """, (token_hash,)).fetchone()
        if not row:
            return None
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("UPDATE auth_sessions SET last_seen_at=CURRENT_TIMESTAMP WHERE id=?", (row[0],))
        return {"session_id": row[0], "user_id": row[1], "tenant_id": row[2], "agent_id": row[3],
                "role": row[4], "email": row[5], "display_name": row[6],
                "ip_address": row[7] or "", "user_agent": row[8] or "",
                "last_seen_at": self._utc_to_local(row[9] or "") if row[9] else ""}

    def revoke_auth_session(self, token: str) -> None:
        token_hash = hashlib.sha256(str(token).encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE token_hash=?", (token_hash,))

    def revoke_user_auth_sessions(self, tenant_id: str, user_id: str) -> int:
        """Force sign-out for every active session owned by a tenant member."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP
                   WHERE tenant_id=? AND user_id=? AND revoked_at IS NULL""",
                (tenant_id, user_id),
            )
        return int(cur.rowcount or 0)

    def create_workspace_invitation(self, tenant_id: str, email: str, role: str,
                                    invited_by: str, agent_id: str = "", expires_hours: int = 72) -> dict:
        email = self._normalize_account_email(email)
        if role not in {"user", "agent_admin", "org_admin", "auditor"}:
            raise ValueError("邀请角色不合法")
        hours = max(1, min(int(expires_hours or 72), 24 * 14))
        with sqlite3.connect(self._db_path) as conn:
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise ValueError("该邮箱已经注册，请直接加入现有成员")
            if agent_id and not conn.execute(
                "SELECT 1 FROM agents WHERE id=? AND tenant_id=? AND status='active'", (agent_id, tenant_id),
            ).fetchone():
                raise ValueError("目标 Agent 不存在或未启用")
            if not agent_id:
                row = conn.execute(
                    "SELECT id FROM agents WHERE tenant_id=? AND status='active' ORDER BY created_at LIMIT 1", (tenant_id,),
                ).fetchone()
                if not row:
                    raise ValueError("工作区没有可用 Agent")
                agent_id = row[0]
            conn.execute(
                "UPDATE workspace_invitations SET status='revoked' WHERE tenant_id=? AND email=? AND status='pending'",
                (tenant_id, email),
            )
            token = "inv_" + secrets.token_urlsafe(32)
            invitation_id = "invite-" + uuid.uuid4().hex[:16]
            conn.execute("""INSERT INTO workspace_invitations
                (id, token_hash, tenant_id, agent_id, email, role, invited_by, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now', ?))""", (
                invitation_id, hashlib.sha256(token.encode("utf-8")).hexdigest(), tenant_id, agent_id,
                email, role, str(invited_by or ""), f"+{hours} hours",
            ))
        self.log_audit(tenant_id, invited_by, agent_id, "workspace.invitation.create", "invitation", invitation_id,
                       {"email": email, "role": role, "expires_hours": hours})
        return {"id": invitation_id, "email": email, "role": role, "agent_id": agent_id,
                "expires_hours": hours, "token": token, "secret_shown_once": True}

    def list_workspace_invitations(self, tenant_id: str, status: str = "pending") -> list[dict]:
        sql = """SELECT id, email, role, agent_id, invited_by, status, expires_at,
                        accepted_at, created_at, user_id, approved_by, approved_at
                 FROM workspace_invitations WHERE tenant_id=?"""
        params: list[object] = [tenant_id]
        if status in {"pending", "accepted_pending"}:
            sql += " AND status IN ('pending', 'accepted_pending')"
        elif status:
            sql += " AND status=?"
            params.append(status)
        sql += " ORDER BY created_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": row[0], "email": row[1], "role": row[2], "agent_id": row[3], "invited_by": row[4],
                 "status": row[5], "expires_at": self._utc_to_local(row[6] or ""),
                 "accepted_at": self._utc_to_local(row[7] or "") if row[7] else "",
                 "created_at": self._utc_to_local(row[8] or ""), "user_id": row[9] or "",
                 "approved_by": row[10] or "",
                 "approved_at": self._utc_to_local(row[11] or "") if row[11] else ""} for row in rows]

    def accept_workspace_invitation(self, token: str, email: str, password: str, display_name: str = "") -> dict:
        email = self._normalize_account_email(email)
        display_name = str(display_name or "").strip()[:80]
        self._validate_account_password(password)
        token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            invitation = conn.execute("""SELECT id, tenant_id, agent_id, email, role
                FROM workspace_invitations WHERE token_hash=? AND status='pending' AND expires_at>CURRENT_TIMESTAMP""",
                (token_hash,)).fetchone()
            if not invitation or invitation[3] != email:
                raise ValueError("邀请码无效、已过期或与邮箱不匹配")
            if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                raise ValueError("该邮箱已经注册，请登录后由管理员加入工作区")
            user_id = "usr-" + uuid.uuid4().hex[:12]
            conn.execute("""INSERT INTO users (id, tenant_id, email, display_name, password_hash, role, status)
                VALUES (?, ?, ?, ?, ?, ?, 'deactivated')""", (user_id, invitation[1], email, display_name,
                                                               self._password_hash(password), invitation[4]))
            conn.execute("""INSERT INTO organization_memberships (tenant_id, user_id, role, status)
                         VALUES (?, ?, ?, 'disabled')""", (invitation[1], user_id, invitation[4]))
            conn.execute("""INSERT INTO agent_memberships (tenant_id, agent_id, user_id, role, status)
                         VALUES (?, ?, ?, ?, 'disabled')""", (invitation[1], invitation[2], user_id, invitation[4]))
            conn.execute("""UPDATE workspace_invitations
                           SET status='accepted_pending', user_id=?, accepted_at=CURRENT_TIMESTAMP
                           WHERE id=?""", (user_id, invitation[0]))
        self.log_audit(invitation[1], user_id, invitation[2], "workspace.invitation.accept", "invitation", invitation[0])
        return {"id": user_id, "tenant_id": invitation[1], "agent_id": invitation[2], "email": email,
                "display_name": display_name, "role": invitation[4], "status": "pending_approval"}

    def approve_workspace_invitation(self, tenant_id: str, invitation_id: str, approved_by: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT user_id, agent_id, email, role, status
                                FROM workspace_invitations
                                WHERE id=? AND tenant_id=?""", (invitation_id, tenant_id)).fetchone()
            if not row or row[4] != "accepted_pending" or not row[0]:
                return None
            user_id = row[0]
            conn.execute("UPDATE users SET status='active', updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                         (user_id, tenant_id))
            conn.execute("""UPDATE organization_memberships SET status='active'
                           WHERE tenant_id=? AND user_id=?""", (tenant_id, user_id))
            conn.execute("""UPDATE agent_memberships SET status='active'
                           WHERE tenant_id=? AND agent_id=? AND user_id=?""", (tenant_id, row[1], user_id))
            conn.execute("""UPDATE workspace_invitations
                           SET status='accepted', approved_by=?, approved_at=CURRENT_TIMESTAMP
                           WHERE id=? AND tenant_id=?""", (approved_by, invitation_id, tenant_id))
        self.log_audit(tenant_id, approved_by, row[1], "workspace.invitation.approve", "invitation", invitation_id,
                       {"user_id": user_id, "email": row[2], "role": row[3]})
        return {"id": user_id, "email": row[2], "role": row[3], "status": "active"}

    def revoke_workspace_invitation(self, tenant_id: str, invitation_id: str, revoked_by: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT user_id, agent_id, status FROM workspace_invitations
                                WHERE id=? AND tenant_id=?""", (invitation_id, tenant_id)).fetchone()
            if not row or row[2] not in {"pending", "accepted_pending"}:
                return False
            conn.execute("UPDATE workspace_invitations SET status='revoked' WHERE id=? AND tenant_id=?",
                         (invitation_id, tenant_id))
            if row[0]:
                conn.execute("UPDATE users SET status='deactivated', updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                             (row[0], tenant_id))
                conn.execute("UPDATE organization_memberships SET status='disabled' WHERE tenant_id=? AND user_id=?",
                             (tenant_id, row[0]))
                conn.execute("UPDATE agent_memberships SET status='disabled' WHERE tenant_id=? AND user_id=?",
                             (tenant_id, row[0]))
                conn.execute("UPDATE auth_sessions SET revoked_at=CURRENT_TIMESTAMP WHERE tenant_id=? AND user_id=? AND revoked_at IS NULL",
                             (tenant_id, row[0]))
        self.log_audit(tenant_id, revoked_by, row[1], "workspace.invitation.revoke", "invitation", invitation_id,
                       {"user_id": row[0] or ""})
        return True

    def resend_workspace_invitation(self, tenant_id: str, invitation_id: str, invited_by: str,
                                    expires_hours: int = 72) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT email, role, agent_id, status FROM workspace_invitations
                                WHERE id=? AND tenant_id=?""", (invitation_id, tenant_id)).fetchone()
            if not row or row[3] not in {"pending", "revoked"}:
                return None
            conn.execute("UPDATE workspace_invitations SET status='revoked' WHERE id=? AND tenant_id=?",
                         (invitation_id, tenant_id))
        return self.create_workspace_invitation(tenant_id, row[0], row[1], invited_by, row[2], expires_hours)

    # ---- SSO / 企业身份集成 ----
    @staticmethod
    def _encrypt_secret(value: str) -> str:
        from llm_config_manager import _encrypt_api_key
        return _encrypt_api_key(value) if value else ""

    @staticmethod
    def _decrypt_secret(value: str) -> str:
        from llm_config_manager import _decrypt_api_key
        return _decrypt_api_key(value) if value else ""

    def list_sso_providers(self, enabled_only: bool = False, include_secrets: bool = False) -> list[dict]:
        sql = ("SELECT id, provider_type, name, config_json, secrets_json_enc, enabled, auto_provision, "
               "default_role, default_tenant_id, display_order, created_by, created_at, updated_at "
               "FROM sso_providers")
        params: list[object] = []
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY display_order, name"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try:
                config = json.loads(row[3] or "{}")
            except (TypeError, json.JSONDecodeError):
                config = {}
            item = {
                "id": row[0], "provider_type": row[1], "name": row[2], "config": config,
                "enabled": bool(row[5]), "auto_provision": bool(row[6]),
                "default_role": row[7], "default_tenant_id": row[8],
                "display_order": row[9], "created_by": row[10],
                "created_at": self._utc_to_local(row[11] or ""),
                "updated_at": self._utc_to_local(row[12] or ""),
                "has_secrets": bool(row[4]),
            }
            if include_secrets and row[4]:
                try:
                    item["secrets"] = json.loads(self._decrypt_secret(row[4]) or "{}")
                except Exception:
                    item["secrets"] = {}
            items.append(item)
        return items

    def get_sso_provider(self, provider_id: str, include_secrets: bool = False) -> dict | None:
        for item in self.list_sso_providers(include_secrets=include_secrets):
            if item["id"] == provider_id:
                return item
        return None

    def save_sso_provider(self, data: dict, provider_id: str = "") -> dict:
        from sso_provider import validate_ldap_config, validate_oidc_config
        provider_type = str(data.get("provider_type") or "").strip().lower()
        name = str(data.get("name") or "").strip()[:120]
        if provider_type not in {"ldap", "oidc"}:
            raise ValueError("SSO 类型仅支持 ldap 或 oidc")
        if not name:
            raise ValueError("SSO 名称不能为空")
        config = dict(data.get("config") or {})
        secrets = dict(data.get("secrets") or {})
        existing = self.get_sso_provider(provider_id, include_secrets=True) if provider_id else None
        if provider_type == "ldap":
            config = validate_ldap_config(config)
            if not secrets.get("bind_password"):
                secrets["bind_password"] = (existing or {}).get("secrets", {}).get("bind_password", "")
        else:
            config = validate_oidc_config(config)
            if not secrets.get("client_secret"):
                secrets["client_secret"] = (existing or {}).get("secrets", {}).get("client_secret", "")
        provider_id = provider_id or "sso-" + uuid.uuid4().hex[:12]
        default_role = str(data.get("default_role") or "user").strip()
        if default_role not in {"user", "agent_admin", "org_admin"}:
            default_role = "user"
        default_tenant_id = str(data.get("default_tenant_id") or "").strip()
        secrets_enc = self._encrypt_secret(json.dumps(secrets, ensure_ascii=False))
        with sqlite3.connect(self._db_path) as conn:
            exists = conn.execute("SELECT 1 FROM sso_providers WHERE id=?", (provider_id,)).fetchone()
            if exists:
                conn.execute("""
                    UPDATE sso_providers SET provider_type=?, name=?, config_json=?, secrets_json_enc=?,
                        enabled=?, auto_provision=?, default_role=?, default_tenant_id=?, display_order=?,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                """, (provider_type, name, json.dumps(config, ensure_ascii=False), secrets_enc,
                      int(bool(data.get("enabled"))), int(bool(data.get("auto_provision"))),
                      default_role, default_tenant_id, int(data.get("display_order") or 0), provider_id))
            else:
                conn.execute("""
                    INSERT INTO sso_providers
                        (id, provider_type, name, config_json, secrets_json_enc, enabled,
                         auto_provision, default_role, default_tenant_id, display_order, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (provider_id, provider_type, name, json.dumps(config, ensure_ascii=False), secrets_enc,
                      int(bool(data.get("enabled"))), int(bool(data.get("auto_provision"))),
                      default_role, default_tenant_id, int(data.get("display_order") or 0),
                      str(data.get("created_by") or "admin")))
        self.log_audit(str(data.get("tenant_id") or "local-default"), "", provider_id,
                       "sso.provider.save", "sso_provider", provider_id,
                       {"provider_type": provider_type, "name": name})
        return self.get_sso_provider(provider_id, include_secrets=False) or {}

    def update_sso_provider_status(self, provider_id: str, enabled: bool) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE sso_providers SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (int(bool(enabled)), provider_id),
            )
        return cur.rowcount > 0

    def delete_sso_provider(self, provider_id: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("DELETE FROM sso_providers WHERE id=?", (provider_id,))
        return cur.rowcount > 0

    def create_oauth_state(self, provider_id: str, redirect_uri: str = "", ttl_seconds: int = 600) -> str:
        state = secrets.token_urlsafe(32)
        ttl = max(60, min(int(ttl_seconds), 3600))
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("DELETE FROM sso_oauth_states WHERE expires_at <= CURRENT_TIMESTAMP")
            conn.execute("""
                INSERT INTO sso_oauth_states (state, provider_id, redirect_uri, expires_at)
                VALUES (?, ?, ?, datetime('now', ?))
            """, (state, provider_id, redirect_uri or "", f"+{ttl} seconds"))
        return state

    def consume_oauth_state(self, state: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT provider_id, redirect_uri FROM sso_oauth_states
                WHERE state=? AND expires_at > CURRENT_TIMESTAMP
            """, (state,)).fetchone()
            if row:
                conn.execute("DELETE FROM sso_oauth_states WHERE state=?", (state,))
        if not row:
            return None
        return {"provider_id": row[0], "redirect_uri": row[1]}

    def find_sso_identity(self, provider_id: str, subject: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT l.id, l.provider_id, l.subject, l.email, l.display_name, l.user_id, l.status,
                       u.tenant_id, u.role, a.id
                FROM sso_identity_links l
                LEFT JOIN users u ON u.id=l.user_id
                LEFT JOIN organization_memberships om ON om.user_id=u.id AND om.tenant_id=u.tenant_id
                LEFT JOIN agent_memberships am ON am.user_id=u.id AND am.tenant_id=om.tenant_id
                LEFT JOIN agents a ON a.id=am.agent_id AND a.tenant_id=am.tenant_id
                WHERE l.provider_id=? AND l.subject=?
                ORDER BY am.created_at LIMIT 1
            """, (provider_id, subject)).fetchone()
        if not row:
            return None
        return {
            "link_id": row[0], "provider_id": row[1], "subject": row[2], "email": row[3],
            "display_name": row[4], "user_id": row[5] or "", "status": row[6],
            "tenant_id": row[7] or "", "role": row[8] or "user", "agent_id": row[9] or "",
        }

    def _provision_sso_user(self, email: str, display_name: str, role: str,
                            default_tenant_id: str = "") -> dict:
        email = self._normalize_account_email(email, "SSO 邮箱")
        display_name = str(display_name or "").strip()[:80]
        role = role if role in {"user", "agent_admin", "org_admin"} else "user"
        with sqlite3.connect(self._db_path) as conn:
            existing = conn.execute(
                "SELECT id, tenant_id FROM users WHERE email=? AND status='active'", (email,),
            ).fetchone()
            if existing:
                user_id, tenant_id = existing[0], existing[1]
                agent = conn.execute("""
                    SELECT a.id FROM agent_memberships am
                    JOIN agents a ON a.id=am.agent_id AND a.tenant_id=am.tenant_id AND a.status='active'
                    JOIN organization_memberships om ON om.tenant_id=am.tenant_id AND om.user_id=am.user_id
                    WHERE am.user_id=? AND am.tenant_id=? AND am.status='active' AND om.status='active'
                    ORDER BY am.created_at LIMIT 1
                """, (user_id, tenant_id)).fetchone()
                if not agent:
                    agent_id = "agt-" + uuid.uuid4().hex[:12]
                    conn.execute("INSERT INTO agents (id, tenant_id, name) VALUES (?, ?, ?)",
                                 (agent_id, tenant_id, "安枢默认 Agent"))
                    conn.execute("""
                        INSERT INTO agent_memberships (tenant_id, agent_id, user_id, role)
                        VALUES (?, ?, ?, ?)
                    """, (tenant_id, agent_id, user_id, role))
                else:
                    agent_id = agent[0]
                return {"user_id": user_id, "tenant_id": tenant_id, "agent_id": agent_id,
                        "email": email, "display_name": display_name, "role": role, "created": False}
            tenant_id = str(default_tenant_id or "").strip()
            if tenant_id and not conn.execute("SELECT 1 FROM organizations WHERE id=?", (tenant_id,)).fetchone():
                tenant_id = ""
            user_id = "usr-" + uuid.uuid4().hex[:12]
            if not tenant_id:
                tenant_id = "org-" + uuid.uuid4().hex[:12]
                conn.execute("INSERT INTO organizations (id, name) VALUES (?, ?)",
                             (tenant_id, f"{display_name or email.split('@')[0]} 的工作区"))
            agent_id = "agt-" + uuid.uuid4().hex[:12]
            conn.execute("""
                INSERT INTO users (id, tenant_id, email, display_name, password_hash, role)
                VALUES (?, ?, ?, ?, '', ?)
            """, (user_id, tenant_id, email, display_name, role))
            conn.execute("""
                INSERT INTO organization_memberships (tenant_id, user_id, role)
                VALUES (?, ?, ?)
            """, (tenant_id, user_id, role))
            conn.execute("INSERT INTO agents (id, tenant_id, name) VALUES (?, ?, ?)",
                         (agent_id, tenant_id, "安枢默认 Agent"))
            conn.execute("""
                INSERT INTO agent_memberships (tenant_id, agent_id, user_id, role)
                VALUES (?, ?, ?, ?)
            """, (tenant_id, agent_id, user_id, role))
        return {"user_id": user_id, "tenant_id": tenant_id, "agent_id": agent_id,
                "email": email, "display_name": display_name, "role": role, "created": True}

    def link_sso_identity(self, provider_id: str, subject: str, email: str,
                          display_name: str = "", auto_provision: bool = False,
                          default_role: str = "user", default_tenant_id: str = "") -> dict:
        subject = str(subject or "").strip()
        email = self._normalize_account_email(email, "SSO 邮箱")
        display_name = str(display_name or "").strip()[:80]
        if not subject:
            raise ValueError("SSO 身份信息不完整")
        existing = self.find_sso_identity(provider_id, subject)
        if existing:
            if existing["status"] == "active" and existing["user_id"]:
                return {**existing, "result": "active"}
            if existing["status"] == "pending":
                return {**existing, "result": "pending"}
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    UPDATE sso_identity_links SET status='pending', email=?, display_name=?,
                        updated_at=CURRENT_TIMESTAMP WHERE id=?
                """, (email, display_name, existing["link_id"]))
            return {**existing, "email": email, "display_name": display_name,
                    "status": "pending", "result": "pending"}
        link_id = "sso-link-" + uuid.uuid4().hex[:12]
        with sqlite3.connect(self._db_path) as conn:
            local = conn.execute(
                "SELECT id, tenant_id FROM users WHERE email=? AND status='active'", (email,),
            ).fetchone()
        if local:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO sso_identity_links (id, provider_id, subject, email, display_name, user_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'active')
                """, (link_id, provider_id, subject, email, display_name, local[0]))
            linked = self.find_sso_identity(provider_id, subject)
            return {**(linked or {}), "result": "active"}
        if auto_provision:
            provisioned = self._provision_sso_user(email, display_name, default_role, default_tenant_id)
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("""
                    INSERT INTO sso_identity_links (id, provider_id, subject, email, display_name, user_id, status)
                    VALUES (?, ?, ?, ?, ?, ?, 'active')
                """, (link_id, provider_id, subject, email, display_name, provisioned["user_id"]))
            linked = self.find_sso_identity(provider_id, subject)
            return {**(linked or {}), "result": "provisioned"}
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO sso_identity_links (id, provider_id, subject, email, display_name, user_id, status)
                VALUES (?, ?, ?, ?, ?, '', 'pending')
            """, (link_id, provider_id, subject, email, display_name))
        return {"link_id": link_id, "provider_id": provider_id, "subject": subject,
                "email": email, "display_name": display_name, "user_id": "",
                "status": "pending", "result": "pending"}

    def list_sso_pending_approvals(self) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT l.id, l.provider_id, p.name, p.provider_type, l.subject, l.email,
                       l.display_name, l.created_at, l.updated_at
                FROM sso_identity_links l JOIN sso_providers p ON p.id=l.provider_id
                WHERE l.status='pending'
                ORDER BY l.created_at
            """).fetchall()
        return [{
            "link_id": row[0], "provider_id": row[1], "provider_name": row[2],
            "provider_type": row[3], "subject": row[4], "email": row[5],
            "display_name": row[6], "created_at": self._utc_to_local(row[7] or ""),
            "updated_at": self._utc_to_local(row[8] or ""),
        } for row in rows]

    def approve_sso_identity_link(self, link_id: str, default_role: str = "",
                                  default_tenant_id: str = "") -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT l.provider_id, l.subject, l.email, l.display_name, l.user_id,
                       p.default_role, p.default_tenant_id
                FROM sso_identity_links l JOIN sso_providers p ON p.id=l.provider_id
                WHERE l.id=? AND l.status='pending'
            """, (link_id,)).fetchone()
        if not row:
            return None
        provider_id, subject, email, display_name, existing_user_id, provider_role, provider_tenant = row
        role = str(default_role or provider_role or "user").strip()
        tenant_id = str(default_tenant_id or provider_tenant or "").strip()
        if existing_user_id:
            user_id = existing_user_id
        else:
            provisioned = self._provision_sso_user(email, display_name, role, tenant_id)
            user_id = provisioned["user_id"]
            tenant_id = provisioned["tenant_id"]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                UPDATE sso_identity_links SET user_id=?, status='active', updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (user_id, link_id))
        self.log_audit(tenant_id, user_id, "", "sso.identity.approve", "sso_identity", link_id,
                       {"provider_id": provider_id, "email": email})
        return self.find_sso_identity(provider_id, subject)

    def reject_sso_identity_link(self, link_id: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE sso_identity_links SET status='rejected', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (link_id,),
            )
        return cur.rowcount > 0

    def create_sso_session(self, identity: dict) -> str:
        user = {
            "id": identity.get("user_id", ""),
            "tenant_id": identity.get("tenant_id", ""),
            "agent_id": identity.get("agent_id", ""),
            "email": identity.get("email", ""),
            "display_name": identity.get("display_name", ""),
            "role": identity.get("role", "user"),
        }
        return self.create_auth_session(user)

    # ---- 站内通知中心 ----
    def create_notification(self, tenant_id: str, event_type: str, title: str,
                            body: str = "", severity: str = "info",
                            user_id: str = "", agent_id: str = "",
                            link_path: str = "", payload: dict | None = None) -> dict:
        severity = str(severity or "info").strip().lower()
        if severity not in {"info", "success", "warning", "error", "critical"}:
            severity = "info"
        notification_id = "ntf-" + uuid.uuid4().hex[:16]
        tenant_id = str(tenant_id or "local-default").strip()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO notification_events
                    (id, tenant_id, user_id, agent_id, event_type, severity,
                     title, body, link_path, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                notification_id, tenant_id, str(user_id or ""), str(agent_id or ""),
                str(event_type or "system.notice")[:80], severity,
                str(title or "系统通知")[:160], str(body or "")[:1000],
                str(link_path or "")[:500], json.dumps(payload or {}, ensure_ascii=False),
            ))
        return {
            "id": notification_id, "tenant_id": tenant_id, "user_id": str(user_id or ""),
            "agent_id": str(agent_id or ""), "event_type": str(event_type or "system.notice"),
            "severity": severity, "title": str(title or "系统通知"),
            "body": str(body or ""), "link_path": str(link_path or ""),
            "status": "unread", "payload": payload or {},
        }

    def list_notifications(self, tenant_id: str, user_id: str = "", agent_id: str = "",
                           include_tenant_wide: bool = True, status: str = "",
                           limit: int = 50) -> list[dict]:
        limit = max(1, min(int(limit or 50), 200))
        conditions = ["tenant_id=?"]
        params: list[object] = [str(tenant_id or "local-default")]
        if user_id:
            conditions.append("(user_id='' OR user_id=?)" if include_tenant_wide else "user_id=?")
            params.append(str(user_id))
        if agent_id:
            conditions.append("(agent_id='' OR agent_id=?)")
            params.append(str(agent_id))
        if status:
            allowed = {item.strip() for item in str(status).split(",")}
            allowed = allowed.intersection({"unread", "read", "archived"})
            if allowed:
                conditions.append("status IN (" + ",".join("?" for _ in allowed) + ")")
                params.extend(sorted(allowed))
        sql = """
            SELECT id, tenant_id, user_id, agent_id, event_type, severity, title, body,
                   link_path, payload_json, status, created_at, read_at, archived_at
            FROM notification_events
            WHERE """ + " AND ".join(conditions) + " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try:
                payload = json.loads(row[9] or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
            items.append({
                "id": row[0], "tenant_id": row[1], "user_id": row[2], "agent_id": row[3],
                "event_type": row[4], "severity": row[5], "title": row[6], "body": row[7],
                "link_path": row[8], "payload": payload, "status": row[10],
                "created_at": self._utc_to_local(row[11] or ""),
                "read_at": self._utc_to_local(row[12] or "") if row[12] else "",
                "archived_at": self._utc_to_local(row[13] or "") if row[13] else "",
            })
        return items

    def notification_summary(self, tenant_id: str, user_id: str = "", agent_id: str = "") -> dict:
        conditions = ["tenant_id=?", "status='unread'"]
        params: list[object] = [str(tenant_id or "local-default")]
        if user_id:
            conditions.append("(user_id='' OR user_id=?)")
            params.append(str(user_id))
        if agent_id:
            conditions.append("(agent_id='' OR agent_id=?)")
            params.append(str(agent_id))
        with sqlite3.connect(self._db_path) as conn:
            unread = conn.execute(
                "SELECT COUNT(*) FROM notification_events WHERE " + " AND ".join(conditions),
                params,
            ).fetchone()[0]
        return {"unread": int(unread or 0)}

    def update_notifications_status(self, tenant_id: str, notification_ids: list[str],
                                    status: str, user_id: str = "", admin: bool = False) -> int:
        status = str(status or "").strip()
        if status not in {"unread", "read", "archived"}:
            raise ValueError("通知状态仅支持 unread、read 或 archived")
        ids = [str(item).strip() for item in (notification_ids or []) if str(item).strip()]
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        conditions = ["tenant_id=?", f"id IN ({placeholders})"]
        params: list[object] = [str(tenant_id or "local-default"), *ids]
        if not admin and user_id:
            conditions.append("(user_id='' OR user_id=?)")
            params.append(str(user_id))
        timestamp_sql = "read_at=CURRENT_TIMESTAMP, archived_at=NULL" if status == "read" else (
            "read_at=NULL, archived_at=CURRENT_TIMESTAMP" if status == "archived" else
            "read_at=NULL, archived_at=NULL"
        )
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE notification_events SET status=?, " + timestamp_sql + " WHERE " + " AND ".join(conditions),
                [status, *params],
            )
        return int(cur.rowcount or 0)

    # ---- SMTP 通知 ----
    def save_email_notification_config(self, config: dict) -> None:
        from email_delivery import validate_email_config
        from llm_config_manager import _encrypt_api_key
        normalized = validate_email_config(config)
        password = str(config.get("password") or "")
        with sqlite3.connect(self._db_path) as conn:
            current = conn.execute("SELECT password_enc FROM email_notification_config WHERE id=1").fetchone()
            password_enc = _encrypt_api_key(password) if password else (current[0] if current else "")
            conn.execute("""
                INSERT INTO email_notification_config
                    (id, enabled, host, port, security, sender, username, password_enc,
                     recipients_json, events_json, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled, host=excluded.host,
                    port=excluded.port, security=excluded.security, sender=excluded.sender,
                    username=excluded.username, password_enc=excluded.password_enc,
                    recipients_json=excluded.recipients_json, events_json=excluded.events_json,
                    updated_at=CURRENT_TIMESTAMP
            """, (int(normalized["enabled"]), normalized["host"], normalized["port"], normalized["security"],
                  normalized["sender"], normalized["username"], password_enc,
                  json.dumps(normalized["recipients"], ensure_ascii=False),
                  json.dumps(normalized["events"], ensure_ascii=False)))

    def get_email_notification_config(self, include_secret: bool = False) -> dict:
        from llm_config_manager import _decrypt_api_key
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT enabled, host, port, security, sender, username, password_enc, recipients_json, events_json, updated_at FROM email_notification_config WHERE id=1").fetchone()
        if not row:
            return {"enabled": False, "host": "", "port": 465, "security": "ssl", "sender": "", "username": "", "recipients": [], "events": [], "has_password": False}
        try:
            recipients, events = json.loads(row[7] or "[]"), json.loads(row[8] or "[]")
        except (TypeError, json.JSONDecodeError):
            recipients, events = [], []
        result = {"enabled": bool(row[0]), "host": row[1], "port": row[2], "security": row[3], "sender": row[4], "username": row[5], "recipients": recipients, "events": events, "has_password": bool(row[6]), "updated_at": self._utc_to_local(row[9] or "")}
        if include_secret:
            result["password"] = _decrypt_api_key(row[6]) if row[6] else ""
        return result

    def record_email_delivery(self, tenant_id: str, event_type: str, notification_id: str, attempt: int, sent: bool, recipient_count: int = 0, error: str = "") -> dict:
        delivery_id = "mail-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("INSERT INTO email_notification_deliveries (id, tenant_id, event_type, notification_id, recipient_count, attempt, sent, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (delivery_id, str(tenant_id or "local-default"), str(event_type), str(notification_id or ""), int(recipient_count), int(attempt), int(bool(sent)), str(error or "")[:1000]))
        return {"id": delivery_id, "sent": bool(sent), "attempt": attempt, "error": str(error or "")}

    def list_email_deliveries(self, tenant_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT id, tenant_id, event_type, notification_id, recipient_count, attempt, sent, error, created_at FROM email_notification_deliveries"
        params: list[object] = []
        if tenant_id:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "tenant_id": r[1], "event_type": r[2], "notification_id": r[3], "recipient_count": r[4], "attempt": r[5], "sent": bool(r[6]), "error": r[7], "created_at": self._utc_to_local(r[8] or "")} for r in rows]

    # ---- 团队协作：只读分享、内部备注与答案修订 ----
    def create_conversation_share(self, tenant_id: str, conversation_id: str, created_by: str,
                                  expires_hours: int = 24, password: str = "",
                                  allow_copy: bool = True) -> dict:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM conversations WHERE id=? AND tenant_id=?",
                (conversation_id, tenant_id),
            ).fetchone()
        if not row:
            raise ValueError("对话不存在或不属于当前工作区")
        password = str(password or "")
        if password and len(password) < 8:
            raise ValueError("分享密码至少需要 8 位")
        hours = max(1, min(int(expires_hours or 24), 24 * 90))
        token = "shr_" + secrets.token_urlsafe(32)
        share_id = "share-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO conversation_shares
                    (id, conversation_id, tenant_id, token_hash, password_hash,
                     allow_copy, expires_at, created_by)
                VALUES (?, ?, ?, ?, ?, ?, datetime('now', ?), ?)
            """, (
                share_id, conversation_id, tenant_id,
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                self._password_hash(password) if password else "",
                int(bool(allow_copy)), f"+{hours} hours", str(created_by or ""),
            ))
        self.log_audit(tenant_id, str(created_by or ""), "", "conversation.share.create",
                       "conversation", conversation_id,
                       {"share_id": share_id, "expires_hours": hours, "allow_copy": bool(allow_copy)})
        return {
            "id": share_id, "conversation_id": conversation_id, "token": token,
            "expires_hours": hours, "allow_copy": bool(allow_copy), "password_protected": bool(password),
            "status": "active", "secret_shown_once": True,
        }

    def list_conversation_shares(self, tenant_id: str, conversation_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, allow_copy, status, expires_at, access_count, last_accessed_at,
                       created_by, created_at, password_hash
                FROM conversation_shares
                WHERE tenant_id=? AND conversation_id=?
                ORDER BY created_at DESC
            """, (tenant_id, conversation_id)).fetchall()
        return [{
            "id": row[0], "conversation_id": conversation_id, "allow_copy": bool(row[1]),
            "status": row[2], "expires_at": self._utc_to_local(row[3] or ""),
            "access_count": int(row[4] or 0),
            "last_accessed_at": self._utc_to_local(row[5] or "") if row[5] else "",
            "created_by": row[6], "created_at": self._utc_to_local(row[7] or ""),
            "password_protected": bool(row[8]),
        } for row in rows]

    def update_conversation_share_status(self, tenant_id: str, conversation_id: str,
                                         share_id: str, status: str) -> bool:
        if status not in {"active", "revoked"}:
            raise ValueError("分享状态仅支持 active 或 revoked")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE conversation_shares SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND conversation_id=? AND tenant_id=?
            """, (status, share_id, conversation_id, tenant_id))
        return cur.rowcount > 0

    def update_capability_extension(self, extension_id: str, version: str, source: str,
                                    description: str = "", manifest: dict | None = None,
                                    permissions: list | None = None, network_scope: str = "",
                                    changed_by: str = "", reason: str = "") -> dict | None:
        """Upgrade an extension and invalidate prior approval and grants."""
        current = self.get_capability_extension(extension_id)
        if not current:
            return None
        version = str(version or "").strip()[:80]
        source = str(source or "").strip()[:500]
        if not version or version == current["version"]:
            raise ValueError("升级必须提供与当前不同的新版本号")
        if not source:
            raise ValueError("扩展来源不能为空")
        if current["kind"] == "mcp":
            parsed = urllib.parse.urlparse(source)
            if parsed.scheme != "https" or not parsed.netloc:
                raise ValueError("MCP 服务地址必须使用 HTTPS")
        manifest = manifest if isinstance(manifest, dict) else {}
        permissions = permissions if isinstance(permissions, list) else []
        with sqlite3.connect(self._db_path) as conn:
            try:
                conn.execute("""
                    INSERT INTO capability_extension_versions (
                        extension_id, version, source, description, manifest_json,
                        permissions_json, network_scope, changed_by, change_reason
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (extension_id, version, source, str(description or "")[:2000],
                      json.dumps(manifest, ensure_ascii=False), json.dumps(permissions, ensure_ascii=False),
                      str(network_scope or "")[:1000], str(changed_by or "")[:120], str(reason or "")[:500]))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该版本已登记，不能重复升级") from exc
            conn.execute("""
                UPDATE capability_extensions SET version=?, source=?, description=?, manifest_json=?,
                    permissions_json=?, network_scope=?, status='pending_review', updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (version, source, str(description or "")[:2000], json.dumps(manifest, ensure_ascii=False),
                  json.dumps(permissions, ensure_ascii=False), str(network_scope or "")[:1000], extension_id))
            conn.execute("UPDATE capability_extension_grants SET enabled=0, updated_at=CURRENT_TIMESTAMP WHERE extension_id=?", (extension_id,))
        self.log_audit("platform", changed_by, action="extension.upgrade", resource_type="extension",
                       resource_id=extension_id, detail={"version": version, "reason": str(reason)[:500]})
        return self.get_capability_extension(extension_id)

    def uninstall_capability_extension(self, extension_id: str, changed_by: str = "") -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("UPDATE capability_extensions SET status='disabled', updated_at=CURRENT_TIMESTAMP WHERE id=?", (extension_id,))
            conn.execute("UPDATE capability_extension_grants SET enabled=0, updated_at=CURRENT_TIMESTAMP WHERE extension_id=?", (extension_id,))
        if cur.rowcount:
            self.log_audit("platform", changed_by, action="extension.uninstall", resource_type="extension", resource_id=extension_id)
        return bool(cur.rowcount)

    def record_ingestion_stage_event(self, job_id: str, document_id: str,
                                     stage: str, status: str, metrics: dict | None = None,
                                     error: str = "", error_type: str = "") -> bool:
        """Append a safe, structured stage trace without persisting document content."""
        allowed_stages = {"starting", "parsing", "cleaning", "incremental_index", "done"}
        allowed_statuses = {"pending", "processing", "completed", "failed"}
        if stage not in allowed_stages or status not in allowed_statuses:
            raise ValueError("入库 Trace 状态不合法")
        metrics_json = json.dumps(metrics or {}, ensure_ascii=False, separators=(",", ":"))[:4000]
        safe_error = str(error or "")[:500]
        safe_type = str(error_type or "")[:80]
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """SELECT id, attempt, status, started_at FROM ingestion_stage_events
                   WHERE job_id=? AND document_id=? AND stage=? ORDER BY id DESC LIMIT 1""",
                (job_id, document_id, stage),
            ).fetchone()
            if status == "processing":
                if row and row[2] == "processing":
                    cur = conn.execute(
                        """UPDATE ingestion_stage_events
                           SET metrics_json=?, error_type=?, error_message=? WHERE id=?""",
                        (metrics_json, safe_type, safe_error, row[0]),
                    )
                    return cur.rowcount > 0
                attempt = (int(row[1]) if row else 0) + 1
                cur = conn.execute(
                    """INSERT INTO ingestion_stage_events
                       (job_id, document_id, stage, status, attempt, started_at,
                        metrics_json, error_type, error_message)
                       VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, ?, ?, ?)""",
                    (job_id, document_id, stage, status, attempt, metrics_json, safe_type, safe_error),
                )
                return cur.rowcount > 0

            if row and row[2] == "processing":
                duration = conn.execute(
                    "SELECT CAST((julianday(CURRENT_TIMESTAMP)-julianday(?))*86400000 AS INTEGER)",
                    (row[3],),
                ).fetchone()[0] or 0
                cur = conn.execute(
                    """UPDATE ingestion_stage_events
                       SET status=?, finished_at=CURRENT_TIMESTAMP, duration_ms=?,
                           metrics_json=?, error_type=?, error_message=? WHERE id=?""",
                    (status, max(0, int(duration)), metrics_json, safe_type, safe_error, row[0]),
                )
                return cur.rowcount > 0

            if row and row[2] == status:
                cur = conn.execute(
                    """UPDATE ingestion_stage_events
                       SET metrics_json=?, error_type=?, error_message=? WHERE id=?""",
                    (metrics_json, safe_type, safe_error, row[0]),
                )
                return cur.rowcount > 0

            attempt = int(row[1]) if row else 1
            cur = conn.execute(
                """INSERT INTO ingestion_stage_events
                   (job_id, document_id, stage, status, attempt, started_at,
                    finished_at, duration_ms, metrics_json, error_type, error_message)
                   VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, 0, ?, ?, ?)""",
                (job_id, document_id, stage, status, attempt, metrics_json, safe_type, safe_error),
            )
        return cur.rowcount > 0

    def list_ingestion_stage_events(self, job_id: str, tenant_id: str | None = None,
                                    document_id: str | None = None) -> list[dict]:
        """Return a tenant-scoped ingestion timeline; legacy jobs return an empty list."""
        with sqlite3.connect(self._db_path) as conn:
            scope = conn.execute("SELECT tenant_id FROM ingestion_jobs WHERE id=?", (job_id,)).fetchone()
            if not scope or (tenant_id is not None and scope[0] != tenant_id):
                return []
            sql = """SELECT id, document_id, stage, status, attempt, started_at,
                            finished_at, duration_ms, metrics_json, error_type,
                            error_message, created_at
                     FROM ingestion_stage_events WHERE job_id=?"""
            params: list[object] = [job_id]
            if document_id:
                sql += " AND document_id=?"
                params.append(document_id)
            sql += " ORDER BY id"
            rows = conn.execute(sql, params).fetchall()
        events = []
        for row in rows:
            try:
                metrics = json.loads(row[8] or "{}")
            except (TypeError, json.JSONDecodeError):
                metrics = {}
            events.append({"id": row[0], "document_id": row[1], "stage": row[2],
                           "status": row[3], "attempt": row[4], "started_at": row[5],
                           "finished_at": row[6], "duration_ms": row[7], "metrics": metrics,
                           "error_type": row[9], "error": row[10], "created_at": row[11]})
        return events

    def resolve_conversation_share(self, token: str, password: str = "") -> dict | None:
        token_hash = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT id, conversation_id, tenant_id, password_hash, allow_copy, expires_at
                FROM conversation_shares
                WHERE token_hash=? AND status='active' AND expires_at > CURRENT_TIMESTAMP
            """, (token_hash,)).fetchone()
            if not row:
                return None
            if row[3] and not self._password_matches(str(password or ""), row[3]):
                return {"password_required": True}
            conn.execute("""
                UPDATE conversation_shares
                SET access_count=access_count+1, last_accessed_at=CURRENT_TIMESTAMP,
                    updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (row[0],))
        detail = self.get_conversation_detail(row[1])
        if not detail:
            return None
        safe_messages = [{
            "role": message["role"], "content": message["content"],
            "created_at": message.get("created_at", ""),
        } for message in detail.get("messages") or []]
        return {
            "share": {
                "id": row[0], "allow_copy": bool(row[4]),
                "expires_at": self._utc_to_local(row[5] or ""),
            },
            "conversation": {
                "id": detail["id"], "title": detail["title"],
                "created_at": detail["created_at"], "updated_at": detail["updated_at"],
                "messages": safe_messages,
            },
        }

    def create_conversation_note(self, tenant_id: str, conversation_id: str, author_id: str,
                                 content: str, author_role: str = "admin") -> dict:
        content = str(content or "").strip()
        if not content:
            raise ValueError("备注不能为空")
        if len(content) > 2000:
            raise ValueError("备注不能超过 2000 字")
        with sqlite3.connect(self._db_path) as conn:
            exists = conn.execute(
                "SELECT 1 FROM conversations WHERE id=? AND tenant_id=?",
                (conversation_id, tenant_id),
            ).fetchone()
            if not exists:
                raise ValueError("对话不存在或不属于当前工作区")
            note_id = "note-" + uuid.uuid4().hex[:16]
            conn.execute("""
                INSERT INTO conversation_notes
                    (id, conversation_id, tenant_id, author_id, author_role, content)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (note_id, conversation_id, tenant_id, str(author_id or ""),
                  str(author_role or "admin")[:40], content))
        self.log_audit(tenant_id, str(author_id or ""), "", "conversation.note.create",
                       "conversation", conversation_id, {"note_id": note_id})
        return {"id": note_id, "conversation_id": conversation_id, "author_id": str(author_id or ""),
                "author_role": str(author_role or "admin"), "content": content}

    def list_conversation_notes(self, tenant_id: str, conversation_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, author_id, author_role, content, created_at, updated_at
                FROM conversation_notes
                WHERE tenant_id=? AND conversation_id=?
                ORDER BY created_at DESC, id DESC
            """, (tenant_id, conversation_id)).fetchall()
        return [{
            "id": row[0], "conversation_id": conversation_id, "author_id": row[1],
            "author_role": row[2], "content": row[3],
            "created_at": self._utc_to_local(row[4] or ""),
            "updated_at": self._utc_to_local(row[5] or ""),
        } for row in rows]

    def create_answer_revision(self, tenant_id: str, conversation_id: str, message_id: int,
                               revised_content: str, correction_note: str,
                               created_by: str) -> dict:
        revised_content = str(revised_content or "").strip()
        if not revised_content:
            raise ValueError("修订内容不能为空")
        if len(revised_content) > 20000:
            raise ValueError("修订内容不能超过 20000 字")
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT m.content
                FROM messages m JOIN conversations c ON c.id=m.conversation_id
                WHERE m.id=? AND m.conversation_id=? AND c.tenant_id=? AND m.role='assistant'
            """, (int(message_id), conversation_id, tenant_id)).fetchone()
            if not row:
                raise ValueError("Agent 回答不存在或不属于当前工作区")
            version = int(conn.execute(
                "SELECT COALESCE(MAX(version), 0)+1 FROM answer_revisions WHERE message_id=?",
                (int(message_id),),
            ).fetchone()[0])
            revision_id = "rev-" + uuid.uuid4().hex[:16]
            conn.execute("""
                INSERT INTO answer_revisions
                    (id, conversation_id, message_id, tenant_id, version, original_content,
                     revised_content, correction_note, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (revision_id, conversation_id, int(message_id), tenant_id, version, row[0],
                  revised_content, str(correction_note or "")[:2000], str(created_by or "")))
        self.log_audit(tenant_id, str(created_by or ""), "", "answer.revision.create",
                       "message", str(message_id), {"revision_id": revision_id, "version": version})
        return {
            "id": revision_id, "conversation_id": conversation_id, "message_id": int(message_id),
            "version": version, "original_content": row[0], "revised_content": revised_content,
            "correction_note": str(correction_note or ""), "created_by": str(created_by or ""),
        }

    def list_answer_revisions(self, tenant_id: str, conversation_id: str,
                              message_id: int | None = None) -> list[dict]:
        sql = """
            SELECT id, message_id, version, original_content, revised_content,
                   correction_note, created_by, created_at
            FROM answer_revisions
            WHERE tenant_id=? AND conversation_id=?
        """
        params: list[object] = [tenant_id, conversation_id]
        if message_id is not None:
            sql += " AND message_id=?"
            params.append(int(message_id))
        sql += " ORDER BY message_id, version DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{
            "id": row[0], "conversation_id": conversation_id, "message_id": row[1],
            "version": row[2], "original_content": row[3], "revised_content": row[4],
            "correction_note": row[5], "created_by": row[6],
            "created_at": self._utc_to_local(row[7] or ""),
        } for row in rows]

    def create_answer_revision_draft(self, tenant_id: str, conversation_id: str, message_id: int,
                                    proposed_content: str, correction_note: str, model: str,
                                    prompt_version: int, created_by: str) -> dict:
        proposed_content = str(proposed_content or '').strip()
        if not proposed_content:
            raise ValueError('草稿内容不能为空')
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute('''SELECT m.content FROM messages m JOIN conversations c ON c.id=m.conversation_id
                                  WHERE m.id=? AND m.conversation_id=? AND c.tenant_id=? AND m.role='assistant' ''',
                               (int(message_id), conversation_id, tenant_id)).fetchone()
            if not row:
                raise ValueError('Agent 回答不存在或不属于当前工作区')
            draft_id = 'rd-' + uuid.uuid4().hex[:16]
            conn.execute('''INSERT INTO answer_revision_drafts
                (id, conversation_id, message_id, tenant_id, original_content, proposed_content,
                 correction_note, model, prompt_version, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (draft_id, conversation_id, int(message_id), tenant_id, row[0], proposed_content,
                 str(correction_note or '')[:2000], str(model or ''), int(prompt_version or 0), str(created_by or '')))
        return {'id': draft_id, 'conversation_id': conversation_id, 'message_id': int(message_id),
                'tenant_id': tenant_id, 'original_content': row[0], 'proposed_content': proposed_content,
                'correction_note': str(correction_note or ''), 'status': 'pending', 'model': str(model or ''),
                'prompt_version': int(prompt_version or 0), 'created_by': str(created_by or '')}

    def list_answer_revision_drafts(self, tenant_id: str, conversation_id: str,
                                    message_id: int | None = None) -> list[dict]:
        sql = '''SELECT id, message_id, original_content, proposed_content, correction_note, status,
                        model, prompt_version, created_by, reviewed_by, review_note, created_at, reviewed_at
                 FROM answer_revision_drafts WHERE tenant_id=? AND conversation_id=?'''
        params: list[object] = [tenant_id, conversation_id]
        if message_id is not None:
            sql += ' AND message_id=?'; params.append(int(message_id))
        sql += ' ORDER BY created_at DESC'
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{'id': r[0], 'conversation_id': conversation_id, 'message_id': r[1], 'original_content': r[2],
                 'proposed_content': r[3], 'correction_note': r[4], 'status': r[5], 'model': r[6],
                 'prompt_version': r[7], 'created_by': r[8], 'reviewed_by': r[9], 'review_note': r[10],
                 'created_at': self._utc_to_local(r[11] or ''), 'reviewed_at': self._utc_to_local(r[12] or '') if r[12] else ''}
                for r in rows]

    def decide_answer_revision_draft(self, tenant_id: str, draft_id: str, decision: str,
                                     reviewed_by: str, review_note: str = '') -> dict:
        decision = str(decision or '').strip().lower()
        if decision not in {'approve', 'reject'}:
            raise ValueError('草稿决定仅支持 approve 或 reject')
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute('''SELECT conversation_id, message_id, proposed_content, correction_note,
                                        status FROM answer_revision_drafts WHERE id=? AND tenant_id=?''',
                               (draft_id, tenant_id)).fetchone()
            if not row:
                raise ValueError('修订草稿不存在或不属于当前工作区')
            if row[4] != 'pending':
                raise ValueError('修订草稿已经处理')
            status = 'approved' if decision == 'approve' else 'rejected'
            conn.execute('''UPDATE answer_revision_drafts SET status=?, reviewed_by=?, review_note=?, reviewed_at=CURRENT_TIMESTAMP WHERE id=?''',
                         (status, str(reviewed_by or ''), str(review_note or '')[:2000], draft_id))
        revision = None
        if decision == 'approve':
            revision = self.create_answer_revision(tenant_id, row[0], int(row[1]), row[2], row[3], reviewed_by)
        return {'id': draft_id, 'status': status, 'revision': revision}

    def log_audit(self, tenant_id: str, user_id: str = "", agent_id: str = "", action: str = "",
                  resource_type: str = "", resource_id: str = "", detail: dict | None = None) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO audit_logs (tenant_id, user_id, agent_id, action, resource_type, resource_id, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, user_id or None, agent_id or None, action, resource_type, resource_id,
                  json.dumps(detail or {}, ensure_ascii=False)))

    def list_audit_logs(self, tenant_id: str, action: str = "", resource_type: str = "",
                        limit: int = 200) -> list[dict]:
        sql = """SELECT id, tenant_id, user_id, agent_id, action, resource_type,
                         resource_id, detail_json, created_at
                  FROM audit_logs WHERE tenant_id=?"""
        params: list[object] = [tenant_id]
        if action:
            sql += " AND action=?"
            params.append(action)
        if resource_type:
            sql += " AND resource_type=?"
            params.append(resource_type)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit or 200), 2000)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            try:
                detail = json.loads(row[7] or "{}")
            except (TypeError, json.JSONDecodeError):
                detail = {}
            result.append({"id": row[0], "tenant_id": row[1], "user_id": row[2] or "",
                           "agent_id": row[3] or "", "action": row[4],
                           "resource_type": row[5], "resource_id": row[6], "detail": detail,
                           "created_at": self._utc_to_local(row[8] or "")})
        return result

    def conversation_belongs_to(self, conversation_id: str, tenant_id: str, user_id: str,
                                agent_id: str | None = None) -> bool:
        sql = "SELECT 1 FROM conversations WHERE id=? AND tenant_id=? AND user_id=?"
        params: list[str] = [conversation_id, tenant_id, user_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        with sqlite3.connect(self._db_path) as conn:
            return conn.execute(sql, params).fetchone() is not None

    def message_belongs_to(self, message_id: int, tenant_id: str, user_id: str,
                           agent_id: str | None = None) -> bool:
        """Check message ownership through its conversation boundary."""
        sql = """
            SELECT 1
            FROM messages m
            JOIN conversations c ON c.id = m.conversation_id
            WHERE m.id=? AND c.tenant_id=? AND c.user_id=?
        """
        params: list[object] = [message_id, tenant_id, user_id]
        if agent_id:
            sql += " AND c.agent_id=?"
            params.append(agent_id)
        with sqlite3.connect(self._db_path) as conn:
            return conn.execute(sql, params).fetchone() is not None

    def add_message(
        self,
        conversation_id: str,
        role: str,
        content: str,
        sources: Optional[list] = None,
    ) -> int:
        sources_json = json.dumps(sources, ensure_ascii=False) if sources else None
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "INSERT INTO messages (conversation_id, role, content, sources) VALUES (?, ?, ?, ?)",
                (conversation_id, role, content, sources_json),
            )
            msg_id = cur.lastrowid
            conn.execute(
                "UPDATE conversations SET updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (conversation_id,),
            )
        return msg_id

    def get_messages(self, conversation_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, role, content, sources, created_at FROM messages WHERE conversation_id=? ORDER BY id",
                (conversation_id,),
            ).fetchall()
        items = []
        for row in rows:
            try:
                sources = json.loads(row[3] or "[]")
            except (TypeError, json.JSONDecodeError):
                sources = []
            items.append({"id": row[0], "role": row[1], "content": row[2], "sources": sources,
                          "created_at": self._utc_to_local(row[4] or "")})
        return items

    def _update_last_message(
        self,
        conversation_id: str,
        content: str,
        sources: Optional[list] = None,
    ):
        """覆盖最后一条 assistant 消息的内容（用于来源核验后修正）"""
        sources_json = json.dumps(sources, ensure_ascii=False) if sources else None
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT id FROM messages WHERE conversation_id = ? AND role = 'assistant' ORDER BY id DESC LIMIT 1",
                (conversation_id,),
            ).fetchone()
            if row:
                conn.execute(
                    "UPDATE messages SET content = ?, sources = COALESCE(?, sources) WHERE id = ?",
                    (content, sources_json, row[0]),
                )

    def get_history(
        self,
        conversation_id: str,
        limit: Optional[int] = None,
    ) -> list[dict]:
        query = """
            SELECT role, content, sources, created_at
            FROM messages
            WHERE conversation_id = ?
            ORDER BY id
        """
        params = [conversation_id]
        if limit:
            query = f"""
                SELECT role, content, sources, created_at
                FROM messages
                WHERE conversation_id = ?
                ORDER BY id DESC
                LIMIT ?
            """
            params.append(limit)
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(query, params).fetchall()
        result = []
        for row in rows:
            msg = {"role": row[0], "content": row[1]}
            if row[2]:
                msg["sources"] = json.loads(row[2])
            result.append(msg)
        if limit:
            result.reverse()
        return result

    def get_compressed_history(
        self,
        conversation_id: str,
        llm_provider=None,
        keep_rounds: int = 5,
    ) -> list[dict]:
        """获取滑动窗口+压缩后的历史

        保留最近 keep_rounds 轮完整对话，更早的轮次压缩为单条摘要。
        返回格式兼容 get_history()：list[{"role": "user"|"assistant", "content": "..."}]
        """
        all_msgs = self.get_history(conversation_id)

        # 将平面消息列表按 (user, assistant) 配对分组为"轮次"
        rounds = []
        current_round = []
        for msg in all_msgs:
            current_round.append(msg)
            if msg["role"] == "assistant":
                rounds.append(current_round)
                current_round = []
        if current_round:
            rounds.append(current_round)

        if len(rounds) <= keep_rounds:
            return all_msgs

        # 需要压缩的旧轮次
        old_rounds = rounds[:-keep_rounds]
        recent_rounds = rounds[-keep_rounds:]

        # 尝试压缩旧轮次
        compressed = self._compress_rounds(old_rounds, llm_provider)

        result = [{"role": "user", "content": f"[历史摘要] {compressed}"}]
        for r in recent_rounds:
            result.extend(r)
        return result

    def _compress_rounds(self, rounds: list[list[dict]], llm_provider=None) -> str:
        """将多轮对话压缩为一段摘要"""
        combined = ""
        for r in rounds:
            for msg in r:
                prefix = "用户：" if msg["role"] == "user" else "助手："
                content = msg["content"][:200]
                combined += f"{prefix}{content}\n"

        if not combined.strip():
            return "无历史对话"

        if llm_provider is None:
            # 不用 LLM 压缩时，用关键词提取方案
            key_points = []
            lines = combined.strip().split("\n")
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                key_points.append(line[:60])
            return "；".join(key_points[:5])

        try:
            self.ensure_prompt_assets([{
                "slot": "memory_summary", "name": "记忆概要", "model_role": "chat",
                "description": "将历史对话压缩为可控长度的 L2 概要。",
                "template": _SUMMARY_PROMPT, "variables": ["conversation"],
            }])
            asset = self.get_active_prompt_asset("memory_summary", _SUMMARY_PROMPT)
            prompt = asset["template"].format(conversation=combined[:2000])
            result = llm_provider.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=100,
                timeout=30,
            )
            if isinstance(result, dict):
                usage = result.get("usage") or {}
                self.record_llm_usage_event(
                    tenant_id="local-default", module="memory_summary",
                    model=result.get("model") or getattr(llm_provider, "model", ""),
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )
                return str(result.get("content") or "").strip()
            return str(result).strip()
        except Exception as e:
            # LLM 压缩失败时回退
            logger.warning(f"LLM 压缩失败，回退到关键词提取: {e}")
            return combined[:200]

    # ---- 跨会话关键记忆 ----
    def get_session_memory(self, conversation_id: str) -> dict:
        """获取跨会话记忆（用户角色、提及的标准、偏好等）"""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT memory_data FROM session_memory WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()
        if row and row[0]:
            return json.loads(row[0])
        return {}

    def save_session_memory(self, conversation_id: str, memory_data: dict):
        """保存跨会话记忆，增量合并"""
        existing = self.get_session_memory(conversation_id)
        existing.update(memory_data)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE session_memory SET memory_data = ?, updated_at = CURRENT_TIMESTAMP WHERE conversation_id = ?",
                (json.dumps(existing, ensure_ascii=False), conversation_id),
            )

    def extract_and_save_memory(self, conversation_id: str, query: str, answer: str):
        """从本轮问答中提取关键记忆并保存

        抽取：
          - user_role: 用户自称的角色
          - mentioned_standards: 提到的法规标准
          - key_preferences: 用户的偏好关键词
        """
        import re
        memory = {}
        # 提取角色
        role_patterns = [
            r"(?:我是|我是一名?|我负责)\s*([^\s，。；,;]{2,10}(?:工程师|主管|经理|专员|负责人|管理员))"
        ]
        for pat in role_patterns:
            m = re.search(pat, query)
            if m:
                memory["user_role"] = m.group(1)
                break

        # 提取标准编号
        doc_ids = re.findall(r"[A-Z]+/[A-Z]?\s*\d+[-]?\d*", query + " " + answer)
        if doc_ids:
            existing = self.get_session_memory(conversation_id)
            prev = set(existing.get("mentioned_standards", []))
            all_standards = list(prev | set(doc_ids))
            memory["mentioned_standards"] = all_standards

        if memory:
            self.save_session_memory(conversation_id, memory)

        scope = self._conversation_scope(conversation_id)
        if not scope:
            return
        if not self.long_term_memory_enabled(**scope):
            return
        if memory.get("user_role"):
            self.write_long_term_memory(
                **scope,
                memory_type="fact",
                content=f"用户角色：{memory['user_role']}",
                tags=["user_role"],
                source_conversation_id=conversation_id,
                importance=0.8,
                confidence=0.8,
            )
        query_doc_ids = re.findall(r"[A-Z]+/[A-Z]?\s*\d+[-]?\d*", query)
        for document_id in sorted(set(query_doc_ids)):
            self.write_long_term_memory(
                **scope,
                memory_type="semantic",
                content=f"用户关注的网络安全标准：{document_id}",
                tags=["security_standard", document_id],
                source_conversation_id=conversation_id,
                importance=0.55,
                confidence=0.7,
            )

    def _conversation_scope(self, conversation_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT tenant_id, user_id, agent_id FROM conversations WHERE id=?",
                (conversation_id,),
            ).fetchone()
        if not row:
            return None
        return {"tenant_id": row[0], "user_id": row[1], "agent_id": row[2]}

    def write_long_term_memory(
        self,
        tenant_id: str,
        user_id: str,
        agent_id: str,
        memory_type: str,
        content: str,
        tags: list[str] | None = None,
        source_conversation_id: str | None = None,
        importance: float = 0.5,
        confidence: float = 0.7,
    ) -> int:
        """Store a governed, scoped long-term memory with conservative deduplication."""
        if memory_type not in {"fact", "preference", "episode", "semantic"}:
            raise ValueError("不支持的长期记忆类型")
        content = str(content or "").strip()[:1000]
        if not content:
            raise ValueError("长期记忆内容不能为空")
        clean_tags = sorted({str(tag).strip()[:80] for tag in (tags or []) if str(tag).strip()})[:12]
        importance = max(0.0, min(1.0, float(importance)))
        confidence = max(0.0, min(1.0, float(confidence)))
        with sqlite3.connect(self._db_path) as conn:
            existing = conn.execute("""
                SELECT id FROM long_term_memories
                WHERE tenant_id=? AND user_id=? AND agent_id=?
                  AND memory_type=? AND content=? AND status='active'
            """, (tenant_id, user_id, agent_id, memory_type, content)).fetchone()
            if existing:
                conn.execute("""
                    UPDATE long_term_memories
                    SET tags_json=?, source_conversation_id=?, importance=?, confidence=?,
                        updated_at=CURRENT_TIMESTAMP
                    WHERE id=?
                """, (json.dumps(clean_tags, ensure_ascii=False), source_conversation_id,
                      importance, confidence, existing[0]))
                return existing[0]
            cur = conn.execute("""
                INSERT INTO long_term_memories (
                    tenant_id, user_id, agent_id, memory_type, content, tags_json,
                    source_conversation_id, importance, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (tenant_id, user_id, agent_id, memory_type, content,
                  json.dumps(clean_tags, ensure_ascii=False), source_conversation_id,
                  importance, confidence))
        memory_id = cur.lastrowid
        self.log_audit(tenant_id, user_id, agent_id, "memory.write", "long_term_memory",
                       str(memory_id), {"memory_type": memory_type, "source": "auto"})
        return memory_id

    def create_memory_profile_proposal(self, tenant_id: str, user_id: str, agent_id: str,
                                       fields: dict, rationale: str = "",
                                       prompt_version: int = 0,
                                       source_conversation_id: str = "") -> dict:
        if not isinstance(fields, dict) or not fields:
            raise ValueError("画像提议不能为空")
        proposal_id = "mpp-" + uuid.uuid4().hex[:16]
        clean_fields = {str(key)[:80]: str(value)[:300] for key, value in fields.items() if str(value).strip()}
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO memory_profile_proposals
                (id, tenant_id, user_id, agent_id, source_conversation_id, fields_json,
                 rationale, prompt_version) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (proposal_id, tenant_id, user_id, agent_id, str(source_conversation_id or "")[:120],
                 json.dumps(clean_fields, ensure_ascii=False), str(rationale or "")[:2000], int(prompt_version or 0)))
        self.log_audit(tenant_id, user_id, agent_id, "memory.profile.proposal.create",
                       "memory_profile_proposal", proposal_id, {"field_count": len(clean_fields), "prompt_version": prompt_version})
        return self.get_memory_profile_proposal(proposal_id, tenant_id, user_id) or {}

    def get_memory_profile_proposal(self, proposal_id: str, tenant_id: str,
                                    user_id: str = "") -> dict | None:
        sql = """SELECT id, tenant_id, user_id, agent_id, source_conversation_id, fields_json,
            rationale, prompt_version, status, reviewed_by, review_note, created_at, updated_at
            FROM memory_profile_proposals WHERE id=? AND tenant_id=?"""
        params: list[object] = [proposal_id, tenant_id]
        if user_id:
            sql += " AND user_id=?"; params.append(user_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        try: fields = json.loads(row[5] or "{}")
        except (TypeError, ValueError): fields = {}
        return {"id": row[0], "tenant_id": row[1], "user_id": row[2], "agent_id": row[3],
                "source_conversation_id": row[4], "fields": fields, "rationale": row[6],
                "prompt_version": row[7], "status": row[8], "reviewed_by": row[9],
                "review_note": row[10], "created_at": _to_utc_iso(row[11]), "updated_at": _to_utc_iso(row[12])}

    def list_memory_profile_proposals(self, tenant_id: str, user_id: str = "",
                                      status: str = "", limit: int = 100) -> list[dict]:
        sql = "SELECT id FROM memory_profile_proposals WHERE tenant_id=?"; params: list[object] = [tenant_id]
        if user_id: sql += " AND user_id=?"; params.append(user_id)
        if status: sql += " AND status=?"; params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 200)))
        with sqlite3.connect(self._db_path) as conn: rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self.get_memory_profile_proposal(row[0], tenant_id, user_id))]

    def review_memory_profile_proposal(self, proposal_id: str, tenant_id: str, reviewer_id: str,
                                       status: str, review_note: str = "") -> dict:
        if status not in {"approved", "rejected"}:
            raise ValueError("画像提议审核状态必须是 approved 或 rejected")
        proposal = self.get_memory_profile_proposal(proposal_id, tenant_id)
        if not proposal: raise ValueError("画像提议不存在")
        if proposal["status"] != "pending": raise ValueError("画像提议已审核")
        if status == "approved":
            for key, value in proposal["fields"].items():
                self.write_long_term_memory(tenant_id, proposal["user_id"], proposal["agent_id"],
                                            "fact", f"{key}：{value}", ["profile", key],
                                            importance=0.75, confidence=0.85)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""UPDATE memory_profile_proposals SET status=?, reviewed_by=?,
                review_note=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?""",
                (status, reviewer_id, str(review_note or "")[:1000], proposal_id, tenant_id))
        self.log_audit(tenant_id, reviewer_id, proposal["agent_id"], "memory.profile.proposal.review",
                       "memory_profile_proposal", proposal_id, {"status": status})
        return self.get_memory_profile_proposal(proposal_id, tenant_id) or {}

    def create_memory_conflict_proposal(self, tenant_id: str, user_id: str, agent_id: str,
                                        existing_memory_id: int, proposed_content: str,
                                        rationale: str = "", prompt_version: int = 0) -> dict:
        existing = self._get_scoped_memory(tenant_id, user_id, agent_id, existing_memory_id)
        if not existing: raise ValueError("既有记忆不存在")
        proposed_content = str(proposed_content or "").strip()[:1000]
        if not proposed_content: raise ValueError("冲突候选内容不能为空")
        proposal_id = "mcp-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO memory_conflict_proposals
                (id, tenant_id, user_id, agent_id, existing_memory_id, proposed_content, rationale, prompt_version)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (proposal_id, tenant_id, user_id, agent_id,
                existing_memory_id, proposed_content, str(rationale or "")[:2000], int(prompt_version or 0)))
        self.log_audit(tenant_id, user_id, agent_id, "memory.conflict.proposal.create",
                       "memory_conflict_proposal", proposal_id, {"existing_memory_id": existing_memory_id})
        return self.get_memory_conflict_proposal(proposal_id, tenant_id, user_id) or {}

    def _get_scoped_memory(self, tenant_id: str, user_id: str, agent_id: str, memory_id: int) -> dict | None:
        return next((item for item in self.list_long_term_memories(tenant_id, user_id, agent_id)
                     if item["id"] == int(memory_id)), None)

    def get_memory_conflict_proposal(self, proposal_id: str, tenant_id: str, user_id: str = "") -> dict | None:
        sql = """SELECT id, tenant_id, user_id, agent_id, existing_memory_id, proposed_content,
            rationale, prompt_version, status, reviewed_by, review_note, created_at, updated_at
            FROM memory_conflict_proposals WHERE id=? AND tenant_id=?"""; params: list[object] = [proposal_id, tenant_id]
        if user_id: sql += " AND user_id=?"; params.append(user_id)
        with sqlite3.connect(self._db_path) as conn: row = conn.execute(sql, params).fetchone()
        if not row: return None
        return {"id": row[0], "tenant_id": row[1], "user_id": row[2], "agent_id": row[3],
                "existing_memory_id": row[4], "proposed_content": row[5], "rationale": row[6],
                "prompt_version": row[7], "status": row[8], "reviewed_by": row[9], "review_note": row[10],
                "created_at": _to_utc_iso(row[11]), "updated_at": _to_utc_iso(row[12])}

    def list_memory_conflict_proposals(self, tenant_id: str, user_id: str = "", status: str = "",
                                       limit: int = 100) -> list[dict]:
        sql = "SELECT id FROM memory_conflict_proposals WHERE tenant_id=?"; params: list[object] = [tenant_id]
        if user_id: sql += " AND user_id=?"; params.append(user_id)
        if status: sql += " AND status=?"; params.append(status)
        sql += " ORDER BY updated_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 200)))
        with sqlite3.connect(self._db_path) as conn: rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self.get_memory_conflict_proposal(row[0], tenant_id, user_id))]

    def review_memory_conflict_proposal(self, proposal_id: str, tenant_id: str, reviewer_id: str,
                                        status: str, review_note: str = "") -> dict:
        if status not in {"approved", "rejected"}: raise ValueError("冲突提议审核状态必须是 approved 或 rejected")
        proposal = self.get_memory_conflict_proposal(proposal_id, tenant_id)
        if not proposal: raise ValueError("冲突提议不存在")
        if proposal["status"] != "pending": raise ValueError("冲突提议已审核")
        existing = self._get_scoped_memory(tenant_id, proposal["user_id"], proposal["agent_id"], proposal["existing_memory_id"])
        if not existing: raise ValueError("既有记忆不存在或已不可用")
        if status == "approved":
            self.update_long_term_memory_status(tenant_id, proposal["user_id"], existing["id"], "suppressed")
            self.write_long_term_memory(tenant_id, proposal["user_id"], proposal["agent_id"], existing["memory_type"],
                                        proposal["proposed_content"], existing["tags"], importance=existing["importance"],
                                        confidence=min(existing["confidence"], 0.8))
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""UPDATE memory_conflict_proposals SET status=?, reviewed_by=?, review_note=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?""", (status, reviewer_id,
                str(review_note or "")[:1000], proposal_id, tenant_id))
        self.log_audit(tenant_id, reviewer_id, proposal["agent_id"], "memory.conflict.proposal.review",
                       "memory_conflict_proposal", proposal_id, {"status": status, "existing_memory_id": existing["id"]})
        return self.get_memory_conflict_proposal(proposal_id, tenant_id) or {}

    def long_term_memory_enabled(self, tenant_id: str, user_id: str, agent_id: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT enabled FROM user_memory_preferences
                WHERE tenant_id=? AND user_id=? AND agent_id=?
            """, (tenant_id, user_id, agent_id)).fetchone()
        return bool(row and row[0])

    def set_long_term_memory_enabled(self, tenant_id: str, user_id: str, agent_id: str,
                                     enabled: bool) -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO user_memory_preferences (tenant_id, user_id, agent_id, enabled)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id, agent_id) DO UPDATE SET
                    enabled=excluded.enabled, updated_at=CURRENT_TIMESTAMP
            """, (tenant_id, user_id, agent_id, int(bool(enabled))))
        self.log_audit(tenant_id, user_id, agent_id, "memory.preference.update", "memory_preference",
                       agent_id, {"enabled": bool(enabled)})

    def get_user_language(self, tenant_id: str, user_id: str, agent_id: str) -> str:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT language FROM user_language_preferences WHERE tenant_id=? AND user_id=? AND agent_id=?",
                               (tenant_id, user_id, agent_id)).fetchone()
        return row[0] if row and row[0] in {"zh-CN", "en-US"} else "zh-CN"

    def set_user_language(self, tenant_id: str, user_id: str, agent_id: str, language: str) -> str:
        language = str(language or "zh-CN")
        if language not in {"zh-CN", "en-US"}:
            raise ValueError("暂只支持 zh-CN 或 en-US")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO user_language_preferences (tenant_id, user_id, agent_id, language)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id, user_id, agent_id) DO UPDATE SET
                    language=excluded.language, updated_at=CURRENT_TIMESTAMP
            """, (tenant_id, user_id, agent_id, language))
        self.log_audit(tenant_id, user_id, agent_id, "user.language.update", "user_language", agent_id,
                       {"language": language})
        return language

    def get_long_term_memories(
        self, tenant_id: str, user_id: str, agent_id: str, query: str = "", limit: int = 6,
    ) -> list[dict]:
        limit = max(1, min(int(limit), 20))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, memory_type, content, tags_json, importance, confidence,
                       source_conversation_id, created_at, updated_at
                FROM long_term_memories
                WHERE tenant_id=? AND user_id=? AND agent_id=? AND status='active'
                  AND (expires_at IS NULL OR expires_at > CURRENT_TIMESTAMP)
                ORDER BY importance DESC, updated_at DESC
                LIMIT 50
            """, (tenant_id, user_id, agent_id)).fetchall()
        query_lower = str(query or "").lower()
        items = []
        for row in rows:
            try:
                tags = json.loads(row[3] or "[]")
            except (TypeError, json.JSONDecodeError):
                tags = []
            relevance = 0.0
            if query_lower and any(str(tag).lower() in query_lower for tag in tags):
                relevance = 0.3
            if query_lower and any(token and token in query_lower for token in str(row[2]).lower().split("：")):
                relevance = max(relevance, 0.2)
            items.append({
                "id": row[0], "memory_type": row[1], "content": row[2], "tags": tags,
                "importance": row[4], "confidence": row[5],
                "source_conversation_id": row[6], "created_at": _to_utc_iso(row[7]),
                "updated_at": _to_utc_iso(row[8]), "_score": row[4] + relevance,
            })
        items.sort(key=lambda item: (item["_score"], item["updated_at"]), reverse=True)
        for item in items:
            item.pop("_score", None)
        return items[:limit]

    def list_long_term_memories(
        self, tenant_id: str, user_id: str, agent_id: str | None = None,
        include_suppressed: bool = True,
    ) -> list[dict]:
        sql = """
            SELECT id, agent_id, memory_type, content, tags_json, importance, confidence,
                   source_conversation_id, status, created_at, updated_at
            FROM long_term_memories
            WHERE tenant_id=? AND user_id=?
        """
        params: list[object] = [tenant_id, user_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        if not include_suppressed:
            sql += " AND status='active'"
        sql += " ORDER BY updated_at DESC, id DESC LIMIT 200"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try:
                tags = json.loads(row[4] or "[]")
            except (TypeError, json.JSONDecodeError):
                tags = []
            items.append({
                "id": row[0], "agent_id": row[1], "memory_type": row[2],
                "content": row[3], "tags": tags, "importance": row[5],
                "confidence": row[6], "source_conversation_id": row[7],
                "status": row[8], "created_at": _to_utc_iso(row[9]),
                "updated_at": _to_utc_iso(row[10]),
            })
        return items

    def update_long_term_memory_status(
        self, tenant_id: str, user_id: str, memory_id: int, status: str,
    ) -> bool:
        if status not in {"active", "suppressed"}:
            raise ValueError("记忆状态仅支持 active 或 suppressed")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE long_term_memories SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=? AND user_id=?
            """, (status, memory_id, tenant_id, user_id))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, action="memory.status.update",
                           resource_type="long_term_memory", resource_id=str(memory_id),
                           detail={"status": status})
        return cur.rowcount > 0

    def update_long_term_memory_content(self, tenant_id: str, user_id: str, memory_id: int,
                                        content: str) -> bool:
        content = str(content or "").strip()[:1000]
        if not content:
            raise ValueError("记忆内容不能为空")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE long_term_memories SET content=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=? AND user_id=?
            """, (content, memory_id, tenant_id, user_id))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, action="memory.content.update",
                           resource_type="long_term_memory", resource_id=str(memory_id))
        return cur.rowcount > 0

    def delete_long_term_memory(self, tenant_id: str, user_id: str, memory_id: int) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                DELETE FROM long_term_memories
                WHERE id=? AND tenant_id=? AND user_id=?
            """, (memory_id, tenant_id, user_id))
        if cur.rowcount:
            self.log_audit(tenant_id, user_id, action="memory.delete",
                           resource_type="long_term_memory", resource_id=str(memory_id))
        return cur.rowcount > 0

    def get_llm_key(self, provider: str) -> Optional[str]:
        """从 SQLite 读取解密后的 API Key（agent.py 中语义评分使用）"""
        import hashlib
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT api_key_enc, api_key_hash FROM llm_provider_keys WHERE provider = ?",
                (provider,),
            ).fetchone()
        if not row or not row[0]:
            return None
        api_key_enc, stored_hash = row
        try:
            from cryptography.fernet import Fernet
            key = os.environ.get("LLM_KEY_ENCRYPTION_KEY")
            if not key:
                return None
            fernet = Fernet(key.encode() if isinstance(key, str) else key)
            decrypted = fernet.decrypt(api_key_enc.encode()).decode()
            if hashlib.sha256(decrypted.encode("utf-8")).hexdigest() != stored_hash:
                logger.error(f"⚠️ {provider} API Key 哈希校验失败")
                return None
            return decrypted
        except Exception as e:
            logger.error(f"⚠️ 解密 {provider} API Key 失败: {e}")
            return None

    def get_conversations(
        self,
        include_deleted: bool = False,
        include_test: bool = False,
        jailbreak: str = "all",
        tenant_id: str | None = None,
        user_id: str | None = None,
        agent_id: str | None = None,
    ) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            sql = "SELECT id, title, created_at, updated_at, deleted, category, jailbreak_status, jailbreak_reason, jailbreak_message_id, knowledge_base_id FROM conversations"
            conditions = []
            params: list[str] = []
            for column, value in (("tenant_id", tenant_id), ("user_id", user_id), ("agent_id", agent_id)):
                if value:
                    conditions.append(f"{column} = ?")
                    params.append(value)
            if not include_deleted:
                conditions.append("(deleted IS NULL OR deleted = 0)")
            if not include_test:
                conditions.append("(category IS NULL OR category = 'user')")
            if jailbreak == "pending":
                conditions.append("jailbreak_status = 'pending'")
            elif jailbreak == "clean":
                conditions.append(
                    "(jailbreak_status IS NULL OR jailbreak_status NOT IN ('pending'))")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY updated_at DESC, id DESC"
            rows = conn.execute(sql, params).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "created_at": _to_utc_iso(r[2]),
                "updated_at": _to_utc_iso(r[3]),
                "deleted": bool(r[4]) if r[4] else False,
                "category": r[5] or "user",
                "jailbreak_status": r[6],
                "jailbreak_reason": r[7],
                "jailbreak_message_id": r[8],
                "knowledge_base_id": r[9] or "",
            }
            for r in rows
        ]

    def get_conversation_detail(self, conversation_id: str) -> Optional[dict]:
        """返回单条对话详情，包含所有消息及来源

        返回格式：
        {
            "id": "...",
            "title": "...",
            "created_at": "...",
            "updated_at": "...",
            "messages": [
                {"role": "user"|"assistant", "content": "...", "sources": [...]},
                ...
            ],
            "stats": {
                "rounds": 5,           # 问答轮次
                "total_sources": 12,   # 所有消息来源总数
            }
        }
        """
        with sqlite3.connect(self._db_path) as conn:
            conv_row = conn.execute(
                "SELECT id, title, created_at, updated_at, deleted, category, jailbreak_status, jailbreak_reason, jailbreak_message_id FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conv_row:
                return None

            msg_rows = conn.execute("""
                SELECT m.id, m.role, m.content, m.sources, m.created_at,
                       u.user_rating, u.semantic_rating, m.jailbreak_flagged
                FROM messages m
                LEFT JOIN usage_logs u ON m.id = u.message_id
                WHERE m.conversation_id = ?
                ORDER BY m.id
            """, (conversation_id,)).fetchall()

        messages = []
        rounds = 0
        total_sources = 0
        for row in msg_rows:
            m_id = row[0]
            msg = {"id": m_id, "role": row[1], "content": row[2], "created_at": _to_utc_iso(row[4])}
            if row[3]:
                sources = json.loads(row[3])
                msg["sources"] = sources
                total_sources += len(sources)
            if row[5] is not None:
                msg["user_rating"] = row[5]
            if row[6] is not None:
                msg["semantic_rating"] = row[6]
            if row[7]:
                msg["jailbreak_flagged"] = bool(row[7])
            messages.append(msg)
            if row[1] == "assistant":
                rounds += 1

        return {
            "id": conv_row[0],
            "title": conv_row[1],
            "created_at": _to_utc_iso(conv_row[2]),
            "updated_at": _to_utc_iso(conv_row[3]),
            "deleted": bool(conv_row[4]) if conv_row[4] else False,
            "category": conv_row[5] or "user",
            "jailbreak_status": conv_row[6],
            "jailbreak_reason": conv_row[7],
            "jailbreak_message_id": conv_row[8],
            "messages": messages,
            "stats": {
                "rounds": rounds,
                "total_sources": total_sources,
            },
        }

    def update_title(self, conversation_id: str, title: str):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE conversations SET title = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (title, conversation_id),
            )

    def delete_conversation(self, conversation_id: str):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("UPDATE conversations SET deleted = 1 WHERE id = ?", (conversation_id,))

    def hard_delete_conversation(self, conversation_id: str):
        with sqlite3.connect(self._db_path) as conn:
            exists = conn.execute("SELECT 1 FROM conversations WHERE id=?", (conversation_id,)).fetchone()
            if not exists:
                return False
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM session_memory WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM usage_logs WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
        return True

    def record_llm_usage_event(self, tenant_id: str = "local-default", user_id: str = "",
                               agent_id: str = "", conversation_id: str = "", message_id: int | None = None,
                               module: str = "chat", provider: str = "", model: str = "",
                               prompt_tokens: int = 0, completion_tokens: int = 0) -> dict:
        provider, model = str(provider or ""), str(model or "")
        prompt_tokens, completion_tokens = max(0, int(prompt_tokens or 0)), max(0, int(completion_tokens or 0))
        pricing = {(str(item["provider"]), str(item["model"])): item for item in self.list_model_pricing()}
        price = pricing.get((provider, model)) or next((item for (p, m), item in pricing.items() if m == model and model), None)
        if price:
            cost = prompt_tokens / 1_000_000 * float(price.get("input_price_per_million") or 0) + completion_tokens / 1_000_000 * float(price.get("output_price_per_million") or 0)
            pricing_status = "estimated_from_local_configuration"
        else:
            cost, pricing_status = None, "unknown_model_or_rate"
        event_id = "llm-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO llm_usage_events
                (id, tenant_id, user_id, agent_id, conversation_id, message_id, module, provider, model,
                 prompt_tokens, completion_tokens, total_tokens, estimated_cost, pricing_status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (event_id, str(tenant_id or "local-default"), str(user_id or ""), str(agent_id or ""),
                 str(conversation_id or ""), message_id, str(module or "chat"), provider, model,
                 prompt_tokens, completion_tokens, prompt_tokens + completion_tokens, cost, pricing_status))
        return {"id": event_id, "tenant_id": tenant_id or "local-default", "user_id": user_id or "",
                "agent_id": agent_id or "", "module": module or "chat", "provider": provider, "model": model,
                "prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens, "estimated_cost": cost,
                "pricing_status": pricing_status}

    def list_llm_usage_events(self, tenant_id: str = "", module: str = "", limit: int = 1000) -> list[dict]:
        sql = "SELECT id, tenant_id, user_id, agent_id, conversation_id, message_id, module, provider, model, prompt_tokens, completion_tokens, total_tokens, estimated_cost, pricing_status, created_at FROM llm_usage_events"
        params: list[object] = []
        conditions = []
        if tenant_id: conditions.append("tenant_id=?"); params.append(tenant_id)
        if module: conditions.append("module=?"); params.append(module)
        if conditions: sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY created_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 10000)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "tenant_id": r[1], "user_id": r[2], "agent_id": r[3], "conversation_id": r[4], "message_id": r[5], "module": r[6], "provider": r[7], "model": r[8], "prompt_tokens": r[9], "completion_tokens": r[10], "total_tokens": r[11], "estimated_cost": r[12], "pricing_status": r[13], "created_at": self._utc_to_local(r[14] or "")} for r in rows]

    def save_quota_budget(self, scope_type: str, scope_id: str, period: str = "monthly",
                          token_limit: int | None = None, cost_limit: float | None = None,
                          warn_percent: float = 80, tenant_id: str = "",
                          created_by: str = "admin") -> dict:
        if scope_type not in {"platform", "tenant", "user"} or period not in {"daily", "monthly"}:
            raise ValueError("预算范围或周期不合法")
        if token_limit is None and cost_limit is None:
            raise ValueError("至少设置 Token 或费用上限")
        if (token_limit is not None and int(token_limit) < 0) or (cost_limit is not None and float(cost_limit) < 0):
            raise ValueError("预算上限不能为负数")
        budget_id = "quota-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO quota_budgets
                (id, scope_type, scope_id, tenant_id, period, token_limit, cost_limit, warn_percent, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(scope_type, scope_id, period) DO UPDATE SET
                    tenant_id=excluded.tenant_id, token_limit=excluded.token_limit,
                    cost_limit=excluded.cost_limit, warn_percent=excluded.warn_percent,
                    enabled=1, updated_at=CURRENT_TIMESTAMP""",
                (budget_id, scope_type, str(scope_id or ""), str(tenant_id or ""), period,
                 int(token_limit) if token_limit is not None else None,
                 float(cost_limit) if cost_limit is not None else None,
                 max(0, min(float(warn_percent), 100)), str(created_by or "")))
            row = conn.execute("""SELECT id, scope_type, scope_id, tenant_id, period,
                token_limit, cost_limit, warn_percent, enabled, created_by, updated_at
                FROM quota_budgets WHERE scope_type=? AND scope_id=? AND period=?""",
                (scope_type, str(scope_id or ""), period)).fetchone()
        return {"id": row[0], "scope_type": row[1], "scope_id": row[2], "tenant_id": row[3],
                "period": row[4], "token_limit": row[5], "cost_limit": row[6],
                "warn_percent": row[7], "enabled": bool(row[8]), "created_by": row[9], "updated_at": row[10]}

    def list_quota_budgets(self, tenant_id: str | None = None) -> list[dict]:
        sql = "SELECT id, scope_type, scope_id, tenant_id, period, token_limit, cost_limit, warn_percent, enabled, created_by, updated_at FROM quota_budgets"
        params: list[object] = []
        if tenant_id is not None:
            sql += " WHERE tenant_id=? OR scope_type='platform'"
            params.append(tenant_id)
        sql += " ORDER BY scope_type, scope_id, period"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "scope_type": r[1], "scope_id": r[2], "tenant_id": r[3], "period": r[4],
                 "token_limit": r[5], "cost_limit": r[6], "warn_percent": r[7], "enabled": bool(r[8]),
                 "created_by": r[9], "updated_at": r[10]} for r in rows]

    def check_quota(self, tenant_id: str, user_id: str, requested_tokens: int = 0,
                    requested_cost: float = 0.0) -> dict:
        budgets = [b for b in self.list_quota_budgets(tenant_id)
                   if b["enabled"] and (b["scope_type"] == "platform" or
                      (b["scope_type"] == "tenant" and b["scope_id"] == tenant_id) or
                      (b["scope_type"] == "user" and b["scope_id"] == user_id))]
        result = {"allowed": True, "warnings": [], "blocked_by": [], "budgets": []}
        for budget in budgets:
            start = "start of day" if budget["period"] == "daily" else "start of month"
            clauses = ["created_at >= datetime('now', ?)", "tenant_id=?"]
            params: list[object] = [start, tenant_id]
            if budget["scope_type"] == "user":
                clauses.append("user_id=?")
                params.append(user_id)
            with sqlite3.connect(self._db_path) as conn:
                row = conn.execute(
                    f"SELECT COALESCE(SUM(total_tokens),0), COALESCE(SUM(estimated_cost),0) FROM llm_usage_events WHERE {' AND '.join(clauses)}",
                    params,
                ).fetchone()
            tokens = int(row[0] or 0) + max(0, int(requested_tokens))
            cost = float(row[1] or 0) + max(0.0, float(requested_cost))
            item = {"id": budget["id"], "scope_type": budget["scope_type"], "period": budget["period"],
                    "tokens": tokens, "cost": round(cost, 6), "token_limit": budget["token_limit"],
                    "cost_limit": budget["cost_limit"]}
            result["budgets"].append(item)
            ratio = max(tokens / budget["token_limit"] if budget["token_limit"] else 0,
                        cost / budget["cost_limit"] if budget["cost_limit"] else 0)
            over = ((budget["token_limit"] is not None and tokens > budget["token_limit"]) or
                    (budget["cost_limit"] is not None and cost > budget["cost_limit"]))
            if over:
                result["allowed"] = False
                result["blocked_by"].append(item)
            elif ratio * 100 >= budget["warn_percent"]:
                result["warnings"].append(item)
        return result

    def create_department(self, tenant_id: str, name: str, cost_center: str = "",
                          created_by: str = "admin") -> dict:
        name = str(name or "").strip()[:120]
        if not name:
            raise ValueError("部门名称不能为空")
        department_id = "dept-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO organization_departments
                (id, tenant_id, name, cost_center) VALUES (?, ?, ?, ?)
                ON CONFLICT(tenant_id, name) DO UPDATE SET
                    cost_center=excluded.cost_center, status='active', updated_at=CURRENT_TIMESTAMP""",
                (department_id, tenant_id, name, str(cost_center or "")[:80]))
            row = conn.execute("""SELECT id, tenant_id, name, cost_center, status
                FROM organization_departments WHERE tenant_id=? AND name=?""", (tenant_id, name)).fetchone()
        self.log_audit(tenant_id, created_by, "", "department.create", "department", row[0], {"name": name})
        return {"id": row[0], "tenant_id": row[1], "name": row[2], "cost_center": row[3], "status": row[4]}

    def assign_user_department(self, tenant_id: str, department_id: str, user_id: str,
                               changed_by: str = "admin") -> bool:
        with sqlite3.connect(self._db_path) as conn:
            if not conn.execute("SELECT 1 FROM organization_departments WHERE id=? AND tenant_id=? AND status='active'", (department_id, tenant_id)).fetchone():
                return False
            if not conn.execute("SELECT 1 FROM users WHERE id=? AND tenant_id=? AND status='active'", (user_id, tenant_id)).fetchone():
                return False
            conn.execute("""INSERT INTO user_department_memberships
                (tenant_id, department_id, user_id, status) VALUES (?, ?, ?, 'active')
                ON CONFLICT(tenant_id, department_id, user_id) DO UPDATE SET status='active'""",
                (tenant_id, department_id, user_id))
        self.log_audit(tenant_id, changed_by, "", "department.user.assign", "user", user_id, {"department_id": department_id})
        return True

    def list_department_costs(self, tenant_id: str, limit: int = 10000) -> list[dict]:
        sql = """SELECT d.id, d.name, d.cost_center, m.user_id,
                    COALESCE(SUM(u.total_tokens),0), COALESCE(SUM(u.estimated_cost),0), COUNT(u.id)
                 FROM organization_departments d
                 JOIN user_department_memberships m ON m.department_id=d.id AND m.tenant_id=d.tenant_id AND m.status='active'
                 LEFT JOIN llm_usage_events u ON u.tenant_id=m.tenant_id AND u.user_id=m.user_id
                 WHERE d.tenant_id=? AND d.status='active'
                 GROUP BY d.id, d.name, d.cost_center, m.user_id ORDER BY d.name, m.user_id LIMIT ?"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, (tenant_id, max(1, min(int(limit), 50000)))).fetchall()
        grouped = {}
        for dept_id, name, cost_center, user_id, tokens, cost, requests in rows:
            item = grouped.setdefault(dept_id, {"department_id": dept_id, "department": name, "cost_center": cost_center, "total_tokens": 0, "cost": 0.0, "requests": 0, "users": []})
            item["total_tokens"] += int(tokens or 0); item["cost"] += float(cost or 0); item["requests"] += int(requests or 0)
            item["users"].append({"user_id": user_id, "total_tokens": int(tokens or 0), "cost": round(float(cost or 0), 6), "requests": int(requests or 0)})
        for item in grouped.values(): item["cost"] = round(item["cost"], 6)
        return list(grouped.values())

    def get_user_usage_summary(self, tenant_id: str, user_id: str, limit: int = 10000) -> dict:
        items = [item for item in self.list_llm_usage_events(tenant_id, limit=limit) if item["user_id"] == user_id]
        return {"user_id": user_id, "total_tokens": sum(i["total_tokens"] for i in items),
                "estimated_cost": round(sum(float(i["estimated_cost"] or 0) for i in items), 6),
                "requests": len(items), "by_model": self.get_usage_cost_analysis(tenant_id, limit).get("by_model", [])}

    def log_usage(
        self,
        conversation_id: str,
        message_id: int,
        query: str,
        rewrite_time: float = 0,
        faiss_time: float = 0,
        chroma_time: float = 0,
        rerank_time: float = 0,
        llm_time: float = 0,
        total_time: float = 0,
        faiss_count: int = 0,
        chroma_count: int = 0,
        bm25_count: int = 0,
        final_count: int = 0,
        returned_count: int = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        llm_success: bool = True,
        was_circuit_break: bool = False,
        circuit_provider: str = None,
        was_truncated: bool = False,
        off_topic: bool = False,
        documents: list = None,
        trace_data: dict = None,
        answer_jailbreak: int = 0,
    ):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO usage_logs (
                    conversation_id, message_id, query,
                    rewrite_time, faiss_time, chroma_time, rerank_time, llm_time, total_time,
                    faiss_count, chroma_count, bm25_count, final_count, returned_count,
                    prompt_tokens, completion_tokens,
                    llm_success, was_circuit_break, circuit_provider,
                    was_truncated, off_topic, documents, trace_data, answer_jailbreak
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                conversation_id, message_id, query,
                rewrite_time, faiss_time, chroma_time, rerank_time, llm_time, total_time,
                faiss_count, chroma_count, bm25_count, final_count, returned_count,
                prompt_tokens, completion_tokens,
                1 if llm_success else 0, 1 if was_circuit_break else 0, circuit_provider,
                1 if was_truncated else 0, 1 if off_topic else 0,
                json.dumps(documents, ensure_ascii=False) if documents else None,
                json.dumps(trace_data, ensure_ascii=False) if trace_data else None,
                answer_jailbreak,
            ))
        trace = trace_data or {}
        llm_context = ((trace.get("context") or {}).get("llm") or {}).get("chat") or {}
        owner = self.get_conversation_owner(conversation_id)
        if owner:
            self.record_llm_usage_event(
                owner["tenant_id"], owner["user_id"], owner["agent_id"], conversation_id, message_id,
                "chat", llm_context.get("provider", ""), llm_context.get("model", ""),
                prompt_tokens, completion_tokens,
            )

    def get_conversation_owner(self, conversation_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT tenant_id, user_id, agent_id FROM conversations WHERE id=?", (conversation_id,)).fetchone()
        return {"tenant_id": row[0], "user_id": row[1], "agent_id": row[2]} if row else None

    def update_rating(self, message_id: int, rating: int, semantic: bool = False):
        field = "semantic_rating" if semantic else "user_rating"
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                f"UPDATE usage_logs SET {field} = ? WHERE message_id = ?", (rating, message_id))

    def update_semantic_rating_if_unrated(self, message_id: int, rating: int) -> bool:
        """仅在用户未手动评分且尚无语义评分时写入兜底评分。"""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                """
                UPDATE usage_logs
                SET semantic_rating = ?
                WHERE message_id = ?
                  AND user_rating IS NULL
                  AND semantic_rating IS NULL
                """,
                (rating, message_id),
            )
            return cur.rowcount > 0

    def get_assistant_message_for_rating(self, message_id: int) -> Optional[dict]:
        """读取单条 assistant 消息及其问答上下文，用于超时语义评分。"""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """
                SELECT m.id, m.conversation_id, m.content, m.sources,
                       u.query, u.user_rating, u.semantic_rating
                FROM messages m
                LEFT JOIN usage_logs u ON m.id = u.message_id
                WHERE m.id = ? AND m.role = 'assistant'
                """,
                (message_id,),
            ).fetchone()
        if not row:
            return None
        return {
            "id": row[0],
            "conversation_id": row[1],
            "content": row[2],
            "sources": json.loads(row[3]) if row[3] else [],
            "query": row[4] or "",
            "user_rating": row[5],
            "semantic_rating": row[6],
        }

    def update_jailbreak_status(self, conversation_id: str, status: str, reason: str = None, message_id: int = None):
        with sqlite3.connect(self._db_path) as conn:
            if reason and message_id:
                # 永久标记消息
                conn.execute("UPDATE messages SET jailbreak_flagged = 1 WHERE id = ?", (message_id,))
                conn.execute(
                    "UPDATE conversations SET jailbreak_status = ?, jailbreak_reason = ?, jailbreak_message_id = ? WHERE id = ?",
                    (status, reason, message_id, conversation_id),
                )
            elif reason:
                conn.execute(
                    "UPDATE conversations SET jailbreak_status = ?, jailbreak_reason = ? WHERE id = ?",
                    (status, reason, conversation_id),
                )
            else:
                conn.execute(
                    "UPDATE conversations SET jailbreak_status = ? WHERE id = ?",
                    (status, conversation_id),
                )

    def flag_jailbreak_message(self, message_id: int):
        """永久标记某条消息为越狱相关（不可撤销）"""
        if message_id:
            with sqlite3.connect(self._db_path) as conn:
                conn.execute("UPDATE messages SET jailbreak_flagged = 1 WHERE id = ?", (message_id,))

    # ---- Prompt 测试集 CRUD ----

    def migrate_builtin_suite(self, suite_path: str = None) -> int:
        """将 prompt_test_suite.json 中的 31 条测试题迁移到数据库"""
        if suite_path is None:
            suite_path = str(Path(self._db_path).parent / "prompt_test_suite.json")
        with sqlite3.connect(self._db_path) as conn:
            existing = conn.execute(
                "SELECT COUNT(*) FROM prompt_test_items WHERE set_id = 'builtin'"
            ).fetchone()[0]
            if existing > 0:
                return existing
            if not os.path.exists(suite_path):
                return 0
            import json
            suite = json.loads(Path(suite_path).read_text(encoding="utf-8"))
            cases = suite.get("test_cases", [])
            for i, case in enumerate(cases):
                conn.execute("""
                    INSERT INTO prompt_test_items (set_id, seq, query, category, difficulty, expected, is_active)
                    VALUES ('builtin', ?, ?, ?, ?, ?, 1)
                """, (i + 1, case["query"], case.get("category", ""),
                      case.get("difficulty", "medium"),
                      json.dumps(case.get("expected", {}), ensure_ascii=False)))
            conn.execute(
                "INSERT INTO prompt_test_sets (source, keyword_input, is_active) VALUES ('builtin', '内置31条', 1)"
            )
        return len(cases)

    def get_test_items(self, set_id: str = 'builtin') -> list:
        """获取测试集列表"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, set_id, seq, query, category, difficulty, expected, is_active
                FROM prompt_test_items
                WHERE set_id = ? AND is_active = 1
                ORDER BY seq
            """, (set_id,)).fetchall()
        return [
            {"id": r[0], "set_id": r[1], "seq": r[2], "query": r[3],
             "category": r[4], "difficulty": r[5], "expected": r[6]}
            for r in rows
        ]

    def get_all_test_sets(self) -> list:
        """获取所有测试集"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, source, keyword_input, created_at, is_active FROM prompt_test_sets ORDER BY id DESC"
            ).fetchall()
        return [
            {"id": r[0], "source": r[1], "keyword_input": r[2],
             "created_at": _to_utc_iso(r[3]), "is_active": bool(r[4])}
            for r in rows
        ]

    def save_ai_test_set(self, keyword: str, items: list) -> str:
        """保存 AI 生成的测试集"""
        import uuid
        set_id = f"ai_{uuid.uuid4().hex[:8]}"
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO prompt_test_sets (source, keyword_input) VALUES ('ai_generated', ?)",
                (keyword,),
            )
            for i, item in enumerate(items):
                conn.execute("""
                    INSERT INTO prompt_test_items (set_id, seq, query, category, difficulty, expected, is_active)
                    VALUES (?, ?, ?, ?, ?, ?, 1)
                """, (set_id, i + 1, item["query"], item.get("category", ""),
                      item.get("difficulty", "medium"),
                      json.dumps(item.get("expected", {}), ensure_ascii=False)))
        return set_id

    def update_test_item(self, item_id: int, query: str = None, category: str = None) -> bool:
        """更新单条测试题"""
        with sqlite3.connect(self._db_path) as conn:
            fields = []
            params = []
            if query is not None:
                fields.append("query = ?")
                params.append(query)
            if category is not None:
                fields.append("category = ?")
                params.append(category)
            if not fields:
                return False
            params.append(item_id)
            conn.execute(
                f"UPDATE prompt_test_items SET {', '.join(fields)} WHERE id = ?", params
            )
        return True

    def save_prompt_ab_run(self, run_id: str, version_a: dict, version_b: dict,
                           set_id: str, report: dict) -> int:
        """Persist an offline Prompt A/B comparison without changing active Prompt."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                INSERT INTO prompt_ab_runs
                    (run_id, version_a_id, version_b_id, version_a_name, version_b_name, set_id, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id, int(version_a["id"]), int(version_b["id"]),
                str(version_a["name"]), str(version_b["name"]), str(set_id),
                json.dumps(report, ensure_ascii=False),
            ))
        return int(cur.lastrowid)

    def get_prompt_ab_runs(self, limit: int = 20) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT run_id, version_a_id, version_b_id, version_a_name, version_b_name, set_id,
                       report_json, created_at
                FROM prompt_ab_runs ORDER BY id DESC LIMIT ?
            """, (max(1, min(int(limit), 100)),)).fetchall()
        items = []
        for row in rows:
            try:
                report = json.loads(row[6] or "{}")
            except (TypeError, ValueError):
                report = {}
            items.append({
                "run_id": row[0], "version_a_id": row[1], "version_b_id": row[2],
                "version_a_name": row[3], "version_b_name": row[4], "set_id": row[5],
                "report": report, "created_at": self._utc_to_local(row[7] or ""),
            })
        return items

    def external_app_rate_allowed(self, app_id: str) -> bool:
        """Check the configured rolling one-minute request limit."""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT rate_limit_per_minute, enabled FROM external_apps WHERE id=?",
                (app_id,),
            ).fetchone()
            if not row or not row[1]:
                return False
            count = conn.execute(
                "SELECT COUNT(*) FROM external_app_usage WHERE app_id=? AND created_at >= datetime('now', '-1 minute')",
                (app_id,),
            ).fetchone()[0]
        return int(count) < int(row[0] or 1)

    # ---- Webhook 通知 ----
    def create_webhook_subscription(self, tenant_id: str, name: str, url: str,
                                    event_types: list[str], max_attempts: int = 3,
                                    timeout_seconds: int = 10, created_by: str = "admin") -> dict:
        from webhook_delivery import WEBHOOK_EVENTS
        name, url = str(name or "").strip(), str(url or "").strip()
        if not name or not url:
            raise ValueError("Webhook 名称和 URL 不能为空")
        if not url.startswith(("http://", "https://")):
            raise ValueError("Webhook URL 必须使用 http 或 https")
        events = [str(item) for item in (event_types or []) if str(item) in WEBHOOK_EVENTS]
        if not events:
            raise ValueError("至少选择一个事件类型")
        from llm_config_manager import _encrypt_api_key
        subscription_id = "wh-" + uuid.uuid4().hex[:16]
        secret = "whsec_" + secrets.token_urlsafe(32)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO webhook_subscriptions
                    (id, tenant_id, name, url, event_types_json, signing_secret_enc,
                     max_attempts, timeout_seconds, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (subscription_id, str(tenant_id or "local-default"), name, url,
                  json.dumps(events, ensure_ascii=False), _encrypt_api_key(secret),
                  max(1, min(int(max_attempts), 8)), max(1, min(int(timeout_seconds), 60)),
                  str(created_by or "admin")))
        return {"id": subscription_id, "tenant_id": tenant_id or "local-default", "name": name,
                "url": url, "event_types": events, "enabled": True,
                "max_attempts": max(1, min(int(max_attempts), 8)),
                "timeout_seconds": max(1, min(int(timeout_seconds), 60)),
                "signing_secret": secret, "secret_shown_once": True}

    def list_webhook_subscriptions(self, tenant_id: str | None = None,
                                   event_type: str | None = None) -> list[dict]:
        sql = "SELECT id, tenant_id, name, url, event_types_json, enabled, max_attempts, timeout_seconds, created_by, created_at, updated_at, last_delivered_at FROM webhook_subscriptions"
        params: list[object] = []
        conditions = []
        if tenant_id:
            conditions.append("tenant_id=?")
            params.append(tenant_id)
        if event_type:
            conditions.append("enabled=1")
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY updated_at DESC, name"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try:
                events = json.loads(row[4] or "[]")
            except (TypeError, json.JSONDecodeError):
                events = []
            if event_type and event_type not in events:
                continue
            items.append({"id": row[0], "tenant_id": row[1], "name": row[2], "url": row[3],
                          "event_types": events, "enabled": bool(row[5]), "max_attempts": row[6],
                          "timeout_seconds": row[7], "created_by": row[8],
                          "created_at": self._utc_to_local(row[9] or ""),
                          "updated_at": self._utc_to_local(row[10] or ""),
                          "last_delivered_at": self._utc_to_local(row[11] or "") if row[11] else ""})
        return items

    def get_webhook_secret(self, subscription_id: str) -> str:
        from llm_config_manager import _decrypt_api_key
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT signing_secret_enc FROM webhook_subscriptions WHERE id=?", (subscription_id,)).fetchone()
        return _decrypt_api_key(row[0]) if row and row[0] else ""

    def list_embed_tokens(self, app_id: str = "") -> list[dict]:
        sql = "SELECT id, app_id, tenant_id, origin, expires_at, enabled, created_by, created_at, last_used_at FROM embed_tokens"
        params: list[object] = []
        if app_id:
            sql += " WHERE app_id=?"
            params.append(app_id)
        sql += " ORDER BY created_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "app_id": r[1], "tenant_id": r[2], "origin": r[3],
                 "expires_at": self._utc_to_local(r[4] or ""), "enabled": bool(r[5]),
                 "created_by": r[6], "created_at": self._utc_to_local(r[7] or ""),
                 "last_used_at": self._utc_to_local(r[8] or "") if r[8] else ""} for r in rows]

    # Agent-bound external applications. Existing applications with an empty
    # binding are intentionally not accepted for chat until an administrator
    # assigns an active Agent in the same tenant.
    def create_external_app(self, tenant_id: str, name: str, scopes: list[str] | None = None,
                            rate_limit_per_minute: int = 60, created_by: str = "admin",
                            agent_id: str = "") -> dict:
        name = str(name or "").strip()
        tenant_id, agent_id = str(tenant_id or "local-default"), str(agent_id or "")
        if not name or not agent_id:
            raise ValueError("应用名称和绑定 Agent 不能为空")
        scopes = scopes or ["chat"]
        allowed = {"chat", "chat_stream", "generation", "usage", "embed"}
        if not set(scopes).issubset(allowed):
            raise ValueError("包含不支持的应用作用域")
        with sqlite3.connect(self._db_path) as conn:
            if not conn.execute("SELECT 1 FROM agents WHERE id=? AND tenant_id=? AND status='active'", (agent_id, tenant_id)).fetchone():
                raise ValueError("绑定 Agent 不存在、未启用或不属于该工作区")
            app_id, app_key = "app-" + uuid.uuid4().hex[:16], "snx_" + uuid.uuid4().hex
            app_secret = "sns_" + secrets.token_urlsafe(32)
            rate = max(1, min(int(rate_limit_per_minute), 10000))
            conn.execute("""
                INSERT INTO external_apps
                    (id, tenant_id, agent_id, name, app_key, app_secret_hash, scopes_json, rate_limit_per_minute, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (app_id, tenant_id, agent_id, name, app_key, hashlib.sha256(app_secret.encode("utf-8")).hexdigest(),
                  json.dumps(scopes, ensure_ascii=False), rate, str(created_by or "admin")))
        return {"id": app_id, "tenant_id": tenant_id, "agent_id": agent_id, "name": name, "app_key": app_key,
                "app_secret": app_secret, "scopes": scopes, "rate_limit_per_minute": rate,
                "enabled": True, "secret_shown_once": True}

    def list_external_apps(self, tenant_id: str | None = None) -> list[dict]:
        sql = """SELECT id, tenant_id, agent_id, name, app_key, scopes_json, rate_limit_per_minute,
                         enabled, created_by, created_at, updated_at, last_used_at FROM external_apps"""
        params: list[object] = []
        if tenant_id:
            sql += " WHERE tenant_id=?"; params.append(tenant_id)
        sql += " ORDER BY updated_at DESC, name"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "tenant_id": r[1], "agent_id": r[2], "name": r[3], "app_key": r[4],
                 "app_key_mask": r[4][:8] + "..." + r[4][-6:], "scopes": json.loads(r[5] or "[]"),
                 "rate_limit_per_minute": r[6], "enabled": bool(r[7]), "created_by": r[8],
                 "created_at": self._utc_to_local(r[9] or ""), "updated_at": self._utc_to_local(r[10] or ""),
                 "last_used_at": self._utc_to_local(r[11] or "") if r[11] else "",
                 "binding_required": not bool(r[2])} for r in rows]

    def authenticate_external_app(self, app_key: str, app_secret: str) -> dict | None:
        digest = hashlib.sha256(str(app_secret or "").encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT a.id, a.tenant_id, a.agent_id, a.name, a.scopes_json,
                                          a.rate_limit_per_minute, a.enabled
                                   FROM external_apps a JOIN agents g ON g.id=a.agent_id AND g.tenant_id=a.tenant_id
                                   WHERE a.app_key=? AND a.app_secret_hash=? AND g.status='active'""",
                               (str(app_key or ""), digest)).fetchone()
            if not row or not row[6]:
                return None
            conn.execute("UPDATE external_apps SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (row[0],))
        return {"id": row[0], "tenant_id": row[1], "agent_id": row[2], "name": row[3],
                "scopes": json.loads(row[4] or "[]"), "rate_limit_per_minute": row[5], "enabled": bool(row[6])}

    def update_external_app_agent(self, app_id: str, tenant_id: str, agent_id: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            if not conn.execute("SELECT 1 FROM agents WHERE id=? AND tenant_id=? AND status='active'", (agent_id, tenant_id)).fetchone():
                return False
            cur = conn.execute("UPDATE external_apps SET agent_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?",
                               (agent_id, app_id, tenant_id))
        return cur.rowcount > 0

    def create_embed_token(self, app_id: str, origin: str = "", ttl_hours: int = 24,
                           created_by: str = "admin") -> dict:
        app_id = str(app_id or "").strip()
        origin = str(origin or "").strip()
        if not app_id:
            raise ValueError("应用 ID 不能为空")
        ttl = max(1, min(int(ttl_hours), 24 * 30))
        with sqlite3.connect(self._db_path) as conn:
            app = conn.execute(
                """SELECT tenant_id, agent_id, enabled, scopes_json
                   FROM external_apps WHERE id=?""",
                (app_id,),
            ).fetchone()
            if not app:
                raise ValueError("外部应用不存在")
            if not app[2]:
                raise ValueError("外部应用已停用")
            if not conn.execute(
                """SELECT 1 FROM agents
                   WHERE id=? AND tenant_id=? AND status='active'""",
                (app[1], app[0]),
            ).fetchone():
                raise ValueError("外部应用未绑定有效 Agent")
        try:
            scopes = json.loads(app[3] or "[]")
        except (TypeError, json.JSONDecodeError):
            scopes = []
        if "chat" not in scopes and "embed" not in scopes:
            raise ValueError("应用没有 chat 或 embed 作用域")

        token = "emb_" + secrets.token_urlsafe(36)
        token_id = "embtok-" + uuid.uuid4().hex[:16]
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO embed_tokens
                   (id, app_id, tenant_id, token_hash, origin, expires_at, created_by)
                   VALUES (?, ?, ?, ?, ?, datetime('now', ?), ?)""",
                (token_id, app_id, app[0], token_hash, origin,
                 f"+{ttl} hours", str(created_by or "admin")),
            )
        return {
            "id": token_id,
            "app_id": app_id,
            "tenant_id": app[0],
            "origin": origin,
            "token": token,
            "expires_in_hours": ttl,
            "secret_shown_once": True,
        }

    def authenticate_embed_token(self, token: str, origin: str = "") -> dict | None:
        digest, origin = hashlib.sha256(str(token or "").encode("utf-8")).hexdigest(), str(origin or "").strip()
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT t.id, t.app_id, t.tenant_id, t.origin, a.agent_id, a.scopes_json, a.enabled
                                   FROM embed_tokens t JOIN external_apps a ON a.id=t.app_id
                                   JOIN agents g ON g.id=a.agent_id AND g.tenant_id=a.tenant_id
                                   WHERE t.token_hash=? AND t.enabled=1 AND a.enabled=1 AND g.status='active'
                                     AND t.expires_at > CURRENT_TIMESTAMP""", (digest,)).fetchone()
            if not row or (row[3] and row[3] != origin):
                return None
            conn.execute("UPDATE embed_tokens SET last_used_at=CURRENT_TIMESTAMP WHERE id=?", (row[0],))
        scopes = json.loads(row[5] or "[]")
        if "chat" not in scopes and "embed" not in scopes:
            return None
        return {"id": row[0], "app_id": row[1], "tenant_id": row[2], "origin": row[3],
                "agent_id": row[4], "scopes": scopes}

    # Tenant-aware accessors are defined here so every administrative mutation
    # can constrain the SQL update instead of trusting a client-supplied ID.
    def get_external_app(self, app_id: str, tenant_id: str | None = None) -> dict | None:
        return next((item for item in self.list_external_apps(tenant_id) if item["id"] == str(app_id)), None)

    def update_external_app_status(self, app_id: str, enabled: bool, tenant_id: str | None = None) -> bool:
        sql = "UPDATE external_apps SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"
        params: list[object] = [int(bool(enabled)), str(app_id)]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
        return cur.rowcount > 0

    def get_webhook_subscription(self, subscription_id: str, tenant_id: str | None = None) -> dict | None:
        return next((item for item in self.list_webhook_subscriptions(tenant_id) if item["id"] == str(subscription_id)), None)

    def update_webhook_subscription_status(self, subscription_id: str, enabled: bool,
                                           tenant_id: str | None = None) -> bool:
        sql = "UPDATE webhook_subscriptions SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?"
        params: list[object] = [int(bool(enabled)), str(subscription_id)]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
        return cur.rowcount > 0

    def get_embed_token(self, token_id: str) -> dict | None:
        return next((item for item in self.list_embed_tokens() if item["id"] == str(token_id)), None)

    def update_embed_token_status(self, token_id: str, enabled: bool, tenant_id: str | None = None) -> bool:
        sql = "UPDATE embed_tokens SET enabled=? WHERE id=?"
        params: list[object] = [int(bool(enabled)), str(token_id)]
        if tenant_id is not None:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(sql, params)
        return cur.rowcount > 0

    def record_webhook_delivery(self, subscription_id: str, event_id: str, event_type: str,
                                payload: dict, attempt: int, status_code: int | None,
                                error: str = "", delivered: bool = False) -> int:
        payload_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        with sqlite3.connect(self._db_path) as conn:
            tenant = conn.execute("SELECT tenant_id FROM webhook_subscriptions WHERE id=?", (subscription_id,)).fetchone()
            cur = conn.execute("""
                INSERT OR IGNORE INTO webhook_deliveries
                    (subscription_id, tenant_id, event_id, event_type, payload_hash, attempt, status_code, error, delivered, delivered_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CASE WHEN ? THEN CURRENT_TIMESTAMP ELSE NULL END)
            """, (subscription_id, tenant[0] if tenant else "", event_id, event_type, payload_hash,
                  attempt, status_code, str(error or "")[:500], int(bool(delivered)), int(bool(delivered))))
            if delivered:
                conn.execute("UPDATE webhook_subscriptions SET last_delivered_at=CURRENT_TIMESTAMP WHERE id=?", (subscription_id,))
        return int(cur.lastrowid or 0)

    def list_webhook_deliveries(self, tenant_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = "SELECT id, subscription_id, tenant_id, event_id, event_type, payload_hash, attempt, status_code, error, delivered, created_at, delivered_at FROM webhook_deliveries"
        params: list[object] = []
        if tenant_id:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "subscription_id": r[1], "tenant_id": r[2], "event_id": r[3],
                 "event_type": r[4], "payload_hash": r[5], "attempt": r[6], "status_code": r[7],
                 "error": r[8], "delivered": bool(r[9]), "created_at": self._utc_to_local(r[10] or ""),
                 "delivered_at": self._utc_to_local(r[11] or "") if r[11] else ""} for r in rows]

    def record_external_app_usage(self, app_id: str, tenant_id: str, user_ref: str,
                                  endpoint: str, status: str, duration_ms: int = 0,
                                  prompt_tokens: int = 0, completion_tokens: int = 0,
                                  error: str = "", request_id: str = "") -> int:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                INSERT INTO external_app_usage
                    (app_id, tenant_id, user_ref, endpoint, status, duration_ms,
                     prompt_tokens, completion_tokens, error, request_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (app_id, tenant_id, str(user_ref or "")[:160], endpoint, status,
                  max(0, int(duration_ms)), max(0, int(prompt_tokens)),
                  max(0, int(completion_tokens)), str(error or "")[:1000], request_id))
        return int(cur.lastrowid)

    def list_external_app_usage(self, tenant_id: str | None = None, limit: int = 100) -> list[dict]:
        sql = """
            SELECT id, app_id, tenant_id, user_ref, endpoint, status, duration_ms,
                   prompt_tokens, completion_tokens, error, request_id, created_at
            FROM external_app_usage
        """
        params = []
        if tenant_id:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(int(limit))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [{"id": r[0], "app_id": r[1], "tenant_id": r[2], "user_ref": r[3],
                 "endpoint": r[4], "status": r[5], "duration_ms": r[6],
                 "prompt_tokens": r[7], "completion_tokens": r[8], "error": r[9],
                 "request_id": r[10], "created_at": self._utc_to_local(r[11] or "")}
                for r in rows]

    # ---- 受控联网检索 ----
    def get_external_retrieval_config(self) -> dict:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT enabled, trigger_mode, max_sources, timeout_seconds, max_bytes,
                       updated_by, updated_at
                FROM external_retrieval_config WHERE id=1
            """).fetchone()
        if not row:
            return {"enabled": False, "trigger_mode": "empty_only", "max_sources": 3,
                    "timeout_seconds": 10, "max_bytes": 2000000, "updated_by": "",
                    "configured": False}
        return {"enabled": bool(row[0]), "trigger_mode": row[1], "max_sources": row[2],
                "timeout_seconds": row[3], "max_bytes": row[4], "updated_by": row[5],
                "updated_at": self._utc_to_local(row[6] or ""), "configured": True}

    def save_external_retrieval_config(self, config: dict, changed_by: str = "admin") -> dict:
        trigger_mode = str(config.get("trigger_mode") or "empty_only")
        if trigger_mode not in ("empty_only", "low_confidence", "always"):
            raise ValueError("trigger_mode 需为 empty_only/low_confidence/always")
        max_sources = max(1, min(int(config.get("max_sources", 3)), 10))
        timeout_seconds = max(1, min(int(config.get("timeout_seconds", 10)), 60))
        max_bytes = max(1024, min(int(config.get("max_bytes", 2000000)), 50 * 1024 * 1024))
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO external_retrieval_config
                    (id, enabled, trigger_mode, max_sources, timeout_seconds, max_bytes, updated_by, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET enabled=excluded.enabled,
                    trigger_mode=excluded.trigger_mode, max_sources=excluded.max_sources,
                    timeout_seconds=excluded.timeout_seconds, max_bytes=excluded.max_bytes,
                    updated_by=excluded.updated_by, updated_at=CURRENT_TIMESTAMP
            """, (int(bool(config.get("enabled"))), trigger_mode, max_sources,
                  timeout_seconds, max_bytes, str(changed_by or "admin")))
        return self.get_external_retrieval_config()

    def list_external_retrieval_sources(self, include_disabled: bool = True) -> list[dict]:
        sql = """
            SELECT id, name, source_type, endpoint, description, enabled, approved,
                   config_json, created_by, created_at, updated_at
            FROM external_retrieval_sources
        """
        params = ()
        if not include_disabled:
            sql += " WHERE enabled=1 AND approved=1"
        sql += " ORDER BY updated_at DESC, name"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try:
                cfg = json.loads(row[7] or "{}")
            except (TypeError, json.JSONDecodeError):
                cfg = {}
            items.append({"id": row[0], "name": row[1], "source_type": row[2],
                          "endpoint": row[3], "description": row[4], "enabled": bool(row[5]),
                          "approved": bool(row[6]), "config": cfg, "created_by": row[8],
                          "created_at": self._utc_to_local(row[9] or ""),
                          "updated_at": self._utc_to_local(row[10] or "")})
        return items

    def upsert_external_retrieval_source(self, source: dict, changed_by: str = "admin") -> dict:
        source_id = str(source.get("id") or ("ext-" + uuid.uuid4().hex[:16]))
        name = str(source.get("name") or "").strip()
        endpoint = str(source.get("endpoint") or "").strip()
        source_type = str(source.get("source_type") or "url").strip()
        if not name or not endpoint:
            raise ValueError("外部来源名称和地址不能为空")
        if source_type not in ("url", "rss", "threat_intel"):
            raise ValueError("source_type 需为 url/rss/threat_intel")
        validation = validate_data_source_config("url", endpoint, source.get("config") or {})
        if not validation["valid"]:
            raise ValueError("；".join(validation["errors"]))
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO external_retrieval_sources
                    (id, name, source_type, endpoint, description, enabled, approved, config_json, created_by, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET name=excluded.name, source_type=excluded.source_type,
                    endpoint=excluded.endpoint, description=excluded.description, enabled=excluded.enabled,
                    config_json=excluded.config_json, updated_at=CURRENT_TIMESTAMP
            """, (source_id, name, source_type, endpoint, str(source.get("description") or "")[:500],
                  int(bool(source.get("enabled"))), int(bool(source.get("approved"))),
                  json.dumps(source.get("config") or {}, ensure_ascii=False), str(changed_by or "admin")))
        return next(item for item in self.list_external_retrieval_sources() if item["id"] == source_id)

    def update_external_retrieval_source_status(self, source_id: str, enabled: bool | None = None,
                                                approved: bool | None = None) -> bool:
        fields, params = [], []
        if enabled is not None:
            fields.append("enabled=?")
            params.append(int(bool(enabled)))
        if approved is not None:
            fields.append("approved=?")
            params.append(int(bool(approved)))
        if not fields:
            return False
        params.append(source_id)
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                f"UPDATE external_retrieval_sources SET {', '.join(fields)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                params,
            )
        return cur.rowcount > 0

    def log_external_retrieval_event(self, event: dict) -> int:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                INSERT INTO external_retrieval_events
                    (trace_id, conversation_id, tenant_id, user_id, query, source_id, source_url,
                     content_hash, status, result_count, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (str(event.get("trace_id") or ""), str(event.get("conversation_id") or ""),
                  str(event.get("tenant_id") or ""), str(event.get("user_id") or ""),
                  str(event.get("query") or "")[:500], str(event.get("source_id") or ""),
                  str(event.get("source_url") or ""), str(event.get("content_hash") or ""),
                  str(event.get("status") or "failed"), int(event.get("result_count") or 0),
                  str(event.get("error") or "")[:1000]))
            return int(cur.lastrowid)

    def list_external_retrieval_events(self, limit: int = 100) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, trace_id, conversation_id, tenant_id, user_id, query, source_id,
                       source_url, content_hash, status, result_count, error, created_at
                FROM external_retrieval_events ORDER BY id DESC LIMIT ?
            """, (int(limit),)).fetchall()
        return [{"id": r[0], "trace_id": r[1], "conversation_id": r[2], "tenant_id": r[3],
                 "user_id": r[4], "query": r[5], "source_id": r[6], "source_url": r[7],
                 "content_hash": r[8], "status": r[9], "result_count": r[10], "error": r[11],
                 "created_at": self._utc_to_local(r[12] or "")} for r in rows]

    def save_langfuse_config(self, config: dict) -> None:
        from llm_config_manager import _encrypt_api_key, _make_key_mask
        current = self.get_langfuse_config(include_secrets=True)
        public_key = str(config.get("public_key") or current.get("public_key") or "").strip()
        secret_key = str(config.get("secret_key") or current.get("secret_key") or "").strip()
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO langfuse_config
                    (id, enabled, host, public_key_enc, public_key_mask, secret_key_enc, secret_key_mask,
                     export_content, annotation_queue, updated_at)
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(id) DO UPDATE SET
                    enabled=excluded.enabled, host=excluded.host,
                    public_key_enc=excluded.public_key_enc, public_key_mask=excluded.public_key_mask,
                    secret_key_enc=excluded.secret_key_enc, secret_key_mask=excluded.secret_key_mask,
                    export_content=excluded.export_content, annotation_queue=excluded.annotation_queue,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                int(bool(config.get("enabled"))), str(config.get("host") or "https://cloud.langfuse.com").rstrip("/"),
                _encrypt_api_key(public_key) if public_key else "", _make_key_mask(public_key) if public_key else "",
                _encrypt_api_key(secret_key) if secret_key else "", _make_key_mask(secret_key) if secret_key else "",
                int(bool(config.get("export_content"))), str(config.get("annotation_queue") or "").strip(),
            ))

    def get_langfuse_config(self, include_secrets: bool = False) -> dict:
        from llm_config_manager import _decrypt_api_key
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT enabled, host, public_key_enc, public_key_mask, secret_key_enc, secret_key_mask,
                       export_content, annotation_queue, updated_at
                FROM langfuse_config WHERE id=1
            """).fetchone()
        if not row:
            return {"enabled": False, "host": "https://cloud.langfuse.com", "export_content": False,
                    "annotation_queue": "", "configured": False}
        result = {"enabled": bool(row[0]), "host": row[1], "public_key_mask": row[3],
                  "secret_key_mask": row[5], "export_content": bool(row[6]),
                  "annotation_queue": row[7], "updated_at": self._utc_to_local(row[8] or ""),
                  "configured": bool(row[2] and row[4])}
        if include_secrets:
            result["public_key"] = _decrypt_api_key(row[2]) if row[2] else ""
            result["secret_key"] = _decrypt_api_key(row[4]) if row[4] else ""
        return result

    def create_prompt_version(self, name: str, description: str, system_prompt: str,
                              changed_by: str = "管理员", change_log: str = "",
                              prompt_diff: str = "") -> dict:
        """创建新版本（带扩展字段）"""
        from datetime import datetime
        with sqlite3.connect(self._db_path) as conn:
            exists = conn.execute(
                "SELECT id FROM prompt_versions WHERE version_name = ?", (name,)
            ).fetchone()
            if exists:
                return {"ok": False, "message": f"版本 {name} 已存在"}
            conn.execute("UPDATE prompt_versions SET is_active = 0")
            conn.execute("""
                INSERT INTO prompt_versions (version_name, description, system_prompt, created_at,
                    is_active, created_by, changed_by, change_log, prompt_diff)
                VALUES (?, ?, ?, ?, 1, ?, ?, ?, ?)
            """, (name, description, system_prompt, datetime.now().isoformat(),
                  changed_by, changed_by, change_log, prompt_diff))
        return {"ok": True, "version": name}

    def restore_prompt_version(self, version_name: str) -> Optional[str]:
        """还原版本，返回该版本的 system_prompt 内容"""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT system_prompt FROM prompt_versions WHERE version_name = ?",
                (version_name,),
            ).fetchone()
            if not row:
                return None
            # 备份当前 active_prompt.txt
            active_path = Path(self._db_path).parent / "active_prompt.txt"
            if active_path.exists():
                import shutil
                shutil.copy2(str(active_path), str(active_path.with_suffix(".bak")))
            # 写入新的 system prompt
            from agent import sanitize_system_prompt
            active_path.write_text(sanitize_system_prompt(row[0]), encoding="utf-8")
            # 切换激活标记
            conn.execute("UPDATE prompt_versions SET is_active = 0")
            conn.execute(
                "UPDATE prompt_versions SET is_active = 1 WHERE version_name = ?",
                (version_name,),
            )
        return row[0]

    def get_jailbreak_report_data(self, conversation_id: str) -> dict:
        """生成越狱报告所需数据"""
        detail = self.get_conversation_detail(conversation_id)
        if not detail:
            return {}
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT answer_jailbreak, trace_data FROM usage_logs WHERE conversation_id = ? AND answer_jailbreak = 1 LIMIT 1",
                (conversation_id,),
            ).fetchone()
            trace = None
            if row:
                detail["has_answer_jailbreak"] = bool(row[0]) if row[0] else False
                if row[1]:
                    try:
                        trace = json.loads(row[1])
                    except (json.JSONDecodeError, TypeError):
                        pass
            else:
                detail["has_answer_jailbreak"] = False
            # 获取全量 usage_logs（每轮的检索/生成数据）
            all_logs = conn.execute("""
                SELECT message_id, query, rewrite_time, faiss_time, chroma_time, rerank_time,
                       llm_time, total_time, faiss_count, chroma_count, bm25_count, final_count,
                       returned_count, prompt_tokens, completion_tokens,
                       llm_success, was_circuit_break, was_truncated, off_topic, documents,
                       answer_jailbreak, trace_data
                FROM usage_logs
                WHERE conversation_id = ?
                ORDER BY id
            """, (conversation_id,)).fetchall()
        detail["trace_data"] = trace
        detail["usage_logs"] = [
            {
                "message_id": r[0],
                "query": r[1][:100] if r[1] else "",
                "rewrite_time": r[2],
                "faiss_time": r[3],
                "chroma_time": r[4] or 0,
                "rerank_time": r[5] or 0,
                "llm_time": r[6],
                "total_time": r[7],
                "faiss_count": r[8],
                "chroma_count": r[9] or 0,
                "bm25_count": r[10] or 0,
                "final_count": r[11] or 0,
                "returned_count": r[12],
                "prompt_tokens": r[13],
                "completion_tokens": r[14],
                "llm_success": bool(r[15]),
                "was_circuit_break": bool(r[16]) if r[16] else False,
                "was_truncated": bool(r[17]) if r[17] else False,
                "off_topic": bool(r[18]) if r[18] else False,
                "documents": json.loads(r[19]) if r[19] else None,
                "answer_jailbreak": r[20],
                "trace": json.loads(r[21]) if r[21] else None,
            }
            for r in all_logs
        ]
        return detail

    def get_unrated_assistant_message(self, conversation_id: str) -> Optional[dict]:
        """获取上一条未手动评分的 assistant 消息"""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT m.id, m.content, m.created_at
                FROM messages m
                LEFT JOIN usage_logs u ON m.id = u.message_id
                WHERE m.conversation_id = ? AND m.role = 'assistant'
                  AND (u.user_rating IS NULL OR u.id IS NULL)
                ORDER BY m.id DESC LIMIT 1
            """, (conversation_id,)).fetchone()
        if row:
            return {"id": row[0], "content": row[1], "created_at": row[2]}
        return None

    def get_dashboard_stats(self, category: str = "all") -> dict:
        """聚合 usage_logs 数据供看板使用
        category: 'all'(默认) | 'user' | 'prompt_test' | 'ide_test' | 'other_test'
        """
        # 如果指定了 category，预查询匹配的 conversation_id 列表
        _cat_ids = None
        if category and category != "all":
            with sqlite3.connect(self._db_path) as conn:
                rows = conn.execute(
                    "SELECT id FROM conversations WHERE category = ?",
                    (category,),
                ).fetchall()
                _cat_ids = [r[0] for r in rows]
                if not _cat_ids:
                    return {"doc_hotness": [], "ratings": {"distribution": {}, "trend": []},
                            "retrieval_trends": [], "llm_health": [], "query_hotspots": [],
                            "activity": [], "truncation": [], "confidence_scatter": []}

        def _cat_sql(sql: str) -> tuple:
            """在 SQL 中插入 conversation_id 过滤（在 GROUP BY/ORDER BY/LIMIT 之前）"""
            if _cat_ids is None:
                return sql, ()
            ph = ",".join("?" for _ in _cat_ids)
            has_where = " WHERE " in sql.upper() or "\nWHERE " in sql.upper()
            prefix = " AND " if has_where else " WHERE "
            clause = f"{prefix}conversation_id IN ({ph})"
            insert_pos = len(sql)
            for kw in ["GROUP BY", "ORDER BY", "LIMIT"]:
                pos = sql.upper().find(f" {kw}")
                if pos != -1 and pos < insert_pos:
                    insert_pos = pos
            return sql[:insert_pos] + clause + sql[insert_pos:], tuple(_cat_ids)

        def _cat_exec(sql: str, conn) -> list:
            s, params = _cat_sql(sql)
            return conn.execute(s, params).fetchall()

        with sqlite3.connect(self._db_path) as conn:
            raw = _cat_exec(
                "SELECT documents FROM usage_logs WHERE documents IS NOT NULL AND documents != 'null' ORDER BY id DESC LIMIT 500",
                conn,
            )
        doc_stats = {}
        for (docs_json,) in raw:
            try:
                docs = json.loads(docs_json)
                for d in docs:
                    fn = d.get("file_name", "未知")
                    if fn not in doc_stats:
                        doc_stats[fn] = {"count": 0, "confidences": [],
                                         "category": d.get("category", "")}
                    doc_stats[fn]["count"] += 1
                    c = d.get("confidence", 0.5)
                    if isinstance(c, (int, float)):
                        doc_stats[fn]["confidences"].append(c)
            except Exception:
                logger.warning("解析舆情统计时异常", exc_info=True)
                pass
        hotness = sorted(doc_stats.items(), key=lambda x: x[1]["count"], reverse=True)[:20]
        doc_hotness = []
        for fn, st in hotness:
            avg_c = sum(st["confidences"]) / len(st["confidences"]) if st["confidences"] else 0
            doc_hotness.append({"file_name": fn, "count": st["count"], "avg_confidence": round(
                avg_c, 3), "category": st["category"]})

        with sqlite3.connect(self._db_path) as conn:
            ratings = _cat_exec(
                "SELECT COALESCE(user_rating, semantic_rating) FROM usage_logs WHERE COALESCE(user_rating, semantic_rating) IS NOT NULL",
                conn,
            )
        dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for (r,) in ratings:
            if r in dist:
                dist[r] += 1

        with sqlite3.connect(self._db_path) as conn:
            rating_rows = _cat_exec(
                "SELECT DATE(created_at) as d, AVG(COALESCE(user_rating, semantic_rating))"
                " FROM usage_logs"
                " WHERE COALESCE(user_rating, semantic_rating) IS NOT NULL"
                "  AND created_at >= DATE('now', '-30 days')"
                " GROUP BY d ORDER BY d",
                conn,
            )
        rating_trend = [{"date": r[0], "avg_rating": round(r[1], 2)} for r in rating_rows]

        with sqlite3.connect(self._db_path) as conn:
            ret_rows = _cat_exec(
                "SELECT DATE(created_at) as d,"
                "  AVG(rewrite_time), AVG(llm_time), AVG(total_time),"
                "  AVG(returned_count), COUNT(*)"
                " FROM usage_logs"
                " WHERE created_at >= DATE('now', '-30 days')"
                " GROUP BY d ORDER BY d",
                conn,
            )
        retrieval_trends = []
        for r in ret_rows:
            retrieval_trends.append({
                "date": r[0], "avg_rewrite_time": round(r[1] or 0, 3),
                "avg_llm_time": round(r[2] or 0, 3), "avg_total_time": round(r[3] or 0, 3),
                "avg_returned_count": round(r[4] or 0, 1), "query_count": r[5],
            })

        with sqlite3.connect(self._db_path) as conn:
            llm_rows = _cat_exec(
                "SELECT DATE(created_at) as d, COUNT(*),"
                "  SUM(llm_success), SUM(was_circuit_break)"
                " FROM usage_logs"
                " WHERE created_at >= DATE('now', '-30 days')"
                " GROUP BY d ORDER BY d",
                conn,
            )
        llm_health = []
        for r in llm_rows:
            total = r[1]
            llm_health.append({
                "date": r[0], "total_calls": total,
                "success_count": r[2] or 0, "fail_count": total - (r[2] or 0),
                "circuit_break_count": r[3] or 0,
            })

        with sqlite3.connect(self._db_path) as conn:
            query_rows = _cat_exec(
                "SELECT query FROM usage_logs ORDER BY id DESC LIMIT 200",
                conn,
            )
        hotspot_words = {}
        from jieba_compat import load_jieba
        jieba = load_jieba()
        stop_words = {"的", "了", "是", "在", "有", "和", "就", "不", "也", "都", "要", "吗", "呢", "吧", "啊",
                      "什么", "怎么", "如何", "哪些", "哪个", "一个", "这个", "那个", "对", "为", "可以", "能",
                      "我", "你", "他", "它", "她", "我们", "你们", "他们", "与", "及", "或", "等", "之"}
        for (q,) in query_rows:
            words = jieba.lcut(q)
            for w in words:
                if len(w) >= 2 and w not in stop_words:
                    hotspot_words[w] = hotspot_words.get(w, 0) + 1
        hotspots = sorted(hotspot_words.items(), key=lambda x: x[1], reverse=True)[:30]
        query_hotspots = [{"keyword": k, "count": c} for k, c in hotspots]

        with sqlite3.connect(self._db_path) as conn:
            act_rows = _cat_exec(
                "SELECT DATE(created_at) as d, COUNT(DISTINCT conversation_id), COUNT(*)"
                " FROM usage_logs"
                " WHERE created_at >= DATE('now', '-30 days')"
                " GROUP BY d ORDER BY d",
                conn,
            )
        activity = [{"date": r[0], "new_convs": 0, "total_queries": r[2]} for r in act_rows]

        with sqlite3.connect(self._db_path) as conn:
            trunc_rows = _cat_exec(
                "SELECT DATE(created_at) as d, SUM(was_truncated), COUNT(*)"
                " FROM usage_logs"
                " WHERE created_at >= DATE('now', '-30 days')"
                " GROUP BY d ORDER BY d",
                conn,
            )
        truncation = [{"date": r[0], "truncation_count": r[1] or 0, "total": r[2]}
                      for r in trunc_rows]

        with sqlite3.connect(self._db_path) as conn:
            conf_rows = _cat_exec(
                "SELECT documents, COALESCE(user_rating, semantic_rating), query"
                " FROM usage_logs"
                " WHERE documents IS NOT NULL AND documents != 'null'"
                "  AND COALESCE(user_rating, semantic_rating) IS NOT NULL"
                " ORDER BY id DESC LIMIT 100",
                conn,
            )
        confidence_scatter = []
        for docs_json, rating, q in conf_rows:
            try:
                docs = json.loads(docs_json)
                confs = [d.get("confidence", 0)
                         for d in docs if isinstance(d.get("confidence"), (int, float))]
                avg_c = sum(confs) / len(confs) if confs else 0
                confidence_scatter.append({
                    "avg_confidence": round(avg_c, 3), "rating": rating,
                    "query_short": (q or "")[:20],
                })
            except Exception:
                logger.warning("雷达图数据异常", exc_info=True)
                pass

        return {
            "doc_hotness": doc_hotness,
            "query_hotspots": query_hotspots,
            "retrieval_trends": retrieval_trends,
            "llm_health": llm_health,
            "ratings": {"distribution": dist, "trend": rating_trend},
            "activity": activity,
            "truncation": truncation,
            "confidence_scatter": confidence_scatter,
        }

    def drill_down(self, drill_type: str, key: str, limit: int = 50, category: str = "all") -> list[dict]:
        """钻取查询，category='all'=全部，其他值按 conversations.category 过滤"""
        cat_join = ""
        cat_where = ""
        cat_params = ()
        if category and category != "all":
            cat_join = " JOIN conversations c ON u.conversation_id = c.id"
            cat_where = " AND c.category = ?"
            cat_params = (category,)

        with sqlite3.connect(self._db_path) as conn:
            if drill_type == 'rating':
                rows = conn.execute(f"""
                    SELECT u.conversation_id, u.query, u.user_rating, u.semantic_rating,
                           COALESCE(u.user_rating, u.semantic_rating), u.created_at
                    FROM usage_logs u{cat_join}
                    WHERE COALESCE(u.user_rating, u.semantic_rating) = ?{cat_where}
                    ORDER BY u.created_at DESC LIMIT ?
                """, (int(key),) + cat_params + (limit,)).fetchall()
                fields = ["user_rating", "semantic_rating", "final_rating", "created_at"]
            elif drill_type == 'keyword':
                rows = conn.execute(f"""
                    SELECT u.conversation_id, u.query, u.user_rating, u.semantic_rating,
                           COALESCE(u.user_rating, u.semantic_rating), u.created_at
                    FROM usage_logs u{cat_join}
                    WHERE u.query LIKE ?{cat_where}
                    ORDER BY u.created_at DESC LIMIT ?
                """, (f'%{key}%',) + cat_params + (limit,)).fetchall()
                fields = ["user_rating", "semantic_rating", "final_rating", "created_at"]
            elif drill_type == 'error_date':
                rows = conn.execute(f"""
                    SELECT u.conversation_id, u.query, u.llm_success, u.was_circuit_break,
                           u.circuit_provider, u.created_at
                    FROM usage_logs u{cat_join}
                    WHERE DATE(u.created_at) = ? AND (u.llm_success = 0 OR u.was_circuit_break = 1){cat_where}
                    ORDER BY u.created_at DESC LIMIT ?
                """, (key,) + cat_params + (limit,)).fetchall()
                fields = ["llm_success", "was_circuit_break", "circuit_provider", "created_at"]
            elif drill_type == 'scatter':
                rows = conn.execute(f"""
                    SELECT u.conversation_id, u.query, u.documents, u.created_at
                    FROM usage_logs u{cat_join}
                    WHERE u.documents IS NOT NULL AND u.documents != 'null'
                      AND COALESCE(u.user_rating, u.semantic_rating) IS NOT NULL{cat_where}
                    ORDER BY u.id DESC LIMIT 100
                """, cat_params).fetchall()
                idx = int(key)
                if 0 <= idx < len(rows):
                    r = rows[idx]
                    docs = json.loads(r[2]) if r[2] else []
                    confs = [d.get("confidence", 0)
                             for d in docs if isinstance(d.get("confidence"), (int, float))]
                    avg_c = sum(confs) / len(confs) if confs else 0
                    return [{"conversation_id": r[0], "query": r[1], "detail": {"avg_confidence": round(avg_c, 3), "doc_count": len(docs), "created_at": r[3]}}]
                return []
            else:
                return []

        return [
            {"conversation_id": r[0], "query": r[1][:100] if r[1] else "",
                "detail": dict(zip(fields, [r[i] for i in range(2, len(r))]))}
            for r in rows
        ]

    # ==================== 反馈与知识缺口 ====================
    _FEEDBACK_TYPES = ("copy", "refresh", "correction", "unhelpful", "no_source")
    _FAILURE_CLASSES = (
        "retrieval_miss",
        "insufficient_evidence",
        "answer_quality",
        "knowledge_gap",
        "positive",
    )

    @classmethod
    def classify_feedback(cls, feedback_type: str, feedback_text: str = "",
                          trace: dict | None = None) -> str:
        """Classify feedback for retrieval evaluation without changing user-facing semantics."""
        if feedback_type == "copy":
            return "positive"
        if feedback_type == "no_source":
            return "knowledge_gap"
        text = str(feedback_text or "").casefold()
        retrieval_terms = ("没找到", "找不到", "检索", "搜索", "搜不到", "文档不对", "引用不对")
        evidence_terms = ("没有依据", "缺少依据", "证据不足", "没有来源", "未引用", "引用不足")
        knowledge_terms = ("知识库没有", "库里没有", "缺资料", "没有资料", "缺少文档")
        if any(term in text for term in knowledge_terms):
            return "knowledge_gap"
        if any(term in text for term in evidence_terms):
            return "insufficient_evidence"
        if feedback_type == "refresh" and any(term in text for term in retrieval_terms):
            return "retrieval_miss"
        if feedback_type in ("refresh", "unhelpful") and trace:
            retrieval = trace.get("retrieval") or trace.get("steps") or {}
            if isinstance(retrieval, dict) and retrieval.get("returned_count") == 0:
                return "retrieval_miss"
        return "answer_quality"

    @staticmethod
    def _normalize_query_for_cluster(query: str) -> str:
        """去掉空白与常见标点，得到可比较的查询指纹。"""
        text = str(query or "").lower()
        text = re.sub(r"[\s，。！？、；：\"\"''（）()【】\[\]\-_—·.,!?;:'\"<>《》]+", "", text)
        return text[:120]

    @staticmethod
    def _query_similarity(a: str, b: str) -> float:
        na = ConversationMemory._normalize_query_for_cluster(a)
        nb = ConversationMemory._normalize_query_for_cluster(b)
        if not na or not nb:
            return 0.0
        if na == nb:
            return 1.0
        seq_score = SequenceMatcher(None, na, nb).ratio()
        stop_words = {"的", "了", "是", "在", "有", "和", "就", "不", "也", "都", "要", "吗", "呢",
                      "吧", "啊", "什么", "怎么", "如何", "哪些", "哪个", "一个", "这个", "那个",
                      "对", "为", "可以", "能", "我", "你", "他", "它", "她", "我们", "你们",
                      "他们", "与", "及", "或", "等", "之", "请", "帮", "请问", "帮我"}
        from jieba_compat import load_jieba
        jieba = load_jieba()
        wa = {w for w in jieba.lcut(na) if len(w) >= 2 and w not in stop_words}
        wb = {w for w in jieba.lcut(nb) if len(w) >= 2 and w not in stop_words}
        if not wa or not wb:
            return seq_score
        jaccard = len(wa & wb) / len(wa | wb)
        return max(seq_score, 0.45 * jaccard + 0.55 * seq_score)

    def record_feedback(self, message_id: int, feedback_type: str,
                        feedback_text: str = "", user_id: str = "",
                        apply_rating: bool = True) -> Optional[int]:
        """记录前台反馈；copy 映射 5 星、refresh/unhelpful 映射 1 星，并触发缺口聚类。"""
        if feedback_type not in self._FEEDBACK_TYPES:
            return None
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT conversation_id, query, user_rating, semantic_rating, trace_data FROM usage_logs WHERE message_id = ?",
                (int(message_id),),
            ).fetchone()
            if not row:
                msg = conn.execute(
                    "SELECT conversation_id FROM messages WHERE id = ?", (int(message_id),)
                ).fetchone()
                if not msg:
                    return None
                conversation_id, query = msg[0], ""
                user_rating = semantic_rating = None
                trace_data = None
            else:
                conversation_id, query = row[0], row[1] or ""
                user_rating, semantic_rating, trace_data = row[2], row[3], row[4]
            try:
                trace = json.loads(trace_data or "{}") if trace_data else {}
            except (TypeError, json.JSONDecodeError):
                trace = {}
            failure_class = self.classify_feedback(feedback_type, feedback_text, trace)
            cur = conn.execute("""
                INSERT INTO feedback_items
                    (message_id, conversation_id, query, feedback_type, feedback_text, user_id, failure_class)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (int(message_id), conversation_id, str(query)[:500], feedback_type,
                  str(feedback_text or "")[:2000], str(user_id or ""), failure_class))
            feedback_id = int(cur.lastrowid)
            if apply_rating and feedback_type in ("copy", "refresh", "unhelpful")                     and user_rating is None and semantic_rating is None:
                rating = 5 if feedback_type == "copy" else 1
                conn.execute(
                    "UPDATE usage_logs SET user_rating = ? WHERE message_id = ? AND user_rating IS NULL",
                    (rating, int(message_id)),
                )
            trace.setdefault("feedback", []).append({
                "id": feedback_id, "type": feedback_type,
                "failure_class": failure_class,
                "has_text": bool(str(feedback_text or "").strip()),
            })
            conn.execute(
                "UPDATE usage_logs SET trace_data = ? WHERE message_id = ?",
                (json.dumps(trace, ensure_ascii=False), int(message_id)),
            )
        self.rebuild_knowledge_gaps()
        return feedback_id

    def promote_feedback_to_retrieval_eval(self, feedback_id: int, created_by: str = "admin",
                                            expected=None, category: str = "feedback") -> Optional[dict]:
        """Create a pending retrieval-eval case from one feedback record.

        Expected evidence is deliberately left for human labeling; feedback alone is not
        treated as ground truth.
        """
        from retrieval_eval_contract import serialize_retrieval_expectation
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                """SELECT id, query, failure_class FROM feedback_items WHERE id = ?""",
                (int(feedback_id),),
            ).fetchone()
            if not row:
                return None
            existing = conn.execute(
                "SELECT id FROM retrieval_eval_items WHERE source_feedback_id = ? LIMIT 1",
                (int(feedback_id),),
            ).fetchone()
            if existing:
                return {"id": int(existing[0]), "created": False, "label_status": "pending"}
            cur = conn.execute(
                """INSERT INTO retrieval_eval_items
                   (query, expected, profile, category, difficulty, source_feedback_id,
                    label_status, failure_class)
                   VALUES (?, ?, 'general', ?, 'medium', ?, 'pending', ?)""",
                (row[1] or "", serialize_retrieval_expectation(expected or {}),
                 f"{category}:{row[2] or 'answer_quality'}", int(feedback_id),
                 row[2] or "answer_quality"),
            )
            return {
                "id": int(cur.lastrowid), "created": True, "label_status": "pending",
                "source_feedback_id": int(feedback_id), "created_by": created_by,
            }

    def promote_knowledge_gap_to_prompt_test(self, gap_id: int,
                                                created_by: str = "admin", tenant_id: str = "") -> Optional[dict]:
        """将知识缺口生成一个独立 Prompt 测试集，保留来源和验收上下文。"""
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT canonical_question, profile, knowledge_base_id, reason_tags,
                       related_message_ids, occurrence_count
                FROM knowledge_gaps WHERE id = ? AND (?='' OR tenant_id=?)
            """, (int(gap_id), tenant_id, tenant_id)).fetchone()
            if not row:
                return None
            query, profile, knowledge_base_id, reason_tags, message_ids, occurrences = row
            existing = conn.execute(
                "SELECT set_id FROM prompt_test_items WHERE category = ? AND query = ? AND is_active = 1 LIMIT 1",
                (f"feedback_gap:{int(gap_id)}", query),
            ).fetchone()
            if existing:
                return {"set_id": existing[0], "item_id": conn.execute(
                    "SELECT id FROM prompt_test_items WHERE set_id = ? AND is_active = 1 LIMIT 1",
                    (existing[0],),
                ).fetchone()[0], "created": False}
            set_id = "feedback_" + uuid.uuid4().hex[:10]
            conn.execute(
                "INSERT INTO prompt_test_sets (source, keyword_input, is_active) VALUES (?, ?, 1)",
                ("feedback", f"知识缺口#{gap_id}: {query[:120]}"),
            )
            conn.execute("""
                INSERT INTO prompt_test_items (set_id, seq, query, category, difficulty, expected, is_active)
                VALUES (?, 1, ?, ?, 'medium', ?, 1)
            """, (
                set_id, query, f"feedback_gap:{int(gap_id)}",
                json.dumps({
                    "profile": profile or "general",
                    "knowledge_base_id": knowledge_base_id or "",
                    "source": "knowledge_gap",
                    "gap_id": int(gap_id),
                    "reason_tags": json.loads(reason_tags or "[]"),
                    "related_message_ids": json.loads(message_ids or "[]"),
                    "occurrence_count": int(occurrences or 0),
                    "created_by": created_by or "admin",
                    "assertions": ["回答应直接回应用户问题", "回答应有可核验依据"],
                }, ensure_ascii=False),
            ))
            item_id = conn.execute(
                "SELECT id FROM prompt_test_items WHERE set_id = ? AND is_active = 1 LIMIT 1",
                (set_id,),
            ).fetchone()[0]
        return {"set_id": set_id, "item_id": item_id, "created": True}

    def list_feedback_items(self, limit: int = 50) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, message_id, conversation_id, query, feedback_type, feedback_text,
                       user_id, failure_class, created_at
                FROM feedback_items ORDER BY id DESC LIMIT ?
            """, (int(limit),)).fetchall()
        return [
            {"id": r[0], "message_id": r[1], "conversation_id": r[2], "query": r[3],
             "feedback_type": r[4], "feedback_text": r[5], "user_id": r[6],
             "failure_class": r[7] or "answer_quality",
             "created_at": self._utc_to_local(r[8])}
            for r in rows
        ]

    def rebuild_knowledge_gaps(self, limit_candidates: int = 2000,
                               similarity_threshold: float = 0.72,
                               tenant_id: str = "") -> dict:
        """从低分、刷新、纠错、无来源回答中聚类并重建知识缺口清单。"""
        limit = max(50, int(limit_candidates))
        with sqlite3.connect(self._db_path) as conn:
            usage_rows = conn.execute("""
                SELECT u.id, u.conversation_id, u.message_id, u.query,
                       COALESCE(u.user_rating, u.semantic_rating) AS rating,
                       COALESCE(u.returned_count, 0), u.documents, u.created_at,
                       COALESCE(c.knowledge_base_id, ''), COALESCE(kb.profile, 'general'), c.tenant_id, c.agent_id, c.user_id
                FROM usage_logs u
                LEFT JOIN conversations c ON u.conversation_id = c.id
                LEFT JOIN knowledge_bases kb ON c.knowledge_base_id = kb.id
                WHERE (?='' OR c.tenant_id=?) AND ((COALESCE(u.user_rating, u.semantic_rating) <= 2)
                   OR (COALESCE(u.returned_count, 0) = 0)
                   OR (u.documents IS NULL OR u.documents = '' OR u.documents = 'null'))
                ORDER BY u.id DESC LIMIT ?
            """, (tenant_id, tenant_id, limit)).fetchall()
            feedback_rows = conn.execute("""
                SELECT f.id, f.conversation_id, f.message_id, f.query, f.feedback_type, f.created_at,
                       COALESCE(c.knowledge_base_id, ''), COALESCE(kb.profile, 'general'), c.tenant_id, c.agent_id, c.user_id
                FROM feedback_items f
                LEFT JOIN conversations c ON f.conversation_id = c.id
                LEFT JOIN knowledge_bases kb ON c.knowledge_base_id = kb.id
                WHERE (?='' OR c.tenant_id=?) ORDER BY f.id DESC LIMIT ?
            """, (tenant_id, tenant_id, limit)).fetchall()
        candidates = []
        for r in usage_rows:
            reasons = []
            if r[4] is not None and int(r[4]) <= 2:
                reasons.append("low_rating")
            if int(r[5]) == 0 or not r[6] or r[6] == "null":
                reasons.append("no_source")
            if not reasons:
                continue
            candidates.append({
                "message_id": r[2], "conversation_id": r[1], "query": r[3] or "",
                "created_at": r[7], "knowledge_base_id": r[8] or "", "profile": r[9] or "general", "tenant_id": r[10], "agent_id": r[11], "owner_user_id": r[12],
                "reasons": reasons,
            })
        for r in feedback_rows:
            ftype = r[4]
            if ftype in ("refresh", "correction", "unhelpful", "no_source"):
                candidates.append({
                    "message_id": r[2], "conversation_id": r[1], "query": r[3] or "",
                "created_at": r[5], "knowledge_base_id": r[6] or "", "profile": r[7] or "general", "tenant_id": r[8], "agent_id": r[9], "owner_user_id": r[10],
                    "reasons": [ftype],
                })
        clusters = []
        for cand in candidates:
            query = (cand["query"] or "").strip()
            if not query:
                continue
            matched = None
            for cluster in clusters:
                if self._query_similarity(cluster["canonical_question"], query) >= similarity_threshold:
                    matched = cluster
                    break
            if matched is None:
                cluster_key = hashlib.sha1(
                    f"{cand['tenant_id']}:{self._normalize_query_for_cluster(query)}".encode("utf-8")
                ).hexdigest()[:32]
                matched = {
                    "cluster_key": cluster_key,
                    "canonical_question": query,
                    "question_counts": {},
                    "question_order": {},
                    "profile_counts": {},
                    "kb_counts": {},
                    "reason_counts": {"low_rating": 0, "refresh": 0, "correction": 0,
                                      "unhelpful": 0, "no_source": 0},
                    "related_queries": [],
                    "related_message_ids": [],
                    "first_seen_at": cand["created_at"],
                    "last_seen_at": cand["created_at"],
                    "total": 0,
                    "tenant_id": cand["tenant_id"], "agent_id": cand["agent_id"], "owner_user_id": cand["owner_user_id"],
                }
                clusters.append(matched)
            matched["question_counts"][query] = matched["question_counts"].get(query, 0) + 1
            matched["question_order"].setdefault(query, len(matched["question_order"]))
            for reason in cand["reasons"]:
                matched["reason_counts"][reason] = matched["reason_counts"].get(reason, 0) + 1
            matched["profile_counts"][cand["profile"]] = matched["profile_counts"].get(cand["profile"], 0) + 1
            kb = cand["knowledge_base_id"] or ""
            matched["kb_counts"][kb] = matched["kb_counts"].get(kb, 0) + 1
            if query not in matched["related_queries"] and len(matched["related_queries"]) < 20:
                matched["related_queries"].append(query)
            mid = cand["message_id"]
            if mid and mid not in matched["related_message_ids"] and len(matched["related_message_ids"]) < 100:
                matched["related_message_ids"].append(mid)
            if cand["created_at"] and (not matched["first_seen_at"] or cand["created_at"] < matched["first_seen_at"]):
                matched["first_seen_at"] = cand["created_at"]
            if cand["created_at"] and (not matched["last_seen_at"] or cand["created_at"] > matched["last_seen_at"]):
                matched["last_seen_at"] = cand["created_at"]
            matched["total"] += 1
        created = updated = 0
        with sqlite3.connect(self._db_path) as conn:
            for cluster in clusters:
                canonical = max(
                    cluster["question_counts"].items(),
                    key=lambda kv: (kv[1], -cluster["question_order"][kv[0]]),
                )[0]
                profile = max(cluster["profile_counts"].items(), key=lambda kv: kv[1])[0] or "general"
                kb_id = max(cluster["kb_counts"].items(), key=lambda kv: kv[1])[0] or ""
                kb_ids = sorted(
                    [k for k in cluster["kb_counts"] if k],
                    key=lambda k: -cluster["kb_counts"][k],
                )[:10]
                reasons = sorted(
                    [k for k, v in cluster["reason_counts"].items() if v > 0],
                    key=lambda k: -cluster["reason_counts"][k],
                )
                existing = conn.execute(
                    "SELECT id, first_seen_at, last_seen_at FROM knowledge_gaps WHERE cluster_key = ?",
                    (cluster["cluster_key"],),
                ).fetchone()
                if existing:
                    first_seen = existing[1] or cluster["first_seen_at"]
                    last_seen = existing[2] or cluster["last_seen_at"]
                    if cluster["first_seen_at"] and (not first_seen or cluster["first_seen_at"] < first_seen):
                        first_seen = cluster["first_seen_at"]
                    if cluster["last_seen_at"] and (not last_seen or cluster["last_seen_at"] > last_seen):
                        last_seen = cluster["last_seen_at"]
                    conn.execute("""
                        UPDATE knowledge_gaps SET
                            canonical_question = ?, profile = ?, knowledge_base_id = ?,
                            knowledge_base_ids = ?, reason_tags = ?,
                            occurrence_count = ?, low_rating_count = ?, refresh_count = ?,
                            correction_count = ?, no_source_count = ?,
                            related_queries = ?, related_message_ids = ?,
                            status = 'open', first_seen_at = ?, last_seen_at = ?,
                            tenant_id = ?, agent_id = ?, owner_user_id = ?, updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?
                    """, (canonical, profile, kb_id, json.dumps(kb_ids, ensure_ascii=False),
                          json.dumps(reasons, ensure_ascii=False), cluster["total"],
                          cluster["reason_counts"]["low_rating"], cluster["reason_counts"]["refresh"],
                          cluster["reason_counts"]["correction"], cluster["reason_counts"]["no_source"],
                          json.dumps(cluster["related_queries"], ensure_ascii=False),
                          json.dumps(cluster["related_message_ids"], ensure_ascii=False),
                          first_seen, last_seen, cluster["tenant_id"], cluster["agent_id"], cluster["owner_user_id"], existing[0]))
                    updated += 1
                else:
                    conn.execute("""
                        INSERT INTO knowledge_gaps (
                            cluster_key, canonical_question, profile, knowledge_base_id,
                            knowledge_base_ids, reason_tags, occurrence_count,
                            low_rating_count, refresh_count, correction_count, no_source_count,
                            related_queries, related_message_ids, status, first_seen_at, last_seen_at, tenant_id, agent_id, owner_user_id
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)
                    """, (cluster["cluster_key"], canonical, profile, kb_id,
                          json.dumps(kb_ids, ensure_ascii=False), json.dumps(reasons, ensure_ascii=False),
                          cluster["total"], cluster["reason_counts"]["low_rating"],
                          cluster["reason_counts"]["refresh"], cluster["reason_counts"]["correction"],
                          cluster["reason_counts"]["no_source"],
                          json.dumps(cluster["related_queries"], ensure_ascii=False),
                          json.dumps(cluster["related_message_ids"], ensure_ascii=False),
                          cluster["first_seen_at"], cluster["last_seen_at"], cluster["tenant_id"], cluster["agent_id"], cluster["owner_user_id"]))
                    created += 1
        return {"clusters": len(clusters), "created": created, "updated": updated}

    def get_knowledge_gaps(self, status: str = "open", profile: str = "",
                           knowledge_base_id: str = "", search: str = "",
                           limit: int = 50, tenant_id: str = "") -> dict:
        where, params = [], []
        if tenant_id:
            where.append("tenant_id = ?")
            params.append(str(tenant_id))
        if status and status != "all":
            where.append("status = ?")
            params.append(status)
        if profile:
            where.append("profile = ?")
            params.append(profile)
        if knowledge_base_id:
            where.append("knowledge_base_id = ?")
            params.append(knowledge_base_id)
        if search:
            where.append("canonical_question LIKE ?")
            params.append(f"%{search}%")
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        cols = ["id", "cluster_key", "canonical_question", "profile", "knowledge_base_id",
                "knowledge_base_ids", "reason_tags", "occurrence_count", "low_rating_count",
                "refresh_count", "correction_count", "no_source_count", "related_queries",
                "related_message_ids", "status", "note", "first_seen_at", "last_seen_at",
                "updated_at"]
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(f"""
                SELECT id, cluster_key, canonical_question, profile, knowledge_base_id,
                       knowledge_base_ids, reason_tags, occurrence_count, low_rating_count,
                       refresh_count, correction_count, no_source_count, related_queries,
                       related_message_ids, status, note, first_seen_at, last_seen_at, updated_at
                FROM knowledge_gaps{where_sql}
                ORDER BY occurrence_count DESC, last_seen_at DESC LIMIT ?
            """, tuple(params) + (int(limit),)).fetchall()
            summary_row = conn.execute("""
                SELECT COUNT(*), COALESCE(SUM(occurrence_count), 0),
                       COALESCE(SUM(low_rating_count), 0), COALESCE(SUM(refresh_count), 0),
                       COALESCE(SUM(correction_count), 0), COALESCE(SUM(no_source_count), 0)
                FROM knowledge_gaps WHERE status = 'open' AND (?='' OR tenant_id=?)
            """, (tenant_id, tenant_id)).fetchone()
            profile_rows = conn.execute("""
                SELECT profile, COUNT(*) FROM knowledge_gaps
                WHERE status = 'open' AND (?='' OR tenant_id=?) GROUP BY profile ORDER BY COUNT(*) DESC LIMIT 10
            """, (tenant_id, tenant_id)).fetchall()
        items = []
        for r in rows:
            item = dict(zip(cols, r))
            for key in ("knowledge_base_ids", "reason_tags", "related_queries", "related_message_ids"):
                try:
                    item[key] = json.loads(item.get(key) or "[]")
                except (TypeError, json.JSONDecodeError):
                    item[key] = []
            for key in ("first_seen_at", "last_seen_at", "updated_at"):
                item[key] = self._utc_to_local(item.get(key) or "")
            items.append(item)
        summary = {"open_count": 0, "occurrences": 0, "low_rating": 0,
                   "refresh": 0, "correction": 0, "no_source": 0}
        if summary_row:
            summary.update({
                "open_count": summary_row[0],
                "occurrences": summary_row[1],
                "low_rating": summary_row[2],
                "refresh": summary_row[3],
                "correction": summary_row[4],
                "no_source": summary_row[5],
            })
        return {
            "items": items,
            "summary": summary,
            "profiles": [{"profile": r[0], "count": r[1]} for r in profile_rows],
        }

    def update_knowledge_gap_status(self, gap_id: int, status: str, note: str = "", tenant_id: str = "") -> bool:
        if status not in ("open", "resolved", "dismissed"):
            return False
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE knowledge_gaps SET status = ?, note = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND (?='' OR tenant_id=?)",
                (status, str(note or "")[:1000], int(gap_id), tenant_id, tenant_id),
            )
        return cur.rowcount > 0

    def create_gap_supply_task(self, gap_id: int, title: str = "", description: str = "",
                               created_by: str = "", tenant_id: str = "", agent_id: str = "") -> Optional[dict]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(
                "SELECT canonical_question, profile, knowledge_base_id FROM knowledge_gaps WHERE id = ? AND (?='' OR tenant_id=?)",
                (int(gap_id), tenant_id, tenant_id),
            ).fetchone()
            if not row:
                return None
            task_id = "task-" + uuid.uuid4().hex[:16]
            title = (title or "").strip() or f"补充资料：{row[0][:60]}"
            conn.execute("""
                INSERT INTO gap_supply_tasks (id, gap_id, title, description, created_by, tenant_id, agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (task_id, int(gap_id), title[:200], str(description or "")[:2000], str(created_by or ""), tenant_id or "local-default", agent_id or ""))
        return self.get_gap_supply_task(task_id)

    def get_gap_supply_task(self, task_id: str, tenant_id: str = "") -> Optional[dict]:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""
                SELECT id, gap_id, title, description, status, created_by, created_at, updated_at
                FROM gap_supply_tasks WHERE id = ? AND (?='' OR tenant_id=?)
            """, (task_id, tenant_id, tenant_id)).fetchone()
        if not row:
            return None
        return {"id": row[0], "gap_id": row[1], "title": row[2], "description": row[3],
                "status": row[4], "created_by": row[5],
                "created_at": self._utc_to_local(row[6]), "updated_at": self._utc_to_local(row[7])}

    def list_gap_supply_tasks(self, gap_id: int | None = None, status: str = "",
                              limit: int = 50, tenant_id: str = "") -> list[dict]:
        where, params = [], []
        if gap_id is not None:
            where.append("gap_id = ?")
            params.append(int(gap_id))
        if status:
            where.append("status = ?")
            params.append(status)
        if tenant_id:
            where.append("tenant_id = ?")
            params.append(tenant_id)
        where_sql = (" WHERE " + " AND ".join(where)) if where else ""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(f"""
                SELECT id, gap_id, title, description, status, created_by, created_at, updated_at
                FROM gap_supply_tasks{where_sql}
                ORDER BY created_at DESC LIMIT ?
            """, tuple(params) + (int(limit),)).fetchall()
        return [{"id": r[0], "gap_id": r[1], "title": r[2], "description": r[3],
                 "status": r[4], "created_by": r[5],
                 "created_at": self._utc_to_local(r[6]), "updated_at": self._utc_to_local(r[7])}
                for r in rows]

    def update_gap_supply_task_status(self, task_id: str, status: str, tenant_id: str = "") -> bool:
        if status not in ("pending", "in_progress", "done", "cancelled"):
            return False
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE gap_supply_tasks SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND (?='' OR tenant_id=?)",
                (status, task_id, tenant_id, tenant_id),
            )
        return cur.rowcount > 0

    def save_pipeline_stats(self, task_id: str, total_files: int, success_count: int, fail_count: int,
                            dedup_l1: int = 0, dedup_l2: int = 0, dedup_l3: int = 0,
                            faiss_after: int = 0, chroma_after: int = 0):
        """保存一次管道处理统计"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO pipeline_stats (task_id, total_files, success_count, fail_count,
                    dedup_l1, dedup_l2, dedup_l3, faiss_after, chroma_after)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (task_id, total_files, success_count, fail_count,
                  dedup_l1, dedup_l2, dedup_l3, faiss_after, chroma_after))

    def get_pipeline_stats(self, limit: int = 30) -> list[dict]:
        """获取最近 pipeline 处理统计，按时间倒序"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, task_id, total_files, success_count, fail_count,
                       dedup_l1, dedup_l2, dedup_l3, faiss_after, chroma_after, created_at
                FROM pipeline_stats ORDER BY id DESC LIMIT ?
            """, (limit,)).fetchall()
        items = [
            {"id": r[0], "task_id": r[1], "total_files": r[2],
             "success_count": r[3], "fail_count": r[4],
             "dedup_l1": r[5], "dedup_l2": r[6], "dedup_l3": r[7],
             "faiss_after": r[8], "chroma_after": r[9],
             "created_at": r[10]}
            for r in rows
        ]
        for i in items:
            i["created_at"] = self._utc_to_local(i.get("created_at", ""))
        return items

    @staticmethod
    def _utc_to_local(utc_str: str) -> str:
        """将 SQLite UTC 时间转为北京时间 (UTC+8)"""
        if not utc_str:
            return utc_str
        try:
            dt = datetime.strptime(utc_str, "%Y-%m-%d %H:%M:%S")
            dt = dt.replace(tzinfo=timezone.utc)
            local = dt.astimezone(timezone(timedelta(hours=8)))
            return local.strftime("%Y-%m-%d %H:%M:%S")
        except (ValueError, TypeError):
            return utc_str


    def save_misclassification_candidates(self, query: str, candidates: list[dict]) -> int:
        """Store suspected misclassified docs from a failed retrieval eval query."""
        with sqlite3.connect(self._db_path) as conn:
            saved = 0
            for c in (candidates or []):
                file_name = str(c.get("file_name") or "").strip()
                if not file_name:
                    continue
                try:
                    conn.execute(
                        """INSERT OR IGNORE INTO misclassification_candidates
                           (query, file_name, current_profile, suggested_profile, reason)
                           VALUES (?, ?, ?, ?, ?)""",
                        (query, file_name,
                         str(c.get("current_profile") or ""),
                         str(c.get("suggested_profile") or ""),
                         str(c.get("reason") or "")),
                    )
                    saved += 1
                except sqlite3.IntegrityError:
                    pass
            conn.commit()
        return saved

    def get_misclassification_candidates(self, include_resolved: bool = False) -> list[dict]:
        """Return pending misclassification candidates for admin review."""
        where = "" if include_resolved else "WHERE resolved = 0"
        with sqlite3.connect(self._db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"SELECT * FROM misclassification_candidates {where} ORDER BY detected_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_misclassification(self, candidate_id: int, resolved_profile: str) -> bool:
        """Mark a candidate as reviewed and record the corrected profile."""
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE misclassification_candidates SET resolved = 1, resolved_profile = ? WHERE id = ?",
                (resolved_profile, candidate_id),
            )
            conn.commit()
        return cur.rowcount > 0

    def save_retrieval_eval(self, query: str, expected_source: str,
                            recall_5: int, recall_10: int, mrr: float,
                            faiss_count: int, chroma_count: int, rerank_top1_match: int,
                            profile: str = "general", context: dict | None = None):
        """保存一次检索质量评估结果"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO retrieval_eval (query, expected_source, profile, context_json, recall_5, recall_10, mrr,
                    faiss_count, chroma_count, rerank_top1_match)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (query, expected_source, profile, json.dumps(context or {}, ensure_ascii=False), recall_5, recall_10, mrr,
                  faiss_count, chroma_count, rerank_top1_match))

    def get_retrieval_eval(self, limit: int = 100) -> dict:
        """获取检索质量评估的汇总统计"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """SELECT id, query, expected_source, profile, context_json, recall_5, recall_10, mrr,
                          faiss_count, chroma_count, rerank_top1_match, eval_at
                   FROM retrieval_eval ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        cols = ["id", "query", "expected_source", "profile", "context_json", "recall_5", "recall_10", "mrr",
                "faiss_count", "chroma_count", "rerank_top1_match", "eval_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for i in items:
            try:
                i["context"] = json.loads(i.pop("context_json") or "{}")
            except (TypeError, ValueError):
                i["context"] = {}
            i["eval_at"] = self._utc_to_local(i.get("eval_at", ""))
        total = len(items)
        if total == 0:
            return {"items": [], "summary": {"count": 0}}
        avg_recall_5 = sum(i["recall_5"] for i in items) / total
        avg_recall_10 = sum(i["recall_10"] for i in items) / total
        avg_mrr = sum(i["mrr"] for i in items) / total
        profile_counts = {}
        for item in items:
            profile = item.get("profile") or "general"
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
        return {
            "items": items,
            "summary": {
                "count": total,
                "avg_recall_5": round(avg_recall_5, 3),
                "avg_recall_10": round(avg_recall_10, 3),
                "avg_mrr": round(avg_mrr, 3),
                "profile_counts": profile_counts,
            }
        }

    def save_eval_comparison(self, query: str, expected_source: str,
                             faiss_only_recall_5: int, faiss_only_mrr: float,
                             bm25_only_recall_5: int, bm25_only_mrr: float,
                             hybrid_no_rerank_recall_5: int, hybrid_no_rerank_mrr: float,
                             hybrid_rerank_recall_5: int, hybrid_rerank_mrr: float,
                             profile: str = "general", context: dict | None = None):
        """保存一次检索模式对比结果"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO eval_comparison
                    (query, expected_source, profile, context_json,
                     faiss_only_recall_5, faiss_only_mrr,
                     bm25_only_recall_5, bm25_only_mrr,
                     hybrid_no_rerank_recall_5, hybrid_no_rerank_mrr,
                     hybrid_rerank_recall_5, hybrid_rerank_mrr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (query, expected_source, profile, json.dumps(context or {}, ensure_ascii=False),
                  faiss_only_recall_5, faiss_only_mrr,
                  bm25_only_recall_5, bm25_only_mrr,
                  hybrid_no_rerank_recall_5, hybrid_no_rerank_mrr,
                  hybrid_rerank_recall_5, hybrid_rerank_mrr))

    def get_eval_comparison(self, limit: int = 100) -> dict:
        """获取检索模式对比的汇总统计"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                """SELECT id, query, expected_source, profile, context_json,
                          faiss_only_recall_5, faiss_only_mrr,
                          bm25_only_recall_5, bm25_only_mrr,
                          hybrid_no_rerank_recall_5, hybrid_no_rerank_mrr,
                          hybrid_rerank_recall_5, hybrid_rerank_mrr, eval_at
                   FROM eval_comparison ORDER BY id DESC LIMIT ?""", (limit,)).fetchall()
        cols = ["id", "query", "expected_source", "profile", "context_json",
                "faiss_only_recall_5", "faiss_only_mrr",
                "bm25_only_recall_5", "bm25_only_mrr",
                "hybrid_no_rerank_recall_5", "hybrid_no_rerank_mrr",
                "hybrid_rerank_recall_5", "hybrid_rerank_mrr",
                "eval_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for item in items:
            try:
                item["context"] = json.loads(item.pop("context_json") or "{}")
            except (TypeError, ValueError):
                item["context"] = {}
        for i in items:
            i["eval_at"] = self._utc_to_local(i.get("eval_at", ""))
        total = len(items)
        if total == 0:
            return {"items": [], "summary": {"count": 0}}

        def _avg(key): return round(sum(i[key] for i in items) / total, 3)
        profile_counts = {}
        for item in items:
            profile = item.get("profile") or "general"
            profile_counts[profile] = profile_counts.get(profile, 0) + 1
        return {
            "items": items,
            "summary": {
                "count": total,
                "faiss_only_recall_5": _avg("faiss_only_recall_5"),
                "faiss_only_mrr": _avg("faiss_only_mrr"),
                "bm25_only_recall_5": _avg("bm25_only_recall_5"),
                "bm25_only_mrr": _avg("bm25_only_mrr"),
                "hybrid_no_rerank_recall_5": _avg("hybrid_no_rerank_recall_5"),
                "hybrid_no_rerank_mrr": _avg("hybrid_no_rerank_mrr"),
                "hybrid_rerank_recall_5": _avg("hybrid_rerank_recall_5"),
                "hybrid_rerank_mrr": _avg("hybrid_rerank_mrr"),
                # 增益计算
                "hybrid_gain_recall_5": round(_avg("hybrid_no_rerank_recall_5") - _avg("faiss_only_recall_5"), 3),
                "rerank_gain_recall_5": round(_avg("hybrid_rerank_recall_5") - _avg("hybrid_no_rerank_recall_5"), 3),
                "hybrid_gain_mrr": round(_avg("hybrid_no_rerank_mrr") - _avg("faiss_only_mrr"), 3),
                "rerank_gain_mrr": round(_avg("hybrid_rerank_mrr") - _avg("hybrid_no_rerank_mrr"), 3),
                "profile_counts": profile_counts,
            }
        }

    # ====== Retrieval Eval 测试集管理 (CRUD) ======

    def get_retrieval_eval_items(self, include_inactive: bool = False) -> list:
        with sqlite3.connect(self._db_path) as conn:
            if include_inactive:
                rows = conn.execute(
                    """SELECT id, query, expected, profile, category, difficulty, is_active,
                              source_feedback_id, label_status, failure_class, created_at
                       FROM retrieval_eval_items ORDER BY id DESC""").fetchall()
            else:
                rows = conn.execute(
                    """SELECT id, query, expected, profile, category, difficulty, is_active,
                              source_feedback_id, label_status, failure_class, created_at
                       FROM retrieval_eval_items WHERE is_active=1 ORDER BY id DESC""").fetchall()
        cols = ["id", "query", "expected", "profile", "category", "difficulty", "is_active",
                "source_feedback_id", "label_status", "failure_class", "created_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for i in items:
            i["created_at"] = self._utc_to_local(i.get("created_at", ""))
        return items

    def add_retrieval_eval_item(self, query: str, expected,
                                category: str = "", difficulty: str = "medium",
                                profile: str = "general") -> int:
        from retrieval_eval_contract import serialize_retrieval_expectation
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "INSERT INTO retrieval_eval_items (query, expected, profile, category, difficulty) VALUES (?, ?, ?, ?, ?)",
                (query, serialize_retrieval_expectation(expected), profile, category, difficulty)
            )
            return cur.lastrowid

    def update_retrieval_eval_item(self, item_id: int, query: str, expected,
                                   category: str, difficulty: str, is_active: int,
                                   profile: str = "general") -> bool:
        from retrieval_eval_contract import serialize_retrieval_expectation
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE retrieval_eval_items SET query=?, expected=?, profile=?, category=?, difficulty=?, is_active=? WHERE id=?",
                (query, serialize_retrieval_expectation(expected), profile, category, difficulty, is_active, item_id)
            )
            return cur.rowcount > 0

    def delete_retrieval_eval_item(self, item_id: int) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("DELETE FROM retrieval_eval_items WHERE id=?", (item_id,))
            return cur.rowcount > 0

    def clear_retrieval_eval_items(self) -> int:
        """清空所有测试用例，返回删除条数"""
        with sqlite3.connect(self._db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM retrieval_eval_items").fetchone()[0]
            conn.execute("DELETE FROM retrieval_eval_items")
            return count

    def batch_import_retrieval_eval_items(self, items: list) -> int:
        """批量导入测试用例，兼容旧四列元组和新版带 profile 的字典。"""
        from retrieval_eval_contract import serialize_retrieval_expectation
        count = 0
        with sqlite3.connect(self._db_path) as conn:
            for row in items:
                try:
                    if isinstance(row, dict):
                        query = row.get("query", "")
                        expected = row.get("expected", "")
                        profile = row.get("profile", "general")
                        category = row.get("category", "")
                        difficulty = row.get("difficulty", "medium")
                    else:
                        query = row[0]
                        expected = row[1]
                        category = row[2] if len(row) > 2 else ""
                        difficulty = row[3] if len(row) > 3 else "medium"
                        profile = row[4] if len(row) > 4 else "general"
                    conn.execute(
                        "INSERT INTO retrieval_eval_items (query, expected, profile, category, difficulty) VALUES (?, ?, ?, ?, ?)",
                        (query, serialize_retrieval_expectation(expected), profile, category, difficulty)
                    )
                    count += 1
                except Exception:
                    continue
        return count

    # ====== E2E Eval 综合质量评测测试集管理 (CRUD) ======

    def get_e2e_eval_items(self, include_inactive: bool = False) -> list:
        with sqlite3.connect(self._db_path) as conn:
            if include_inactive:
                rows = conn.execute(
                    "SELECT id, query, domain, profile, difficulty, style, is_active, created_at "
                    "FROM e2e_eval_items ORDER BY id ASC").fetchall()
            else:
                rows = conn.execute(
                    "SELECT id, query, domain, profile, difficulty, style, is_active, created_at "
                    "FROM e2e_eval_items WHERE is_active=1 ORDER BY id ASC").fetchall()
        cols = ["id", "query", "domain", "profile", "difficulty", "style", "is_active", "created_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for i in items:
            i["created_at"] = self._utc_to_local(i.get("created_at", ""))
        return items

    def add_e2e_eval_item(self, query: str, domain: str = "",
                          difficulty: str = "中等", style: str = "plain",
                          profile: str = "general") -> int:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "INSERT INTO e2e_eval_items (query, domain, profile, difficulty, style) VALUES (?, ?, ?, ?, ?)",
                (query, domain, profile, difficulty, style)
            )
            return cur.lastrowid

    def update_e2e_eval_item(self, item_id: int, query: str, domain: str,
                             difficulty: str, style: str, is_active: int,
                             profile: str = "general") -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE e2e_eval_items SET query=?, domain=?, profile=?, difficulty=?, style=?, is_active=? WHERE id=?",
                (query, domain, profile, difficulty, style, is_active, item_id)
            )
            return cur.rowcount > 0

    def delete_e2e_eval_item(self, item_id: int) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("DELETE FROM e2e_eval_items WHERE id=?", (item_id,))
            return cur.rowcount > 0

    def clear_e2e_eval_items(self) -> int:
        with sqlite3.connect(self._db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM e2e_eval_items").fetchone()[0]
            conn.execute("DELETE FROM e2e_eval_items")
            return count

    def batch_import_e2e_eval_items(self, items: list) -> int:
        """批量导入，兼容旧四列元组和新版带 profile 的字典。"""
        count = 0
        with sqlite3.connect(self._db_path) as conn:
            for row in items:
                try:
                    if isinstance(row, dict):
                        query = row.get("query", "")
                        domain = row.get("domain", "")
                        profile = row.get("profile", "general")
                        difficulty = row.get("difficulty", "中等")
                        style = row.get("style", "plain")
                    else:
                        query = row[0]
                        domain = row[1] if len(row) > 1 else ""
                        difficulty = row[2] if len(row) > 2 else "中等"
                        style = row[3] if len(row) > 3 else "plain"
                        profile = row[4] if len(row) > 4 else "general"
                    conn.execute(
                        "INSERT INTO e2e_eval_items (query, domain, profile, difficulty, style) VALUES (?, ?, ?, ?, ?)",
                        (query, domain, profile, difficulty, style)
                    )
                    count += 1
                except Exception:
                    continue
        return count

    # ==================== P6-G Reflection rules ====================

    def ensure_prompt_assets(self, defaults: list[dict]) -> None:
        """Seed known slots without overwriting administrator-owned prompt versions."""
        with sqlite3.connect(self._db_path) as conn:
            for item in defaults:
                slot = str(item.get("slot") or "").strip()
                if not slot:
                    continue
                conn.execute("""INSERT OR IGNORE INTO prompt_assets (slot, name, description, model_role)
                    VALUES (?, ?, ?, ?)""", (slot, str(item.get("name") or slot),
                                                str(item.get("description") or ""), str(item.get("model_role") or "")))
                exists = conn.execute("SELECT 1 FROM prompt_asset_versions WHERE slot=? LIMIT 1", (slot,)).fetchone()
                if not exists and item.get("template"):
                    conn.execute("""INSERT INTO prompt_asset_versions
                        (id, slot, version, template, variables_json, status, change_note, changed_by)
                        VALUES (?, ?, 1, ?, ?, 'published', '系统默认迁移', 'system')""", (
                            "pav-" + uuid.uuid4().hex[:16], slot, str(item["template"]),
                        json.dumps(item.get("variables") or [], ensure_ascii=False),
                    ))
                elif item.get("template"):
                    current = conn.execute("""SELECT version, template, changed_by
                        FROM prompt_asset_versions WHERE slot=? AND status='published'
                        ORDER BY version DESC LIMIT 1""", (slot,)).fetchone()
                    # System-seeded prompts may receive code-level safety updates; never
                    # overwrite an administrator-authored version in place.
                    if (current and current[2] == "system" and
                            str(current[1]) != str(item["template"])):
                        conn.execute("""UPDATE prompt_asset_versions SET status='archived'
                                      WHERE slot=? AND version=?""", (slot, current[0]))
                        conn.execute("""INSERT INTO prompt_asset_versions
                            (id, slot, version, template, variables_json, status, change_note, changed_by)
                            VALUES (?, ?, ?, ?, ?, 'published', '系统默认 Prompt 安全更新', 'system')""", (
                                "pav-" + uuid.uuid4().hex[:16], slot, int(current[0]) + 1,
                                str(item["template"]), json.dumps(item.get("variables") or [], ensure_ascii=False),
                            ))

    def list_prompt_assets(self) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""SELECT a.slot, a.name, a.description, a.model_role, a.enabled,
                v.version, v.status, v.created_at FROM prompt_assets a
                LEFT JOIN (SELECT slot, MAX(version) AS max_version FROM prompt_asset_versions
                           WHERE status='published' GROUP BY slot) latest ON latest.slot=a.slot
                LEFT JOIN prompt_asset_versions v ON v.slot=latest.slot AND v.version=latest.max_version
                ORDER BY a.slot""").fetchall()
        return [{"slot": r[0], "name": r[1], "description": r[2], "model_role": r[3],
                 "enabled": bool(r[4]), "active_version": r[5], "status": r[6] or "unmigrated",
                 "updated_at": r[7]} for r in rows]

    def get_prompt_asset_governance_summary(self) -> dict:
        items = self.list_prompt_assets()
        critical = {"reflection", "query_rewrite", "self_verify", "jailbreak_detect", "semantic_scoring",
                    "tool_router", "skill_call_planner", "mcp_call_planner",
                    "tool_result_summarizer", "tool_failure_fallback",
                    "generation_evidence_search"}
        migrated = [item for item in items if item.get("active_version")]
        deterministic = [item for item in items if item.get("model_role") == "deterministic"]
        unmigrated = [item for item in items if item.get("status") == "unmigrated" and item.get("model_role") != "deterministic"]
        missing_critical = sorted(slot for slot in critical if not any(item.get("slot") == slot and item.get("active_version") for item in items))
        return {"total": len(items), "migrated": len(migrated), "deterministic": len(deterministic),
                "unmigrated": len(unmigrated), "missing_critical": missing_critical,
                "ready_for_prompt_governance": not missing_critical,
                "items": items}

    def get_active_prompt_asset(self, slot: str, fallback: str = "") -> dict:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT version, template, variables_json, changed_by, created_at
                FROM prompt_asset_versions WHERE slot=? AND status='published'
                ORDER BY version DESC LIMIT 1""", (slot,)).fetchone()
        if not row:
            return {"slot": slot, "version": 0, "template": fallback, "variables": [], "source": "fallback"}
        try: variables = json.loads(row[2] or "[]")
        except (TypeError, ValueError): variables = []
        return {"slot": slot, "version": row[0], "template": row[1], "variables": variables,
                "changed_by": row[3], "created_at": row[4], "source": "asset"}

    def list_prompt_asset_versions(self, slot: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""SELECT version, template, variables_json, status, change_note,
                changed_by, created_at FROM prompt_asset_versions WHERE slot=? ORDER BY version DESC""", (slot,)).fetchall()
        items = []
        for row in rows:
            try: variables = json.loads(row[2] or "[]")
            except (TypeError, ValueError): variables = []
            items.append({"slot": slot, "version": row[0], "template": row[1], "variables": variables,
                          "status": row[3], "change_note": row[4], "changed_by": row[5], "created_at": row[6]})
        return items

    def save_prompt_asset_version(self, slot: str, template: str, variables: list | None = None,
                                  status: str = "draft", change_note: str = "", changed_by: str = "admin") -> dict:
        slot, template = str(slot).strip(), str(template).strip()
        if not slot or not template: raise ValueError("Prompt 槽位和模板不能为空")
        if status not in {"draft", "published", "archived"}: raise ValueError("Prompt 状态不合法")
        if slot in {"reflection"} and status == "published":
            raise ValueError("安全关键 Prompt 必须先保存草稿并通过黄金回归测试，再执行发布")
        with sqlite3.connect(self._db_path) as conn:
            asset = conn.execute("SELECT 1 FROM prompt_assets WHERE slot=?", (slot,)).fetchone()
            if not asset: raise ValueError("Prompt 槽位不存在")
            next_version = conn.execute("SELECT COALESCE(MAX(version), 0)+1 FROM prompt_asset_versions WHERE slot=?", (slot,)).fetchone()[0]
            if status == "published": conn.execute("UPDATE prompt_asset_versions SET status='archived' WHERE slot=? AND status='published'", (slot,))
            conn.execute("""INSERT INTO prompt_asset_versions (id, slot, version, template, variables_json, status, change_note, changed_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", ("pav-" + uuid.uuid4().hex[:16], slot, next_version, template,
                json.dumps(variables or [], ensure_ascii=False), status, str(change_note)[:500], str(changed_by)[:120]))
        item = next(item for item in self.list_prompt_asset_versions(slot) if item["version"] == next_version)
        self.log_audit("platform", changed_by, "", "prompt.asset.save", "prompt_asset", slot,
                       {"version": next_version, "status": status, "change_note": change_note[:200]})
        return item

    def restore_prompt_asset_version(self, slot: str, version: int, changed_by: str = "admin") -> dict:
        snapshot = next((item for item in self.list_prompt_asset_versions(slot) if item["version"] == int(version)), None)
        if not snapshot: raise ValueError("Prompt 历史版本不存在")
        source_report = self.get_latest_prompt_asset_test_report(slot, version)
        if slot == "reflection" and not source_report:
            raise ValueError("安全关键 Prompt 的历史版本没有回归证据，不能直接回滚发布")
        restored = self.save_prompt_asset_version(slot, snapshot["template"], snapshot["variables"], "draft",
                                                  f"恢复 v{version}", changed_by)
        if source_report:
            self.record_prompt_asset_test_run(slot, restored["version"], source_report, changed_by)
        return self.publish_prompt_asset_version(slot, restored["version"], changed_by)

    def record_prompt_asset_test_run(self, slot: str, version: int, report: dict, created_by: str = "admin",
                                     test_type: str = "contract") -> str:
        if test_type not in {"contract", "calibration"}:
            raise ValueError("Prompt 测试类型不合法")
        run_id = "pat-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO prompt_asset_test_runs
                (id, slot, version, test_type, report_json, created_by) VALUES (?, ?, ?, ?, ?, ?)""",
                         (run_id, slot, int(version), test_type, json.dumps(report, ensure_ascii=False), str(created_by)[:120]))
        self.log_audit("platform", created_by, "", "prompt.asset.test", "prompt_asset", slot,
                       {"run_id": run_id, "version": version, "passed": report.get("passed")})
        return run_id

    def get_latest_prompt_asset_test_report(self, slot: str, version: int, test_type: str = "contract") -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT report_json FROM prompt_asset_test_runs
                WHERE slot=? AND version=? AND test_type=? ORDER BY created_at DESC LIMIT 1""",
                               (slot, int(version), test_type)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row[0] or "{}")
        except (TypeError, ValueError):
            return None

    def publish_prompt_asset_version(self, slot: str, version: int, changed_by: str = "admin") -> dict:
        version = int(version)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("SELECT template FROM prompt_asset_versions WHERE slot=? AND version=?", (slot, version)).fetchone()
        if not row:
            raise ValueError("Prompt 版本不存在")
        # Every slot with a governed golden executor must prove the exact
        # candidate version before publication. Judge faithfulness additionally
        # requires human calibration below.
        if slot in {"reflection", "graph_relation_extraction", "memory_profile_proposal",
                    "memory_conflict", "judge_faithfulness", "judge_relevancy",
                    "judge_hallucination", "tool_router", "skill_call_planner",
                    "mcp_call_planner", "tool_result_summarizer",
                    "tool_failure_fallback", "query_rewrite", "jailbreak_detect",
                    "semantic_scoring", "generation_evidence_search"}:
            report = self.get_latest_prompt_asset_test_report(slot, version)
            if not report or not report.get("passed"):
                raise ValueError("关键 Prompt 必须先通过该版本的黄金回归测试后才能发布")
        if slot == "judge_faithfulness":
            calibration = self.get_latest_prompt_asset_test_report(slot, version, "calibration")
            if not calibration or not calibration.get("passed"):
                raise ValueError("Judge Prompt 必须先通过该版本的人审校准后才能发布")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("UPDATE prompt_asset_versions SET status='archived' WHERE slot=? AND status='published'", (slot,))
            conn.execute("UPDATE prompt_asset_versions SET status='published' WHERE slot=? AND version=?", (slot, version))
        item = next(item for item in self.list_prompt_asset_versions(slot) if item["version"] == version)
        self.log_audit("platform", changed_by, "", "prompt.asset.publish", "prompt_asset", slot,
                       {"version": version})
        return item

    def list_reflection_rules(self, status: str = "published") -> list[dict]:
        sql = "SELECT id, name, category, severity, rule_text, capability_modes_json, version, status, created_by, created_at, updated_at FROM reflection_rules"
        params: list[object] = []
        if status:
            sql += " WHERE status=?"; params.append(status)
        sql += " ORDER BY updated_at DESC, name"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            try: modes = json.loads(row[5] or "[]")
            except (TypeError, ValueError): modes = []
            items.append({"id": row[0], "name": row[1], "category": row[2], "severity": row[3],
                          "rule_text": row[4], "capability_modes": modes, "version": row[6], "status": row[7],
                          "created_by": row[8], "created_at": row[9], "updated_at": row[10]})
        return items

    def ensure_default_reflection_rules(self) -> None:
        """Seed review rules once; administrators can version and replace them later."""
        defaults = [
            {
                "id": "builtin-reflection-evidence",
                "name": "证据与来源边界",
                "category": "evidence",
                "severity": "high",
                "rule_text": "每个法规、标准、组织现状和定量结论都必须能在授权资料中核验；外部网页只能标记为待核验补充，不得伪装成权威依据。",
                "capability_modes": ["chat", "writing", "presentation"],
            },
            {
                "id": "builtin-reflection-scope",
                "name": "网络安全范围边界",
                "category": "scope",
                "severity": "high",
                "rule_text": "输出必须属于网络安全通用场景及用户明确的行业扩展范围，不得擅自编造行业、公司现状、资产数量、责任人或适用范围。",
                "capability_modes": ["chat", "writing", "presentation"],
            },
            {
                "id": "builtin-reflection-safety",
                "name": "安全与越权边界",
                "category": "safety",
                "severity": "critical",
                "rule_text": "不得提供未授权访问、绕过控制、攻击实施、密钥泄露或其他危险操作指导；不得声称绝对安全、绝对合规或保证结果。",
                "capability_modes": ["chat", "writing", "presentation"],
            },
        ]
        for item in defaults:
            with sqlite3.connect(self._db_path) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM reflection_rules WHERE id=?", (item["id"],)
                ).fetchone()
                if exists:
                    continue
                modes_json = json.dumps(item["capability_modes"], ensure_ascii=False)
                conn.execute(
                    """INSERT INTO reflection_rules
                        (id, name, category, severity, rule_text, capability_modes_json, status, created_by)
                        VALUES (?, ?, ?, ?, ?, ?, 'published', 'system')""",
                    (item["id"], item["name"], item["category"], item["severity"],
                     item["rule_text"], modes_json),
                )
                conn.execute(
                    """INSERT INTO reflection_rule_versions
                        (id, rule_id, version, name, category, severity, rule_text,
                         capability_modes_json, status, changed_by, change_type)
                        VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'published', 'system', 'seed')""",
                    ("rrv-" + uuid.uuid4().hex[:16], item["id"], item["name"],
                     item["category"], item["severity"], item["rule_text"], modes_json),
                )

    def save_reflection_rule(self, data: dict, rule_id: str = "") -> dict:
        name = str(data.get("name") or "").strip()[:120]
        rule_text = str(data.get("rule_text") or "").strip()[:4000]
        severity = str(data.get("severity") or "medium")
        status = str(data.get("status") or "draft")
        modes = data.get("capability_modes") if isinstance(data.get("capability_modes"), list) else ["chat"]
        if not name or not rule_text: raise ValueError("规则名称和规则内容不能为空")
        if severity not in {"low", "medium", "high", "critical"} or status not in {"draft", "published", "archived"}: raise ValueError("反思规则状态或严重级别不合法")
        with sqlite3.connect(self._db_path) as conn:
            if rule_id:
                row = conn.execute("SELECT version FROM reflection_rules WHERE id=?", (rule_id,)).fetchone()
                if not row: raise ValueError("反思规则不存在")
                conn.execute("""UPDATE reflection_rules SET name=?, category=?, severity=?, rule_text=?, capability_modes_json=?, version=?, status=?, created_by=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""", (name, str(data.get("category") or "general")[:80], severity, rule_text, json.dumps(modes, ensure_ascii=False), int(row[0]) + 1, status, str(data.get("created_by") or "admin")[:120], rule_id))
            else:
                rule_id = "rr-" + uuid.uuid4().hex[:16]
                conn.execute("""INSERT INTO reflection_rules (id, name, category, severity, rule_text, capability_modes_json, status, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", (rule_id, name, str(data.get("category") or "general")[:80], severity, rule_text, json.dumps(modes, ensure_ascii=False), status, str(data.get("created_by") or "admin")[:120]))
        item = next(item for item in self.list_reflection_rules("") if item["id"] == rule_id)
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT OR REPLACE INTO reflection_rule_versions
                (id, rule_id, version, name, category, severity, rule_text, capability_modes_json,
                 status, changed_by, change_type)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    "rrv-" + uuid.uuid4().hex[:16], item["id"], item["version"], item["name"],
                    item["category"], item["severity"], item["rule_text"],
                    json.dumps(item["capability_modes"], ensure_ascii=False), item["status"],
                    str(data.get("created_by") or "admin")[:120], str(data.get("change_type") or "save")[:40],
                ))
        self.log_audit(
            "platform", str(data.get("created_by") or "admin"), "",
            "reflection.rule.save", "reflection_rule", rule_id,
            {"name": item["name"], "status": item["status"], "version": item["version"],
             "severity": item["severity"], "modes": item["capability_modes"]},
        )
        return item

    def list_reflection_rule_versions(self, rule_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""SELECT version, name, category, severity, rule_text,
                capability_modes_json, status, changed_by, change_type, created_at
                FROM reflection_rule_versions WHERE rule_id=? ORDER BY version DESC""", (rule_id,)).fetchall()
        result = []
        for row in rows:
            try: modes = json.loads(row[5] or "[]")
            except (TypeError, ValueError): modes = []
            result.append({"version": row[0], "name": row[1], "category": row[2], "severity": row[3],
                           "rule_text": row[4], "capability_modes": modes, "status": row[6],
                           "changed_by": row[7], "change_type": row[8], "created_at": row[9]})
        return result

    def restore_reflection_rule_version(self, rule_id: str, version: int,
                                        changed_by: str = "admin") -> dict:
        versions = self.list_reflection_rule_versions(rule_id)
        snapshot = next((item for item in versions if item["version"] == int(version)), None)
        if not snapshot:
            raise ValueError("反思规则历史版本不存在")
        restored = self.save_reflection_rule({
            **snapshot, "created_by": changed_by, "change_type": f"restore_v{version}",
        }, rule_id)
        self.log_audit("platform", changed_by, "", "reflection.rule.restore", "reflection_rule", rule_id,
                       {"restored_from_version": version, "new_version": restored["version"]})
        return restored

    def record_reflection_run(self, tenant_id: str, user_id: str, agent_id: str, conversation_id: str, data: dict) -> str:
        run_id = "rfr-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO reflection_runs (id, tenant_id, user_id, agent_id, conversation_id, message_id, mode, model, rule_version, decision, input_summary, output_summary, revision_diff, rounds, duration_ms, error) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (run_id, tenant_id, user_id, agent_id, conversation_id, data.get("message_id"), str(data.get("mode") or "chat"), str(data.get("model") or ""), int(data.get("rule_version") or 0), str(data.get("decision") or "skipped"), str(data.get("input_summary") or "")[:1000], str(data.get("output_summary") or "")[:1000], str(data.get("revision_diff") or "")[:3000], int(data.get("rounds") or 0), int(data.get("duration_ms") or 0), str(data.get("error") or "")[:1000]))
        return run_id

    def list_reflection_runs(self, tenant_id: str = "", limit: int = 100) -> list[dict]:
        limit = max(1, min(int(limit), 500))
        sql = """SELECT id, tenant_id, user_id, agent_id, conversation_id, message_id, mode, model,
                 rule_version, decision, input_summary, output_summary, revision_diff, rounds,
                 duration_ms, error, created_at FROM reflection_runs"""
        params: list[object] = []
        if tenant_id:
            sql += " WHERE tenant_id=?"; params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        keys = ("id", "tenant_id", "user_id", "agent_id", "conversation_id", "message_id", "mode",
                "model", "rule_version", "decision", "input_summary", "output_summary",
                "revision_diff", "rounds", "duration_ms", "error", "created_at")
        return [dict(zip(keys, row)) for row in rows]

    def get_reflection_summary(self, tenant_id: str = "") -> dict:
        items = self.list_reflection_runs(tenant_id, 500)
        decisions = {key: 0 for key in ("pass", "revise", "block", "clarify", "degraded", "skipped")}
        for item in items:
            decision = str(item.get("decision") or "skipped").lower()
            decisions[decision] = decisions.get(decision, 0) + 1
        actionable = decisions.get("revise", 0) + decisions.get("block", 0) + decisions.get("clarify", 0)
        return {"total": len(items), "decisions": decisions, "actionable": actionable,
                "actionable_rate": round(actionable / len(items), 4) if items else 0,
                "recent": items[:20]}

    # ==================== P6-E3 Cost and model routing ====================

    @staticmethod
    def _trace_model(trace: dict | None) -> str:
        trace = trace or {}
        context = trace.get("context") or {}
        llm = context.get("llm") or {}
        model = ((llm.get("chat") or {}).get("model") or "")
        if model:
            return str(model)
        for step in trace.get("steps") or []:
            if step.get("step") == "llm_generation" and step.get("model"):
                return str(step["model"])
        return "unknown"

    def upsert_model_pricing(self, data: dict) -> dict:
        model = str(data.get("model") or "").strip()[:200]
        provider = str(data.get("provider") or "").strip()[:100]
        if not model:
            raise ValueError("模型名称不能为空")
        try:
            input_price = max(0.0, float(data.get("input_price_per_million", 0)))
            output_price = max(0.0, float(data.get("output_price_per_million", 0)))
            quality = max(0.0, min(1.0, float(data.get("quality_score", 0.5))))
        except (TypeError, ValueError) as exc:
            raise ValueError("模型价格和质量分必须是数字") from exc
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO model_pricing
                (id, provider, model, input_price_per_million, output_price_per_million,
                 quality_score, enabled, notes, updated_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, model) DO UPDATE SET
                input_price_per_million=excluded.input_price_per_million,
                output_price_per_million=excluded.output_price_per_million,
                quality_score=excluded.quality_score, enabled=excluded.enabled,
                notes=excluded.notes, updated_by=excluded.updated_by,
                updated_at=CURRENT_TIMESTAMP""", (
                    "price-" + uuid.uuid4().hex[:16], provider, model, input_price, output_price,
                    quality, int(bool(data.get("enabled", True))), str(data.get("notes") or "")[:500],
                    str(data.get("updated_by") or "admin")[:120],
                ))
            row = conn.execute("""SELECT id, provider, model, input_price_per_million,
                output_price_per_million, quality_score, enabled, notes, updated_by, updated_at
                FROM model_pricing WHERE provider=? AND model=?""", (provider, model)).fetchone()
        return dict(zip(("id", "provider", "model", "input_price_per_million",
                         "output_price_per_million", "quality_score", "enabled", "notes",
                         "updated_by", "updated_at"), row))

    def list_model_pricing(self, enabled_only: bool = False) -> list[dict]:
        sql = """SELECT id, provider, model, input_price_per_million, output_price_per_million,
            quality_score, enabled, notes, updated_by, updated_at FROM model_pricing"""
        if enabled_only:
            sql += " WHERE enabled=1"
        sql += " ORDER BY provider, model"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql).fetchall()
        return [dict(zip(("id", "provider", "model", "input_price_per_million",
                          "output_price_per_million", "quality_score", "enabled", "notes",
                          "updated_by", "updated_at"), row)) for row in rows]

    def update_model_pricing_status(self, pricing_id: str, enabled: bool) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("UPDATE model_pricing SET enabled=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                               (int(bool(enabled)), pricing_id))
        return cur.rowcount > 0

    def get_usage_cost_summary(self, tenant_id: str = "", limit: int = 100) -> dict:
        pricing = {(str(item["provider"]), str(item["model"])): item for item in self.list_model_pricing()}
        sql = """SELECT u.prompt_tokens, u.completion_tokens, u.trace_data, c.tenant_id,
            c.user_id, c.agent_id FROM usage_logs u JOIN conversations c ON c.id=u.conversation_id"""
        params: list[object] = []
        if tenant_id:
            sql += " WHERE c.tenant_id=?"; params.append(tenant_id)
        sql += " ORDER BY u.id DESC LIMIT ?"; params.append(max(1, min(int(limit), 10000)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        by_model: dict[str, dict] = {}
        by_user: dict[str, dict] = {}
        by_agent: dict[str, dict] = {}
        total = {"prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "requests": 0}
        for prompt_tokens, completion_tokens, trace_json, row_tenant, user_id, agent_id in rows:
            try: trace = json.loads(trace_json or "{}")
            except (TypeError, ValueError): trace = {}
            model = self._trace_model(trace)
            provider = str(((trace.get("context") or {}).get("llm") or {}).get("chat", {}).get("provider") or "")
            price = pricing.get((provider, model)) or next((item for (p, m), item in pricing.items() if m == model), None)
            input_price = float((price or {}).get("input_price_per_million", 0))
            output_price = float((price or {}).get("output_price_per_million", 0))
            cost = int(prompt_tokens or 0) / 1_000_000 * input_price + int(completion_tokens or 0) / 1_000_000 * output_price
            key = f"{provider}/{model}" if provider else model
            target = by_model.setdefault(key, {"model": model, "provider": provider, "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "requests": 0})
            for item in (target, by_user.setdefault(user_id or "unknown", {"id": user_id or "unknown", "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "requests": 0}), by_agent.setdefault(agent_id or "unknown", {"id": agent_id or "unknown", "prompt_tokens": 0, "completion_tokens": 0, "cost": 0.0, "requests": 0})):
                item["prompt_tokens"] += int(prompt_tokens or 0); item["completion_tokens"] += int(completion_tokens or 0); item["cost"] += cost; item["requests"] += 1
            total["prompt_tokens"] += int(prompt_tokens or 0); total["completion_tokens"] += int(completion_tokens or 0); total["cost"] += cost; total["requests"] += 1
        total["cost"] = round(total["cost"], 6)
        for group in (by_model, by_user, by_agent):
            for item in group.values(): item["cost"] = round(item["cost"], 6)
        return {"total": total, "by_model": list(by_model.values()), "by_user": list(by_user.values()), "by_agent": list(by_agent.values())}

    def get_usage_cost_analysis(self, tenant_id: str = "", limit: int = 10000) -> dict:
        """Analyze the authoritative usage-event ledger by model, module and day."""
        sql = """SELECT tenant_id, user_id, agent_id, module, provider, model,
                         prompt_tokens, completion_tokens, total_tokens,
                         COALESCE(estimated_cost, 0), pricing_status, created_at
                  FROM llm_usage_events"""
        params: list[object] = []
        if tenant_id:
            sql += " WHERE tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY created_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 50000)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        groups = {"by_model": {}, "by_module": {}, "by_user": {}, "by_agent": {}, "by_day": {}}
        for row in rows:
            tenant, user, agent, module, provider, model, prompt, completion, total_tokens, cost, pricing_status, created_at = row
            dimensions = {
                "by_model": f"{provider}/{model}" if provider else model,
                "by_module": module or "unknown",
                "by_user": user or "unknown",
                "by_agent": agent or "unknown",
                "by_day": str(created_at or "")[:10] or "unknown",
            }
            for group, key in dimensions.items():
                item = groups[group].setdefault(key, {"key": key, "prompt_tokens": 0,
                    "completion_tokens": 0, "total_tokens": 0, "cost": 0.0, "requests": 0,
                    "unpriced_requests": 0})
                item["prompt_tokens"] += int(prompt or 0)
                item["completion_tokens"] += int(completion or 0)
                item["total_tokens"] += int(total_tokens or 0)
                item["cost"] += float(cost or 0)
                item["requests"] += 1
                if pricing_status != "estimated_from_local_configuration":
                    item["unpriced_requests"] += 1
        return {group: [{**item, "cost": round(item["cost"], 6)} for item in values.values()]
                for group, values in groups.items()}

    def get_usage_cost_windows(self, tenant_id: str = "") -> dict:
        """Return estimated usage cost for the current and previous 24-hour windows."""
        windows = {
            "current": ("datetime('now', '-24 hours')", "datetime('now', '+1 second')"),
            "previous": ("datetime('now', '-48 hours')", "datetime('now', '-24 hours')"),
        }
        result = {}
        with sqlite3.connect(self._db_path) as conn:
            for name, (start, end) in windows.items():
                query = f"""SELECT COALESCE(SUM(estimated_cost), 0), COUNT(*),
                    COALESCE(SUM(total_tokens), 0)
                    FROM llm_usage_events
                    WHERE pricing_status='estimated_from_local_configuration'
                      AND created_at >= {start} AND created_at < {end}"""
                params: list[object] = []
                if tenant_id:
                    query = query.replace("WHERE pricing_status", "WHERE tenant_id=? AND pricing_status")
                    params.append(tenant_id)
                row = conn.execute(query, params).fetchone()
                result[name] = {"cost": round(float(row[0] or 0), 6), "requests": int(row[1] or 0), "total_tokens": int(row[2] or 0)}
        return result

    def recommend_model_route(self, mode: str = "chat", quality_floor: float = 0.0,
                              prompt_tokens: int = 0, completion_tokens: int = 0,
                              budget_limit: float | None = None, budget_used: float = 0.0,
                              locked_provider: str = "", locked_model: str = "") -> dict:
        """Return a cost-aware, explainable model route without executing a call."""
        try:
            quality_floor = max(0.0, min(1.0, float(quality_floor)))
            prompt_tokens = max(0, int(prompt_tokens or 0))
            completion_tokens = max(0, int(completion_tokens or 0))
            budget_used = max(0.0, float(budget_used or 0.0))
            budget_limit = None if budget_limit is None else max(0.0, float(budget_limit))
        except (TypeError, ValueError) as exc:
            raise ValueError("路由约束参数格式不正确") from exc

        all_items = self.list_model_pricing(True)
        candidates = [item for item in all_items if float(item["quality_score"]) >= quality_floor]
        if locked_model or locked_provider:
            candidates = [item for item in candidates
                          if (not locked_model or item["model"] == locked_model)
                          and (not locked_provider or item["provider"] == locked_provider)]
        estimated_total = prompt_tokens + completion_tokens

        def projected(item: dict) -> float:
            return (prompt_tokens / 1_000_000 * float(item["input_price_per_million"])
                    + completion_tokens / 1_000_000 * float(item["output_price_per_million"]))

        budget_filtered = []
        for item in candidates:
            item = dict(item)
            item["projected_cost"] = round(projected(item), 8)
            item["projected_budget_after"] = (round(budget_used + item["projected_cost"], 8)
                                                 if budget_limit is not None else None)
            if budget_limit is None or budget_used + item["projected_cost"] <= budget_limit:
                budget_filtered.append(item)
        candidates = budget_filtered
        if not candidates:
            reason = "没有满足质量、锁定模型和预算约束的可用模型"
            if locked_model or locked_provider:
                reason += "；请检查人工锁定模型是否启用且满足门槛"
            elif budget_limit is not None:
                reason += "；预计成本会超过预算上限"
            elif not all_items:
                reason = "没有已启用的模型价格配置"
            return {"mode": mode, "recommended": None, "candidates": [],
                    "decision": "blocked", "estimated_tokens": estimated_total,
                    "budget_limit": budget_limit, "budget_used": round(budget_used, 8),
                    "reason": reason, "fallback": "caller_must_ask_for_approval_or_use_configured_fallback"}

        ranked = sorted(candidates, key=lambda item: (
            float(item["quality_score"]) / max(float(item["input_price_per_million"])
                                                + float(item["output_price_per_million"]), 0.01),
            float(item["quality_score"]),
        ), reverse=True)
        selected = max(ranked, key=lambda item: float(item["quality_score"])) if mode in {"writing", "presentation", "complex_analysis"} else ranked[0]
        return {"mode": mode, "recommended": selected, "candidates": ranked[:10],
                "decision": "locked" if (locked_model or locked_provider) else "recommended",
                "estimated_tokens": estimated_total, "budget_limit": budget_limit,
                "budget_used": round(budget_used, 8),
                "reason": "已按质量门槛、预计 Token 成本、预算上限和人工锁定条件排序；复杂生成优先质量分。"}

    # ==================== P6-E2 Knowledge graph ====================

    @staticmethod
    def _graph_json(value, fallback):
        try:
            return json.loads(value or json.dumps(fallback))
        except (TypeError, ValueError):
            return fallback

    @staticmethod
    def _graph_name(value: str) -> str:
        return " ".join(str(value or "").strip().lower().split())

    def create_graph_entity(self, tenant_id: str, knowledge_base_id: str,
                            entity_type: str, name: str, properties: dict | None = None,
                            source_document_id: str = "", created_by: str = "",
                            status: str = "pending_review") -> dict:
        allowed = {"document", "clause", "standard", "industry", "penalty", "topic", "organization"}
        if entity_type not in allowed:
            raise ValueError("知识图谱实体类型不合法")
        if status not in {"pending_review", "approved", "rejected", "archived"}:
            raise ValueError("知识图谱审核状态不合法")
        name = str(name or "").strip()[:300]
        if not name:
            raise ValueError("实体名称不能为空")
        normalized = self._graph_name(name)
        kb = self.get_knowledge_base(knowledge_base_id, tenant_id) if knowledge_base_id else None
        if knowledge_base_id and not kb:
            raise ValueError("知识库不存在或不属于当前工作区")
        entity_id = "ge-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            existing = conn.execute("""SELECT id FROM knowledge_graph_entities
                WHERE tenant_id=? AND knowledge_base_id=? AND entity_type=? AND normalized_name=?""",
                (tenant_id, knowledge_base_id, entity_type, normalized)).fetchone()
            if existing:
                conn.execute("""UPDATE knowledge_graph_entities SET properties_json=?,
                    source_document_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (json.dumps(properties or {}, ensure_ascii=False), source_document_id, existing[0]))
                entity_id = existing[0]
            else:
                conn.execute("""INSERT INTO knowledge_graph_entities
                    (id, tenant_id, knowledge_base_id, entity_type, name, normalized_name,
                     properties_json, status, source_document_id, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (entity_id, tenant_id,
                    knowledge_base_id, entity_type, name, normalized,
                    json.dumps(properties or {}, ensure_ascii=False), status,
                    source_document_id, created_by))
        return self.get_graph_entity(entity_id, tenant_id) or {}

    def get_graph_entity(self, entity_id: str, tenant_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT id, tenant_id, knowledge_base_id, entity_type, name,
                properties_json, status, source_document_id, created_by, created_at, updated_at
                FROM knowledge_graph_entities WHERE id=? AND tenant_id=?""", (entity_id, tenant_id)).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "tenant_id", "knowledge_base_id", "entity_type", "name",
                         "properties_json", "status", "source_document_id", "created_by",
                         "created_at", "updated_at"), row))
        item["properties"] = self._graph_json(item.pop("properties_json"), {})
        return item

    def list_graph_entities(self, tenant_id: str, knowledge_base_id: str = "",
                            status: str = "", entity_type: str = "", limit: int = 500) -> list[dict]:
        sql = "SELECT id, tenant_id, knowledge_base_id, entity_type, name, properties_json, status, source_document_id, created_by, created_at, updated_at FROM knowledge_graph_entities WHERE tenant_id=?"
        params: list[object] = [tenant_id]
        if knowledge_base_id:
            sql += " AND knowledge_base_id=?"; params.append(knowledge_base_id)
        if status:
            sql += " AND status=?"; params.append(status)
        if entity_type:
            sql += " AND entity_type=?"; params.append(entity_type)
        sql += " ORDER BY updated_at DESC, name LIMIT ?"; params.append(max(1, min(int(limit), 2000)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = dict(zip(("id", "tenant_id", "knowledge_base_id", "entity_type", "name", "properties_json", "status", "source_document_id", "created_by", "created_at", "updated_at"), row))
            item["properties"] = self._graph_json(item.pop("properties_json"), {})
            items.append(item)
        return items

    def update_graph_entity_status(self, entity_id: str, tenant_id: str, status: str,
                                   changed_by: str = "admin") -> bool:
        if status not in {"pending_review", "approved", "rejected", "archived"}:
            raise ValueError("知识图谱审核状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""UPDATE knowledge_graph_entities SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=?""", (status, entity_id, tenant_id))
        if cur.rowcount:
            self.log_audit(tenant_id, changed_by, "", "graph.entity.status.update", "graph_entity", entity_id, {"status": status})
        return cur.rowcount > 0

    def create_graph_relation(self, tenant_id: str, knowledge_base_id: str, subject_id: str,
                              predicate: str, object_id: str, properties: dict | None = None,
                              source_document_id: str = "", confidence: float = 0.5,
                              created_by: str = "", status: str = "pending_review") -> dict:
        if status not in {"pending_review", "approved", "rejected", "archived"}:
            raise ValueError("知识图谱审核状态不合法")
        predicate = str(predicate or "").strip()[:120]
        if not predicate or not subject_id or not object_id or subject_id == object_id:
            raise ValueError("图谱关系字段不完整")
        confidence = max(0.0, min(float(confidence), 1.0))
        with sqlite3.connect(self._db_path) as conn:
            valid = conn.execute("""SELECT COUNT(*) FROM knowledge_graph_entities
                WHERE tenant_id=? AND id IN (?, ?)""", (tenant_id, subject_id, object_id)).fetchone()[0] == 2
            if not valid:
                raise ValueError("关系两端实体不存在或不属于当前工作区")
            relation_id = "gr-" + uuid.uuid4().hex[:16]
            existing = conn.execute("""SELECT id FROM knowledge_graph_relations
                WHERE tenant_id=? AND knowledge_base_id=? AND subject_id=? AND predicate=? AND object_id=?""",
                (tenant_id, knowledge_base_id, subject_id, predicate, object_id)).fetchone()
            if existing:
                relation_id = existing[0]
                conn.execute("""UPDATE knowledge_graph_relations SET properties_json=?, confidence=?,
                    source_document_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (json.dumps(properties or {}, ensure_ascii=False), confidence, source_document_id, relation_id))
            else:
                conn.execute("""INSERT INTO knowledge_graph_relations
                    (id, tenant_id, knowledge_base_id, subject_id, predicate, object_id,
                     properties_json, status, source_document_id, confidence, created_by)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (relation_id, tenant_id,
                    knowledge_base_id, subject_id, predicate, object_id,
                    json.dumps(properties or {}, ensure_ascii=False), status,
                    source_document_id, confidence, created_by))
        return self.get_graph_relation(relation_id, tenant_id) or {}

    def get_graph_relation(self, relation_id: str, tenant_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT r.id, r.tenant_id, r.knowledge_base_id, r.subject_id,
                s.name, r.predicate, r.object_id, o.name, r.properties_json, r.status,
                r.source_document_id, r.confidence, r.created_by, r.created_at, r.updated_at
                FROM knowledge_graph_relations r
                JOIN knowledge_graph_entities s ON s.id=r.subject_id
                JOIN knowledge_graph_entities o ON o.id=r.object_id
                WHERE r.id=? AND r.tenant_id=?""", (relation_id, tenant_id)).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "tenant_id", "knowledge_base_id", "subject_id", "subject_name",
                         "predicate", "object_id", "object_name", "properties_json", "status",
                         "source_document_id", "confidence", "created_by", "created_at", "updated_at"), row))
        item["properties"] = self._graph_json(item.pop("properties_json"), {})
        return item

    def list_graph_relations(self, tenant_id: str, knowledge_base_id: str = "",
                             status: str = "", entity_id: str = "", limit: int = 1000) -> list[dict]:
        sql = """SELECT r.id, r.tenant_id, r.knowledge_base_id, r.subject_id, s.name,
                   r.predicate, r.object_id, o.name, r.properties_json, r.status,
                   r.source_document_id, r.confidence, r.created_by, r.created_at, r.updated_at
                   FROM knowledge_graph_relations r
                   JOIN knowledge_graph_entities s ON s.id=r.subject_id AND s.tenant_id=r.tenant_id
                   JOIN knowledge_graph_entities o ON o.id=r.object_id AND o.tenant_id=r.tenant_id
                   WHERE r.tenant_id=?"""; params: list[object] = [tenant_id]
        if knowledge_base_id: sql += " AND r.knowledge_base_id=?"; params.append(knowledge_base_id)
        if status: sql += " AND r.status=?"; params.append(status)
        if entity_id: sql += " AND (r.subject_id=? OR r.object_id=?)"; params.extend([entity_id, entity_id])
        sql += " ORDER BY r.updated_at DESC LIMIT ?"; params.append(max(1, min(int(limit), 5000)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        items = []
        for row in rows:
            item = dict(zip(("id", "tenant_id", "knowledge_base_id", "subject_id", "subject_name", "predicate", "object_id", "object_name", "properties_json", "status", "source_document_id", "confidence", "created_by", "created_at", "updated_at"), row))
            item["properties"] = self._graph_json(item.pop("properties_json"), {})
            items.append(item)
        return items

    def update_graph_relation_status(self, relation_id: str, tenant_id: str, status: str,
                                     changed_by: str = "admin") -> bool:
        if status not in {"pending_review", "approved", "rejected", "archived"}:
            raise ValueError("知识图谱审核状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""UPDATE knowledge_graph_relations SET status=?, updated_at=CURRENT_TIMESTAMP
                WHERE id=? AND tenant_id=?""", (status, relation_id, tenant_id))
        if cur.rowcount:
            self.log_audit(tenant_id, changed_by, "", "graph.relation.status.update", "graph_relation", relation_id, {"status": status})
        return cur.rowcount > 0

    def graph_review_recommendations(self, tenant_id: str, knowledge_base_id: str = "",
                                     limit: int = 200) -> dict:
        """Classify pending graph candidates for human-in-the-loop review."""
        entities = self.list_graph_entities(tenant_id, knowledge_base_id, "pending_review", limit=2000)
        relations = self.list_graph_relations(tenant_id, knowledge_base_id, "pending_review", limit=5000)
        trusted = {"standard_identifier", "legal_reference", "clause_identifier",
                   "document_metadata", "penalty_sentence"}
        high = []
        medium = []
        noise = []
        for relation in relations:
            extraction = str((relation.get("properties") or {}).get("extraction") or "")
            confidence = float(relation.get("confidence") or 0)
            item = {"id": relation["id"], "subject_name": relation["subject_name"],
                    "predicate": relation["predicate"], "object_name": relation["object_name"],
                    "confidence": confidence, "extraction": extraction,
                    "source_document_id": relation.get("source_document_id", ""),
                    "reason": ""}
            if extraction == "same_document_cooccurrence" or confidence < 0.6:
                item["reason"] = "同文档共现或置信度较低，建议人工确认或拒绝"
                noise.append(item)
            elif confidence >= 0.9 and extraction in trusted:
                item["reason"] = "高置信度且来自明确字段抽取，可批量进入人工确认"
                high.append(item)
            else:
                item["reason"] = "中等置信度，建议抽样复核"
                medium.append(item)
        cap = max(1, min(int(limit), 500))
        return {"summary": {"entities_pending": len(entities), "relations_pending": len(relations),
                             "high_confidence": len(high), "medium_confidence": len(medium),
                             "noise_or_low_confidence": len(noise)},
                "high_confidence": high[:cap], "medium_confidence": medium[:cap],
                "noise_or_low_confidence": noise[:cap]}

    def batch_update_graph_status(self, tenant_id: str, knowledge_base_id: str,
                                  kind: str, ids: list[str], status: str,
                                  changed_by: str = "admin") -> dict:
        """Batch review selected candidates with tenant and endpoint checks."""
        if kind not in {"entity", "relation"}:
            raise ValueError("批量审核对象不合法")
        if status not in {"approved", "rejected", "archived", "pending_review"}:
            raise ValueError("知识图谱审核状态不合法")
        unique_ids = list(dict.fromkeys(str(item).strip() for item in ids if str(item).strip()))[:2000]
        if not unique_ids:
            return {"updated": 0, "skipped": 0, "skipped_ids": []}
        table = "knowledge_graph_entities" if kind == "entity" else "knowledge_graph_relations"
        updated = 0
        skipped_ids = []
        updated_ids = []
        with sqlite3.connect(self._db_path) as conn:
            for item_id in unique_ids:
                row = conn.execute(f"SELECT id FROM {table} WHERE id=? AND tenant_id=? AND knowledge_base_id=? AND status='pending_review'", (item_id, tenant_id, knowledge_base_id)).fetchone()
                if not row:
                    skipped_ids.append(item_id)
                    continue
                if kind == "relation" and status == "approved":
                    endpoints = conn.execute("SELECT COUNT(*) FROM knowledge_graph_entities e JOIN knowledge_graph_relations r ON (e.id=r.subject_id OR e.id=r.object_id) WHERE r.id=? AND e.tenant_id=? AND e.status != 'rejected'", (item_id, tenant_id)).fetchone()[0]
                    if endpoints != 2:
                        skipped_ids.append(item_id)
                        continue
                cur = conn.execute(f"UPDATE {table} SET status=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=? AND knowledge_base_id=? AND status='pending_review'", (status, item_id, tenant_id, knowledge_base_id))
                updated += cur.rowcount
                if cur.rowcount:
                    updated_ids.append(item_id)
        for item_id in updated_ids:
            self.log_audit(tenant_id, changed_by, "", f"graph.{kind}.batch_status.update", f"graph_{kind}", item_id, {"status": status})
        return {"updated": updated, "skipped": len(skipped_ids), "skipped_ids": skipped_ids}

    def create_graph_extraction_run(self, tenant_id: str, knowledge_base_id: str,
                                    document_id: str, created_by: str = "") -> dict:
        run_id = "gxr-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO knowledge_graph_extraction_runs
                (id, tenant_id, knowledge_base_id, document_id, created_by)
                VALUES (?, ?, ?, ?, ?)""", (run_id, tenant_id, knowledge_base_id, document_id, created_by))
        return self.get_graph_extraction_run(run_id, tenant_id) or {}

    def update_graph_extraction_run(self, run_id: str, tenant_id: str, status: str,
                                    entity_count: int = 0, relation_count: int = 0,
                                    error: str = "") -> bool:
        if status not in {"pending_review", "approved", "failed", "rejected"}:
            raise ValueError("图谱抽取任务状态不合法")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""UPDATE knowledge_graph_extraction_runs SET status=?, entity_count=?,
                relation_count=?, error=?, finished_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=?""",
                (status, int(entity_count), int(relation_count), str(error or "")[:1000], run_id, tenant_id))
        return cur.rowcount > 0

    def get_graph_extraction_run(self, run_id: str, tenant_id: str) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT id, tenant_id, knowledge_base_id, document_id, status,
                entity_count, relation_count, error, created_by, created_at, finished_at
                FROM knowledge_graph_extraction_runs WHERE id=? AND tenant_id=?""", (run_id, tenant_id)).fetchone()
        if not row: return None
        return dict(zip(("id", "tenant_id", "knowledge_base_id", "document_id", "status", "entity_count",
                         "relation_count", "error", "created_by", "created_at", "finished_at"), row))

    # ==================== P6-E1 Workflow orchestration ====================

    @staticmethod
    def _workflow_json(value, fallback):
        try:
            return json.loads(value or json.dumps(fallback))
        except (TypeError, ValueError):
            return fallback

    def list_workflow_templates(self, tenant_id: str = "", include_platform: bool = True,
                                viewer_id: str = "") -> list[dict]:
        """List templates visible to a tenant; platform templates are read-only."""
        tenant_id = str(tenant_id or "")
        sql = """SELECT id, template_key, name, description, nodes_json, edges_json,
                         visibility, tenant_id, status, source_template_key, version,
                         created_by, created_at, updated_at
                  FROM workflow_templates WHERE status='active'"""
        params: list[object] = []
        clauses = []
        if include_platform:
            clauses.append("visibility='platform'")
        if tenant_id:
            clauses.append("(tenant_id=? AND visibility='org')")
            params.append(tenant_id)
            if viewer_id:
                clauses.append("(tenant_id=? AND visibility='private' AND created_by=? )")
                params.extend([tenant_id, viewer_id])
        if clauses:
            sql += " AND (" + " OR ".join(clauses) + ")"
        else:
            sql += " AND 1=0"
        sql += " ORDER BY visibility='platform' DESC, updated_at DESC, name"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        keys = ("id", "template_key", "name", "description", "nodes_json", "edges_json",
                "visibility", "tenant_id", "status", "source_template_key", "version",
                "created_by", "created_at", "updated_at")
        items = []
        for row in rows:
            item = dict(zip(keys, row))
            item["nodes"] = self._workflow_json(item.pop("nodes_json"), [])
            item["edges"] = self._workflow_json(item.pop("edges_json"), [])
            item["created_at"] = _to_utc_iso(item["created_at"])
            item["updated_at"] = _to_utc_iso(item["updated_at"])
            items.append(item)
        return items

    def get_workflow_template(self, template_id: str, tenant_id: str = "", viewer_id: str = "") -> dict | None:
        return next((item for item in self.list_workflow_templates(tenant_id, viewer_id=viewer_id)
                     if item["id"] == str(template_id)), None)

    def create_workflow_template(self, tenant_id: str, template_key: str, name: str,
                                 description: str, nodes: list, edges: list,
                                 created_by: str = "", visibility: str = "private",
                                 source_template_key: str = "") -> dict:
        from workflow_engine import validate_workflow_graph
        validate_workflow_graph(nodes, edges)
        if visibility not in {"private", "org"}:
            raise ValueError("模板可见范围仅支持 private 或 org")
        if not tenant_id or not str(name or "").strip():
            raise ValueError("模板必须有归属工作区和名称")
        template_id = "wft-" + uuid.uuid4().hex[:16]
        key = str(template_key or "custom").strip()[:100] or "custom"
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO workflow_templates
                (id, template_key, name, description, nodes_json, edges_json, visibility,
                 tenant_id, source_template_key, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                template_id, key, str(name).strip()[:120], str(description or "")[:2000],
                json.dumps(nodes, ensure_ascii=False), json.dumps(edges, ensure_ascii=False),
                visibility, tenant_id, str(source_template_key or "")[:100], created_by,
            ))
        self.log_audit(tenant_id, created_by, "", "workflow.template.create", "workflow_template",
                       template_id, {"visibility": visibility, "source_template_key": source_template_key})
        return self.get_workflow_template(template_id, tenant_id, created_by) or {}

    def share_workflow_template(self, template_id: str, tenant_id: str, changed_by: str = "") -> dict:
        item = self.get_workflow_template(template_id, tenant_id, changed_by)
        if not item or item["visibility"] == "platform":
            raise ValueError("平台模板不可直接修改")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE workflow_templates SET visibility='org', updated_at=CURRENT_TIMESTAMP "
                "WHERE id=? AND tenant_id=?", (template_id, tenant_id)
            )
        self.log_audit(tenant_id, changed_by, "", "workflow.template.share", "workflow_template", template_id)
        return self.get_workflow_template(template_id, tenant_id, changed_by) or {}

    def copy_workflow_template(self, template_id: str, tenant_id: str, agent_id: str,
                               created_by: str = "", name: str = "", description: str = "") -> dict:
        item = self.get_workflow_template(template_id, tenant_id, created_by)
        if not item:
            raise ValueError("模板不存在或不属于当前工作区")
        workflow = self.create_workflow_definition(
            tenant_id, agent_id, name or item["name"], description or item["description"],
            item["nodes"], item["edges"], created_by,
            "从模板复制: " + item["template_key"],
        )
        self.log_audit(tenant_id, created_by, agent_id, "workflow.template.copy", "workflow",
                       workflow.get("id", ""), {"template_id": template_id})
        return workflow

    def create_workflow_definition(self, tenant_id: str, agent_id: str, name: str,
                                   description: str, nodes: list, edges: list,
                                   created_by: str = "", change_note: str = "") -> dict:
        from workflow_engine import validate_workflow_graph
        validate_workflow_graph(nodes, edges)
        if not self._agent_in_workspace(agent_id, tenant_id):
            raise ValueError("工作区或 Agent 不存在")
        name = str(name or "").strip()[:120]
        if not name:
            raise ValueError("工作流名称不能为空")
        workflow_id = "wf-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO workflow_definitions
                (id, tenant_id, agent_id, name, description, created_by)
                VALUES (?, ?, ?, ?, ?, ?)""", (workflow_id, tenant_id, agent_id, name,
                    str(description or "")[:2000], created_by))
            conn.execute("""INSERT INTO workflow_versions
                (id, workflow_id, version, nodes_json, edges_json, change_note, created_by)
                VALUES (?, ?, 1, ?, ?, ?, ?)""", ("wfv-" + uuid.uuid4().hex[:16], workflow_id,
                    json.dumps(nodes, ensure_ascii=False), json.dumps(edges, ensure_ascii=False),
                    str(change_note or "")[:1000], created_by))
        self.log_audit(tenant_id, created_by, agent_id, "workflow.create", "workflow", workflow_id,
                       {"name": name, "version": 1})
        return self.get_workflow_definition(workflow_id, tenant_id, agent_id) or {}

    def _agent_in_workspace(self, agent_id: str, tenant_id: str) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            return bool(conn.execute("SELECT 1 FROM agents WHERE id=? AND tenant_id=? AND status='active'",
                                     (agent_id, tenant_id)).fetchone())

    def get_workflow_definition(self, workflow_id: str, tenant_id: str,
                                agent_id: str = "") -> dict | None:
        sql = """SELECT id, tenant_id, agent_id, name, description, status, current_version,
                        published_version, created_by, created_at, updated_at
                 FROM workflow_definitions WHERE id=? AND tenant_id=?"""
        params: list[object] = [workflow_id, tenant_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "tenant_id", "agent_id", "name", "description", "status",
                         "current_version", "published_version", "created_by", "created_at", "updated_at"), row))
        item["created_at"] = _to_utc_iso(item["created_at"])
        item["updated_at"] = _to_utc_iso(item["updated_at"])
        item["version"] = self.get_workflow_version(workflow_id, int(item["current_version"]))
        return item

    def list_workflow_definitions(self, tenant_id: str, agent_id: str = "") -> list[dict]:
        sql = "SELECT id FROM workflow_definitions WHERE tenant_id=?"
        params: list[object] = [tenant_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        sql += " ORDER BY updated_at DESC, created_at DESC"
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self.get_workflow_definition(row[0], tenant_id, agent_id))]

    def get_workflow_version(self, workflow_id: str, version: int) -> dict | None:
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute("""SELECT id, workflow_id, version, nodes_json, edges_json,
                change_note, created_by, created_at FROM workflow_versions
                WHERE workflow_id=? AND version=?""", (workflow_id, int(version))).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "workflow_id", "version", "nodes_json", "edges_json",
                         "change_note", "created_by", "created_at"), row))
        item["nodes"] = self._workflow_json(item.pop("nodes_json"), [])
        item["edges"] = self._workflow_json(item.pop("edges_json"), [])
        item["created_at"] = _to_utc_iso(item["created_at"])
        return item

    def list_workflow_versions(self, workflow_id: str, tenant_id: str, agent_id: str = "") -> list[dict]:
        if not self.get_workflow_definition(workflow_id, tenant_id, agent_id):
            return []
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("SELECT version FROM workflow_versions WHERE workflow_id=? ORDER BY version DESC", (workflow_id,)).fetchall()
        return [item for row in rows if (item := self.get_workflow_version(workflow_id, row[0]))]

    def save_workflow_version(self, workflow_id: str, tenant_id: str, agent_id: str,
                              nodes: list, edges: list, created_by: str = "",
                              change_note: str = "") -> dict:
        from workflow_engine import validate_workflow_graph
        validate_workflow_graph(nodes, edges)
        workflow = self.get_workflow_definition(workflow_id, tenant_id, agent_id)
        if not workflow:
            raise ValueError("工作流不存在")
        version = int(workflow["current_version"]) + 1
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO workflow_versions
                (id, workflow_id, version, nodes_json, edges_json, change_note, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", ("wfv-" + uuid.uuid4().hex[:16], workflow_id, version,
                    json.dumps(nodes, ensure_ascii=False), json.dumps(edges, ensure_ascii=False),
                    str(change_note or "")[:1000], created_by))
            conn.execute("""UPDATE workflow_definitions SET current_version=?, status='draft',
                updated_at=CURRENT_TIMESTAMP WHERE id=?""", (version, workflow_id))
        self.log_audit(tenant_id, created_by, agent_id, "workflow.version.save", "workflow", workflow_id,
                       {"version": version})
        return self.get_workflow_definition(workflow_id, tenant_id, agent_id) or {}

    def publish_workflow_version(self, workflow_id: str, tenant_id: str, agent_id: str,
                                 version: int, published_by: str = "") -> bool:
        workflow = self.get_workflow_definition(workflow_id, tenant_id, agent_id)
        item = self.get_workflow_version(workflow_id, version)
        if not workflow or not item:
            return False
        from workflow_engine import validate_workflow_contract
        validate_workflow_contract(item["nodes"], item["edges"])
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""UPDATE workflow_definitions SET status='published', published_version=?,
                updated_at=CURRENT_TIMESTAMP WHERE id=? AND tenant_id=? AND agent_id=?""",
                         (int(version), workflow_id, tenant_id, agent_id))
        self.log_audit(tenant_id, published_by, agent_id, "workflow.publish", "workflow", workflow_id,
                       {"version": int(version)})
        return True

    def create_workflow_run(self, workflow_id: str, tenant_id: str, agent_id: str,
                            version: int, input_data: dict, created_by: str = "") -> dict:
        if not self.get_workflow_definition(workflow_id, tenant_id, agent_id):
            raise ValueError("工作流不存在")
        if not self.get_workflow_version(workflow_id, version):
            raise ValueError("工作流版本不存在")
        run_id = "wfr-" + uuid.uuid4().hex[:16]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT INTO workflow_runs
                (id, workflow_id, tenant_id, agent_id, version, input_json, created_by)
                VALUES (?, ?, ?, ?, ?, ?, ?)""", (run_id, workflow_id, tenant_id, agent_id, int(version),
                    json.dumps(input_data or {}, ensure_ascii=False), created_by))
        return self.get_workflow_run(run_id, tenant_id, agent_id) or {}

    def get_workflow_run(self, run_id: str, tenant_id: str, agent_id: str = "") -> dict | None:
        sql = """SELECT id, workflow_id, tenant_id, agent_id, version, status, input_json, output_json,
                created_by, error, started_at, finished_at FROM workflow_runs WHERE id=? AND tenant_id=?"""
        params: list[object] = [run_id, tenant_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        with sqlite3.connect(self._db_path) as conn:
            row = conn.execute(sql, params).fetchone()
        if not row:
            return None
        item = dict(zip(("id", "workflow_id", "tenant_id", "agent_id", "version", "status",
                         "input_json", "output_json", "created_by", "error", "started_at", "finished_at"), row))
        item["input"] = self._workflow_json(item.pop("input_json"), {})
        item["output"] = self._workflow_json(item.pop("output_json"), {})
        item["started_at"] = _to_utc_iso(item["started_at"])
        item["finished_at"] = _to_utc_iso(item["finished_at"])
        return item

    def update_workflow_run(self, run_id: str, tenant_id: str, agent_id: str,
                            status: str, output: dict, error: str = "") -> bool:
        if status not in {"running", "awaiting_approval", "completed", "failed", "cancelled"}:
            raise ValueError("工作流运行状态不合法")
        finished = "CURRENT_TIMESTAMP" if status in {"completed", "failed", "cancelled"} else "NULL"
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(f"""UPDATE workflow_runs SET status=?, output_json=?, error=?, finished_at={finished}
                WHERE id=? AND tenant_id=? AND agent_id=?""", (status, json.dumps(output or {}, ensure_ascii=False),
                    str(error or "")[:1000], run_id, tenant_id, agent_id))
        return cur.rowcount > 0

    def add_workflow_run_trace(self, run_id: str, node_id: str, node_type: str, sequence: int,
                               status: str, input_summary: str, output_summary: str,
                               duration_ms: int, error: str = "") -> None:
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""INSERT OR REPLACE INTO workflow_run_traces
                (run_id, node_id, node_type, sequence, status, input_summary, output_summary, duration_ms, error)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (run_id, node_id, node_type, int(sequence), status,
                    str(input_summary or "")[:1000], str(output_summary or "")[:2000], max(0, int(duration_ms or 0)),
                    str(error or "")[:1000]))

    def list_workflow_run_traces(self, run_id: str, tenant_id: str, agent_id: str = "") -> list[dict]:
        if not self.get_workflow_run(run_id, tenant_id, agent_id):
            return []
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""SELECT node_id, node_type, sequence, status, input_summary,
                output_summary, duration_ms, error, created_at FROM workflow_run_traces
                WHERE run_id=? ORDER BY sequence ASC""", (run_id,)).fetchall()
        return [dict(zip(("node_id", "node_type", "sequence", "status", "input_summary",
                          "output_summary", "duration_ms", "error", "created_at"), row)) for row in rows]

    def list_workflow_runs(self, tenant_id: str, agent_id: str = "", limit: int = 30) -> list[dict]:
        sql = "SELECT id FROM workflow_runs WHERE tenant_id=?"
        params: list[object] = [tenant_id]
        if agent_id:
            sql += " AND agent_id=?"
            params.append(agent_id)
        sql += " ORDER BY started_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 100)))
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(sql, params).fetchall()
        return [item for row in rows if (item := self.get_workflow_run(row[0], tenant_id, agent_id))]

    # ==================== Agent Evaluation ====================

    def upsert_agent_eval_case(self, case: dict, tenant_id: str = "local-default") -> int:
        """新增或更新一条通用 Agent Evaluation 用例。"""
        case_key = str(case.get("case_key") or case.get("id") or "").strip()
        if not case_key:
            raise ValueError("agent eval case_key is required")
        query = case.get("query", {})
        if isinstance(query, str):
            query = {"text": query}
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO agent_eval_cases
                    (case_key, profile, case_type, domain, query_json, expected_json, risk_tags_json, is_active, tenant_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(case_key) DO UPDATE SET
                    profile=excluded.profile, case_type=excluded.case_type, domain=excluded.domain,
                    query_json=excluded.query_json, expected_json=excluded.expected_json,
                    risk_tags_json=excluded.risk_tags_json, is_active=excluded.is_active,
                    updated_at=CURRENT_TIMESTAMP
            """, (
                case_key,
                case.get("profile") or "general",
                case.get("case_type") or "answer_quality",
                case.get("domain") or "",
                json.dumps(query, ensure_ascii=False),
                json.dumps(case.get("expected") or {}, ensure_ascii=False),
                json.dumps(case.get("risk_tags") or [], ensure_ascii=False),
                int(case.get("is_active", 1)), str(tenant_id or "local-default"),
            ))
            row = conn.execute(
                "SELECT id FROM agent_eval_cases WHERE case_key=?", (case_key,)
            ).fetchone()
        return int(row[0])

    def get_agent_eval_cases(self, profile: str | None = None,
                             include_inactive: bool = False,
                             tenant_id: str = "local-default") -> list[dict]:
        conditions = ["tenant_id=?"]
        params = [str(tenant_id or "local-default")]
        if profile:
            conditions.append("profile=?")
            params.append(profile)
        if not include_inactive:
            conditions.append("is_active=1")
        where = f" WHERE {' AND '.join(conditions)}" if conditions else ""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute(
                "SELECT id, case_key, profile, case_type, domain, query_json, expected_json, "
                "risk_tags_json, is_active, created_at, updated_at FROM agent_eval_cases" + where + " ORDER BY id ASC",
                params,
            ).fetchall()
        items = []
        for row in rows:
            item = dict(zip((
                "id", "case_key", "profile", "case_type", "domain", "query_json", "expected_json",
                "risk_tags_json", "is_active", "created_at", "updated_at",
            ), row))
            for field, fallback in (("query_json", {}), ("expected_json", {}), ("risk_tags_json", [])):
                try:
                    item[field.removesuffix("_json")] = json.loads(item.pop(field) or json.dumps(fallback))
                except (TypeError, ValueError):
                    item[field.removesuffix("_json")] = fallback
            item["created_at"] = self._utc_to_local(item.get("created_at", ""))
            item["updated_at"] = self._utc_to_local(item.get("updated_at", ""))
            items.append(item)
        return items

    def create_agent_eval_run(self, profile: str = "general", context: dict | None = None,
                              run_id: str | None = None, tenant_id: str = "local-default",
                              agent_id: str = "", requested_by: str = "") -> str:
        run_id = run_id or f"agent_eval_{uuid.uuid4().hex[:12]}"
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO agent_eval_runs (run_id, profile, context_json, tenant_id, agent_id, requested_by) VALUES (?, ?, ?, ?, ?, ?)",
                (run_id, profile or "general", json.dumps(context or {}, ensure_ascii=False),
                 str(tenant_id or "local-default"), str(agent_id or ""), str(requested_by or "")),
            )
        return run_id

    def complete_agent_eval_run(self, run_id: str, summary: dict,
                                status: str = "completed") -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                UPDATE agent_eval_runs
                SET status=?, summary_json=?, finished_at=CURRENT_TIMESTAMP
                WHERE run_id=?
            """, (status, json.dumps(summary or {}, ensure_ascii=False), run_id))
        return cur.rowcount > 0

    def save_agent_eval_result(self, run_id: str, case: dict, result: dict,
                               tenant_id: str = "local-default", agent_id: str = "") -> int:
        """保存单条 case 的答案、标准化 trace 和规则/LLM 指标。"""
        case_query = case.get("query", {})
        if isinstance(case_query, str):
            case_query = {"text": case_query}
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute("""
                INSERT INTO agent_eval_results
                    (run_id, case_id, case_key, profile, case_type, query_text, answer_text,
                     trace_json, metrics_json, status, elapsed_ms, error, tenant_id, agent_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                run_id,
                case.get("id"),
                case.get("case_key") or case.get("id") or "",
                case.get("profile") or "general",
                case.get("case_type") or "answer_quality",
                result.get("query") or case_query.get("text", ""),
                result.get("answer") or "",
                json.dumps(result.get("trace") or {}, ensure_ascii=False),
                json.dumps(result.get("metrics") or {}, ensure_ascii=False),
                result.get("status") or "completed",
                int(result.get("elapsed_ms") or 0),
                result.get("error") or "", str(tenant_id or "local-default"), str(agent_id or ""),
            ))
        return cur.lastrowid

    def get_agent_eval_runs(self, limit: int = 20, tenant_id: str = "local-default") -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT run_id, profile, status, context_json, summary_json, started_at, finished_at, tenant_id, agent_id, requested_by
                FROM agent_eval_runs WHERE tenant_id=?
                ORDER BY started_at DESC, rowid DESC LIMIT ?
            """, (str(tenant_id or "local-default"), limit)).fetchall()
        items = []
        for row in rows:
            item = dict(zip(("run_id", "profile", "status", "context_json", "summary_json", "started_at", "finished_at", "tenant_id", "agent_id", "requested_by"), row))
            for field in ("context_json", "summary_json"):
                try:
                    item[field.removesuffix("_json")] = json.loads(item.pop(field) or "{}")
                except (TypeError, ValueError):
                    item[field.removesuffix("_json")] = {}
            item["started_at"] = self._utc_to_local(item.get("started_at", ""))
            item["finished_at"] = self._utc_to_local(item.get("finished_at", ""))
            items.append(item)
        return items

    def get_agent_eval_results(self, run_id: str, tenant_id: str = "local-default") -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT r.id, r.run_id, r.case_id, r.case_key, r.profile, r.case_type, r.query_text, r.answer_text,
                       r.trace_json, r.metrics_json, r.status, r.elapsed_ms, r.error, r.created_at,
                       c.domain, c.expected_json, c.risk_tags_json
                FROM agent_eval_results r
                LEFT JOIN agent_eval_cases c ON c.id=r.case_id
                WHERE r.run_id=? AND r.tenant_id=? ORDER BY r.id ASC
            """, (run_id, str(tenant_id or "local-default"))).fetchall()
        items = []
        for row in rows:
            item = dict(zip((
                "id", "run_id", "case_id", "case_key", "profile", "case_type", "query", "answer",
                "trace_json", "metrics_json", "status", "elapsed_ms", "error", "created_at",
                "domain", "expected_json", "risk_tags_json",
            ), row))
            for field in ("trace_json", "metrics_json", "expected_json", "risk_tags_json"):
                try:
                    item[field.removesuffix("_json")] = json.loads(item.pop(field) or "{}")
                except (TypeError, ValueError):
                    item[field.removesuffix("_json")] = [] if field == "risk_tags_json" else {}
            item["created_at"] = self._utc_to_local(item.get("created_at", ""))
            items.append(item)
        return items

    def update_agent_eval_result_metrics(self, result_id: int, metrics: dict) -> bool:
        """Replace the metrics snapshot after an advisory Judge rerun."""
        if not isinstance(metrics, dict):
            raise ValueError("metrics 必须是对象")
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE agent_eval_results SET metrics_json=? WHERE id=?",
                (json.dumps(metrics, ensure_ascii=False), int(result_id)),
            )
        return cur.rowcount > 0

    # ====== Human calibration reviews ======

    def upsert_eval_human_review(self, evaluation_type: str, run_id: str, result_key: str,
                                 scores: dict, reviewer: str = "", note: str = "") -> int:
        """Save an independent human review for a single evaluation result."""
        if not all(str(value or "").strip() for value in (evaluation_type, run_id, result_key)):
            raise ValueError("evaluation_type、run_id 和 result_key 不能为空")
        if not isinstance(scores, dict):
            raise ValueError("scores 必须是对象")
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO eval_human_reviews
                    (evaluation_type, run_id, result_key, reviewer, scores_json, note)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(evaluation_type, run_id, result_key) DO UPDATE SET
                    reviewer=excluded.reviewer, scores_json=excluded.scores_json,
                    note=excluded.note, updated_at=CURRENT_TIMESTAMP
            """, (
                str(evaluation_type).strip(), str(run_id).strip(), str(result_key).strip(),
                str(reviewer or "").strip(), json.dumps(scores, ensure_ascii=False), str(note or "").strip(),
            ))
            row = conn.execute("""
                SELECT id FROM eval_human_reviews
                WHERE evaluation_type=? AND run_id=? AND result_key=?
            """, (str(evaluation_type).strip(), str(run_id).strip(), str(result_key).strip())).fetchone()
        return int(row[0])

    def get_eval_human_reviews(self, evaluation_type: str, run_id: str) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("""
                SELECT id, evaluation_type, run_id, result_key, reviewer, scores_json, note, created_at, updated_at
                FROM eval_human_reviews WHERE evaluation_type=? AND run_id=? ORDER BY id ASC
            """, (evaluation_type, run_id)).fetchall()
        items = []
        for row in rows:
            item = dict(zip((
                "id", "evaluation_type", "run_id", "result_key", "reviewer", "scores_json", "note",
                "created_at", "updated_at",
            ), row))
            try:
                item["scores"] = json.loads(item.pop("scores_json") or "{}")
            except (TypeError, ValueError):
                item["scores"] = {}
            item["created_at"] = self._utc_to_local(item.get("created_at", ""))
            item["updated_at"] = self._utc_to_local(item.get("updated_at", ""))
            items.append(item)
        return items

    # ====== Release gate evidence ======

    def save_release_gate_evidence(self, evidence_key: str, evidence: dict,
                                   updated_by: str = "") -> None:
        """Persist explicit administrator evidence for a release gate item."""
        key = str(evidence_key or "").strip()
        if not key or not isinstance(evidence, dict):
            raise ValueError("发布门禁证据必须包含有效 key 和对象内容")
        payload = dict(evidence)
        payload["passed"] = bool(payload.get("passed"))
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                """INSERT INTO release_gate_evidence
                   (evidence_key, evidence_json, updated_by, updated_at)
                   VALUES (?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(evidence_key) DO UPDATE SET
                     evidence_json=excluded.evidence_json,
                     updated_by=excluded.updated_by,
                     updated_at=CURRENT_TIMESTAMP""",
                (key, json.dumps(payload, ensure_ascii=False),
                 str(updated_by or payload.get("reviewer") or "").strip()),
            )

    def get_release_gate_evidence(self, evidence_key: str = "") -> dict | dict[str, dict]:
        """Read release evidence without exposing unrelated database fields."""
        with sqlite3.connect(self._db_path) as conn:
            if evidence_key:
                row = conn.execute(
                    """SELECT evidence_key, evidence_json, updated_by, updated_at
                       FROM release_gate_evidence WHERE evidence_key=?""",
                    (str(evidence_key).strip(),),
                ).fetchone()
                rows = [row] if row else []
            else:
                rows = conn.execute(
                    """SELECT evidence_key, evidence_json, updated_by, updated_at
                       FROM release_gate_evidence ORDER BY evidence_key"""
                ).fetchall()
        result = {}
        for row in rows:
            try:
                payload = json.loads(row[1] or "{}")
            except (TypeError, ValueError):
                payload = {}
            payload["evidence_key"] = row[0]
            payload["updated_by"] = row[2] or ""
            payload["updated_at"] = self._utc_to_local(row[3] or "")
            result[row[0]] = payload
        if evidence_key:
            return result.get(str(evidence_key).strip(), {})
        return result
