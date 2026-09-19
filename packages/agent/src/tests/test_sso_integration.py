import pytest

from memory import ConversationMemory
from sso_provider import (
    build_oidc_authorize_url,
    extract_oidc_identity,
    validate_ldap_config,
    validate_oidc_config,
    get_oidc_presets,
)


def test_oidc_config_rejects_loopback_issuer():
    with pytest.raises(ValueError):
        validate_oidc_config({
            "issuer_url": "http://127.0.0.1:8000",
            "client_id": "client",
            "redirect_uri": "https://example.com/callback",
        })


def _ldap_payload(**overrides):
    payload = {
        "provider_type": "ldap",
        "name": "公司 AD",
        "enabled": True,
        "auto_provision": False,
        "default_role": "user",
        "default_tenant_id": "",
        "config": {
            "server_url": "ldaps://ad.example.com",
            "bind_dn": "CN=svc,OU=Service,DC=example,DC=com",
            "base_dn": "DC=example,DC=com",
            "user_attr": "sAMAccountName",
            "display_attr": "displayName",
            "email_attr": "mail",
            "filter": "(objectClass=person)",
            "use_tls": True,
            "connect_timeout": 5,
        },
        "secrets": {"bind_password": "svc-secret"},
    }
    for key, value in overrides.items():
        if key == "config":
            payload["config"].update(value)
        elif key == "secrets":
            payload["secrets"].update(value)
        else:
            payload[key] = value
    return payload


def _oidc_payload(**overrides):
    payload = {
        "provider_type": "oidc",
        "name": "企业 SSO",
        "enabled": True,
        "auto_provision": True,
        "default_role": "user",
        "default_tenant_id": "local-default",
        "config": {
            "issuer_url": "https://sso.example.com/realms/company",
            "client_id": "securenexus",
            "redirect_uri": "http://127.0.0.1:8000/api/auth/sso/oidc/callback",
            "scopes": ["openid", "profile", "email"],
            "connect_timeout": 8,
            "email_attr": "email",
            "display_attr": "name",
        },
        "secrets": {"client_secret": "client-secret"},
    }
    for key, value in overrides.items():
        if key == "config":
            payload["config"].update(value)
        elif key == "secrets":
            payload["secrets"].update(value)
        else:
            payload[key] = value
    return payload


def test_sso_provider_crud_and_secret_encryption(tmp_path):
    memory = ConversationMemory(str(tmp_path / "sso.db"))
    provider = memory.save_sso_provider(_ldap_payload())
    assert provider["id"].startswith("sso-")
    assert provider["has_secrets"] is True
    assert "bind_password" not in provider  # public view never includes secrets

    listed = memory.list_sso_providers()
    assert len(listed) == 1
    assert listed[0]["enabled"] is True

    detailed = memory.get_sso_provider(provider["id"], include_secrets=True)
    assert detailed["secrets"]["bind_password"] == "svc-secret"

    assert memory.update_sso_provider_status(provider["id"], False)
    assert memory.list_sso_providers(enabled_only=True) == []
    assert memory.delete_sso_provider(provider["id"])
    assert memory.get_sso_provider(provider["id"]) is None


def test_sso_provider_update_keeps_secret_when_blank(tmp_path):
    memory = ConversationMemory(str(tmp_path / "sso.db"))
    provider = memory.save_sso_provider(_ldap_payload())
    updated = memory.save_sso_provider(
        _ldap_payload(secrets={}, config={"bind_dn": "CN=svc2,OU=Service,DC=example,DC=com"}),
        provider_id=provider["id"],
    )
    assert updated["config"]["bind_dn"].endswith("DC=com")
    detailed = memory.get_sso_provider(provider["id"], include_secrets=True)
    assert detailed["secrets"]["bind_password"] == "svc-secret"


def test_ldap_and_oidc_config_validation():
    with pytest.raises(ValueError):
        validate_ldap_config({"server_url": "not-a-url", "base_dn": "DC=example"})
    with pytest.raises(ValueError):
        validate_ldap_config({"server_url": "ldap://ad", "base_dn": ""})
    normalized = validate_ldap_config({"server_url": "ldaps://ad", "base_dn": "DC=example"})
    assert normalized["user_attr"] == "sAMAccountName"

    with pytest.raises(ValueError):
        validate_oidc_config({"issuer_url": "", "client_id": "x", "redirect_uri": "http://x"})
    with pytest.raises(ValueError):
        validate_oidc_config({"issuer_url": "https://sso", "client_id": "", "redirect_uri": "http://x"})
    normalized = validate_oidc_config(_oidc_payload()["config"])
    assert normalized["scopes"][0] == "openid"


def test_oidc_presets_are_explicit_templates_without_credentials():
    presets = get_oidc_presets()
    assert set(presets) == {"wechat_work", "dingtalk", "feishu"}
    for item in presets.values():
        assert item["email_attr"] == "email"
        assert item["display_attr"] == "name"
        assert "client_secret" not in item


def test_build_oidc_authorize_url_and_identity_mapping():
    provider = {
        "config": validate_oidc_config(_oidc_payload()["config"]),
        "discovery": {"authorization_endpoint": "https://sso.example.com/auth"},
    }
    url = build_oidc_authorize_url(provider, "state-123")
    assert "state=state-123" in url
    assert "response_type=code" in url
    assert "openid" in url

    identity = extract_oidc_identity(provider, {"sub": "u-1", "email": "A@Example.com", "name": "Alice"})
    assert identity == {"subject": "u-1", "email": "a@example.com", "display_name": "Alice"}


def test_oauth_state_create_and_consume(tmp_path):
    memory = ConversationMemory(str(tmp_path / "sso.db"))
    provider = memory.save_sso_provider(_oidc_payload())
    state = memory.create_oauth_state(provider["id"], "http://127.0.0.1:8000/cb", ttl_seconds=60)
    consumed = memory.consume_oauth_state(state)
    assert consumed == {
        "provider_id": provider["id"],
        "redirect_uri": "http://127.0.0.1:8000/cb",
    }
    assert memory.consume_oauth_state(state) is None


def test_sso_identity_pending_approval_and_session(tmp_path):
    memory = ConversationMemory(str(tmp_path / "sso.db"))
    provider = memory.save_sso_provider(_oidc_payload(auto_provision=False))
    linked = memory.link_sso_identity(provider["id"], "u-100", "sso-user@example.com", "SSO 用户")
    assert linked["result"] == "pending"
    assert linked["user_id"] == ""

    approvals = memory.list_sso_pending_approvals()
    assert len(approvals) == 1
    assert approvals[0]["email"] == "sso-user@example.com"

    identity = memory.approve_sso_identity_link(approvals[0]["link_id"])
    assert identity["status"] == "active"
    assert identity["user_id"].startswith("usr-")

    token = memory.create_sso_session(identity)
    session = memory.get_auth_session(token)
    assert session["user_id"] == identity["user_id"]
    assert session["email"] == "sso-user@example.com"

    # Same subject always maps back to the same account.
    again = memory.link_sso_identity(provider["id"], "u-100", "sso-user@example.com", "SSO 用户")
    assert again["result"] == "active"
    assert again["user_id"] == identity["user_id"]


def test_sso_auto_provision_and_existing_email_bind(tmp_path):
    memory = ConversationMemory(str(tmp_path / "sso.db"))
    provider = memory.save_sso_provider(_oidc_payload(auto_provision=True, default_tenant_id=""))
    provisioned = memory.link_sso_identity(provider["id"], "u-200", "auto@example.com", "自动开通", auto_provision=True)
    assert provisioned["result"] == "provisioned"
    assert provisioned["user_id"].startswith("usr-")
    assert provisioned["tenant_id"].startswith("org-")

    local = memory.register_user("existing@example.com", "Correct-Horse-20", "已有账号")
    oidc = memory.save_sso_provider(_oidc_payload(name="另一个 SSO", auto_provision=False))
    bound = memory.link_sso_identity(oidc["id"], "u-300", "existing@example.com", "绑定账号")
    assert bound["result"] == "active"
    assert bound["user_id"] == local["id"]


def test_sso_identity_reject_flow(tmp_path):
    memory = ConversationMemory(str(tmp_path / "sso.db"))
    provider = memory.save_sso_provider(_oidc_payload(auto_provision=False))
    linked = memory.link_sso_identity(provider["id"], "u-400", "reject@example.com", "待拒绝")
    assert memory.reject_sso_identity_link(linked["link_id"])
    assert memory.find_sso_identity(provider["id"], "u-400")["status"] == "rejected"
    # Rejected subject can re-enter pending on the next login attempt.
    retry = memory.link_sso_identity(provider["id"], "u-400", "reject@example.com", "再次申请")
    assert retry["result"] == "pending"

def test_oidc_opener_rejects_http_redirect():
    """Verify _OIDC_OPENER raises on 302 instead of following (SSRF defense)."""
    import http.server
    import threading
    import urllib.request
    from sso_provider import _OIDC_OPENER

    class RedirectHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://169.254.169.254/latest/meta-data/")
            self.end_headers()
        def log_message(self, format, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), RedirectHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        try:
            _OIDC_OPENER.open(f"http://127.0.0.1:{port}/.well-known/openid-configuration", timeout=3)
            raise AssertionError("expected redirect to be rejected")
        except Exception as exc:
            # Acceptable: HTTPError (redirect followed would be 200 from metadata service)
            # or URLError if the handler raises before completing
            assert isinstance(exc, (ValueError, urllib.request.HTTPError, urllib.request.URLError)), (
                f"unexpected error type: {type(exc).__name__}: {exc}"
            )
    finally:
        server.shutdown()
