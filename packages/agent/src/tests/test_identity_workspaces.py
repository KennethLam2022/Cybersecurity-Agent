from types import SimpleNamespace
import sqlite3

from identity import default_principal, has_permission, principal_from_request, require_platform_permission, Principal
from memory import ConversationMemory
from fastapi import HTTPException
import pytest


def test_default_workspace_keeps_legacy_conversations_scoped(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    principal = default_principal()
    conversation = memory.create_conversation(
        tenant_id=principal.tenant_id, user_id=principal.user_id, agent_id=principal.agent_id,
    )

    items = memory.get_conversations(
        tenant_id=principal.tenant_id, user_id=principal.user_id, agent_id=principal.agent_id,
    )
    assert [item["id"] for item in items] == [conversation["id"]]


def test_monitoring_permissions_follow_role_boundaries():
    platform = Principal("t", "u", "a", "platform_admin", True)
    org = Principal("t", "u", "a", "org_admin", True)
    auditor = Principal("t", "u", "a", "auditor", True)
    user = Principal("t", "u", "a", "user", True)

    assert has_permission(platform, "monitoring.change.execute")
    assert has_permission(org, "monitoring.plan.edit")
    assert has_permission(org, "monitoring.review")
    assert not has_permission(org, "monitoring.change.execute")
    assert has_permission(auditor, "monitoring.read")
    assert not has_permission(auditor, "monitoring.plan.edit")
    assert not has_permission(user, "monitoring.read")
    assert has_permission(user, "memory.profile.propose")
    assert not has_permission(user, "memory.profile.review")
    assert has_permission(user, "memory.conflict.propose")
    assert not has_permission(user, "memory.conflict.review")
    assert has_permission(platform, "audit.read")
    assert has_permission(org, "audit.read")
    assert has_permission(auditor, "audit.read")
    assert not has_permission(user, "audit.read")


def test_legacy_local_principal_cannot_be_used_for_administration(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "legacy-admin.db"))
    monkeypatch.setenv("ALLOW_LEGACY_LOCAL_WORKSPACE", "1")
    request = SimpleNamespace(headers={}, cookies={})

    assert principal_from_request(request, memory).authenticated is False
    with pytest.raises(HTTPException) as exc:
        require_platform_permission(request, memory)
    assert exc.value.status_code == 401


def test_registered_workspaces_cannot_read_each_others_conversations(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    alice = memory.register_user("alice@example.com", "Correct-Horse-01", "Alice")
    bob = memory.register_user("bob@example.com", "Correct-Horse-02", "Bob")
    alice_conversation = memory.create_conversation(
        tenant_id=alice["tenant_id"], user_id=alice["id"], agent_id=alice["agent_id"],
    )

    alice_items = memory.get_conversations(
        tenant_id=alice["tenant_id"], user_id=alice["id"], agent_id=alice["agent_id"],
    )
    bob_items = memory.get_conversations(
        tenant_id=bob["tenant_id"], user_id=bob["id"], agent_id=bob["agent_id"],
    )

    assert [item["id"] for item in alice_items] == [alice_conversation["id"]]
    assert bob_items == []
    assert memory.conversation_belongs_to(
        alice_conversation["id"], bob["tenant_id"], bob["id"], bob["agent_id"],
    ) is False


def test_password_session_and_request_principal(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    user = memory.register_user("user@example.com", "Correct-Horse-03", "User")
    assert memory.authenticate_user("user@example.com", "wrong-password") is None

    authenticated = memory.authenticate_user("user@example.com", "Correct-Horse-03")
    token = memory.create_auth_session(authenticated, ip_address="127.0.0.1", user_agent="pytest-agent")
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"})
    principal = principal_from_request(request, memory)

    assert principal.authenticated is True
    assert principal.user_id == user["id"]
    session = memory.get_auth_session(token)
    assert session["ip_address"] == "127.0.0.1"
    assert session["user_agent"] == "pytest-agent"
    assert session["last_seen_at"]
    memory.revoke_auth_session(token)
    assert memory.get_auth_session(token) is None


def test_first_run_bootstrap_activates_the_local_platform_admin_once(tmp_path):
    memory = ConversationMemory(str(tmp_path / "bootstrap.db"))
    assert memory.platform_admin_setup_required() is True

    admin = memory.bootstrap_platform_admin(
        "admin@example.com", "Correct-Horse-31", "Platform Admin",
    )
    assert admin["role"] == "platform_admin"
    assert memory.platform_admin_setup_required() is False

    authenticated = memory.authenticate_user("admin@example.com", "Correct-Horse-31")
    assert authenticated and authenticated["id"] == "local-owner"
    assert authenticated["role"] == "platform_admin"

    try:
        memory.bootstrap_platform_admin("other@example.com", "Correct-Horse-32", "Other")
    except ValueError as exc:
        assert "已初始化" in str(exc)
    else:
        raise AssertionError("platform bootstrap must be one-time")


def test_account_password_policy_requires_upper_lower_and_digit(tmp_path):
    memory = ConversationMemory(str(tmp_path / "password-policy.db"))
    for weak in ("short1A", "alllowercase1", "ALLUPPERCASE1", "NoDigitsHere"):
        try:
            memory.register_user(f"{weak}@example.com", weak, "Weak")
        except ValueError as exc:
            assert "大写字母、小写字母和数字" in str(exc)
        else:
            raise AssertionError("weak account password must be rejected")

    user = memory.register_user("strong@example.com", "StrongPass1", "Strong")
    assert user["email"] == "strong@example.com"


def test_cookie_session_can_build_principal_without_exposing_bearer_header(tmp_path):
    memory = ConversationMemory(str(tmp_path / "cookie-session.db"))
    user = memory.register_user("cookie@example.com", "Correct-Horse-18", "Cookie")
    token = memory.create_auth_session(user)
    request = SimpleNamespace(headers={}, cookies={"securenexus_session": token})
    principal = principal_from_request(request, memory)
    assert principal.authenticated is True
    assert principal.user_id == user["id"]


def test_message_ownership_follows_its_conversation(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    alice = memory.register_user("alice@example.com", "Correct-Horse-01", "Alice")
    bob = memory.register_user("bob@example.com", "Correct-Horse-02", "Bob")
    conversation = memory.create_conversation(
        tenant_id=alice["tenant_id"], user_id=alice["id"], agent_id=alice["agent_id"],
    )
    message_id = memory.add_message(conversation["id"], "assistant", "安全建议")

    assert memory.message_belongs_to(
        message_id, alice["tenant_id"], alice["id"], alice["agent_id"],
    )
    assert not memory.message_belongs_to(
        message_id, bob["tenant_id"], bob["id"], bob["agent_id"],
    )


def test_admin_workspace_inventory_and_lifecycle(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    user = memory.register_user("owner@example.com", "Correct-Horse-04", "Owner")
    extra = memory.create_agent(user["tenant_id"], "调查 Agent")

    workspaces = memory.list_workspaces()
    workspace = next(item for item in workspaces if item["id"] == user["tenant_id"])
    assert workspace["user_count"] == 1
    assert {item["id"] for item in workspace["agents"]} == {user["agent_id"], extra["id"]}

    assert memory.update_agent_status(user["tenant_id"], extra["id"], "disabled")
    assert memory.update_user_status(user["tenant_id"], user["id"], "suspended")
    assert memory.authenticate_user("owner@example.com", "Correct-Horse-04") is None


def test_user_can_switch_only_to_an_authorized_active_agent(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    owner = memory.register_user("owner@example.com", "Correct-Horse-05", "Owner")
    extra = memory.create_agent(owner["tenant_id"], "制度写作 Agent")
    token = memory.create_auth_session(owner)

    agents = memory.list_user_agents(owner["tenant_id"], owner["id"])
    assert {item["id"] for item in agents} == {owner["agent_id"], extra["id"]}
    assert memory.switch_auth_session_agent(token, extra["id"])["id"] == extra["id"]
    assert memory.get_auth_session(token)["agent_id"] == extra["id"]

    other = memory.register_user("other@example.com", "Correct-Horse-06", "Other")
    other_token = memory.create_auth_session(other)
    assert memory.switch_auth_session_agent(other_token, extra["id"]) is None

    assert memory.update_agent_status(owner["tenant_id"], extra["id"], "disabled")
    assert memory.get_auth_session(token) is None


def test_workspace_membership_and_agent_grant_are_independent(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    owner = memory.register_user("owner@example.com", "Correct-Horse-07", "Owner")
    guest = memory.register_user("guest@example.com", "Correct-Horse-08", "Guest")
    extra = memory.create_agent(owner["tenant_id"], "分析 Agent")

    memory.add_workspace_member(owner["tenant_id"], guest["email"])
    assert memory.list_user_agents(owner["tenant_id"], guest["id"]) == []
    assert memory.authenticate_user("guest@example.com", "Correct-Horse-08")

    assert memory.update_agent_membership(owner["tenant_id"], extra["id"], guest["id"])
    agents = memory.list_user_agents(owner["tenant_id"], guest["id"])
    assert [item["id"] for item in agents] == [extra["id"]]

    token = memory.create_auth_session(memory.authenticate_user("guest@example.com", "Correct-Horse-08"))
    assert memory.get_auth_session(token)["tenant_id"] == guest["tenant_id"]
    assert memory.switch_auth_session_workspace(token, owner["tenant_id"])
    assert memory.get_auth_session(token)["tenant_id"] == owner["tenant_id"]
    assert memory.update_workspace_member_status(owner["tenant_id"], guest["id"], "disabled")
    assert memory.get_auth_session(token) is None


def test_admin_can_create_workspace_user_with_strong_or_generated_password(tmp_path):
    memory = ConversationMemory(str(tmp_path / "admin-create-user.db"))
    owner = memory.register_user("owner-create@example.com", "Correct-Horse-41", "Owner")

    created = memory.create_workspace_user(
        owner["tenant_id"], "new-user@example.com", "", "New User", "user", "", owner["id"],
    )
    assert created["temporary_password"]
    assert memory.authenticate_user("new-user@example.com", created["temporary_password"])["id"] == created["id"]
    assert created["agent_id"] == owner["agent_id"]

    with pytest.raises(ValueError, match="至少需要 8 位"):
        memory.create_workspace_user(owner["tenant_id"], "weak@example.com", "weakpass", "Weak")
    with pytest.raises(ValueError, match="该邮箱已注册"):
        memory.create_workspace_user(owner["tenant_id"], "new-user@example.com", "StrongPass1", "Duplicate")


def test_account_email_validation_rejects_masked_or_incomplete_addresses(tmp_path):
    memory = ConversationMemory(str(tmp_path / "email-validation.db"))
    invalid = ["fi****@163.com", "user@", "@example.com", "user@example", "user name@example.com"]
    for email in invalid:
        with pytest.raises(ValueError, match="完整邮箱"):
            memory.submit_user_registration(email, "StrongPass1", "Invalid")
    created = memory.submit_user_registration("Valid.User@Example.com", "StrongPass1", "Valid")
    assert created["email"] == "valid.user@example.com"


def test_self_registration_requires_platform_approval_before_login(tmp_path):
    memory = ConversationMemory(str(tmp_path / "registration-approval.db"))
    pending = memory.submit_user_registration("pending@example.com", "Correct-Horse-43", "Pending")
    assert pending["status"] == "pending_approval"
    assert memory.authenticate_user("pending@example.com", "Correct-Horse-43") is None
    assert memory.list_pending_user_registrations()[0]["id"] == pending["id"]
    approved = memory.approve_user_registration(pending["id"], "local-owner")
    assert approved["status"] == "active"
    assert memory.authenticate_user("pending@example.com", "Correct-Horse-43")["id"] == pending["id"]


def test_admin_created_org_admin_gets_active_workspace_agents(tmp_path):
    memory = ConversationMemory(str(tmp_path / "admin-create-admin.db"))
    owner = memory.register_user("owner-admin@example.com", "Correct-Horse-42", "Owner")
    second = memory.create_agent(owner["tenant_id"], "第二 Agent")
    created = memory.create_workspace_user(
        owner["tenant_id"], "workspace-admin@example.com", "StrongPass1", "Workspace Admin", "org_admin", "", owner["id"],
    )
    with sqlite3.connect(memory._db_path) as conn:
        rows = conn.execute(
            "SELECT agent_id FROM agent_memberships WHERE tenant_id=? AND user_id=? AND status='active'",
            (owner["tenant_id"], created["id"]),
        ).fetchall()
    assert {row[0] for row in rows} == {owner["agent_id"], second["id"]}


def test_password_reset_clears_login_lock_and_invalidates_sessions(tmp_path):
    memory = ConversationMemory(str(tmp_path / "password-reset.db"))
    user = memory.register_user("reset@example.com", "Correct-Horse-44", "Reset")
    authenticated = memory.authenticate_user("reset@example.com", "Correct-Horse-44")
    token = memory.create_auth_session(authenticated)
    for _ in range(5):
        assert memory.authenticate_user("reset@example.com", "WrongPass1") is None
    assert memory.reset_user_password(user["tenant_id"], user["id"], "New-Correct-44", "local-owner")
    assert memory.get_auth_session(token) is None
    assert memory.authenticate_user("reset@example.com", "New-Correct-44")["id"] == user["id"]


def test_login_lock_is_visible_and_admin_can_unlock(tmp_path):
    memory = ConversationMemory(str(tmp_path / "login-lock-admin.db"))
    user = memory.register_user("locked@example.com", "Correct-Horse-45", "Locked")
    for _ in range(5):
        memory.authenticate_user(user["email"], "WrongPass1")
    locks = memory.list_login_locks()
    assert locks and locks[0]["email"] == user["email"]
    assert locks[0]["failed_count"] == 5
    assert memory.unlock_login(user["email"], "local-owner")
    assert memory.list_login_locks() == []
    assert memory.authenticate_user(user["email"], "Correct-Horse-45")["id"] == user["id"]


def test_user_can_switch_between_authorized_workspaces(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    owner = memory.register_user("owner@example.com", "Correct-Horse-09", "Owner")
    member = memory.register_user("member@example.com", "Correct-Horse-10", "Member")
    shared = memory.create_agent(owner["tenant_id"], "共享 Agent")
    memory.add_workspace_member(owner["tenant_id"], member["email"])
    assert memory.update_agent_membership(owner["tenant_id"], shared["id"], member["id"])
    token = memory.create_auth_session(memory.authenticate_user("member@example.com", "Correct-Horse-10"))
    switched = memory.switch_auth_session_workspace(token, owner["tenant_id"])
    assert switched and switched["agent_id"] == shared["id"]
    assert memory.get_auth_session(token)["tenant_id"] == owner["tenant_id"]


def test_custom_roles_are_tenant_scoped_and_revoked_when_disabled(tmp_path):
    memory = ConversationMemory(str(tmp_path / "custom-roles.db"))
    owner = memory.register_user("role-owner@example.com", "Correct-Horse-30", "Owner")
    other = memory.register_user("role-other@example.com", "Correct-Horse-30", "Other")
    role = memory.save_custom_role(
        owner["tenant_id"], "knowledge-reviewer", "只读知识审核",
        ["graph.read", "workflow.read"], owner["id"],
    )
    assert has_permission(
        Principal(owner["tenant_id"], owner["id"], owner["agent_id"], role["id"], True),
        "graph.read", memory,
    )
    assert not has_permission(
        Principal(other["tenant_id"], other["id"], other["agent_id"], role["id"], True),
        "graph.read", memory,
    )
    assert memory.assign_custom_role(owner["tenant_id"], owner["id"], role["id"], owner["id"])
    assert memory.get_custom_role_permissions(role["id"], owner["tenant_id"]) == ["graph.read", "workflow.read"]
    assert memory.set_custom_role_status(role["id"], owner["tenant_id"], "disabled", owner["id"])
    assert not has_permission(
        Principal(owner["tenant_id"], owner["id"], owner["agent_id"], role["id"], True),
        "graph.read", memory,
    )


def test_custom_role_cannot_grant_platform_security_permissions(tmp_path):
    memory = ConversationMemory(str(tmp_path / "custom-role-boundary.db"))
    owner = memory.register_user("role-boundary@example.com", "Correct-Horse-30", "Owner")
    try:
        memory.save_custom_role(
            owner["tenant_id"], "unsafe-role", "", ["secrets.read"], owner["id"],
        )
    except ValueError as exc:
        assert "平台安全权限" in str(exc)
    else:
        raise AssertionError("custom role must not grant secrets permissions")

def test_document_registry_keeps_server_generated_scope(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    user = memory.register_user("owner@example.com", "Correct-Horse-11", "Owner")
    memory.register_document(
        "doc-stable-1", "制度.docx", "上传文档", "general", "private",
        user["tenant_id"], user["id"], user["agent_id"],
    )
    memory.mark_document_indexed("doc-stable-1", "RAG_DATA/03_cleaned/doc-stable-1__制度.md")
    import sqlite3
    with sqlite3.connect(memory._db_path) as conn:
        row = conn.execute("SELECT tenant_id, owner_user_id, visibility, status FROM documents WHERE id='doc-stable-1'").fetchone()
    assert row == (user["tenant_id"], user["id"], "private", "indexed")


def test_login_lock_password_change_and_deletion_request(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    user = memory.register_user("owner@example.com", "Correct-Horse-12", "Owner")
    for _ in range(5):
        assert memory.authenticate_user(user["email"], "wrong-password") is None
    assert memory.authenticate_user(user["email"], "Correct-Horse-12") is None

    import sqlite3
    with sqlite3.connect(memory._db_path) as conn:
        conn.execute("DELETE FROM login_attempts WHERE email=?", (user["email"],))
    assert memory.change_password(user["id"], "Correct-Horse-12", "New-Correct-Horse-12")
    assert memory.authenticate_user(user["email"], "New-Correct-Horse-12")
    memory.request_account_deletion(user["id"], "测试注销")
    assert memory.authenticate_user(user["email"], "New-Correct-Horse-12") is None
    with sqlite3.connect(memory._db_path) as conn:
        request = conn.execute("SELECT status FROM account_deletion_requests WHERE user_id=?", (user["id"],)).fetchone()
    assert request == ("pending",)


def test_authentication_outcomes_are_audited_without_password_data(tmp_path):
    memory = ConversationMemory(str(tmp_path / "auth-audit.db"))
    user = memory.register_user("audit@example.com", "Correct-Horse-17", "Audit")
    memory.record_login_event(user["email"], False, "invalid_credentials")
    memory.record_login_event(user["email"], True, "password")
    import sqlite3
    with sqlite3.connect(memory._db_path) as conn:
        rows = conn.execute("""SELECT action, detail_json FROM audit_logs
                              WHERE user_id=? AND action LIKE 'auth.login.%'
                              ORDER BY id""", (user["id"],)).fetchall()
    assert [row[0] for row in rows] == ["auth.login.failure", "auth.login.success"]
    assert all("Correct-Horse-17" not in row[1] for row in rows)


def test_audit_log_query_is_tenant_scoped_and_does_not_expose_payload(tmp_path):
    memory = ConversationMemory(str(tmp_path / "audit-query.db"))
    alice = memory.register_user("audit-alice@example.com", "Correct-Horse-19", "Alice")
    bob = memory.register_user("audit-bob@example.com", "Correct-Horse-20", "Bob")
    memory.log_audit(alice["tenant_id"], alice["id"], alice["agent_id"], "sensitive.read",
                     "conversation", "conv-a", {"reason": "安全事件复核", "secret": "must-not-be-content"})
    memory.log_audit(bob["tenant_id"], bob["id"], bob["agent_id"], "sensitive.read",
                     "conversation", "conv-b", {"reason": "其他租户"})
    rows = memory.list_audit_logs(alice["tenant_id"], action="sensitive.read")
    assert len(rows) == 1
    assert rows[0]["resource_id"] == "conv-a"
    assert rows[0]["detail"]["reason"] == "安全事件复核"
    assert memory.list_audit_logs(bob["tenant_id"], action="sensitive.read")[0]["resource_id"] == "conv-b"


def test_workspace_invitation_requires_approval_before_login(tmp_path):
    memory = ConversationMemory(str(tmp_path / "invitation.db"))
    owner = memory.register_user("owner@example.com", "Correct-Horse-13", "Owner")
    invitation = memory.create_workspace_invitation(
        owner["tenant_id"], "member@example.com", "user", owner["id"], owner["agent_id"], 72,
    )
    pending = memory.accept_workspace_invitation(
        invitation["token"], "member@example.com", "Correct-Horse-14", "Member",
    )
    assert pending["status"] == "pending_approval"
    assert memory.authenticate_user("member@example.com", "Correct-Horse-14") is None
    listed = memory.list_workspace_invitations(owner["tenant_id"])
    assert listed[0]["status"] == "accepted_pending"

    approved = memory.approve_workspace_invitation(owner["tenant_id"], invitation["id"], owner["id"])
    assert approved and approved["status"] == "active"
    assert memory.authenticate_user("member@example.com", "Correct-Horse-14")["role"] == "user"


def test_workspace_invitation_revoke_and_resend_are_one_time(tmp_path):
    memory = ConversationMemory(str(tmp_path / "invitation-revoke.db"))
    owner = memory.register_user("owner@example.com", "Correct-Horse-15", "Owner")
    invitation = memory.create_workspace_invitation(
        owner["tenant_id"], "member@example.com", "user", owner["id"], owner["agent_id"], 72,
    )
    resent = memory.resend_workspace_invitation(owner["tenant_id"], invitation["id"], owner["id"], 72)
    assert resent and resent["token"] != invitation["token"]
    assert memory.revoke_workspace_invitation(owner["tenant_id"], resent["id"], owner["id"])
    try:
        memory.accept_workspace_invitation(resent["token"], "member@example.com", "Correct-Horse-16", "Member")
    except ValueError as exc:
        assert "无效" in str(exc)
    else:
        raise AssertionError("revoked invitation must not be accepted")


def test_long_term_memory_is_scoped_and_deduplicated(tmp_path):
    memory = ConversationMemory(str(tmp_path / "identity.db"))
    alice = memory.register_user("alice@example.com", "Correct-Horse-01", "Alice")
    bob = memory.register_user("bob@example.com", "Correct-Horse-02", "Bob")
    first_id = memory.write_long_term_memory(
        alice["tenant_id"], alice["id"], alice["agent_id"], "preference",
        "用户偏好简洁的风险结论", ["response_style"], importance=0.8,
    )
    second_id = memory.write_long_term_memory(
        alice["tenant_id"], alice["id"], alice["agent_id"], "preference",
        "用户偏好简洁的风险结论", ["response_style", "concise"], importance=0.9,
    )

    assert first_id == second_id
    assert len(memory.get_long_term_memories(
        alice["tenant_id"], alice["id"], alice["agent_id"], "风险结论",
    )) == 1
    assert memory.get_long_term_memories(
        bob["tenant_id"], bob["id"], bob["agent_id"], "风险结论",
    ) == []
    assert memory.update_long_term_memory_status(
        alice["tenant_id"], alice["id"], first_id, "suppressed",
    )
    assert memory.get_long_term_memories(
        alice["tenant_id"], alice["id"], alice["agent_id"], "风险结论",
    ) == []
    assert memory.delete_long_term_memory(alice["tenant_id"], alice["id"], first_id)
