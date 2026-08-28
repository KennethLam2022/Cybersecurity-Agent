import pytest

from governance_store import GovernanceStore


def test_industry_profile_grants_are_disabled_by_default_and_tenant_scoped(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    assert store.enabled_profiles("tenant-a") == {"general"}

    grant = store.set_industry_profile_grant(
        "tenant-a", "industry/finance", True, "registry-2026.08", "admin-a", "approved for finance cyber security",
    )
    assert grant["enabled"] is True
    assert grant["profile_version"] == "registry-2026.08"
    assert store.enabled_profiles("tenant-a") == {"general", "industry/finance"}
    assert store.enabled_profiles("tenant-b") == {"general"}
    assert any(item["action"] == "industry_profile.grant" for item in store.list_audit("tenant-a"))

    store.set_industry_profile_grant("tenant-a", "industry/finance", False, "registry-2026.08", "admin-a")
    assert store.enabled_profiles("tenant-a") == {"general"}


def test_industry_profile_grants_reject_non_industry_profiles(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    with pytest.raises(ValueError, match="industry/"):
        store.set_industry_profile_grant("tenant-a", "general", True, "v1", "admin-a")


def test_secret_metadata_never_contains_value_and_rotation_is_audited(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    created = store.create_secret("tenant-a", "smtp", "smtp", "first-secret", "admin-a", "org_admin")
    assert "value" not in created
    assert "encrypted_value" not in created

    rotated = store.rotate_secret("tenant-a", created["id"], "second-secret", "admin-a", "org_admin", "scheduled")
    assert rotated["current_version"] == 2
    assert store.get_secret("tenant-a", created["id"], "service", "system", include_value=True)["value"] == "second-secret"
    assert len(store.secret_versions("tenant-a", created["id"])) == 2
    assert any(row["action"] == "secret.rotate" for row in store.list_audit("tenant-a"))


def test_secret_is_tenant_scoped_and_revoked_value_cannot_be_resolved(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    created = store.create_secret("tenant-a", "mcp-key", "mcp", "value", "admin-a", "org_admin")
    with pytest.raises(ValueError):
        store.secret_versions("tenant-b", created["id"])
    store.revoke_secret("tenant-a", created["id"], "admin-a", "org_admin")
    with pytest.raises(ValueError, match="已撤销"):
        store.get_secret("tenant-a", created["id"], "service", "system", include_value=True)


def test_mcp_policy_defaults_to_deny_and_audits_redacted_parameters(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    secret = store.create_secret("tenant-a", "mcp-token", "mcp", "token", "admin", "org_admin")
    server = store.register_mcp_server("tenant-a", "intel", "https://mcp.example.test", "admin", secret_ref_id=secret["id"], auth_type="bearer")
    store.upsert_mcp_tool("tenant-a", server["id"], "search", "lookup", {"type": "object"}, False, "admin", "org_admin")
    assert not store.authorize_mcp_call("tenant-a", server["id"], "search", user_id="u", role="org_admin", agent_id="a", params={"query": "x"})
    store.set_mcp_status("tenant-a", server["id"], "enabled", "admin", "org_admin")
    store.set_mcp_tool_policy("tenant-a", server["id"], "search", allowed_roles=["org_admin"], allowed_agents=["a"], param_allowlist=["query"], param_denylist=["token"], enabled=True, actor_user_id="admin", actor_role="org_admin")
    assert store.authorize_mcp_call("tenant-a", server["id"], "search", user_id="u", role="org_admin", agent_id="a", params={"query": "x"})
    assert not store.authorize_mcp_call("tenant-a", server["id"], "search", user_id="u", role="org_admin", agent_id="a", params={"token": "secret"})
    store.record_mcp_call("tenant-a", server["id"], "search", "u", "a", {"query": "x", "token": "secret"}, "ok", 12)
    with store._connect() as conn:
        summary = conn.execute("SELECT param_summary FROM p8_mcp_call_audit").fetchone()[0]
    assert "secret" not in summary


def test_saved_searches_are_scoped_to_the_creating_user(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    saved = store.save_search("tenant-a", "user-a", "recent failures", "failed evaluation", ["agent_eval"])
    assert store.list_saved_searches("tenant-a", "user-a")[0]["id"] == saved["id"]
    assert store.list_saved_searches("tenant-a", "user-b") == []
    assert not store.delete_saved_search("tenant-a", "user-b", saved["id"])
    assert store.delete_saved_search("tenant-a", "user-a", saved["id"])


def test_search_synonyms_are_tenant_scoped_and_expand_queries(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    store.upsert_search_synonym("tenant-a", "zero trust", ["ztna"], "admin-a")
    assert store.expand_search_terms("tenant-a", "zero trust policy") == ["zero trust policy", "ztna policy"]
    assert store.expand_search_terms("tenant-b", "zero trust policy") == ["zero trust policy"]
    store.record_search_execution("tenant-a", "admin-a", "zero trust policy", 2, 3)
    assert store.list_audit("tenant-a", "search")[0]["action"] == "search.execute"


def test_search_ranking_is_configurable_per_tenant(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    weights = store.save_search_ranking("tenant-a", {"trace": 900, "document": 1}, "admin-a")
    assert weights["trace"] == 900
    ranked = store.rank_search_results("tenant-a", [{"type": "document"}, {"type": "trace"}])
    assert [item["type"] for item in ranked] == ["trace", "document"]
    assert ranked[0]["ranking_policy"] == "tenant-configured"


def test_operations_task_supports_assignment_sla_comments_and_tenant_scope(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    task = store.create_operations_task("tenant-a", "admin-a", source_type="knowledge_gap", source_id="7", title="补充资料", priority="high", assignee_user_id="owner-a", collaborator_user_ids=["reviewer-a"], sla_hours=24)
    assert store.list_operations_tasks("tenant-b") == []
    updated = store.update_operations_task("tenant-a", task["id"], "admin-a", {"status": "in_progress", "department": "research"})
    assert updated["department"] == "research"
    comment = store.add_operations_task_comment("tenant-a", task["id"], "reviewer-a", "已开始核查")
    assert comment["task_id"] == task["id"]
    assert store.update_operations_task("tenant-b", task["id"], "admin-b", {"status": "done"}) is None


def test_operations_task_source_sync_is_idempotent_and_attachment_is_metadata_only(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    first, created = store.ensure_operations_task("tenant-a", "admin-a", source_type="document_review", source_id="doc-1", title="审核文档")
    second, duplicate = store.ensure_operations_task("tenant-a", "admin-a", source_type="document_review", source_id="doc-1", title="审核文档")
    assert created is True and duplicate is False and first["id"] == second["id"]
    attachment = store.add_operations_task_attachment("tenant-a", first["id"], "admin-a", "来源文档", "document", "doc-1")
    assert attachment["resource_id"] == "doc-1"


def test_operations_task_calculates_and_filters_overdue_sla(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    task = store.create_operations_task("tenant-a", "admin-a", source_type="manual", title="过期待办", due_at="2020-01-01T00:00:00+00:00")
    assert store.get_operations_task("tenant-a", task["id"])["sla_state"] == "overdue"
    assert store.list_operations_tasks("tenant-a", sla_state="overdue")[0]["id"] == task["id"]


def test_notification_policy_controls_channels_and_quiet_window(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    policy = store.upsert_notification_policy("tenant-a", "admin-a", {"name": "eval", "event_types": ["eval.completed"], "channels": ["in_app"], "recipient_user_ids": ["reviewer-a"], "cooldown_minutes": 10})
    decision = store.notification_decision("tenant-a", "eval.completed", "info")
    assert decision["channels"] == ["in_app"] and decision["policy_ids"] == [policy["id"]]
    assert decision["recipient_user_ids"] == ["reviewer-a"]
    assert store.notification_decision("tenant-a", "eval.completed", "info")["suppressed"] is True
    assert store.notification_decision("tenant-a", "quota.warning", "warning")["channels"] == ["in_app", "email", "webhook"]
    assert any(item["decision"] == "suppressed" for item in store.list_notification_policy_deliveries("tenant-a", policy["id"]))
