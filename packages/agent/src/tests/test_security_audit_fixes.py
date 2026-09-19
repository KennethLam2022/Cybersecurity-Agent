import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

import routes_api
import app_state


def test_upload_filename_rejects_absolute_and_parent_paths():
    with pytest.raises(HTTPException):
        routes_api._safe_upload_filename("..\\..\\secrets.txt")
    with pytest.raises(HTTPException):
        routes_api._safe_upload_filename("C:\\Windows\\win.ini")
    with pytest.raises(HTTPException):
        routes_api._safe_upload_filename("nested/document.pdf")


def test_upload_filename_accepts_supported_basename():
    assert routes_api._safe_upload_filename("policy-2026.pdf") == "policy-2026.pdf"


def test_upload_batch_size_is_enforced():
    with pytest.raises(HTTPException):
        routes_api._validate_upload_batch_size(routes_api._MAX_BATCH_UPLOAD_SIZE, 1)


def test_ingestion_job_scope_rejects_other_tenant(monkeypatch):
    principal = type("Principal", (), {"role": "org_admin", "tenant_id": "tenant-a"})()
    monkeypatch.setattr(routes_api, "principal_from_request", lambda request, memory: principal)
    with pytest.raises(HTTPException) as exc:
        routes_api._require_ingestion_job_scope(object(), {"tenant_id": "tenant-b"})
    assert exc.value.status_code == 403


def test_access_migration_requires_platform_permission(monkeypatch):
    called = {}

    def fake_require(request, memory):
        called["yes"] = True
        return "platform-principal"

    monkeypatch.setattr(routes_api, "require_platform_permission", fake_require)
    assert routes_api._require_access_migration_permission("request") == "platform-principal"
    assert called["yes"] is True


def test_conversation_stats_handles_messages_without_sources(monkeypatch):
    detail = {
        "messages": [{"role": "user", "sources": []}],
        "stats": {"rounds": 1, "total_sources": 0},
    }
    principal = type(
        "Principal",
        (),
        {"role": "platform_admin", "tenant_id": "tenant-a", "user_id": "u1", "agent_id": "a1"},
    )()
    monkeypatch.setattr(routes_api, "_admin_conversation_scope", lambda *args: (principal, "tenant-a"))
    monkeypatch.setattr(routes_api.agent.memory, "get_conversation_detail", lambda conv_id: detail)
    response = routes_api.get_conversation_stats("conv-1", object())
    assert response.status_code == 200


def test_source_map_placeholder_resolves_to_project_cleaned_data(monkeypatch, tmp_path):
    monkeypatch.setattr(app_state, "_PROJECT_ROOT", tmp_path)
    resolved = app_state._resolve_source_dir("<knowledge-base>/01-国家法律")
    assert resolved == str(tmp_path / "RAG_DATA" / "03_cleaned" / "01-国家法律")


def test_conversation_memory_has_no_duplicate_public_methods():
    source = Path(__file__).resolve().parents[1] / "memory.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    memory_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ConversationMemory"
    )
    methods = [
        node.name for node in memory_class.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    ]
    assert len(methods) == len(set(methods))
