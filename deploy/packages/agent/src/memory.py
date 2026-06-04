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
import sqlite3, json, uuid, time, logging
from pathlib import Path
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

_DB_DIR = Path(__file__).parent.parent.parent.parent / "agent_data"
_DB_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = str(_DB_DIR / "conversations.db")

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
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                    user_rating INTEGER,
                    semantic_rating INTEGER,
                    documents TEXT,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (conversation_id) REFERENCES conversations(id)
                );
            """)
            try:
                conn.execute("ALTER TABLE conversations ADD COLUMN deleted INTEGER DEFAULT 0")
            except sqlite3.OperationalError:
                pass

    def create_conversation(self, title: str = "新对话") -> dict:
        conv_id = str(uuid.uuid4())[:8]
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "INSERT INTO conversations (id, title) VALUES (?, ?)",
                (conv_id, title),
            )
            conn.execute(
                "INSERT INTO session_memory (conversation_id, memory_data) VALUES (?, '{}')",
                (conv_id,),
            )
        return {"id": conv_id, "title": title}

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
        except Exception:
            # LLM 压缩失败时回退
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

    def get_conversations(self, include_deleted: bool = False) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            sql = "SELECT id, title, created_at, updated_at, deleted FROM conversations"
            if not include_deleted:
                sql += " WHERE deleted IS NULL OR deleted = 0"
            sql += " ORDER BY updated_at DESC, id DESC"
            rows = conn.execute(sql).fetchall()
        return [
            {
                "id": r[0],
                "title": r[1],
                "created_at": r[2],
                "updated_at": r[3],
                "deleted": bool(r[4]) if r[4] else False,
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
                "SELECT id, title, created_at, updated_at, deleted FROM conversations WHERE id = ?",
                (conversation_id,),
            ).fetchone()
            if not conv_row:
                return None

            msg_rows = conn.execute("""
                SELECT m.role, m.content, m.sources, m.created_at,
                       u.user_rating, u.semantic_rating
                FROM messages m
                LEFT JOIN usage_logs u ON m.id = u.message_id
                WHERE m.conversation_id = ?
                ORDER BY m.id
            """, (conversation_id,)).fetchall()

        messages = []
        rounds = 0
        total_sources = 0
        for row in msg_rows:
            msg = {"role": row[0], "content": row[1], "created_at": row[3]}
            if row[2]:
                sources = json.loads(row[2])
                msg["sources"] = sources
                total_sources += len(sources)
            if row[4] is not None:
                msg["user_rating"] = row[4]
            if row[5] is not None:
                msg["semantic_rating"] = row[5]
            messages.append(msg)
            if row[0] == "assistant":
                rounds += 1

        return {
            "id": conv_row[0],
            "title": conv_row[1],
            "created_at": conv_row[2],
            "updated_at": conv_row[3],
            "deleted": bool(conv_row[4]) if conv_row[4] else False,
            "messages": messages,
            "stats": {
                "rounds": rounds,
                "total_sources": total_sources,
            },
        }

    def update_title(self, conversation_id: str, title: str):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(
                "UPDATE conversations SET title = ? WHERE id = ?",
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
    ):
        with sqlite3.connect(self._db_path) as conn:
            conn.execute("""
                INSERT INTO usage_logs (
                    conversation_id, message_id, query,
                    rewrite_time, faiss_time, chroma_time, rerank_time, llm_time, total_time,
                    faiss_count, chroma_count, bm25_count, final_count, returned_count,
                    prompt_tokens, completion_tokens,
                    llm_success, was_circuit_break, circuit_provider,
                    was_truncated, off_topic, documents
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                conversation_id, message_id, query,
                rewrite_time, faiss_time, chroma_time, rerank_time, llm_time, total_time,
                faiss_count, chroma_count, bm25_count, final_count, returned_count,
                prompt_tokens, completion_tokens,
                1 if llm_success else 0, 1 if was_circuit_break else 0, circuit_provider,
                1 if was_truncated else 0, 1 if off_topic else 0,
                json.dumps(documents, ensure_ascii=False) if documents else None,
            ))

    def update_rating(self, message_id: int, rating: int, semantic: bool = False):
        field = "semantic_rating" if semantic else "user_rating"
        with sqlite3.connect(self._db_path) as conn:
            conn.execute(f"UPDATE usage_logs SET {field} = ? WHERE message_id = ?", (rating, message_id))

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

    def get_dashboard_stats(self) -> dict:
        """聚合 usage_logs 数据供看板使用"""
        with sqlite3.connect(self._db_path) as conn:
            raw = conn.execute(
                "SELECT documents FROM usage_logs WHERE documents IS NOT NULL AND documents != 'null' ORDER BY id DESC LIMIT 500"
            ).fetchall()
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
            ratings = conn.execute(
                "SELECT COALESCE(user_rating, semantic_rating) FROM usage_logs WHERE COALESCE(user_rating, semantic_rating) IS NOT NULL"
            ).fetchall()
        dist = {1: 0, 2: 0, 3: 0, 4: 0, 5: 0}
        for (r,) in ratings:
            if r in dist:
                dist[r] += 1

        with sqlite3.connect(self._db_path) as conn:
            rating_rows = conn.execute("""
                SELECT DATE(created_at) as d, AVG(COALESCE(user_rating, semantic_rating))
                FROM usage_logs
                WHERE COALESCE(user_rating, semantic_rating) IS NOT NULL
                  AND created_at >= DATE('now', '-30 days')
                GROUP BY d ORDER BY d
            """).fetchall()
        rating_trend = [{"date": r[0], "avg_rating": round(r[1], 2)} for r in rating_rows]

        with sqlite3.connect(self._db_path) as conn:
            ret_rows = conn.execute("""
                SELECT DATE(created_at) as d,
                    AVG(rewrite_time), AVG(llm_time), AVG(total_time),
                    AVG(returned_count), COUNT(*)
                FROM usage_logs
                WHERE created_at >= DATE('now', '-30 days')
                GROUP BY d ORDER BY d
            """).fetchall()
        retrieval_trends = []
        for r in ret_rows:
            retrieval_trends.append({
                "date": r[0], "avg_rewrite_time": round(r[1] or 0, 3),
                "avg_llm_time": round(r[2] or 0, 3), "avg_total_time": round(r[3] or 0, 3),
                "avg_returned_count": round(r[4] or 0, 1), "query_count": r[5],
            })

        with sqlite3.connect(self._db_path) as conn:
            llm_rows = conn.execute("""
                SELECT DATE(created_at) as d,
                    COUNT(*), SUM(llm_success), SUM(was_circuit_break)
                FROM usage_logs
                WHERE created_at >= DATE('now', '-30 days')
                GROUP BY d ORDER BY d
            """).fetchall()
        llm_health = []
        for r in llm_rows:
            total = r[1]
            llm_health.append({
                "date": r[0], "total_calls": total,
                "success_count": r[2] or 0, "fail_count": total - (r[2] or 0),
                "circuit_break_count": r[3] or 0,
            })

        with sqlite3.connect(self._db_path) as conn:
            query_rows = conn.execute(
                "SELECT query FROM usage_logs ORDER BY id DESC LIMIT 200"
            ).fetchall()
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
            act_rows = conn.execute("""
                SELECT DATE(created_at) as d, COUNT(DISTINCT conversation_id), COUNT(*)
                FROM usage_logs
                WHERE created_at >= DATE('now', '-30 days')
                GROUP BY d ORDER BY d
            """).fetchall()
        activity = [{"date": r[0], "new_convs": 0, "total_queries": r[2]} for r in act_rows]

        with sqlite3.connect(self._db_path) as conn:
            trunc_rows = conn.execute("""
                SELECT DATE(created_at) as d, SUM(was_truncated), COUNT(*)
                FROM usage_logs
                WHERE created_at >= DATE('now', '-30 days')
                GROUP BY d ORDER BY d
            """).fetchall()
        truncation = [{"date": r[0], "truncation_count": r[1] or 0, "total": r[2]} for r in trunc_rows]

        with sqlite3.connect(self._db_path) as conn:
            conf_rows = conn.execute("""
                SELECT documents, COALESCE(user_rating, semantic_rating), query
                FROM usage_logs
                WHERE documents IS NOT NULL AND documents != 'null'
                  AND COALESCE(user_rating, semantic_rating) IS NOT NULL
                ORDER BY id DESC LIMIT 100
            """).fetchall()
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

    def drill_down(self, drill_type: str, key: str, limit: int = 50) -> list[dict]:
        with sqlite3.connect(self._db_path) as conn:
            if drill_type == 'rating':
                rows = conn.execute("""
                    SELECT u.conversation_id, u.query, u.user_rating, u.semantic_rating,
                           COALESCE(u.user_rating, u.semantic_rating), u.created_at
                    FROM usage_logs u
                    WHERE COALESCE(u.user_rating, u.semantic_rating) = ?
                    ORDER BY u.created_at DESC LIMIT ?
                """, (int(key), limit)).fetchall()
                fields = ["user_rating", "semantic_rating", "final_rating", "created_at"]
            elif drill_type == 'keyword':
                rows = conn.execute("""
                    SELECT u.conversation_id, u.query, u.user_rating, u.semantic_rating,
                           COALESCE(u.user_rating, u.semantic_rating), u.created_at
                    FROM usage_logs u
                    WHERE u.query LIKE ?
                    ORDER BY u.created_at DESC LIMIT ?
                """, (f'%{key}%', limit)).fetchall()
                fields = ["user_rating", "semantic_rating", "final_rating", "created_at"]
            elif drill_type == 'error_date':
                rows = conn.execute("""
                    SELECT u.conversation_id, u.query, u.llm_success, u.was_circuit_break,
                           u.circuit_provider, u.created_at
                    FROM usage_logs u
                    WHERE DATE(u.created_at) = ? AND (u.llm_success = 0 OR u.was_circuit_break = 1)
                    ORDER BY u.created_at DESC LIMIT ?
                """, (key, limit)).fetchall()
                fields = ["llm_success", "was_circuit_break", "circuit_provider", "created_at"]
            elif drill_type == 'scatter':
                rows = conn.execute("""
                    SELECT u.conversation_id, u.query, u.documents, u.created_at
                    FROM usage_logs u
                    WHERE u.documents IS NOT NULL AND u.documents != 'null'
                      AND COALESCE(u.user_rating, u.semantic_rating) IS NOT NULL
                    ORDER BY u.id DESC LIMIT 100
                """).fetchall()
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