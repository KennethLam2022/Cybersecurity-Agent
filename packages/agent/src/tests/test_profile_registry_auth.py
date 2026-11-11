from auth import is_admin_route
import asyncio
from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from governance_store import GovernanceStore
from identity import ADMIN_SESSION_COOKIE, FRONT_SESSION_COOKIE, session_token_from_request
from profile_classifier import profile_options
import routes_api
import main
from memory import ConversationMemory


def test_profile_registry_requires_management_session():
    assert is_admin_route("/api/documents/profile-registry") is True


def test_profile_registry_uses_admin_cookie_not_frontend_cookie():
    request = Request({
        "type": "http", "method": "GET", "path": "/api/documents/profile-registry",
        "headers": [(b"cookie", b"securenexus_admin_session=admin-session; securenexus_session=front-session")],
        "query_string": b"", "server": ("test", 80),
        "client": ("127.0.0.1", 1),
    })

    assert session_token_from_request(request) == "admin-session"


def test_profile_registry_rejects_frontend_cookie_as_admin_session(monkeypatch):
    monkeypatch.setenv("ALLOW_LEGACY_LOCAL_WORKSPACE", "0")
    request = Request({
        "type": "http", "method": "GET", "path": "/api/documents/profile-registry",
        "headers": [(b"cookie", b"securenexus_session=front-session")],
        "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
    })

    assert session_token_from_request(request) == ""
    with pytest.raises(HTTPException) as exc:
        from identity import principal_from_request
        principal_from_request(request, object())
    assert exc.value.status_code == 401


def test_profile_registry_middleware_accepts_admin_session_and_rejects_frontend_session(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "document-admin-session.db"))
    user = memory.bootstrap_platform_admin("admin@example.com", "Correct-Horse-30", "Admin")
    admin_token = memory.create_auth_session(user)
    monkeypatch.setattr(main.agent, "memory", memory)

    async def check_request(cookie):
        headers = [(b"accept", b"application/json")]
        if cookie:
            headers.append((b"cookie", cookie.encode("ascii")))
        request = Request({
            "type": "http", "method": "GET", "path": "/api/documents/profile-registry",
            "headers": headers, "query_string": b"", "server": ("test", 80),
            "client": ("127.0.0.1", 1),
        })

        async def accepted(_request):
            from starlette.responses import Response
            return Response(status_code=204)

        return await main.auth_middleware(request, accepted)

    admin_response = asyncio.run(check_request(f"{ADMIN_SESSION_COOKIE}={admin_token}"))
    frontend_response = asyncio.run(check_request(f"{FRONT_SESSION_COOKIE}={admin_token}"))
    anonymous_response = asyncio.run(check_request(""))

    assert admin_response.status_code == 204
    assert frontend_response.status_code == 401
    assert anonymous_response.status_code == 401


def test_document_write_endpoints_remain_protected():
    assert is_admin_route("/api/documents/profile-migration/confirm") is True
    assert is_admin_route("/api/documents/start-processing") is True


def test_profile_registry_defaults_to_general_and_can_include_enabled_industry_profile(tmp_path):
    store = GovernanceStore(str(tmp_path / "governance.db"))
    options = profile_options()
    assert [item["profile"] for item in options if item["profile"] in store.enabled_profiles("tenant-a")] == ["general"]
    store.set_industry_profile_grant("tenant-a", "industry/finance", True, "v1", "admin-a")
    visible = [item["profile"] for item in options if item["profile"] in store.enabled_profiles("tenant-a")]
    assert visible == ["general", "industry/finance"]


def test_document_profile_registry_returns_only_current_tenant_enabled_profiles(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "profile-registry.db"))
    owner = memory.register_user("profile-owner@example.com", "Correct-Horse-30", "Owner")
    token = memory.create_auth_session(owner)
    monkeypatch.setattr(routes_api.agent, "memory", memory)
    request = SimpleNamespace(headers={"Authorization": f"Bearer {token}"}, cookies={})

    response = routes_api.documents_profile_registry(request)
    assert [item["profile"] for item in json.loads(response.body)["profiles"]] == ["general"]

    GovernanceStore(str(memory._db_path)).set_industry_profile_grant(
        owner["tenant_id"], "industry/telecom", True, "v1", owner["id"],
    )
    response = routes_api.documents_profile_registry(request)
    payload = json.loads(response.body)
    assert [item["profile"] for item in payload["profiles"]] == ["general", "industry/telecom"]


def test_profile_request_scope_rejects_disabled_industry_extension(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "profile-request.db"))
    monkeypatch.setattr(routes_api.agent, "memory", memory)

    assert routes_api._authorized_profiles("tenant-a", None) == {"general"}
    with pytest.raises(HTTPException) as exc:
        routes_api._authorized_profiles("tenant-a", ["industry/finance"])
    assert exc.value.status_code == 403

    GovernanceStore(str(memory._db_path)).set_industry_profile_grant(
        "tenant-a", "industry/finance", True, "v1", "admin-a",
    )
    assert routes_api._authorized_profiles("tenant-a", ["general", "industry/finance"]) == {"general", "industry/finance"}
