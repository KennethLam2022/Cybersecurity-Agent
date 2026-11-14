from pathlib import Path
from html.parser import HTMLParser


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


def test_document_stat_cards_keep_readable_columns_on_desktop_and_mobile():
    content = (Path(__file__).parents[1] / "static" / "data_preview.html").read_text(encoding="utf-8")

    assert "grid-template-columns: repeat(auto-fit, minmax(min(100%, 220px), 1fr))" in content
    assert ".stat-item .info { min-width: 0; }" in content


def test_document_preview_escapes_untrusted_metadata_in_html_contexts():
    content = (Path(__file__).parents[1] / "static" / "data_preview.html").read_text(encoding="utf-8")
    render = content[content.index("function render() {"):content.index("function filterFiles() {")]

    for expression in (
        "escHtml(f.id)",
        "escHtml(f.ext.toUpperCase())",
        "escHtml(f.name)",
        "escHtml(f.path || '')",
        "escHtml(statusLabel(f.state, f.isDup))",
        "escHtml(f.dedupReason || '')",
        "escHtml(f.dedupLayer)",
    ):
        assert expression in render, expression

    for unsafe_expression in (
        "data-id=\"${f.id}\"",
        "data-id=\"${f.id}",
        "<div class=\"fname\">${f.name}</div>",
        "<div class=\"fpath\" style=\"font-size:11px;color:#6e6e73;\">${f.path || ''}</div>",
        "title=\"${f.dedupReason || ''}\"",
        "${statusLabel(f.state, f.isDup)}",
    ):
        assert unsafe_expression not in render, unsafe_expression


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


def test_admin_knowledge_navigation_links_to_governance_page():
    content = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")

    assert 'data-tab="knowledgeBases" role="link" onclick="location.href=\'/admin/knowledge-bases\'"' in content
    assert "'knowledgeBases']" in content


def test_knowledge_base_risky_actions_require_targeted_confirmation():
    content = (Path(__file__).parents[1] / "static" / "knowledge_base_governance.html").read_text(encoding="utf-8")
    set_status = content[content.index("async function setKbStatus"):content.index("async function setDocStatus")]
    revoke = next(line for line in content.splitlines() if "async function revokeAccess" in line)

    assert "if(!confirm(`确认${action}知识库 ${id}" in set_status
    assert "此操作会改变该知识库的可用性" in set_status
    assert "if(!confirm(`确认撤销知识库 ${id}" in revoke
    assert "将失去该知识库访问权" in revoke


class _AccessibleControlParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.controls = []
        self.labels = []
        self.current_label = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "label":
            self.current_label = {"for": attrs.get("for"), "controls": []}
            self.labels.append(self.current_label)
        if tag in {"input", "select", "textarea"}:
            control = {"id": attrs.get("id"), "aria": attrs.get("aria-label")}
            self.controls.append(control)
            if self.current_label is not None:
                self.current_label["controls"].append(control)

    def handle_endtag(self, tag):
        if tag == "label":
            self.current_label = None


def test_knowledge_base_form_controls_have_accessible_names():
    page = (Path(__file__).parents[1] / "static" / "knowledge_base_governance.html").read_text(encoding="utf-8")
    parser = _AccessibleControlParser()
    parser.feed(page)
    unlabelled = [
        control for control in parser.controls
        if not control["aria"] and not any(
            control in label["controls"] or label["for"] == control["id"]
            for label in parser.labels
        )
    ]
    assert not unlabelled, unlabelled


def test_model_configuration_template_associates_labels_with_controls():
    page = (Path(__file__).parents[1] / "static" / "admin_model_preview.html").read_text(encoding="utf-8")
    template = page[page.index("function buildCard(c)"):page.index("// ─── Events")]
    for field in ("provider", "model", "baseUrl", "apiKey"):
        assert f'<label for="${{c.id}}-{field}">' in template
        assert f'id="${{c.id}}-{field}"' in template
