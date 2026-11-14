import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

import routes_api
from memory import ConversationMemory


def test_org_admin_reads_only_its_notification_and_monitoring_scope(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "admin-tenant-scope.db"))
    organization = memory.create_organization("Test Organization")
    other_organization = memory.create_organization("Other Organization")
    account = memory.create_workspace_user(
        organization["id"], "org-admin@example.test", "StrongPass1",
        "Org Admin", role="org_admin", agent_id=organization["agent_id"],
    )
    user = memory.authenticate_user(account["email"], "StrongPass1")
    token = memory.create_auth_session(user)
    monkeypatch.setattr(routes_api.agent, "memory", memory)
    request = Request({
        "type": "http",
        "method": "GET",
        "path": "/api/admin/notifications",
        "headers": [(b"authorization", f"Bearer {token}".encode("ascii"))],
        "query_string": b"",
        "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    })

    assert json.loads(routes_api.admin_notifications(request, organization["id"]).body)["items"] == []
    assert json.loads(routes_api.admin_monitoring_events(request, organization["id"]).body)["items"] == []
    assert json.loads(routes_api.admin_monitoring_summary(request, organization["id"]).body)["total"] == 0

    for endpoint in (
        routes_api.admin_notifications,
        routes_api.admin_monitoring_events,
        routes_api.admin_monitoring_summary,
    ):
        with pytest.raises(HTTPException) as exc:
            endpoint(request, other_organization["id"])
        assert exc.value.status_code == 403
