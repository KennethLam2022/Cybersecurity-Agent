from pathlib import Path


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
