import json
import sqlite3

import app_state
import eval_e2e
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
        context={"taxonomy_version": "test-taxonomy", "prompt_version": "test-prompt"},
    )

    data = memory.get_retrieval_eval()

    assert data["items"][0]["profile"] == "industry/telecom"
    assert data["items"][0]["context"]["taxonomy_version"] == "test-taxonomy"
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
        context={"taxonomy_version": "test-taxonomy"},
    )

    data = memory.get_eval_comparison()

    assert data["items"][0]["profile"] == "industry/telecom"
    assert data["items"][0]["context"]["taxonomy_version"] == "test-taxonomy"
    assert data["summary"]["profile_counts"] == {"industry/telecom": 1}


def test_e2e_eval_items_persist_profile_and_keep_old_import_format(tmp_path):
    memory = ConversationMemory(str(tmp_path / "eval.db"))

    memory.add_e2e_eval_item(
        "核心网安全控制有哪些要求", "行业安全", "中等", "plain",
        profile="industry/telecom",
    )
    memory.batch_import_e2e_eval_items([
        ("通用网络安全控制要求", "等保合规", "基础", "plain"),
    ])

    profiles = {item["query"]: item["profile"] for item in memory.get_e2e_eval_items()}

    assert profiles["核心网安全控制有哪些要求"] == "industry/telecom"
    assert profiles["通用网络安全控制要求"] == "general"


def test_e2e_evaluation_passes_question_profile_to_agent(tmp_path, monkeypatch):
    class FakeAgent:
        llm = None

        def __init__(self):
            self.profile_calls = []

        def ask(self, query, profiles=None):
            self.profile_calls.append((query, profiles))
            return {
                "answer": "基于检索结果给出控制建议",
                "sources": [{"title": "安全控制指南"}],
                "retrieved_docs": [{"content": "控制要求"}],
                "stats": {},
            }

    def score(*args, **kwargs):
        return {"score": 1.0, "reason": "test"}

    monkeypatch.setattr(eval_e2e, "eval_context_precision", score)
    monkeypatch.setattr(eval_e2e, "eval_context_recall", score)
    monkeypatch.setattr(eval_e2e, "eval_faithfulness", score)
    monkeypatch.setattr(eval_e2e, "eval_relevancy", score)
    monkeypatch.setattr(eval_e2e, "eval_hallucination", score)

    agent = FakeAgent()
    results = eval_e2e.run_evaluation(
        agent,
        [
            {"id": "G01", "domain": "通用", "difficulty": "基础", "query": "通用题"},
            {
                "id": "F01", "domain": "金融安全", "profile": "industry/finance",
                "difficulty": "中等", "query": "金融题",
            },
        ],
        use_llm=False,
        output_file=tmp_path / "e2e.json",
    )

    assert agent.profile_calls == [
        ("通用题", {"general"}),
        ("金融题", {"industry/finance"}),
    ]
    assert [item["profile"] for item in results] == ["general", "industry/finance"]
    assert all(item["profile_snapshot"]["status"] == "known" for item in results)
    assert all(item["profile_version"].startswith("2026.08.phase2:") for item in results)


def test_e2e_precision_preserves_annotations_and_excludes_unscored_results(tmp_path, monkeypatch):
    class FakeAgent:
        llm = None

        def ask(self, query, profiles=None):
            return {
                "answer": "answer",
                "sources": [{"file_name": "doc.pdf", "content": "context"}],
                "stats": {},
            }

    received = {}

    def precision(query, docs, answer, **kwargs):
        received.update(kwargs)
        return {
            "score": None,
            "status": "unscored_missing_annotation",
            "explanation": "labels unavailable",
            "details": {"missing_inputs": ["relevance_annotations"]},
            "evaluator": {
                "method": "not_run", "model_id": None, "prompt_version": None,
            },
        }

    def score(*_args, **_kwargs):
        return {"score": 0.8, "explanation": "ok", "details": {}}

    monkeypatch.setattr(eval_e2e, "eval_context_precision", precision)
    monkeypatch.setattr(eval_e2e, "eval_context_recall", score)
    monkeypatch.setattr(eval_e2e, "eval_faithfulness", score)
    monkeypatch.setattr(eval_e2e, "eval_relevancy", score)
    monkeypatch.setattr(eval_e2e, "eval_hallucination", score)

    result = eval_e2e.run_evaluation(
        FakeAgent(),
        [{
            "id": "E01", "domain": "test", "difficulty": "基础",
            "query": "query", "relevance_annotations": [True],
            "relevance_annotation_source": "gold-set-v1",
        }],
        use_llm=False,
        output_file=tmp_path / "e2e.json",
    )[0]

    assert received["relevance_annotations"] == [True] and received["annotation_source"] == "gold-set-v1"
    assert result["scores"]["context_precision"]["score"] is None
    assert result["scores"]["context_precision"]["status"] == "unscored_missing_annotation"
    persisted = json.loads((tmp_path / "e2e.json").read_text(encoding="utf-8"))
    assert persisted[0]["scores"]["context_precision"] == result["scores"]["context_precision"]
    scored_result = {
        **result,
        "scores": {
            **result["scores"],
            "context_precision": {"score": 0.6, "status": "scored"},
        },
    }
    stats = eval_e2e._compute_stats([result, scored_result])
    assert stats["avg_scores"]["context_precision"] == 0.6
    assert stats["avg_scores"]["context_recall"] == 0.8

    report = tmp_path / "report.html"
    eval_e2e.generate_html([result], report)
    html = report.read_text(encoding="utf-8")
    assert "Precision" in html
    assert "未评分" in html
    assert ">0.000<" not in html


def test_e2e_admin_analysis_omits_unscored_precision_from_low_items(monkeypatch):
    class EvalLLM:
        model = "test-judge"

        def chat(self, messages):
            self.prompt = messages[0]["content"]
            return {"content": "analysis"}

    llm = EvalLLM()
    result = {
        "total": 2,
        "errors": 0,
        "avg_scores": {
            "context_precision": 0.2,
            "context_recall": 0.8,
            "faithfulness": 0.8,
            "relevancy": 0.8,
            "hallucination": 0.8,
        },
    }
    items = [
        {
            "query": "unscored query",
            "domain": "通用",
            "scores": {"context_precision": {"score": None, "status": "judge_error"}},
        },
        {
            "query": "scored low query",
            "domain": "通用",
            "scores": {"context_precision": {"score": 0.2, "status": "scored"}},
        },
    ]
    monkeypatch.setattr(app_state, "_save_eval_summary", lambda *_args: None)

    summary = app_state._generate_eval_summary(llm, "e2e_quality", result, items)

    assert summary == "analysis"
    assert "平均 Context Precision：20%" in llm.prompt
    assert "「scored low query」" in llm.prompt
    assert "「unscored query」" not in llm.prompt
    assert "未评分" in llm.prompt


def test_e2e_admin_analysis_formats_missing_average_as_unscored(monkeypatch):
    class EvalLLM:
        model = "test-judge"

        def chat(self, messages):
            self.prompt = messages[0]["content"]
            return {"content": "analysis"}

    llm = EvalLLM()
    monkeypatch.setattr(app_state, "_save_eval_summary", lambda *_args: None)

    app_state._generate_eval_summary(llm, "e2e_quality", {
        "total": 1,
        "errors": 0,
        "avg_scores": {"context_precision": None},
    }, [{
        "query": "unscored query",
        "domain": "通用",
        "scores": {"context_precision": {"score": None, "status": "unscored_missing_annotation"}},
    }])

    assert "平均 Context Precision：未评分" in llm.prompt
    assert "Context Precision未评分" in llm.prompt
    assert "偏低（< 60%）" not in llm.prompt


def test_e2e_compare_report_does_not_calculate_delta_for_unscored_metric(tmp_path, monkeypatch):
    previous_file = tmp_path / "previous.json"
    previous_file.write_text(
        '[{"scores":{"context_precision":{"score":0.8,"status":"scored"}}}]',
        encoding="utf-8",
    )
    versions_file = tmp_path / "versions.json"
    versions_file.write_text(
        '[{"version":"v1","results_file":"previous.json"},{"version":"v2"}]',
        encoding="utf-8",
    )
    monkeypatch.setattr(eval_e2e, "_EVAL_DIR", tmp_path)
    monkeypatch.setattr(eval_e2e, "_VERSION_FILE", versions_file)

    report = eval_e2e._generate_compare_html({"context_precision": None}, [{
        "scores": {"context_precision": {"score": None, "status": "judge_error"}},
    }])

    precision_section = report.split('<div class="label">Context Precision</div>')[1].split('</div>')[0]
    assert '<span class="curr">未评分</span>' in precision_section
    assert "上次 0.800" in report
    assert "diff-up" not in precision_section and "diff-down" not in precision_section


def test_e2e_averages_accept_legacy_scores_and_omit_missing_fields():
    results = [
        {"scores": {"context_precision": 0.8}},
        {"scores": {"context_precision": {"score": 0.4}}},
        {"scores": {}},
        {"scores": {"context_precision": {"score": 0.9, "status": "judge_error"}}},
    ]
    assert eval_e2e._average_metric(results, "context_precision") == 0.6
    assert eval_e2e._average_metric(results, "context_recall") is None


def test_e2e_stats_and_html_report_keep_profile_breakdown(tmp_path):
    results = [
        {
            "profile": "general", "scores": {key: {"score": 0.8} for key in (
                "context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"
            )},
            "auto_status": "有来源(1条)", "sources": [], "truncation": {},
            "id": "G01", "domain": "通用", "difficulty": "基础", "query": "通用题", "answer": "回答",
        },
        {
            "profile": "industry/finance", "scores": {key: {"score": 0.6} for key in (
                "context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"
            )},
            "auto_status": "有来源(1条)", "sources": [], "truncation": {},
            "id": "F01", "domain": "金融安全", "difficulty": "中等", "query": "金融题", "answer": "回答",
        },
    ]

    stats = eval_e2e._compute_stats(results)
    output = tmp_path / "report.html"
    eval_e2e.generate_html(results, output)

    assert stats["profile_counts"] == {"general": 1, "industry/finance": 1}
    assert stats["profile_stats"]["industry/finance"]["avg_scores"]["relevancy"] == 0.6
    assert "industry/finance" in output.read_text(encoding="utf-8")


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
