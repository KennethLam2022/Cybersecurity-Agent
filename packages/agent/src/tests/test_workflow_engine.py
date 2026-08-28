from memory import ConversationMemory
from workflow_engine import workflow_templates, execute_workflow


def _workflow(memory: ConversationMemory):
    user = memory.register_user("workflow-owner@example.com", "Correct-Horse-30", "Workflow Owner")
    template = next(item for item in workflow_templates() if item["key"] == "report_generation")
    workflow = memory.create_workflow_definition(
        user["tenant_id"], user["agent_id"], template["name"], template["description"],
        template["nodes"], template["edges"], user["id"], "test template",
    )
    assert memory.publish_workflow_version(
        workflow["id"], user["tenant_id"], user["agent_id"], 1, user["id"],
    )
    return user, workflow


def test_workflow_versioning_publication_and_workspace_isolation(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow.db"))
    user, workflow = _workflow(memory)
    version = memory.get_workflow_version(workflow["id"], 1)
    updated = memory.save_workflow_version(
        workflow["id"], user["tenant_id"], user["agent_id"], version["nodes"], version["edges"],
        user["id"], "metadata update",
    )
    assert updated["current_version"] == 2
    assert updated["status"] == "draft"
    assert [item["version"] for item in memory.list_workflow_versions(
        workflow["id"], user["tenant_id"], user["agent_id"],
    )] == [2, 1]

    other = memory.register_user("workflow-other@example.com", "Correct-Horse-30", "Other")
    assert memory.get_workflow_definition(workflow["id"], other["tenant_id"], other["agent_id"]) is None
    assert memory.list_workflow_definitions(other["tenant_id"], other["agent_id"]) == []


def test_workflow_pauses_for_human_approval_then_records_ordered_trace(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow.db"))
    user, workflow = _workflow(memory)
    run = memory.create_workflow_run(
        workflow["id"], user["tenant_id"], user["agent_id"], 1,
        {"query": "起草网络安全管理制度，面向管理层，用于合规落地，适用通用组织"}, user["id"],
    )
    paused = execute_workflow(memory, run)
    assert paused["status"] == "awaiting_approval"
    assert paused["output"]["state"]["pending_node"] == "approval"
    assert memory.list_workflow_run_traces(paused["id"], user["tenant_id"], user["agent_id"])[-1]["status"] == "awaiting_approval"

    completed = execute_workflow(memory, paused, approval=True)
    traces = memory.list_workflow_run_traces(completed["id"], user["tenant_id"], user["agent_id"])
    assert completed["status"] == "completed"
    assert [item["sequence"] for item in traces] == [1, 2, 3, 4, 5, 6]
    assert all(item["duration_ms"] >= 0 for item in traces)
    assert traces[3]["status"] == "completed"


def test_workflow_rejects_unapproved_node_type_and_cycles(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow.db"))
    user = memory.register_user("workflow-invalid@example.com", "Correct-Horse-30", "Invalid")
    try:
        memory.create_workflow_definition(
            user["tenant_id"], user["agent_id"], "invalid", "", [{"id": "in", "type": "shell"}], [], user["id"],
        )
    except ValueError as exc:
        assert "不支持" in str(exc)
    else:
        raise AssertionError("unsupported node type must be rejected")

    try:
        memory.create_workflow_definition(
            user["tenant_id"], user["agent_id"], "cycle", "",
            [{"id": "in", "type": "input"}, {"id": "note", "type": "notification"}],
            [["in", "note"], ["note", "in"]], user["id"],
        )
    except ValueError as exc:
        assert "循环" in str(exc)
    else:
        raise AssertionError("cycle must be rejected")


def test_workflow_node_parameters_are_validated_and_applied(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow-config.db"))
    user = memory.register_user("workflow-config@example.com", "Correct-Horse-30", "Config")
    nodes = [
        {"id": "input", "type": "input", "label": "输入"},
        {"id": "retrieval", "type": "retrieval", "label": "检索", "config": {"top_k": 7}},
    ]
    workflow = memory.create_workflow_definition(
        user["tenant_id"], user["agent_id"], "config", "", nodes,
        [["input", "retrieval"]], user["id"], "参数测试",
    )
    run = memory.create_workflow_run(
        workflow["id"], user["tenant_id"], user["agent_id"], 1,
        {"query": "访问控制", "user_id": user["id"]}, user["id"],
    )
    completed = execute_workflow(memory, run)
    assert completed["status"] == "completed"
    assert completed["output"]["state"]["retrieval"]["config"]["top_k"] == 7

    try:
        memory.save_workflow_version(
            workflow["id"], user["tenant_id"], user["agent_id"],
            [{"id": "input", "type": "input"}, {"id": "retrieval", "type": "retrieval", "config": {"top_k": 99}}],
            [["input", "retrieval"]], user["id"], "非法参数",
        )
    except ValueError as exc:
        assert "top_k" in str(exc)
    else:
        raise AssertionError("out-of-range top_k must be rejected")


def test_real_execution_retrieves_scoped_evidence_and_creates_controlled_artifact(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "workflow-real.db"))
    user, workflow = _workflow(memory)

    class Retriever:
        def search_multi(self, *args, **kwargs):
            assert kwargs["access_scope"] == {
                "tenant_id": user["tenant_id"], "user_id": user["id"], "agent_id": user["agent_id"],
            }
            return [{"document_id": "doc-1", "display_name": "授权制度", "section": "访问控制",
                     "profile": "general", "visibility": "tenant", "score": 0.9}]

    class Agent:
        retriever = Retriever()

    import generation_manager
    rendered = {}
    monkeypatch.setattr(generation_manager, "artifact_path", lambda *args: tmp_path / "draft.docx")
    monkeypatch.setattr(generation_manager, "render_artifact", lambda mode, outline, path, version, references: rendered.update({
        "mode": mode, "path": str(path), "references": references,
    }))

    run = memory.create_workflow_run(
        workflow["id"], user["tenant_id"], user["agent_id"], 1,
        {"query": "起草网络安全管理制度，面向管理层，用于合规落地，适用通用组织", "user_id": user["id"],
         "fields": {"document_type": "网络安全制度", "audience": "管理层", "purpose": "合规落地", "scope": "通用组织"}},
        user["id"],
    )
    paused = execute_workflow(memory, run, agent=Agent())
    assert paused["status"] == "awaiting_approval"
    completed = execute_workflow(memory, paused, approval=True, agent=Agent())
    assert completed["status"] == "completed"
    result = completed["output"]["result"]
    assert result["status"] == "artifact_created"
    assert result["references"][0]["document_id"] == "doc-1"
    assert rendered["mode"] == "writing"


def test_notification_node_uses_auditable_publisher(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow-notify.db"))
    user, workflow = _workflow(memory)
    events = []
    run = memory.create_workflow_run(
        workflow["id"], user["tenant_id"], user["agent_id"], 1,
        {"query": "起草网络安全管理制度，面向管理层，用于合规落地，适用通用组织", "user_id": user["id"]},
        user["id"],
    )
    paused = execute_workflow(memory, run)
    completed = execute_workflow(
        memory, paused, approval=True,
        notifier=lambda event_type, payload: events.append((event_type, payload)) or {"notification_id": "n-1"},
    )
    assert completed["status"] == "completed"
    assert events and events[0][0] == "workflow.completed"
    assert completed["output"]["state"]["notification"]["published"] is True


def test_workflow_template_catalog_supports_copy_and_org_sharing(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow-templates.db"))
    owner = memory.register_user("template-owner@example.com", "Correct-Horse-30", "Owner")
    peer = memory.register_user("template-peer@example.com", "Correct-Horse-30", "Peer")
    builtins = memory.list_workflow_templates(owner["tenant_id"], viewer_id=owner["id"])
    report = next(item for item in builtins if item["template_key"] == "report_generation")
    assert report["visibility"] == "platform"

    private = memory.create_workflow_template(
        owner["tenant_id"], "custom-review", "自定义审核", "组织审核流程",
        report["nodes"], report["edges"], owner["id"], "private",
    )
    assert private["id"] in {item["id"] for item in memory.list_workflow_templates(
        owner["tenant_id"], viewer_id=owner["id"]
    )}
    assert private["id"] not in {item["id"] for item in memory.list_workflow_templates(
        owner["tenant_id"], viewer_id=peer["id"]
    )}
    shared = memory.share_workflow_template(private["id"], owner["tenant_id"], owner["id"])
    assert shared["visibility"] == "org"
    assert shared["id"] in {item["id"] for item in memory.list_workflow_templates(
        owner["tenant_id"], viewer_id=peer["id"]
    )}

    copied = memory.copy_workflow_template(
        shared["id"], owner["tenant_id"], owner["agent_id"], owner["id"], "复制的审核流程"
    )
    assert copied["name"] == "复制的审核流程"
    assert copied["description"] == "组织审核流程"


def test_workflow_release_contract_requires_approval_before_generation(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow-contract.db"))
    user = memory.register_user("contract-owner@example.com", "Correct-Horse-30", "Owner")
    nodes = [
        {"id": "input", "type": "input"},
        {"id": "generation", "type": "generation"},
    ]
    workflow = memory.create_workflow_definition(
        user["tenant_id"], user["agent_id"], "unsafe", "", nodes,
        [["input", "generation"]], user["id"],
    )
    try:
        memory.publish_workflow_version(workflow["id"], user["tenant_id"], user["agent_id"], 1, user["id"])
    except ValueError as exc:
        assert "人工审批" in str(exc)
    else:
        raise AssertionError("generation without approval must not be published")


def test_workflow_tool_gate_executes_granted_builtin_skill_only_after_approval(tmp_path):
    memory = ConversationMemory(str(tmp_path / "workflow-tool.db"))
    user = memory.register_user("workflow-tool@example.com", "Correct-Horse-30", "Tool Owner")
    extension = memory.create_capability_extension(
        "skill", "Outline Skill", "1.0", "builtin://outline",
        manifest={"runtime": "builtin", "operation": "security_outline"},
    )
    memory.review_capability_extension(extension["id"], "approved", user["id"])
    memory.set_capability_extension_grant(
        extension["id"], user["tenant_id"], user["agent_id"], True, user["id"],
    )
    nodes = [
        {"id": "input", "type": "input"},
        {"id": "approval", "type": "human_approval"},
        {"id": "tool", "type": "tool_gate",
         "config": {"extension_id": extension["id"]}},
        {"id": "notify", "type": "notification"},
    ]
    workflow = memory.create_workflow_definition(
        user["tenant_id"], user["agent_id"], "tool workflow", "",
        nodes, [["input", "approval"], ["approval", "tool"], ["tool", "notify"]],
        user["id"], "tool test",
    )
    memory.publish_workflow_version(
        workflow["id"], user["tenant_id"], user["agent_id"], 1, user["id"],
    )
    run = memory.create_workflow_run(
        workflow["id"], user["tenant_id"], user["agent_id"], 1,
        {"query": "检查访问控制", "user_id": user["id"]}, user["id"],
    )
    paused = execute_workflow(memory, run)
    assert paused["status"] == "awaiting_approval"
    assert memory.capability_extension_usage(extension["id"])["calls"] == 0
    completed = execute_workflow(memory, paused, approval=True)
    assert completed["status"] == "completed"
    assert completed["output"]["state"]["tool_gate"]["execution"]["status"] == "success"
    assert memory.capability_extension_usage(extension["id"])["calls"] == 1
