import ast
from pathlib import Path

import pytest
from fastapi import HTTPException

import routes_api
import app_state


def test_html_csp_is_applied_to_pages_and_auth_redirects():
    from fastapi.testclient import TestClient
    import main

    with TestClient(main.app, follow_redirects=False) as client:
        html_responses = [
            client.get(path, headers={"accept": "text/html"})
            for path in ("/login", "/register", "/setup", "/admin/login")
        ]
        protected = client.get("/admin", headers={"accept": "text/html"})

    assert all(response.headers.get("content-security-policy") for response in html_responses)
    assert protected.status_code == 303
    assert protected.headers["location"].startswith("/admin/login?")


def test_csp_limits_scripts_and_preserves_embed_iframe_support():
    from fastapi.responses import HTMLResponse
    from starlette.requests import Request
    import asyncio
    import main

    async def html_response(_request):
        return HTMLResponse("<h1>Page</h1>")

    def request(path):
        return Request({
            "type": "http", "method": "GET", "path": path, "headers": [],
            "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
        })

    policy = asyncio.run(main.csp_middleware(request("/admin"), html_response)).headers[
        "content-security-policy"
    ]
    embed_policy = asyncio.run(main.csp_middleware(request("/embed"), html_response)).headers[
        "content-security-policy"
    ]
    assert "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net" in policy
    assert "object-src 'none'" in policy and "base-uri 'self'" in policy
    assert "frame-ancestors 'self'" in policy
    assert "frame-ancestors" not in embed_policy
    assert "*" not in policy


def test_csp_does_not_change_json_responses_or_allow_unapproved_chart_cdns():
    from fastapi.responses import JSONResponse
    from starlette.requests import Request
    import asyncio
    import main

    request = Request({
        "type": "http", "method": "GET", "path": "/api/status", "headers": [],
        "query_string": b"", "server": ("test", 80), "client": ("127.0.0.1", 1),
    })

    async def json_response(_request):
        return JSONResponse({"ok": True})

    response = asyncio.run(main.csp_middleware(request, json_response))
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "content-security-policy" not in response.headers
    assert "cdn.jsdelivr.net/npm/echarts@" in html
    assert "cdn.jsdelivr.net/npm/echarts-wordcloud@" in html
    assert "unpkg.com" not in html and "cdnjs.cloudflare.com" not in html


def test_upload_filename_rejects_absolute_and_parent_paths():
    with pytest.raises(HTTPException):
        routes_api._safe_upload_filename("..\\..\\secrets.txt")
    with pytest.raises(HTTPException):
        routes_api._safe_upload_filename("C:\\Windows\\win.ini")
    with pytest.raises(HTTPException):
        routes_api._safe_upload_filename("nested/document.pdf")


def test_upload_filename_accepts_supported_basename():
    assert routes_api._safe_upload_filename("policy-2026.pdf") == "policy-2026.pdf"


def test_admin_chart_scripts_pin_versions_and_integrity():
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "echarts@5.6.0/dist/echarts.min.js" in html
    assert "echarts-wordcloud@2.1.0/dist/echarts-wordcloud.min.js" in html
    assert "sha384-pPi0zxBAoDu6+JXW/C68UZLvBUUtU+7zonhif43rqj7pxsGyqyqzcian2Rj37Rss" in html
    assert "sha384-U1KEY0DDCF4Dq6Yx1J+EZ5Hnj8X5bMn52OAcJB8C4OiAWeU4iJhJ/Tv5KhTqu8zZ" in html
    assert "script.crossOrigin='anonymous'" in html
    assert "script.integrity=integrity" in html
    assert "script.onerror=function(){reject(" in html
    assert "_eChartLoaded=false" in html
    assert "_eChartPromise=null" in html


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
