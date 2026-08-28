from auth import is_admin_route
from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException

from governance_store import GovernanceStore
from profile_classifier import profile_options
import routes_api
from memory import ConversationMemory


def test_profile_registry_metadata_is_readable_without_management_session():
    assert is_admin_route("/api/documents/profile-registry") is False


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
