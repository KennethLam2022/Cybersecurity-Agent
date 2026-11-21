from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import routes_api
from memory import ConversationMemory


@pytest.fixture
def account(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "password.db"))
    user = memory.register_user("reset@example.com", "Original-Test-19")
    token = memory.create_auth_session(user)
    monkeypatch.setattr(routes_api.agent, "memory", memory)
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"})
    return memory, user, token, request


@pytest.mark.parametrize("confirmation", [None, "", "Different-Test-20"])
def test_should_reject_reset_without_matching_confirmation(account, confirmation):
    memory, user, token, request = account
    with pytest.raises(HTTPException) as error:
        routes_api.admin_reset_user_password(
            user["tenant_id"], user["id"], request,
            {"new_password": "Replacement-Test-20", "password_confirm": confirmation},
        )
    assert error.value.status_code == 400
    assert memory.authenticate_user(user["email"], "Original-Test-19")
    assert memory.get_auth_session(token)


def test_should_apply_confirmed_reset_and_revoke_sessions(account):
    memory, user, token, request = account
    response = routes_api.admin_reset_user_password(
        user["tenant_id"], user["id"], request,
        {"new_password": "Replacement-Test-20", "password_confirm": "Replacement-Test-20"},
    )
    assert response.status_code == 200
    assert memory.authenticate_user(user["email"], "Replacement-Test-20")
    assert not memory.authenticate_user(user["email"], "Original-Test-19")
    assert memory.get_auth_session(token) is None
