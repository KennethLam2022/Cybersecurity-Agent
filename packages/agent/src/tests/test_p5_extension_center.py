import pytest

from memory import ConversationMemory


def _active_agent(memory: ConversationMemory):
    user = memory.register_user("admin@example.com", "Correct-Horse-16", "Admin")
    return user, memory.create_agent(user["tenant_id"], "Writing Agent")


def test_extension_defaults_to_pending_review_and_cannot_be_granted(tmp_path):
    memory = ConversationMemory(str(tmp_path / "extensions.db"))
    user, agent = _active_agent(memory)
    extension = memory.create_capability_extension(
        "skill", "Controlled writer", "1.0.0", "approved-registry/writer",
        permissions=["read:authorized_rag"],
    )

    assert extension["status"] == "pending_review"
    with pytest.raises(ValueError, match="审核通过"):
        memory.set_capability_extension_grant(
            extension["id"], user["tenant_id"], agent["id"], True,
        )


def test_approved_extension_can_be_scoped_to_one_agent_and_observed(tmp_path):
    memory = ConversationMemory(str(tmp_path / "extensions.db"))
    user, allowed_agent = _active_agent(memory)
    other_agent = memory.create_agent(user["tenant_id"], "Other Agent")
    extension = memory.create_capability_extension(
        "mcp", "Threat intelligence MCP", "1.0.0", "https://mcp.example.test",
        network_scope="Only approved metadata leaves the workspace.",
    )
    assert memory.review_capability_extension(extension["id"], "approved", "platform_admin")
    assert memory.set_capability_extension_grant(
        extension["id"], user["tenant_id"], allowed_agent["id"], True, "platform_admin",
    )
    assert memory.record_capability_extension_call(
        extension["id"], user["tenant_id"], allowed_agent["id"], "success", 120,
    )
    assert not memory.record_capability_extension_call(
        extension["id"], user["tenant_id"], other_agent["id"], "success", 120,
    )

    usage = memory.capability_extension_usage(extension["id"])
    assert usage["calls"] == 1
    assert usage["success_rate"] == 100.0
    assert usage["p95_duration_ms"] == 120
    assert usage["grants"][0]["agent_id"] == allowed_agent["id"]


def test_mcp_registration_rejects_insecure_endpoints_and_plaintext_credentials(tmp_path):
    memory = ConversationMemory(str(tmp_path / "extensions.db"))
    with pytest.raises(ValueError, match="HTTPS"):
        memory.create_capability_extension("mcp", "Bad MCP", "1", "http://mcp.example.test", network_scope="none")
    with pytest.raises(ValueError, match="凭证"):
        memory.create_capability_extension(
            "mcp", "Credential MCP", "1", "https://mcp.example.test",
            manifest={"api_key": "do-not-store"}, network_scope="metadata only",
        )


def test_extension_health_check_is_metadata_only_and_audited(tmp_path):
    memory = ConversationMemory(str(tmp_path / "extension-health.db"))
    extension = memory.create_capability_extension(
        "mcp", "Health MCP", "1", "https://mcp.example.test",
        manifest={"tools": ["search"], "timeout_seconds": 15},
        network_scope="approved metadata only",
    )
    health = memory.capability_extension_health(extension["id"])
    assert health["ok"] is True
    assert health["mode"] == "metadata_only"
    assert "extension.health_check" in [
        item["action"] for item in memory.list_audit_logs("platform", limit=20)
    ]


def test_enabled_extensions_are_visible_only_to_the_granted_agent(tmp_path):
    memory = ConversationMemory(str(tmp_path / "extensions.db"))
    user, allowed_agent = _active_agent(memory)
    other_agent = memory.create_agent(user["tenant_id"], "Other Agent")
    extension = memory.create_capability_extension(
        "skill", "Approved local writer", "1.0", "approved-registry/writer",
    )
    memory.review_capability_extension(extension["id"], "approved", "platform_admin")
    memory.set_capability_extension_grant(
        extension["id"], user["tenant_id"], allowed_agent["id"], True, "platform_admin",
    )
    assert [item["id"] for item in memory.list_enabled_capability_extensions(
        user["tenant_id"], allowed_agent["id"],
    )] == [extension["id"]]
    assert memory.list_enabled_capability_extensions(
        user["tenant_id"], other_agent["id"],
    ) == []


def test_extension_upgrade_disables_grants_and_requires_fresh_review(tmp_path):
    memory = ConversationMemory(str(tmp_path / "extensions.db"))
    user, scoped_agent = _active_agent(memory)
    extension = memory.create_capability_extension("skill", "Writer", "1.0", "registry/writer")
    memory.review_capability_extension(extension["id"], "approved", "platform_admin")
    memory.set_capability_extension_grant(extension["id"], user["tenant_id"], scoped_agent["id"], True)
    upgraded = memory.update_capability_extension(extension["id"], "2.0", "registry/writer", changed_by="platform_admin")
    assert upgraded["status"] == "pending_review"
    assert memory.list_enabled_capability_extensions(user["tenant_id"], scoped_agent["id"]) == []


def test_extension_usage_and_grants_can_be_limited_to_one_tenant(tmp_path):
    memory = ConversationMemory(str(tmp_path / "extensions.db"))
    first_user, first_agent = _active_agent(memory)
    second_user = memory.register_user("other@example.com", "Correct-Horse-16", "Other")
    second_agent = memory.create_agent(second_user["tenant_id"], "Other Tenant Agent")
    extension = memory.create_capability_extension(
        "skill", "Tenant scoped skill", "1.0", "approved-registry/tenant-skill",
    )
    memory.review_capability_extension(extension["id"], "approved", "platform_admin")
    for user, scoped_agent in ((first_user, first_agent), (second_user, second_agent)):
        assert memory.set_capability_extension_grant(
            extension["id"], user["tenant_id"], scoped_agent["id"], True, "platform_admin",
        )
        assert memory.record_capability_extension_call(
            extension["id"], user["tenant_id"], scoped_agent["id"], "success", 100,
        )

    usage = memory.capability_extension_usage(extension["id"], first_user["tenant_id"])
    assert usage["calls"] == 1
    assert {item["tenant_id"] for item in usage["grants"]} == {first_user["tenant_id"]}
    assert {item["tenant_id"] for item in usage["recent_calls"]} == {first_user["tenant_id"]}


def test_builtin_ppt_skills_are_seeded_but_not_auto_granted(tmp_path):
    memory = ConversationMemory(str(tmp_path / "builtin-ppt-skills.db"))
    user = memory.register_user("builtin-ppt@example.com", "Correct-Horse-30", "Builtin PPT")
    memory.ensure_builtin_capability_extensions()
    skills = {item["id"]: item for item in memory.list_capability_extensions("skill")}
    assert "builtin-skill-security-ppt" in skills
    assert "builtin-skill-critical-infrastructure-ppt" in skills
    assert skills["builtin-skill-critical-infrastructure-ppt"]["status"] == "approved"
    assert memory.list_enabled_capability_extensions(user["tenant_id"], user["agent_id"]) == []
