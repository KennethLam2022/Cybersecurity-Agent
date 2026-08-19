from memory import ConversationMemory


def test_human_review_upsert_and_lookup(tmp_path):
    memory = ConversationMemory(str(tmp_path / "reviews.db"))

    first_id = memory.upsert_eval_human_review(
        "agent", "run-1", "case-1", {"faithfulness": 0.8}, reviewer="alice", note="有一处遗漏",
    )
    second_id = memory.upsert_eval_human_review(
        "agent", "run-1", "case-1", {"faithfulness": 0.7}, reviewer="bob", note="复核后调整",
    )
    reviews = memory.get_eval_human_reviews("agent", "run-1")

    assert first_id == second_id
    assert len(reviews) == 1
    assert reviews[0]["evaluation_type"] == "agent"
    assert reviews[0]["result_key"] == "case-1"
    assert reviews[0]["reviewer"] == "bob"
    assert reviews[0]["scores"] == {"faithfulness": 0.7}
    assert reviews[0]["note"] == "复核后调整"
