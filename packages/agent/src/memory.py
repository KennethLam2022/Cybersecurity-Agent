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
import sqlite3, json, uuid, logging, os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

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
                if hashlib.sha256(decrypted.encode()).hexdigest() == stored_hash:
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
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT DEFAULT '新对话',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    jailbreak_status TEXT DEFAULT NULL,
                    jailbreak_reason TEXT DEFAULT NULL,
                    jailbreak_message_id INTEGER DEFAULT NULL
                );
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
                conn.execute("ALTER TABLE conversations ADD COLUMN category TEXT DEFAULT 'user'")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN jailbreak_status TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN jailbreak_reason TEXT DEFAULT NULL")
            except sqlite3.OperationalError:
                pass
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN jailbreak_message_id INTEGER DEFAULT NULL")
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
            # ---- retrieval_eval_items 测试集表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS retrieval_eval_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    expected TEXT NOT NULL,
                    category TEXT DEFAULT '',
                    difficulty TEXT DEFAULT 'medium',
                    is_active INTEGER DEFAULT 1,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            # ---- e2e_eval_items 综合质量评测测试集表 ----
            conn.execute("""
                CREATE TABLE IF NOT EXISTS e2e_eval_items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    query TEXT NOT NULL,
                    domain TEXT DEFAULT '',
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

    def create_conversation(self, title: str = "新对话", category: str = "user") -> dict:
        conv_id = str(uuid.uuid4())[:8]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO conversations (id, title, category) VALUES (?, ?, ?)",
                (conv_id, title, category),
            )
            conn.execute(
                "INSERT INTO session_memory (conversation_id, memory_data) VALUES (?, '{}')",
                (conv_id,),
            )
        return {"id": conv_id, "title": title, "category": category}

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
            prompt = _SUMMARY_PROMPT.format(conversation=combined[:2000])
            result = llm_provider.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=100,
                timeout=10,
            )
            return result.strip()
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

    def get_conversations(self, include_deleted: bool = False, include_test: bool = False, jailbreak: str = "all") -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            sql = "SELECT id, title, created_at, updated_at, deleted, category, jailbreak_status, jailbreak_reason, jailbreak_message_id FROM conversations"
            conditions = []
            if not include_deleted:
                conditions.append("(deleted IS NULL OR deleted = 0)")
            if not include_test:
                conditions.append("(category IS NULL OR category = 'user')")
            if jailbreak == "pending":
                conditions.append("jailbreak_status = 'pending'")
            elif jailbreak == "clean":
                conditions.append("(jailbreak_status IS NULL OR jailbreak_status NOT IN ('pending'))")
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY updated_at DESC, id DESC"
            rows = conn.execute(sql).fetchall()
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
            conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM session_memory WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM usage_logs WHERE conversation_id = ?", (conversation_id,))
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

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

    def update_rating(self, message_id: int, rating: int, semantic: bool = False):
        field = "semantic_rating" if semantic else "user_rating"
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(f"UPDATE usage_logs SET {field} = ? WHERE message_id = ?", (rating, message_id))

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
            active_path.write_text(row[0], encoding="utf-8")
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
                        doc_stats[fn] = {"count": 0, "confidences": [], "category": d.get("category", "")}
                    doc_stats[fn]["count"] += 1
                    c = d.get("confidence", 0.5)
                    if isinstance(c, (int, float)):
                        doc_stats[fn]["confidences"].append(c)
            except:
                pass
        hotness = sorted(doc_stats.items(), key=lambda x: x[1]["count"], reverse=True)[:20]
        doc_hotness = []
        for fn, st in hotness:
            avg_c = sum(st["confidences"]) / len(st["confidences"]) if st["confidences"] else 0
            doc_hotness.append({"file_name": fn, "count": st["count"], "avg_confidence": round(avg_c, 3), "category": st["category"]})

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
        import jieba
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
        truncation = [{"date": r[0], "truncation_count": r[1] or 0, "total": r[2]} for r in trunc_rows]

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
                confs = [d.get("confidence", 0) for d in docs if isinstance(d.get("confidence"), (int, float))]
                avg_c = sum(confs) / len(confs) if confs else 0
                confidence_scatter.append({
                    "avg_confidence": round(avg_c, 3), "rating": rating,
                    "query_short": (q or "")[:20],
                })
            except:
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
                    confs = [d.get("confidence", 0) for d in docs if isinstance(d.get("confidence"), (int, float))]
                    avg_c = sum(confs) / len(confs) if confs else 0
                    return [{"conversation_id": r[0], "query": r[1], "detail": {"avg_confidence": round(avg_c, 3), "doc_count": len(docs), "created_at": r[3]}}]
                return []
            else:
                return []

        return [
            {"conversation_id": r[0], "query": r[1][:100] if r[1] else "", "detail": dict(zip(fields, [r[i] for i in range(2, len(r))]))}
            for r in rows
        ]

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

    def save_retrieval_eval(self, query: str, expected_source: str,
                            recall_5: int, recall_10: int, mrr: float,
                            faiss_count: int, chroma_count: int, rerank_top1_match: int):
        """保存一次检索质量评估结果"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO retrieval_eval (query, expected_source, recall_5, recall_10, mrr,
                    faiss_count, chroma_count, rerank_top1_match)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (query, expected_source, recall_5, recall_10, mrr,
                   faiss_count, chroma_count, rerank_top1_match))

    def get_retrieval_eval(self, limit: int = 100) -> dict:
        """获取检索质量评估的汇总统计"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("SELECT * FROM retrieval_eval ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        cols = ["id", "query", "expected_source", "recall_5", "recall_10", "mrr",
                "faiss_count", "chroma_count", "rerank_top1_match", "eval_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for i in items:
            i["eval_at"] = self._utc_to_local(i.get("eval_at", ""))
        total = len(items)
        if total == 0:
            return {"items": [], "summary": {"count": 0}}
        avg_recall_5 = sum(i["recall_5"] for i in items) / total
        avg_recall_10 = sum(i["recall_10"] for i in items) / total
        avg_mrr = sum(i["mrr"] for i in items) / total
        return {
            "items": items,
            "summary": {
                "count": total,
                "avg_recall_5": round(avg_recall_5, 3),
                "avg_recall_10": round(avg_recall_10, 3),
                "avg_mrr": round(avg_mrr, 3),
            }
        }

    def save_eval_comparison(self, query: str, expected_source: str,
                              faiss_only_recall_5: int, faiss_only_mrr: float,
                              bm25_only_recall_5: int, bm25_only_mrr: float,
                              hybrid_no_rerank_recall_5: int, hybrid_no_rerank_mrr: float,
                              hybrid_rerank_recall_5: int, hybrid_rerank_mrr: float):
        """保存一次检索模式对比结果"""
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO eval_comparison
                    (query, expected_source,
                     faiss_only_recall_5, faiss_only_mrr,
                     bm25_only_recall_5, bm25_only_mrr,
                     hybrid_no_rerank_recall_5, hybrid_no_rerank_mrr,
                     hybrid_rerank_recall_5, hybrid_rerank_mrr)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (query, expected_source,
                  faiss_only_recall_5, faiss_only_mrr,
                  bm25_only_recall_5, bm25_only_mrr,
                  hybrid_no_rerank_recall_5, hybrid_no_rerank_mrr,
                  hybrid_rerank_recall_5, hybrid_rerank_mrr))

    def get_eval_comparison(self, limit: int = 100) -> dict:
        """获取检索模式对比的汇总统计"""
        with sqlite3.connect(self._db_path) as conn:
            rows = conn.execute("SELECT * FROM eval_comparison ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        cols = ["id", "query", "expected_source",
                "faiss_only_recall_5", "faiss_only_mrr",
                "bm25_only_recall_5", "bm25_only_mrr",
                "hybrid_no_rerank_recall_5", "hybrid_no_rerank_mrr",
                "hybrid_rerank_recall_5", "hybrid_rerank_mrr",
                "eval_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for i in items:
            i["eval_at"] = self._utc_to_local(i.get("eval_at", ""))
        total = len(items)
        if total == 0:
            return {"items": [], "summary": {"count": 0}}
        def _avg(key): return round(sum(i[key] for i in items) / total, 3)
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
            }
        }

    # ====== Retrieval Eval 测试集管理 (CRUD) ======

    def get_retrieval_eval_items(self, include_inactive: bool = False) -> list:
        with sqlite3.connect(self._db_path) as conn:
            if include_inactive:
                rows = conn.execute("SELECT * FROM retrieval_eval_items ORDER BY id DESC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM retrieval_eval_items WHERE is_active=1 ORDER BY id DESC").fetchall()
        cols = ["id","query","expected","category","difficulty","is_active","created_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for i in items:
            i["created_at"] = self._utc_to_local(i.get("created_at", ""))
        return items

    def add_retrieval_eval_item(self, query: str, expected: str,
                                category: str = "", difficulty: str = "medium") -> int:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "INSERT INTO retrieval_eval_items (query, expected, category, difficulty) VALUES (?, ?, ?, ?)",
                (query, expected, category, difficulty)
            )
            return cur.lastrowid

    def update_retrieval_eval_item(self, item_id: int, query: str, expected: str,
                                   category: str, difficulty: str, is_active: int) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE retrieval_eval_items SET query=?, expected=?, category=?, difficulty=?, is_active=? WHERE id=?",
                (query, expected, category, difficulty, is_active, item_id)
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
        """批量导入测试用例: items=[(query, expected, category, difficulty), ...]"""
        count = 0
        with sqlite3.connect(self._db_path) as conn:
            for row in items:
                try:
                    conn.execute(
                        "INSERT INTO retrieval_eval_items (query, expected, category, difficulty) VALUES (?, ?, ?, ?)",
                        (row[0], row[1], row[2] if len(row) > 2 else "", row[3] if len(row) > 3 else "medium")
                    )
                    count += 1
                except Exception:
                    continue
        return count

    # ====== E2E Eval 综合质量评测测试集管理 (CRUD) ======

    def get_e2e_eval_items(self, include_inactive: bool = False) -> list:
        with sqlite3.connect(self._db_path) as conn:
            if include_inactive:
                rows = conn.execute("SELECT * FROM e2e_eval_items ORDER BY id ASC").fetchall()
            else:
                rows = conn.execute("SELECT * FROM e2e_eval_items WHERE is_active=1 ORDER BY id ASC").fetchall()
        cols = ["id", "query", "domain", "difficulty", "style", "is_active", "created_at"]
        items = [dict(zip(cols, r)) for r in rows]
        for i in items:
            i["created_at"] = self._utc_to_local(i.get("created_at", ""))
        return items

    def add_e2e_eval_item(self, query: str, domain: str = "",
                          difficulty: str = "中等", style: str = "plain") -> int:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "INSERT INTO e2e_eval_items (query, domain, difficulty, style) VALUES (?, ?, ?, ?)",
                (query, domain, difficulty, style)
            )
            return cur.lastrowid

    def update_e2e_eval_item(self, item_id: int, query: str, domain: str,
                             difficulty: str, style: str, is_active: int) -> bool:
        with sqlite3.connect(self._db_path) as conn:
            cur = conn.execute(
                "UPDATE e2e_eval_items SET query=?, domain=?, difficulty=?, style=?, is_active=? WHERE id=?",
                (query, domain, difficulty, style, is_active, item_id)
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
        """批量导入: items=[(query, domain, difficulty, style), ...]"""
        count = 0
        with sqlite3.connect(self._db_path) as conn:
            for row in items:
                try:
                    conn.execute(
                        "INSERT INTO e2e_eval_items (query, domain, difficulty, style) VALUES (?, ?, ?, ?)",
                        (row[0], row[1] if len(row) > 1 else "",
                         row[2] if len(row) > 2 else "中等",
                         row[3] if len(row) > 3 else "plain")
                    )
                    count += 1
                except Exception:
                    continue
        return count