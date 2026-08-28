from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes_api
from memory import ConversationMemory


def _request(token: str):
    return SimpleNamespace(headers={"Authorization": f"Bearer {token}"})


def test_workflow_scope_uses_authenticated_tenant_and_agent(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "workflow-api.db"))
    owner = memory.register_user("workflow-api@example.com", "Correct-Horse-30", "Owner")
    second_agent = memory.create_agent(owner["tenant_id"], "Second Agent")
    token = memory.create_auth_session(owner)
    monkeypatch.setattr(routes_api.agent, "memory", memory)

    principal, tenant_id, agent_id = routes_api._workflow_scope(
        _request(token), "workflow.read", owner["tenant_id"], owner["agent_id"],
    )
    assert principal.user_id == owner["id"]
    assert tenant_id == owner["tenant_id"]
    assert agent_id == owner["agent_id"]

    with pytest.raises(HTTPException) as exc:
        routes_api._workflow_scope(
            _request(token), "workflow.read", owner["tenant_id"], second_agent["id"],
        )
    assert exc.value.status_code == 403


def test_workflow_scope_rejects_another_tenant(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "workflow-api.db"))
    owner = memory.register_user("workflow-owner@example.com", "Correct-Horse-30", "Owner")
    other = memory.register_user("workflow-other@example.com", "Correct-Horse-30", "Other")
    token = memory.create_auth_session(owner)
    monkeypatch.setattr(routes_api.agent, "memory", memory)

    with pytest.raises(HTTPException) as exc:
        routes_api._workflow_scope(
            _request(token), "workflow.read", other["tenant_id"], other["agent_id"],
        )
    assert exc.value.status_code == 403
