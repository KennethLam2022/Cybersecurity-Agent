from pathlib import Path
from html.parser import HTMLParser


class _PagePurposeParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.details = []
        self.current_details = None
        self.in_summary = False

    def handle_starttag(self, tag, attrs):
        if tag == "details":
            self.current_details = {"open": "open" in dict(attrs), "summary": "", "text": ""}
            self.details.append(self.current_details)
        elif tag == "summary" and self.current_details:
            self.in_summary = True

    def handle_endtag(self, tag):
        if tag == "summary":
            self.in_summary = False
        elif tag == "details":
            self.current_details = None

    def handle_data(self, data):
        if self.current_details:
            self.current_details["text"] += data
            if self.in_summary:
                self.current_details["summary"] += data


def test_admin_navigation_persists_and_restores_the_selected_tab():
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")
    assert "url.searchParams.set('tab', name)" in html
    assert "const requestedTab = new URLSearchParams(location.search).get('tab')" in html


def test_admin_navigation_checks_role_visibility_before_restoring_a_tab():
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")
    principal_loader = html[html.index("async function loadAdminPrincipal()") : html.index("function currentAdminActor")]
    assert "const requestedTab = new URLSearchParams(location.search).get('tab')" in principal_loader
    assert principal_loader.index("tab.classList.toggle('role-hidden'") < principal_loader.index("switchTab(requestedTab)")
    assert "target.classList.contains('role-hidden')" in html


def test_admin_navigation_falls_back_to_a_tab_visible_to_the_current_role():
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")
    switch_tab = html[html.index("function switchTab(name)") : html.index("const tabContext = {")]
    assert "document.querySelector('.tab-item:not(.role-hidden)')?.dataset.tab" in switch_tab
    assert "name = 'detail'" not in switch_tab


def test_platform_only_tab_request_falls_back_instead_of_returning_early():
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")
    switch_tab = html[html.index("function switchTab(name)") : html.index("const tabContext = {")]
    permission_guard = switch_tab[: switch_tab.index("const target =")]
    assert "name = document.querySelector('.tab-item:not(.role-hidden)')?.dataset.tab" in permission_guard
    assert "return;" not in permission_guard


def test_workspace_and_notification_purpose_is_collapsed_but_available():
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")
    expected = {
        "tabWorkspaces": "管理组织工作区、用户账号与 Agent 的生命周期。",
        "tabNotifications": "集中处理系统事件、审批、评测、入库和安全告警。",
    }
    for tab_id, purpose in expected.items():
        section_start = html.index(f'<div class="tab-content" id="{tab_id}">')
        section_end = html.find('<div class="tab-content"', section_start + 1)
        section = html[section_start:section_end if section_end >= 0 else None]
        parser = _PagePurposeParser()
        parser.feed(section)
        assert len(parser.details) == 1
        assert parser.details[0]["open"] is False
        assert parser.details[0]["summary"].strip() == "页面说明"
        assert purpose in parser.details[0]["text"]
