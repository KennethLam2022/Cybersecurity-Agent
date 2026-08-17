from trace_observability import add_trace_step, build_trace_envelope, finish_trace, retrieval_counts


def test_trace_envelope_contains_runtime_context():
    trace = build_trace_envelope(
        original_query="等保三级访问控制要求",
        rewrite_enabled=True,
        conversation_id="conv1",
        conversation_category="user",
        path="chat.sync",
        db_path="",
    )
    add_trace_step(trace, "query_rewrite", enabled=True)
    finish_trace(trace, "answered", returned_count=3)

    assert trace["schema_version"].startswith("2026.08.phase2.trace")
    assert trace["conversation"]["id"] == "conv1"
    assert trace["context"]["taxonomy_version"]
    assert "knowledge_base" in trace["context"]
    assert trace["steps"][0]["step"] == "query_rewrite"
    assert trace["outcome"] == "answered"
    assert trace["outcome_detail"]["returned_count"] == 3


def test_retrieval_counts_supports_single_and_multi_trace_shapes():
    single = {
        "retrieval": {
            "counts": {
                "faiss": 5,
                "chroma": 4,
                "bm25": 3,
                "after_rerank": 6,
                "returned": 2,
            }
        }
    }
    assert retrieval_counts(single) == {
        "faiss": 5,
        "chroma": 4,
        "bm25": 3,
        "final": 6,
        "returned": 2,
    }

    multi = {
        "retrieval": {
            "branches": [
                {"counts": {"faiss": 2, "chroma": 1, "bm25": 3}},
                {"counts": {"faiss": 4, "chroma": 5, "bm25": 0}},
            ],
            "merged_counts": {"final": 7, "returned": 4},
        }
    }
    assert retrieval_counts(multi) == {
        "faiss": 6,
        "chroma": 6,
        "bm25": 3,
        "final": 7,
        "returned": 4,
    }
