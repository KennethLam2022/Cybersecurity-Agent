import sqlite3

from memory import ConversationMemory


def test_retrieval_eval_items_persist_profile_and_keep_old_import_format(tmp_path):
    memory = ConversationMemory(str(tmp_path / "eval.db"))

    memory.add_retrieval_eval_item(
        "通信行业专项题", "核心网", "通信行业", "medium", profile="industry/telecom"
    )
    memory.batch_import_retrieval_eval_items([
        ("通用题", "等保", "等保", "medium"),
    ])

    items = memory.get_retrieval_eval_items()
    profiles = {item["query"]: item["profile"] for item in items}

    assert profiles["通信行业专项题"] == "industry/telecom"
    assert profiles["通用题"] == "general"


def test_retrieval_eval_history_records_profile(tmp_path):
    memory = ConversationMemory(str(tmp_path / "eval.db"))

    memory.save_retrieval_eval(
        query="通信专项", expected_source="核心网", recall_5=1, recall_10=1,
        mrr=1.0, faiss_count=1, chroma_count=1, rerank_top1_match=1,
        profile="industry/telecom",
    )

    data = memory.get_retrieval_eval()

    assert data["items"][0]["profile"] == "industry/telecom"
    assert data["summary"]["profile_counts"] == {"industry/telecom": 1}


def test_retrieval_compare_history_records_profile(tmp_path):
    memory = ConversationMemory(str(tmp_path / "eval.db"))

    memory.save_eval_comparison(
        query="通信专项", expected_source="核心网",
        faiss_only_recall_5=0, faiss_only_mrr=0.0,
        bm25_only_recall_5=0, bm25_only_mrr=0.0,
        hybrid_no_rerank_recall_5=1, hybrid_no_rerank_mrr=0.5,
        hybrid_rerank_recall_5=1, hybrid_rerank_mrr=1.0,
        profile="industry/telecom",
    )

    data = memory.get_eval_comparison()

    assert data["items"][0]["profile"] == "industry/telecom"
    assert data["summary"]["profile_counts"] == {"industry/telecom": 1}


def test_existing_eval_item_table_is_migrated_without_column_misalignment(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute("""
            CREATE TABLE retrieval_eval_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query TEXT NOT NULL,
                expected TEXT NOT NULL,
                category TEXT DEFAULT '',
                difficulty TEXT DEFAULT 'medium',
                is_active INTEGER DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute(
            "INSERT INTO retrieval_eval_items (query, expected, category, difficulty) VALUES (?, ?, ?, ?)",
            ("历史通用题", "等保", "等保", "medium"),
        )

    memory = ConversationMemory(str(db_path))
    items = memory.get_retrieval_eval_items()

    assert items[0]["query"] == "历史通用题"
    assert items[0]["profile"] == "general"
    assert items[0]["category"] == "等保"
