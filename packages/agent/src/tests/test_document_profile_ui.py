from pathlib import Path


def test_document_profile_registry_loads_after_admin_fetch_authentication():
    content = (Path(__file__).parents[1] / "static" / "data_preview.html").read_text(encoding="utf-8")

    assert content.index("function initAdminFetch()") < content.index(
        "fetch('/api/documents/profile-registry')"
    )
    assert content.count("window.fetch = function(input, init)") == 1
    assert "industry/telecom" not in content  # options must come from the registry API


def test_document_profile_ui_supports_reclassification_and_history():
    content = (Path(__file__).parents[1] / "static" / "data_preview.html").read_text(encoding="utf-8")

    assert 'id="migrationViewMode"' in content
    assert 'value="all">全部入库资料' in content
    assert "修改已确认资料时必须填写原因" in content
    assert "/api/documents/profile-migration/history" in content


def test_frontend_generation_flow_requires_outline_confirmation():
    content = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")

    assert "/api/generation/route" in content
    assert "appendGenerationOutline" in content
    assert "/render" in content
    assert "appendGenerationClarification" in content
    assert "/outline" in content
    assert "已检索授权资料" in content
    assert "未找到可绑定的授权资料" in content


def test_frontend_exposes_personal_usage_view():
    content = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
    assert "myUsageButton" in content
    assert "/api/usage/me" in content


def test_knowledge_base_governance_page_exposes_sources_and_lifecycle_controls():
    content = (Path(__file__).parents[1] / "static" / "knowledge_base_governance.html").read_text(encoding="utf-8")
    assert "数据源登记" in content
    assert "/api/admin/data-sources" in content
    assert "/api/admin/documents/" in content
    assert "validateSource" in content
    assert "rollbackDocVersion" in content
    assert "/api/admin/documents/expire" in content
    assert "/grants" in content
