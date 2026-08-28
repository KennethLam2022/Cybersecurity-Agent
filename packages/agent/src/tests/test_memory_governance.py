from memory import ConversationMemory
from memory_governance import propose_memory_conflict, propose_user_profile


class FakeProfileLlm:
    model = "profile-test-model"

    def chat(self, messages, **kwargs):
        assert "允许字段" in messages[-1]["content"]
        return {"content": '{"fields":{"occupation":"网络安全工程师","income":"高收入"},"rationale":"用户明确表达职业"}',
                "usage": {"prompt_tokens": 12, "completion_tokens": 10}, "model": self.model}


class FakeConflictLlm:
    model = "conflict-test-model"

    def chat(self, messages, **kwargs):
        assert "既有记忆冲突" in messages[-1]["content"]
        assert "网络安全工程师" not in messages[-1]["content"]
        return {"content": '{"decision":"update","proposed_content":"occupation：安全架构师","rationale":"用户明确说明职业已变更"}', "model": self.model}


def test_profile_proposal_is_filtered_and_requires_manual_confirmation(tmp_path):
    memory = ConversationMemory(str(tmp_path / "profile-governance.db"))
    owner = memory.register_user("profile@example.com", "Correct-Horse-33", "Owner")
    proposal = propose_user_profile(
        memory, FakeProfileLlm(), owner["tenant_id"], owner["id"], owner["agent_id"],
        "我是一名网络安全工程师。",
    )
    assert proposal["status"] == "pending"
    assert proposal["fields"] == {"occupation": "网络安全工程师"}
    assert memory.list_long_term_memories(owner["tenant_id"], owner["id"], owner["agent_id"]) == []

    approved = memory.review_memory_profile_proposal(
        proposal["id"], owner["tenant_id"], owner["id"], "approved", "人工确认",
    )
    assert approved["status"] == "approved"
    memories = memory.list_long_term_memories(owner["tenant_id"], owner["id"], owner["agent_id"])
    assert any("occupation：网络安全工程师" == item["content"] for item in memories)


def test_profile_proposal_rejection_does_not_write_memory(tmp_path):
    memory = ConversationMemory(str(tmp_path / "profile-reject.db"))
    owner = memory.register_user("profile-reject@example.com", "Correct-Horse-34", "Owner")
    proposal = propose_user_profile(
        memory, FakeProfileLlm(), owner["tenant_id"], owner["id"], owner["agent_id"],
        "我是一名网络安全工程师。",
    )
    rejected = memory.review_memory_profile_proposal(
        proposal["id"], owner["tenant_id"], owner["id"], "rejected", "暂不保存",
    )
    assert rejected["status"] == "rejected"
    assert memory.list_long_term_memories(owner["tenant_id"], owner["id"], owner["agent_id"]) == []


def test_memory_conflict_requires_review_before_replacing_existing_memory(tmp_path):
    memory = ConversationMemory(str(tmp_path / "memory-conflict.db"))
    owner = memory.register_user("conflict@example.com", "Correct-Horse-35", "Owner")
    memory_id = memory.write_long_term_memory(
        owner["tenant_id"], owner["id"], owner["agent_id"], "fact", "occupation：网络安全工程师", ["profile", "occupation"],
    )
    proposal = propose_memory_conflict(
        memory, FakeConflictLlm(), owner["tenant_id"], owner["id"], owner["agent_id"], memory_id,
        "我现在是安全架构师。",
    )
    assert proposal["status"] == "pending"
    assert memory._get_scoped_memory(owner["tenant_id"], owner["id"], owner["agent_id"], memory_id)["status"] == "active"

    approved = memory.review_memory_conflict_proposal(proposal["id"], owner["tenant_id"], owner["id"], "approved")
    assert approved["status"] == "approved"
    all_memories = memory.list_long_term_memories(owner["tenant_id"], owner["id"], owner["agent_id"])
    assert next(item for item in all_memories if item["id"] == memory_id)["status"] == "suppressed"
    assert any(item["content"] == "occupation：安全架构师" and item["status"] == "active" for item in all_memories)
