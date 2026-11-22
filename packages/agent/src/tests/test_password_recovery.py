import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memory import ConversationMemory
from password_recovery import recovery_response, router


def test_should_require_change_after_reset_and_clear_after_change(tmp_path):
    memory = ConversationMemory(str(tmp_path / "recovery.db"))
    user = memory.register_user("recovery@example.com", "Original-Test-19")
    assert memory.reset_user_password(user["tenant_id"], user["id"], "Temporary-Test-20", user["id"])
    token = memory.create_auth_session(user)
    assert memory.get_auth_session(token)["password_change_required"]
    with pytest.raises(ValueError):
        memory.change_password(user["id"], "Temporary-Test-20", "Temporary-Test-20")
    assert not memory.change_password(user["id"], "Wrong-Test-21", "Replacement-Test-21")
    assert memory.get_auth_session(token)["password_change_required"]
    assert memory.change_password(user["id"], "Temporary-Test-20", "Replacement-Test-21")
    assert memory.get_auth_session(token) is None
    new_token = memory.create_auth_session(user)
    assert not memory.get_auth_session(new_token)["password_change_required"]


def test_should_initialize_recovery_state_on_existing_database(tmp_path):
    path = str(tmp_path / "existing.db")
    memory = ConversationMemory(path)
    user = memory.register_user("existing@example.com", "Original-Test-19")
    token = memory.create_auth_session(user)
    memory.require_password_change(user["id"], user["id"])
    reopened = ConversationMemory(path)
    assert reopened.get_auth_session(token) is None
    fresh_token = reopened.create_auth_session(user)
    assert reopened.get_auth_session(fresh_token)["password_change_required"]


@pytest.mark.parametrize("context", ["front", "admin", "bearer"])
def test_should_block_business_requests_until_password_changed(tmp_path, context):
    memory = ConversationMemory(str(tmp_path / "gate.db"))
    user = memory.register_user("gate@example.com", "Original-Test-19")
    memory.reset_user_password(user["tenant_id"], user["id"], "Temporary-Test-20", user["id"])
    token = memory.create_auth_session(user)
    app = FastAPI()
    app.include_router(router)

    @app.middleware("http")
    async def gate(request, call_next):
        blocked = recovery_response(request, memory)
        return blocked if blocked is not None else await call_next(request)

    @app.get("/api/business")
    def business():
        return {"ok": True}

    client = TestClient(app)
    headers = {}
    if context == "bearer":
        headers["Authorization"] = "Bearer " + token
    elif context == "admin":
        client.cookies.set("securenexus_admin_session", token)
        headers["X-Admin-Context"] = "1"
    else:
        client.cookies.set("securenexus_session", token)
    assert client.get("/api/business", headers=headers).status_code == 403
    html = client.get("/admin" if context == "admin" else "/chat",
                      headers={**headers, "Accept": "text/html"}, follow_redirects=False)
    assert html.status_code == 303
    assert html.headers["location"] == "/change-password" + ("?admin=1" if context == "admin" else "")
    page = client.get(html.headers["location"], headers=headers)
    assert page.status_code == 200
    assert 'type="password"' in page.text
    assert 'password_confirm' in page.text
    assert page.headers["cache-control"] == "no-store"
