from memory import ConversationMemory


def _conversation(memory: ConversationMemory):
    user = memory.register_user("owner@example.com", "Correct-Horse-30", "Owner")
    conversation = memory.create_conversation(
        tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"],
    )
    message_id = memory.add_message(conversation["id"], "assistant", "原始安全建议")
    return user, conversation, message_id


def test_conversation_share_password_expiry_scope_and_read_only_payload(tmp_path):
    memory = ConversationMemory(str(tmp_path / "collaboration.db"))
    user, conversation, _ = _conversation(memory)
    share = memory.create_conversation_share(
        user["tenant_id"], conversation["id"], user["id"], 24, "share-pass-01", False,
    )
    assert share["token"].startswith("shr_")
    assert memory.resolve_conversation_share(share["token"]) == {"password_required": True}
    resolved = memory.resolve_conversation_share(share["token"], "share-pass-01")
    assert resolved["share"]["allow_copy"] is False
    assert resolved["conversation"]["messages"][0]["content"] == "原始安全建议"
    assert "sources" not in resolved["conversation"]["messages"][0]
    listed = memory.list_conversation_shares(user["tenant_id"], conversation["id"])
    assert listed[0]["access_count"] == 1
    assert memory.update_conversation_share_status(
        user["tenant_id"], conversation["id"], share["id"], "revoked",
    )
    assert memory.resolve_conversation_share(share["token"], "share-pass-01") is None


def test_conversation_notes_and_answer_revisions_are_scoped_and_versioned(tmp_path):
    memory = ConversationMemory(str(tmp_path / "collaboration.db"))
    user, conversation, message_id = _conversation(memory)
    note = memory.create_conversation_note(
        user["tenant_id"], conversation["id"], "admin-1", "需要复核来源", "auditor",
    )
    assert memory.list_conversation_notes(user["tenant_id"], conversation["id"])[0]["id"] == note["id"]

    first = memory.create_answer_revision(
        user["tenant_id"], conversation["id"], message_id,
        "修订后的安全建议", "引用依据已补充", "admin-1",
    )
    second = memory.create_answer_revision(
        user["tenant_id"], conversation["id"], message_id,
        "第二版安全建议", "措辞调整", "admin-1",
    )
    assert (first["version"], second["version"]) == (1, 2)
    revisions = memory.list_answer_revisions(user["tenant_id"], conversation["id"], message_id)
    assert [item["version"] for item in revisions] == [2, 1]
    assert revisions[0]["original_content"] == "原始安全建议"


def test_answer_revision_draft_requires_review_before_formal_revision(tmp_path):
    memory = ConversationMemory(str(tmp_path / "drafts.db"))
    user, conversation, message_id = _conversation(memory)
    draft = memory.create_answer_revision_draft(
        user["tenant_id"], conversation["id"], message_id,
        "模型起草的安全建议", "补充引用和适用边界", "reflection-model", 3, "admin-1",
    )
    assert draft["status"] == "pending"
    assert memory.list_answer_revisions(user["tenant_id"], conversation["id"], message_id) == []
    rejected = memory.decide_answer_revision_draft(
        user["tenant_id"], draft["id"], "reject", "admin-2", "来源不足",
    )
    assert rejected["status"] == "rejected"
    assert rejected["revision"] is None

    second = memory.create_answer_revision_draft(
        user["tenant_id"], conversation["id"], message_id,
        "确认后的安全建议", "已补齐来源", "reflection-model", 3, "admin-1",
    )
    approved = memory.decide_answer_revision_draft(
        user["tenant_id"], second["id"], "approve", "admin-2", "确认发布",
    )
    assert approved["status"] == "approved"
    assert approved["revision"]["revised_content"] == "确认后的安全建议"
