"""Regression tests for retrieval hard constraints."""

import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SRC = os.path.join(_ROOT, "preprocessor", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from metadata_filter import MetadataFilterSpec
from retriever import CyberRetriever, validate_rerank_results


def test_rerank_result_validation_rejects_invalid_and_duplicate_indexes():
    accepted, status = validate_rerank_results(
        [{"index": 1, "relevance_score": 0.8}, {"index": 1, "relevance_score": 0.7}, {"index": 9, "relevance_score": 0.5}],
        candidate_count=2,
    )

    assert status == "invalid_results"
    assert accepted == [{"index": 1, "relevance_score": 0.8}]


def test_rrf_merge_preserves_rank_score_and_contributors():
    vector = [{"chunk_id": "a", "score": 0.1, "source": "faiss"}]
    keyword = [{"chunk_id": "a", "score": 2.0, "source": "bm25"}]

    merged = CyberRetriever._rrf_merge(vector, keyword, top_k=5)

    assert merged[0]["rank_score"] > 0
    assert merged[0]["rank_sources"] == "hybrid"
    assert "_rrf_score" not in merged[0]


def test_rrf_merge_weights_exact_lookup_toward_keyword_results():
    vector = [
        {"chunk_id": "semantic", "score": 0.1, "source": "faiss"},
        {"chunk_id": "exact", "score": 0.2, "source": "faiss"},
    ]
    keyword = [
        {"chunk_id": "exact", "score": 2.0, "source": "bm25"},
        {"chunk_id": "semantic", "score": 1.0, "source": "bm25"},
    ]

    merged = CyberRetriever._rrf_merge(
        vector, keyword, top_k=2, query_type="standard_lookup"
    )

    assert merged[0]["chunk_id"] == "exact"
    assert merged[0]["rank_sources"] == "hybrid"


def test_multi_query_rrf_preserves_rank_score():
    merged = CyberRetriever._rrf_merge_many(
        [[{"chunk_id": "a", "score": 0.1}], [{"chunk_id": "a", "score": 0.2}]],
        top_k=5,
    )

    assert merged[0]["rank_score"] > 0
    assert merged[0]["rank_sources"] == [0, 1]


def test_parent_result_keeps_chunk_level_evidence_and_access_metadata():
    retriever = CyberRetriever.__new__(CyberRetriever)
    retriever._parent_index = {
        "parent": {"text": "完整条款", "file_name": "law.md", "category": "general",
                    "section": "第1条", "visibility": "tenant", "tenant_id": "t1",
                    "owner_user_id": "", "agent_id": "", "document_id": "doc1"}
    }
    result = retriever._resolve_parent_docs([{
        "parent_id": "parent", "chunk_id": "chunk-1", "content": "命中条款",
        "source": "faiss", "score": 0.1, "clause": "第1条",
        "clause_awareness": 1.0, "clause_exact_match": True,
        "page": 3, "document_id": "doc1", "tenant_id": "t1",
    }], 1, {"tenant_id": "t1", "user_id": "u1", "agent_id": "a1"})

    assert result[0]["matched_chunk"]["chunk_id"] == "chunk-1"
    assert result[0]["evidence_location"]["page"] == 3


def test_parent_context_keeps_clause_and_bounds_long_parent_text(monkeypatch):
    monkeypatch.setenv("RAG_PARENT_CONTEXT_CHARS", "1200")
    retriever = CyberRetriever.__new__(CyberRetriever)
    long_text = "前置说明。" * 500 + "\n第8.1.4条 访问控制应当满足最小权限原则。\n" + "后续说明。" * 500
    content, meta = retriever._build_parent_context(long_text, "第8.1.4条")
    assert meta["truncated"] is True
    assert meta["strategy"] == "anchor_window"
    assert "第8.1.4条" in content
    assert len(content) < len(long_text)


def test_adjacent_clause_expansion_is_limited_to_same_document_and_section():
    docs = [
        {"file_name": "law", "section": "访问控制", "clause": "第8条", "content": "命中"},
        {"file_name": "law", "section": "访问控制", "clause": "第9条", "content": "相邻"},
        {"file_name": "other", "section": "访问控制", "clause": "第9条", "content": "其他文档"},
    ]
    expanded = CyberRetriever._expand_adjacent_clauses(docs, "请看第8条")
    assert expanded[1]["adjacent_clause"] is True
    assert "adjacent_clause" not in expanded[2]


def test_diversify_docs_avoids_duplicate_parent_when_alternative_exists():
    docs = [
        {"parent_id": "p1", "content": "同一段落" * 20, "rank_score": 0.99},
        {"parent_id": "p1", "content": "同一段落" * 20, "rank_score": 0.98},
        {"parent_id": "p2", "content": "另一段落" * 20, "rank_score": 0.80},
    ]
    result = CyberRetriever._diversify_docs(docs, 2)
    assert [item["parent_id"] for item in result] == ["p1", "p2"]


def test_hard_metadata_filter_does_not_return_unmatched_candidates(monkeypatch):
    retriever = CyberRetriever.__new__(CyberRetriever)
    retriever._use_hybrid = False
    retriever._rerank_api_key = None
    retriever.last_trace = {}
    retriever._last_rerank_trace = {"status": "not_run"}

    candidates = [
        {
            "content": "其他标准的访问控制要求",
            "file_name": "GB-T-99999.md",
            "category": "02-等保国标",
            "section": "第1条",
            "chunk_id": "other",
            "parent_id": "other",
            "score": 0.1,
            "rerank_score": None,
            "source": "test",
        }
    ]
    monkeypatch.setattr(retriever, "search", lambda *args, **kwargs: candidates)

    result = retriever.search_multi(
        ["GB/T 22239-2019 第8.1.4条"],
        top_k=5,
        use_rerank=False,
        use_parent=False,
        metadata_filter=MetadataFilterSpec(
            doc_ids=["22239"], article_numbers=["第8.1.4条"], hard_filter=True
        ),
    )

    assert result == []
    assert retriever.last_trace["metadata_filter_fallback"] is False
    assert retriever.last_trace["hard_filter_no_match"] is True


def test_vector_failure_is_exposed_when_bm25_fallback_is_used(monkeypatch):
    import re
    retriever = CyberRetriever.__new__(CyberRetriever)
    retriever._use_hybrid = False
    retriever._rerank_api_key = None
    retriever.last_trace = {}
    retriever._last_rerank_trace = {"status": "not_run"}
    retriever._negation_patterns = re.compile(r"(不能|不含|禁止|除外|不要|不得|不可|不会|没有|不包含|不包括|不应|不允许)")
    monkeypatch.setattr(retriever, "_load_faiss", lambda: (_ for _ in ()).throw(RuntimeError("faiss down")))
    monkeypatch.setattr(retriever, "_load_chroma", lambda: (_ for _ in ()).throw(RuntimeError("chroma down")))
    monkeypatch.setattr(retriever, "_bm25_search", lambda *args, **kwargs: [{
        "content": "授权内容", "file_name": "a.md", "category": "general",
        "section": "第一条", "chunk_id": "a", "parent_id": "a",
        "profile": "general", "visibility": "public", "tenant_id": "",
        "score": 0.1, "rerank_score": None, "source": "bm25",
    }])
    monkeypatch.setattr(retriever, "_resolve_parent_docs", lambda docs, top_k, access_scope: docs[:top_k])

    result = retriever.search("访问控制", top_k=1, use_rerank=False, use_parent=True)

    assert result
    assert retriever.last_trace["retrieval_degraded"] is True
    assert set(retriever.last_trace["degraded_stages"]) >= {"faiss", "chroma", "bm25_fallback"}
