import sqlite3

from memory import ConversationMemory


def test_existing_documents_are_migrated_to_default_knowledge_base(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    with sqlite3.connect(memory._db_path) as conn:
        conn.execute(
            "INSERT INTO documents (id, source_name, category, profile) VALUES (?, ?, ?, ?)",
            ("legacy-doc", "legacy.md", "通用", "general"),
        )
    # A fresh initialization/migration pass must attach legacy rows.
    ConversationMemory(str(tmp_path / "governance.db"))
    with sqlite3.connect(memory._db_path) as conn:
        row = conn.execute(
            "SELECT knowledge_base_id FROM documents WHERE id=?", ("legacy-doc",)
        ).fetchone()
    assert row == ("kb-public-general",)


def test_existing_private_documents_keep_private_migrated_knowledge_base(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    with sqlite3.connect(memory._db_path) as conn:
        conn.execute(
            "INSERT INTO documents "
            "(id, tenant_id, owner_user_id, agent_id, visibility, source_name, category, profile) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            ("legacy-private", "org-1", "user-1", "agent-1", "private", "private.md", "制度", "general"),
        )
    ConversationMemory(str(tmp_path / "governance.db"))
    with sqlite3.connect(memory._db_path) as conn:
        kb_id, visibility = conn.execute(
            "SELECT knowledge_base_id, visibility FROM documents WHERE id=?", ("legacy-private",)
        ).fetchone()
        kb_visibility = conn.execute(
            "SELECT visibility FROM knowledge_bases WHERE id=?", (kb_id,)
        ).fetchone()[0]
    assert kb_id.startswith("kb-migrated-")
    assert visibility == kb_visibility == "private"


def test_knowledge_base_lifecycle_and_document_assignment(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    user = memory.register_user("kb-owner@example.com", "Correct-Horse-21", "KB Owner")
    kb = memory.create_knowledge_base(
        user["tenant_id"], "整改项目库", "项目资料", "general", "tenant", user["id"],
    )
    memory.register_document(
        "kb-doc", "整改方案.docx", "制度", "general", "tenant",
        user["tenant_id"], "", user["agent_id"], kb["id"],
    )

    listed = memory.list_knowledge_bases(user["tenant_id"])
    assert any(item["id"] == kb["id"] and item["document_count"] == 1 for item in listed)
    docs = memory.list_knowledge_base_documents(kb["id"], user["tenant_id"])
    assert [item["id"] for item in docs] == ["kb-doc"]

    other = memory.create_knowledge_base(user["tenant_id"], "另一个资料库")
    assert memory.assign_document_knowledge_base("kb-doc", other["id"], user["tenant_id"])
    assert memory.list_knowledge_base_documents(kb["id"], user["tenant_id"]) == []
    assert memory.list_knowledge_base_documents(other["id"], user["tenant_id"])[0]["id"] == "kb-doc"

    assert memory.update_knowledge_base_status(other["id"], "archived", user["tenant_id"])
    assert not memory.assign_document_knowledge_base("kb-doc", other["id"], user["tenant_id"])


def test_public_document_gets_logical_public_knowledge_base(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    memory.register_document("public-doc", "公开标准.pdf", "标准", "general")
    with sqlite3.connect(memory._db_path) as conn:
        row = conn.execute(
            "SELECT knowledge_base_id FROM documents WHERE id=?", ("public-doc",)
        ).fetchone()
    assert row == ("kb-public-general",)


def test_document_lifecycle_and_version_record_are_separate_from_index_status(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    user = memory.register_user("reviewer@example.com", "Correct-Horse-22", "Reviewer")
    memory.register_document(
        "review-doc", "审核资料.md", "制度", "general", "tenant",
        user["tenant_id"], "", user["agent_id"],
    )
    memory.mark_document_indexed("review-doc", str(tmp_path / "missing.md"))
    document = memory.get_document("review-doc", user["tenant_id"])
    assert document["status"] == "indexed"
    assert document["lifecycle_status"] == "review"
    assert document["version"] == 1
    assert memory.list_document_versions("review-doc", user["tenant_id"])[0]["version"] == 1

    assert memory.update_document_lifecycle(
        "review-doc", "published", user["tenant_id"], "reviewer", "已完成人工审核",
    )
    assert memory.get_document("review-doc", user["tenant_id"])["lifecycle_status"] == "published"
    assert memory.list_document_versions("review-doc", user["tenant_id"])[0]["lifecycle_status"] == "published"


def test_document_lifecycle_rejects_invalid_status(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    memory.register_document("status-doc", "状态.md", "制度", "general")
    try:
        memory.update_document_lifecycle("status-doc", "released")
    except ValueError as exc:
        assert "状态" in str(exc)
    else:
        raise AssertionError("invalid lifecycle status must fail")


def test_document_version_rollback_creates_new_version_and_expiry_is_scoped(tmp_path):
    memory = ConversationMemory(str(tmp_path / "document-lifecycle.db"))
    owner = memory.register_user("lifecycle-owner@example.com", "Correct-Horse-34", "Lifecycle Owner")
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("第一版", encoding="utf-8")
    second.write_text("第二版", encoding="utf-8")
    memory.register_document("versioned-doc", "版本文档.md", "制度", "general",
                             visibility="tenant", tenant_id=owner["tenant_id"])
    memory.mark_document_indexed("versioned-doc", str(first))
    memory.create_document_version("versioned-doc", str(second), owner["tenant_id"],
                                  lifecycle_status="review", change_reason="替换内容", created_by=owner["id"])
    rolled = memory.rollback_document_version("versioned-doc", 1, owner["tenant_id"], owner["id"])
    assert rolled["version"] == 3
    assert rolled["cleaned_path"] == str(first)
    assert len(memory.list_document_versions("versioned-doc", owner["tenant_id"])) == 3

    with sqlite3.connect(memory._db_path) as conn:
        conn.execute("UPDATE documents SET lifecycle_status='published', expiry_date='2020-01-01' WHERE id=?",
                     ("versioned-doc",))
    assert memory.expire_documents(owner["tenant_id"], owner["id"]) == 1
    assert memory.get_document("versioned-doc", owner["tenant_id"])["lifecycle_status"] == "expired"


def test_knowledge_base_explicit_grants_are_enforced_for_agent_access(tmp_path):
    memory = ConversationMemory(str(tmp_path / "knowledge-base-grants.db"))
    owner = memory.register_user("grant-owner@example.com", "Correct-Horse-35", "Grant Owner")
    member = memory.register_user("grant-member@example.com", "Correct-Horse-36", "Grant Member")
    kb = memory.create_knowledge_base(owner["tenant_id"], "受控知识库", visibility="tenant")
    assert memory.can_access_knowledge_base(kb["id"], owner["tenant_id"], member["id"], member["agent_id"])
    assert memory.grant_knowledge_base_access(kb["id"], owner["tenant_id"], owner["id"], member["agent_id"])
    assert memory.can_access_knowledge_base(kb["id"], owner["tenant_id"], owner["id"], member["agent_id"])
    assert not memory.can_access_knowledge_base(kb["id"], owner["tenant_id"], member["id"], member["agent_id"])
    assert memory.revoke_knowledge_base_access(kb["id"], owner["tenant_id"], owner["id"], member["agent_id"])
    assert memory.list_knowledge_base_grants(kb["id"], owner["tenant_id"]) == []


def test_ingestion_job_persists_pipeline_steps_and_summary(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    user = memory.register_user("pipeline@example.com", "Correct-Horse-23", "Pipeline")
    memory.register_document("pipeline-doc", "管线.md", "制度", "general")
    memory.create_ingestion_job(
        "job-1", user["tenant_id"], user["id"], user["agent_id"],
        [{"document_id": "pipeline-doc", "source_name": "管线.md"}],
    )
    assert memory.update_ingestion_item("job-1", "pipeline-doc", "parsing", "processing")
    assert memory.update_ingestion_item("job-1", "pipeline-doc", "parsing", "completed")
    assert memory.update_ingestion_item("job-1", "pipeline-doc", "cleaning", "completed")
    assert memory.update_ingestion_item("job-1", "pipeline-doc", "done", "completed")
    assert memory.finish_ingestion_job("job-1", "completed", 1, 0)

    job = memory.get_ingestion_job("job-1")
    assert job["status"] == "completed"
    assert job["success_count"] == 1
    assert job["items"][0]["stage"] == "done"
    assert job["items"][0]["status"] == "completed"


def test_ingestion_trace_preserves_metrics_failures_and_retry_history(tmp_path):
    memory = ConversationMemory(str(tmp_path / "ingestion-trace.db"))
    owner = memory.register_user("trace-owner@example.com", "Correct-Horse-31", "Trace Owner")
    other = memory.register_user("trace-other@example.com", "Correct-Horse-32", "Trace Other")
    memory.create_ingestion_job(
        "trace-job", owner["tenant_id"], owner["id"], owner["agent_id"],
        [{"document_id": "trace-doc", "source_name": "安全文档.md"}],
    )
    memory.record_ingestion_stage_event(
        "trace-job", "trace-doc", "parsing", "processing",
        {"file_type": ".docx", "raw_characters": 1234},
    )
    memory.record_ingestion_stage_event(
        "trace-job", "trace-doc", "parsing", "failed",
        error="格式不支持", error_type="UnsupportedFormat",
    )
    memory.record_ingestion_stage_event(
        "trace-job", "trace-doc", "parsing", "processing",
        {"file_type": ".docx", "raw_characters": 1234},
    )
    memory.record_ingestion_stage_event(
        "trace-job", "trace-doc", "parsing", "completed",
        {"file_type": ".docx", "raw_characters": 1234},
    )

    trace = memory.list_ingestion_stage_events("trace-job", owner["tenant_id"])
    assert [event["status"] for event in trace] == ["failed", "completed"]
    assert [event["attempt"] for event in trace] == [1, 2]
    assert trace[-1]["metrics"]["raw_characters"] == 1234
    assert trace[0]["error_type"] == "UnsupportedFormat"
    assert trace[-1]["duration_ms"] >= 0
    assert memory.list_ingestion_stage_events("trace-job", other["tenant_id"]) == []

    job = memory.get_ingestion_job("trace-job")
    assert len(job["trace"]) == 2
    assert job["items"][0]["trace"][1]["attempt"] == 2


def test_legacy_ingestion_job_returns_empty_trace(tmp_path):
    memory = ConversationMemory(str(tmp_path / "legacy-ingestion.db"))
    owner = memory.register_user("legacy-ingestion@example.com", "Correct-Horse-33", "Legacy")
    memory.create_ingestion_job(
        "legacy-job", owner["tenant_id"], owner["id"], owner["agent_id"],
        [{"document_id": "legacy-doc", "source_name": "旧任务.md"}],
    )
    job = memory.get_ingestion_job("legacy-job")
    assert job["trace"] == []
    assert job["items"][0]["trace"] == []


def test_data_source_registry_is_scoped_and_tracks_sync_state(tmp_path):
    memory = ConversationMemory(str(tmp_path / "governance.db"))
    owner = memory.register_user("source-owner@example.com", "Correct-Horse-24", "Source Owner")
    kb = memory.create_knowledge_base(owner["tenant_id"], "外部资料库")
    source = memory.create_data_source(
        owner["tenant_id"], "安全公告 URL", "url", "https://example.com/advisories",
        {"timeout_seconds": 10}, kb["id"], owner["id"], "scheduled", "0 3 * * *",
    )
    assert source["status"] == "draft"
    assert source["config"]["timeout_seconds"] == 10
    assert memory.list_data_sources(owner["tenant_id"])[0]["id"] == source["id"]
    assert memory.mark_data_source_synced(source["id"], "hash-1", owner["tenant_id"])
    assert memory.get_data_source(source["id"], owner["tenant_id"])["status"] == "active"
    assert memory.get_data_source(source["id"], owner["tenant_id"])["last_content_hash"] == "hash-1"

    other = memory.register_user("source-other@example.com", "Correct-Horse-25", "Other")
    assert memory.list_data_sources(other["tenant_id"]) == []
    assert not memory.update_data_source_status(source["id"], "paused", other["tenant_id"])


def test_source_document_is_staged_with_review_metadata(tmp_path):
    memory = ConversationMemory(str(tmp_path / "staged-source.db"))
    user = memory.register_user("staged-source@example.com", "Correct-Horse-28", "Staged Source")
    source = memory.create_data_source(
        user["tenant_id"], "公告源", "url", "https://example.com/advisories",
    )
    staged_path = tmp_path / "staging" / "advisory.txt"
    staged_path.parent.mkdir()
    staged_path.write_text("advisory", encoding="utf-8")
    doc = memory.create_staged_document_from_source(
        "doc-staged-source", "advisory.txt", str(staged_path),
        {"source_id": source["id"], "content_hash": "hash-1", "sync_run_id": "sync-1"},
        tenant_id=user["tenant_id"], visibility="tenant",
    )
    assert doc["status"] == "staged"
    assert doc["lifecycle_status"] == "review"
    assert doc["metadata"]["content_hash"] == "hash-1"
    assert memory.get_document("doc-staged-source", user["tenant_id"])["metadata"]["source_id"] == source["id"]


def test_ingestion_failures_are_classified_and_scoped(tmp_path):
    memory = ConversationMemory(str(tmp_path / "failure-list.db"))
    user = memory.register_user("failure-owner@example.com", "Correct-Horse-29", "Failure Owner")
    memory.create_ingestion_job(
        "job-failure", user["tenant_id"], user["id"], user["agent_id"],
        [{"document_id": "doc-failure", "source_name": "失败文档.md"}],
    )
    memory.update_ingestion_item("job-failure", "doc-failure", "parsing", "failed", "解析失败：格式不支持")
    failures = memory.list_ingestion_failures(user["tenant_id"])
    assert failures[0]["failure_type"] == "parsing"
    assert failures[0]["document_id"] == "doc-failure"


def test_knowledge_base_retrieval_config_is_versioned_and_flags_rebuild(tmp_path):
    memory = ConversationMemory(str(tmp_path / "retrieval-config.db"))
    user = memory.register_user("retrieval-owner@example.com", "Correct-Horse-30", "Retrieval Owner")
    kb = memory.create_knowledge_base(user["tenant_id"], "检索配置库")
    default = memory.get_knowledge_base_retrieval_config(kb["id"], user["tenant_id"])
    assert default["version"] == 0
    saved = memory.update_knowledge_base_retrieval_config(
        kb["id"], {"top_k": 15, "use_rerank": False, "chunk_size": 500},
        user["tenant_id"], user["id"], "调整检索参数",
    )
    assert saved["version"] == 1
    assert saved["config"]["top_k"] == 15
    assert saved["config"]["rebuild_required"] is True
    versions = memory.list_knowledge_base_retrieval_config_versions(kb["id"], user["tenant_id"])
    assert versions[0]["version"] == 1
    assert versions[0]["status"] == "active"


def test_conversation_keeps_selected_knowledge_base(tmp_path):
    memory = ConversationMemory(str(tmp_path / "conversation-kb.db"))
    user = memory.register_user("conversation-kb@example.com", "Correct-Horse-31", "Conversation KB")
    kb = memory.create_knowledge_base(user["tenant_id"], "会话资料库")
    conv = memory.create_conversation(
        tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"],
        knowledge_base_id=kb["id"],
    )
    assert conv["knowledge_base_id"] == kb["id"]
    assert memory.get_conversation_knowledge_base(conv["id"]) == kb["id"]


def test_semantic_cache_is_scoped_and_expires(tmp_path):
    memory = ConversationMemory(str(tmp_path / "semantic-cache.db"))
    memory.put_semantic_cache(
        "key-a", "网络安全 问题", "答案", [{"file_name": "a.md"}],
        "model-a", "kb-a", "general", "prompt-1", ttl_seconds=60,
    )
    hit = memory.get_semantic_cache("key-a")
    assert hit["answer"] == "答案"
    assert hit["knowledge_base_id"] == "kb-a"
    assert hit["hit_count"] == 1
    assert memory.semantic_cache_stats()["hits"] == 1
    assert memory.clear_semantic_cache("kb-b") == 0
    assert memory.clear_semantic_cache("kb-a") == 1
    assert memory.get_semantic_cache("key-a") is None
def test_feedback_records_and_maps_copy_to_five_star(tmp_path):
    memory = ConversationMemory(str(tmp_path / "feedback-gaps.db"))
    user = memory.register_user("feedback@example.com", "Correct-Horse-32", "Feedback")
    conv = memory.create_conversation(
        tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"],
    )
    msg_id = memory.add_message(conv["id"], "user", "数据安全法适用范围是什么？")
    memory.log_usage(conv["id"], msg_id, "数据安全法适用范围是什么？", returned_count=2)
    fb_id = memory.record_feedback(msg_id, "copy", user_id=user["id"])
    assert fb_id is not None
    with sqlite3.connect(memory._db_path) as conn:
        rating = conn.execute(
            "SELECT user_rating FROM usage_logs WHERE message_id=?", (msg_id,)
        ).fetchone()[0]
    assert rating == 5
    assert memory.list_feedback_items()[0]["feedback_type"] == "copy"


def test_refresh_feedback_clusters_into_knowledge_gap(tmp_path):
    memory = ConversationMemory(str(tmp_path / "gap-cluster.db"))
    user = memory.register_user("gap-owner@example.com", "Correct-Horse-33", "Gap Owner")
    conv = memory.create_conversation(
        tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"],
    )
    msg1 = memory.add_message(conv["id"], "user", "等保三级基本要求有哪些？")
    msg2 = memory.add_message(conv["id"], "user", "等保三级的基本要求是什么？")
    memory.log_usage(conv["id"], msg1, "等保三级基本要求有哪些？", returned_count=0)
    memory.log_usage(
        conv["id"], msg2, "等保三级的基本要求是什么？",
        returned_count=1, documents=[{"file_name": "baseline.md"}],
    )
    memory.record_feedback(msg2, "refresh", "回答太简短", user_id=user["id"])
    gaps = memory.get_knowledge_gaps()
    assert gaps["summary"]["open_count"] == 1
    gap = gaps["items"][0]
    assert gap["occurrence_count"] == 3
    assert gap["low_rating_count"] == 1
    assert gap["refresh_count"] == 1
    assert gap["no_source_count"] == 1
    assert gap["profile"] == "general"
    assert "refresh" in gap["reason_tags"]


def test_knowledge_gap_status_and_supply_task_flow(tmp_path):
    memory = ConversationMemory(str(tmp_path / "gap-task.db"))
    user = memory.register_user("gap-task@example.com", "Correct-Horse-34", "Gap Task")
    conv = memory.create_conversation(
        tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"],
    )
    msg_id = memory.add_message(conv["id"], "user", "云上等保测评流程是什么？")
    memory.log_usage(conv["id"], msg_id, "云上等保测评流程是什么？", returned_count=0)
    memory.rebuild_knowledge_gaps()
    gap_id = memory.get_knowledge_gaps()["items"][0]["id"]
    task = memory.create_gap_supply_task(gap_id, created_by="admin")
    assert task["title"].startswith("补充资料")
    assert memory.list_gap_supply_tasks()[0]["id"] == task["id"]
    assert memory.update_gap_supply_task_status(task["id"], "done")
    assert memory.list_gap_supply_tasks(status="done")[0]["id"] == task["id"]
    assert memory.update_knowledge_gap_status(gap_id, "resolved", "已补资料")
    assert memory.get_knowledge_gaps(status="resolved")["items"][0]["note"] == "已补资料"


def test_feedback_marks_trace_and_promotes_gap_to_prompt_test(tmp_path):
    memory = ConversationMemory(str(tmp_path / "feedback-prompt-test.db"))
    user = memory.register_user("feedback-prompt@example.com", "Correct-Horse-35", "Feedback Prompt")
    conv = memory.create_conversation(
        tenant_id=user["tenant_id"], user_id=user["id"], agent_id=user["agent_id"],
    )
    msg_id = memory.add_message(conv["id"], "user", "数据分类分级如何落地？")
    memory.log_usage(
        conv["id"], msg_id, "数据分类分级如何落地？", returned_count=0,
        trace_data={"steps": [{"step": "retrieve"}]},
    )
    memory.record_feedback(msg_id, "correction", "缺少实施步骤", user_id=user["id"])
    with sqlite3.connect(memory._db_path) as conn:
        trace_json = conn.execute(
            "SELECT trace_data FROM usage_logs WHERE message_id=?", (msg_id,)
        ).fetchone()[0]
    trace = __import__("json").loads(trace_json)
    assert trace["feedback"][0]["type"] == "correction"
    gap = memory.get_knowledge_gaps()["items"][0]
    promoted = memory.promote_knowledge_gap_to_prompt_test(gap["id"], "admin")
    assert promoted["created"] is True
    assert memory.get_test_items(promoted["set_id"])[0]["query"] == gap["canonical_question"]
    assert "feedback_gap:" in memory.get_test_items(promoted["set_id"])[0]["category"]
    duplicate = memory.promote_knowledge_gap_to_prompt_test(gap["id"], "admin")
    assert duplicate["created"] is False


def test_external_retrieval_config_requires_explicit_approval(tmp_path):
    memory = ConversationMemory(str(tmp_path / "external-config.db"))
    config = memory.get_external_retrieval_config()
    assert config["enabled"] is False
    assert config["trigger_mode"] == "empty_only"
    saved = memory.save_external_retrieval_config(
        {"enabled": True, "trigger_mode": "low_confidence", "max_sources": 2}, "admin",
    )
    assert saved["enabled"] is True
    assert saved["trigger_mode"] == "low_confidence"
    source = memory.upsert_external_retrieval_source({
        "name": "公开安全公告", "source_type": "url",
        "endpoint": "https://example.com/advisories", "enabled": False, "approved": False,
    })
    assert source["approved"] is False
    assert memory.list_external_retrieval_sources(include_disabled=False) == []
    assert memory.update_external_retrieval_source_status(source["id"], approved=True, enabled=True)
    assert memory.list_external_retrieval_sources(include_disabled=False)[0]["id"] == source["id"]


def test_external_retrieval_events_are_auditable(tmp_path):
    memory = ConversationMemory(str(tmp_path / "external-events.db"))
    event_id = memory.log_external_retrieval_event({
        "trace_id": "trace-test", "conversation_id": "conv-test", "tenant_id": "tenant-test",
        "user_id": "user-test", "query": "最新安全公告", "source_id": "ext-test",
        "source_url": "https://example.com/advisories", "content_hash": "hash-test",
        "status": "succeeded", "result_count": 1,
    })
    assert event_id > 0
    event = memory.list_external_retrieval_events()[0]
    assert event["trace_id"] == "trace-test"
    assert event["content_hash"] == "hash-test"
    assert event["result_count"] == 1


def test_external_app_credentials_scopes_and_usage(tmp_path):
    memory = ConversationMemory(str(tmp_path / "external-app.db"))
    owner = memory.register_user("external-app@example.com", "Correct-Horse-30", "External App")
    app = memory.create_external_app(
        owner["tenant_id"], "企业门户", ["chat", "usage"], 30, "admin", owner["agent_id"],
    )
    assert app["app_secret"].startswith("sns_")
    auth = memory.authenticate_external_app(app["app_key"], app["app_secret"])
    assert auth["tenant_id"] == owner["tenant_id"]
    assert auth["agent_id"] == owner["agent_id"]
    assert "chat" in auth["scopes"]
    assert memory.authenticate_external_app(app["app_key"], "wrong-secret") is None
    usage_id = memory.record_external_app_usage(
        app["id"], owner["tenant_id"], "user-1", "/api/external/chat", "succeeded",
        duration_ms=123, prompt_tokens=10, completion_tokens=20, request_id="req-1",
    )
    assert usage_id > 0
    usage = memory.list_external_app_usage(owner["tenant_id"])[0]
    assert usage["app_id"] == app["id"]
    assert usage["prompt_tokens"] == 10
    assert memory.update_external_app_status(app["id"], False)
    assert memory.authenticate_external_app(app["app_key"], app["app_secret"]) is None


def test_external_app_requires_active_agent_binding(tmp_path):
    memory = ConversationMemory(str(tmp_path / "external-app-binding.db"))
    owner = memory.register_user("external-binding@example.com", "Correct-Horse-30", "External Binding")
    app = memory.create_external_app(owner["tenant_id"], "企业门户", agent_id=owner["agent_id"])
    with sqlite3.connect(memory._db_path) as conn:
        conn.execute("UPDATE external_apps SET agent_id='' WHERE id=?", (app["id"],))
    assert memory.authenticate_external_app(app["app_key"], app["app_secret"]) is None
    assert not memory.update_external_app_agent(app["id"], owner["tenant_id"], "other-agent")
    assert memory.update_external_app_agent(app["id"], owner["tenant_id"], owner["agent_id"])
    assert memory.authenticate_external_app(app["app_key"], app["app_secret"])["agent_id"] == owner["agent_id"]
