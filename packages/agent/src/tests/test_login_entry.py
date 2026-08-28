from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_dedicated_login_page_has_password_registration_and_safe_return_path():
    page = (ROOT / "packages" / "agent" / "src" / "templates" / "login.html").read_text(encoding="utf-8")
    for fragment in ("/api/auth/login", "/api/auth/register", "nextPath", "startsWith('/')", "sso/providers"):
        assert fragment in page


def test_register_url_forces_registration_mode_even_when_template_is_reused():
    page = (ROOT / "packages" / "agent" / "src" / "templates" / "login.html").read_text(encoding="utf-8")
    assert "location.pathname==='/register'" in page
    routes = (ROOT / "packages" / "agent" / "src" / "routes_admin_pages.py").read_text(encoding="utf-8")
    assert '@router.get("/register"' in routes


def test_register_has_a_dedicated_frontend_page():
    page = (ROOT / "packages" / "agent" / "src" / "templates" / "register.html").read_text(encoding="utf-8")
    routes = (ROOT / "packages" / "agent" / "src" / "routes_admin_pages.py").read_text(encoding="utf-8")
    assert 'id="registerForm"' in page
    assert "/api/auth/register" in page
    assert 'html_path = _TEMPLATES / "register.html"' in routes


def test_frontend_registration_controls_navigate_to_dedicated_register_page():
    login = (ROOT / "packages" / "agent" / "src" / "templates" / "login.html").read_text(encoding="utf-8")
    index = (ROOT / "packages" / "agent" / "src" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "location.href='/register'" in login
    assert "location.href='/register?next=/chat'" in index


def test_account_and_admin_user_management_controls_exist():
    index = (ROOT / "packages" / "agent" / "src" / "templates" / "index.html").read_text(encoding="utf-8")
    admin = (ROOT / "packages" / "agent" / "src" / "templates" / "admin.html").read_text(encoding="utf-8")
    routes = (ROOT / "packages" / "agent" / "src" / "routes_api.py").read_text(encoding="utf-8")
    assert "function loadMyUsage" in index
    assert "const password_confirm" in index
    assert "/password-reset" in routes
    assert "重置密码" in admin


def test_organization_workspace_ui_exposes_owner_counts_and_creation():
    admin = (ROOT / "packages" / "agent" / "src" / "templates" / "admin.html").read_text(encoding="utf-8")
    memory = (ROOT / "packages" / "agent" / "src" / "memory.py").read_text(encoding="utf-8")
    routes = (ROOT / "packages" / "agent" / "src" / "routes_api.py").read_text(encoding="utf-8")
    assert "组织工作区" in admin
    assert "owner_name" in memory
    assert "knowledge_base_count" in memory
    assert "createFormalOrganization" in admin
    assert '@router.post("/api/admin/organizations")' in routes


def test_admin_login_lock_management_is_exposed():
    admin = (ROOT / "packages" / "agent" / "src" / "templates" / "admin.html").read_text(encoding="utf-8")
    routes = (ROOT / "packages" / "agent" / "src" / "routes_api.py").read_text(encoding="utf-8")
    assert "账号登录锁定" in admin
    assert "loadLoginLocks" in admin
    assert "/api/admin/security/login-locks" in routes


def test_admin_browser_navigation_redirects_unauthenticated_users_to_login():
    source = (ROOT / "packages" / "agent" / "src" / "main.py").read_text(encoding="utf-8")
    auth = (ROOT / "packages" / "agent" / "src" / "auth.py").read_text(encoding="utf-8")
    assert "return _login_redirect(request.url.path)" in source
    assert 'login_path = "/admin/login" if path.startswith("/admin") else "/login"' in source
    assert 'login_path + "?next="' in source
    assert "if not principal.authenticated" in source
    assert '"/admin",' in auth


def test_chat_page_and_login_route_are_explicit():
    routes = (ROOT / "packages" / "agent" / "src" / "routes_api.py").read_text(encoding="utf-8")
    pages = (ROOT / "packages" / "agent" / "src" / "routes_admin_pages.py").read_text(encoding="utf-8")
    assert '@router.get("/chat"' in routes
    assert '@router.get("/login"' in pages
    assert '@router.get("/setup"' in pages
    assert "if not principal.authenticated" in routes


def test_admin_has_a_dedicated_login_entry_and_admin_only_api():
    pages = (ROOT / "packages" / "agent" / "src" / "routes_admin_pages.py").read_text(encoding="utf-8")
    auth = (ROOT / "packages" / "agent" / "src" / "auth.py").read_text(encoding="utf-8")
    main = (ROOT / "packages" / "agent" / "src" / "main.py").read_text(encoding="utf-8")
    api = (ROOT / "packages" / "agent" / "src" / "routes_api.py").read_text(encoding="utf-8")
    page = (ROOT / "packages" / "agent" / "src" / "templates" / "admin_login.html").read_text(encoding="utf-8")
    assert '@router.get("/admin/login"' in pages
    assert "admin_login.html" in pages
    assert '"/admin/login"' in auth
    assert 'login_path = "/admin/login"' in main
    assert '@router.post("/api/auth/admin-login")' in api
    assert "平台管理员或组织管理员" in page
    assert "/api/auth/admin-login" in page


def test_chat_client_redirects_when_the_browser_session_is_missing():
    page = (ROOT / "packages" / "agent" / "src" / "templates" / "index.html").read_text(encoding="utf-8")
    assert "const authenticated = await refreshAccount()" in page
    assert "location.replace('/login?next=/chat')" in page


def test_first_run_setup_is_one_time_and_requires_password_confirmation():
    memory = (ROOT / "packages" / "agent" / "src" / "memory.py").read_text(encoding="utf-8")
    setup = (ROOT / "packages" / "agent" / "src" / "templates" / "setup.html").read_text(encoding="utf-8")
    assert "def bootstrap_platform_admin" in memory
    assert "BEGIN IMMEDIATE" in memory
    assert "/api/auth/bootstrap" in setup
    assert "passwordConfirm" in setup
    assert "setup_token" not in setup


def test_password_policy_is_enforced_server_side_and_shown_in_forms():
    memory = (ROOT / "packages" / "agent" / "src" / "memory.py").read_text(encoding="utf-8")
    login = (ROOT / "packages" / "agent" / "src" / "templates" / "login.html").read_text(encoding="utf-8")
    setup = (ROOT / "packages" / "agent" / "src" / "templates" / "setup.html").read_text(encoding="utf-8")
    assert "def _validate_account_password" in memory
    assert "包含大写字母、小写字母和数字" in memory
    assert "passwordConfirm" in login
    assert "passwordConfirm" in setup
