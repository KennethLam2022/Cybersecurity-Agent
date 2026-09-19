"""SSO/LDAP enterprise identity protocol support (P6-D1).

This module stays generic: it only performs LDAP/OIDC protocol work. Account
provisioning, approval, and session creation are handled by ConversationMemory
so SSO identities share the same tenant/user/agent boundaries as local
password accounts.
"""
from __future__ import annotations

import base64
import json
import urllib.parse
import urllib.request
import ipaddress
import socket
from typing import Any

OIDC_PRESETS = {
    "wechat_work": {"name": "企业微信 OIDC", "issuer_hint": "https://open.work.weixin.qq.com/wwopen/sso/3rd_qrConnect", "email_attr": "email", "display_attr": "name", "scopes": ["openid", "profile", "email"]},
    "dingtalk": {"name": "钉钉 OIDC", "issuer_hint": "https://login.dingtalk.com/oauth2", "email_attr": "email", "display_attr": "name", "scopes": ["openid", "profile", "email"]},
    "feishu": {"name": "飞书 OIDC", "issuer_hint": "https://open.feishu.cn/open-apis/authen/v1/authorize", "email_attr": "email", "display_attr": "name", "scopes": ["openid", "profile", "email"]},
}


def get_oidc_presets() -> dict:
    return {key: dict(value) for key, value in OIDC_PRESETS.items()}

try:
    import ldap3  # type: ignore
    LDAP_AVAILABLE = True
except ImportError:  # pragma: no cover - optional dependency
    ldap3 = None  # type: ignore
    LDAP_AVAILABLE = False

_OIDC_WELL_KNOWN_PATHS = (
    "/.well-known/openid-configuration",
    "/.well-known/oauth-authorization-server",
)


class _RejectRedirect(urllib.request.HTTPRedirectHandler):
    """OIDC endpoints must not silently redirect to an unvalidated host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401
        raise ValueError("OIDC 出站请求禁止重定向")


_OIDC_OPENER = urllib.request.build_opener(_RejectRedirect)


def _validate_oidc_outbound_url(url: str, *, require_https: bool = True) -> str:
    """Validate an OIDC endpoint immediately before making an outbound call."""
    value = _clean_str(url).rstrip("/")
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" and (require_https or parsed.scheme not in {"http", "https"}):
        raise ValueError("OIDC 出站地址必须使用 HTTPS")
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname:
        raise ValueError("OIDC 出站地址缺少主机名")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("OIDC 出站地址无法解析") from exc
    for address in addresses:
        parsed_address = ipaddress.ip_address(address)
        if (parsed_address.is_private or parsed_address.is_loopback
                or parsed_address.is_link_local or parsed_address.is_reserved
                or parsed_address.is_multicast or parsed_address.is_unspecified):
            raise ValueError("OIDC 出站地址不得指向内网、回环或保留地址")
    return value


def _clean_str(value: Any, default: str = "") -> str:
    return str(value or default).strip()


def _require_ldap() -> None:
    if not LDAP_AVAILABLE:
        raise RuntimeError(
            "LDAP 登录需要安装 ldap3 依赖。请运行: pip install ldap3，"
            "或 pip install -r packages/agent/requirements-optional.txt"
        )


def validate_ldap_config(config: dict | None) -> dict:
    """Validate and normalize LDAP provider configuration."""
    cfg = config or {}
    server_url = _clean_str(cfg.get("server_url"))
    base_dn = _clean_str(cfg.get("base_dn"))
    if not server_url:
        raise ValueError("LDAP 服务器地址 (server_url) 不能为空")
    if not server_url.startswith(("ldap://", "ldaps://")):
        raise ValueError("LDAP 服务器地址必须以 ldap:// 或 ldaps:// 开头")
    if not base_dn:
        raise ValueError("LDAP 基础 DN (base_dn) 不能为空")
    try:
        timeout = max(1, min(int(cfg.get("connect_timeout") or 5), 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("LDAP 连接超时必须为整数") from exc
    return {
        "server_url": server_url,
        "bind_dn": _clean_str(cfg.get("bind_dn")),
        "base_dn": base_dn,
        "user_attr": _clean_str(cfg.get("user_attr"), "sAMAccountName"),
        "display_attr": _clean_str(cfg.get("display_attr"), "displayName"),
        "email_attr": _clean_str(cfg.get("email_attr"), "mail"),
        "filter": _clean_str(cfg.get("filter"), "(objectClass=person)"),
        "use_tls": bool(cfg.get("use_tls")),
        "connect_timeout": timeout,
    }


def validate_oidc_config(config: dict | None) -> dict:
    """Validate and normalize OpenID Connect provider configuration."""
    cfg = config or {}
    issuer_url = _clean_str(cfg.get("issuer_url"))
    client_id = _clean_str(cfg.get("client_id"))
    redirect_uri = _clean_str(cfg.get("redirect_uri"))
    if not issuer_url:
        raise ValueError("OIDC 颁发者地址 (issuer_url) 不能为空")
    if not issuer_url.startswith(("https://", "http://")):
        raise ValueError("OIDC 颁发者地址必须以 http(s):// 开头")
    parsed_issuer = urllib.parse.urlparse(issuer_url)
    if parsed_issuer.scheme != "https":
        raise ValueError("OIDC 颁发者地址必须使用 HTTPS")
    hostname = (parsed_issuer.hostname or "").lower().rstrip(".")
    if not hostname:
        raise ValueError("OIDC 颁发者地址缺少主机名")
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(hostname, None, type=socket.SOCK_STREAM)}
    except OSError:
        # DNS may be unavailable during configuration; the actual connection
        # path performs the same check again before making an outbound request.
        addresses = set()
    for address in addresses:
        parsed_address = ipaddress.ip_address(address)
        if (parsed_address.is_private or parsed_address.is_loopback
                or parsed_address.is_link_local or parsed_address.is_reserved):
            raise ValueError("OIDC 颁发者地址不得指向内网或回环地址")
    if not client_id:
        raise ValueError("OIDC Client ID 不能为空")
    if not redirect_uri.startswith(("http://", "https://")):
        raise ValueError("OIDC 回调地址 (redirect_uri) 必须为 http(s) 地址")
    scopes = [
        str(item).strip()
        for item in (cfg.get("scopes") or ["openid", "profile", "email"])
        if str(item).strip()
    ]
    if "openid" not in scopes:
        scopes.insert(0, "openid")
    try:
        timeout = max(1, min(int(cfg.get("connect_timeout") or 8), 60))
    except (TypeError, ValueError) as exc:
        raise ValueError("OIDC 连接超时必须为整数") from exc
    return {
        "issuer_url": issuer_url.rstrip("/"),
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "scopes": scopes,
        "connect_timeout": timeout,
        "email_attr": _clean_str(cfg.get("email_attr"), "email"),
        "display_attr": _clean_str(cfg.get("display_attr"), "name"),
    }


def _http_get_json(url: str, timeout: int) -> dict:
    url = _validate_oidc_outbound_url(url)
    request = urllib.request.Request(
        url, headers={"Accept": "application/json"},
    )
    with _OIDC_OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _http_post_form(url: str, data: dict, timeout: int) -> dict:
    url = _validate_oidc_outbound_url(url)
    body = urllib.parse.urlencode(data).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    with _OIDC_OPENER.open(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def discover_oidc_provider(issuer_url: str, timeout: int = 8) -> dict:
    """Fetch OIDC discovery metadata from the issuer."""
    issuer_url = _validate_oidc_outbound_url(issuer_url)
    if not issuer_url:
        raise ValueError("OIDC 颁发者地址不能为空")
    last_error: Exception | None = None
    for path in _OIDC_WELL_KNOWN_PATHS:
        try:
            return _http_get_json(issuer_url + path, timeout)
        except Exception as exc:  # noqa: BLE001 - try next well-known path
            last_error = exc
    raise RuntimeError(f"无法从 {issuer_url} 获取 OIDC 发现信息: {last_error}")


def build_oidc_authorize_url(provider: dict, state: str, nonce: str = "") -> str:
    """Build the authorization-code authorize URL from provider + discovery."""
    cfg = provider.get("config") or {}
    discovery = provider.get("discovery") or {}
    authorization_endpoint = (
        discovery.get("authorization_endpoint") or cfg.get("authorization_endpoint") or ""
    )
    if not authorization_endpoint:
        raise RuntimeError("OIDC 发现信息缺少 authorization_endpoint")
    params = {
        "response_type": "code",
        "client_id": cfg.get("client_id", ""),
        "redirect_uri": cfg.get("redirect_uri", ""),
        "scope": " ".join(cfg.get("scopes") or ["openid", "profile", "email"]),
        "state": state,
    }
    if nonce:
        params["nonce"] = nonce
    separator = "&" if "?" in authorization_endpoint else "?"
    return authorization_endpoint + separator + urllib.parse.urlencode(params)


def _decode_jwt_payload(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
    except Exception:
        return {}


def exchange_oidc_code(provider: dict, secrets: dict, code: str) -> dict:
    """Exchange an authorization code and return merged userinfo."""
    cfg = provider.get("config") or {}
    discovery = provider.get("discovery") or {}
    token_endpoint = discovery.get("token_endpoint") or cfg.get("token_endpoint") or ""
    if not token_endpoint:
        raise RuntimeError("OIDC 发现信息缺少 token_endpoint")
    timeout = int(cfg.get("connect_timeout") or 8)
    token_response = _http_post_form(
        token_endpoint,
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": cfg.get("redirect_uri", ""),
            "client_id": cfg.get("client_id", ""),
            "client_secret": str(secrets.get("client_secret") or ""),
        },
        timeout,
    )
    if "error" in token_response:
        detail = f"{token_response.get('error')} {token_response.get('error_description', '')}".strip()
        raise RuntimeError(f"OIDC 令牌交换失败: {detail}")
    access_token = _clean_str(token_response.get("access_token"))
    if not access_token:
        raise RuntimeError("OIDC 令牌响应缺少 access_token")
    userinfo: dict = {}
    userinfo_endpoint = discovery.get("userinfo_endpoint")
    if userinfo_endpoint:
        userinfo_endpoint = _validate_oidc_outbound_url(userinfo_endpoint)
        request = urllib.request.Request(
            userinfo_endpoint,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
        )
        try:
            with _OIDC_OPENER.open(request, timeout=timeout) as response:
                userinfo = json.loads(response.read().decode("utf-8"))
        except Exception as exc:  # noqa: BLE001 - surface as login error
            raise RuntimeError(f"获取 OIDC 用户信息失败: {exc}") from exc
    id_token = _clean_str(token_response.get("id_token"))
    if id_token:
        userinfo = {**_decode_jwt_payload(id_token), **userinfo}
    return userinfo


def extract_oidc_identity(provider: dict, userinfo: dict) -> dict:
    """Map OIDC userinfo/id-token claims to a stable identity."""
    cfg = provider.get("config") or {}
    email_attr = cfg.get("email_attr") or "email"
    display_attr = cfg.get("display_attr") or "name"
    email = _clean_str(userinfo.get(email_attr) or userinfo.get("email"))
    subject = _clean_str(userinfo.get("sub") or email)
    if not subject:
        raise RuntimeError("OIDC 用户信息缺少 sub，无法建立身份绑定")
    if not email:
        raise RuntimeError("OIDC 用户信息缺少 email，无法建立本地账号")
    display_name = _clean_str(
        userinfo.get(display_attr) or userinfo.get("preferred_username") or email.split("@")[0],
    )
    return {
        "subject": subject,
        "email": email.lower(),
        "display_name": display_name,
    }


def _escape_ldap_filter(value: str) -> str:
    return (
        value.replace("\\", "\\5c")
        .replace("*", "\\2a")
        .replace("(", "\\28")
        .replace(")", "\\29")
        .replace("\x00", "\\00")
    )


def _new_ldap_connection(config: dict) -> Any:
    _require_ldap()
    return ldap3.Connection(
        ldap3.Server(
            config["server_url"],
            use_ssl=bool(config.get("use_tls")),
            connect_timeout=int(config.get("connect_timeout") or 5),
            get_info=ldap3.NONE,
        ),
        user=_clean_str(config.get("bind_dn")) or None,
        password=_clean_str(config.get("_bind_password")) or None,
        auto_bind=False,
        raise_exceptions=True,
    )


def test_ldap_connection(config: dict, secrets: dict) -> dict:
    """Bind against the configured LDAP service account and report status."""
    connection_config = dict(config or {})
    connection_config["_bind_password"] = secrets.get("bind_password", "")
    connection = _new_ldap_connection(connection_config)
    try:
        if not connection.bind():
            raise RuntimeError(f"LDAP 服务账号绑定失败: {connection.result}")
        return {
            "ok": True,
            "server": config.get("server_url", ""),
            "base_dn": config.get("base_dn", ""),
            "bind_dn": config.get("bind_dn") or "(anonymous)",
        }
    finally:
        try:
            connection.unbind()
        except Exception:
            pass


def authenticate_ldap(config: dict, secrets: dict, username: str, password: str) -> dict:
    """Search for the user with the service account, then verify user bind."""
    _require_ldap()
    username = _clean_str(username)
    password = str(password or "")
    if not username or not password:
        raise ValueError("请输入 LDAP 用户名和密码")
    connection_config = dict(config or {})
    connection_config["_bind_password"] = secrets.get("bind_password", "")
    connection = _new_ldap_connection(connection_config)
    try:
        if not connection.bind():
            raise RuntimeError(f"LDAP 服务账号绑定失败: {connection.result}")
        user_attr = config.get("user_attr") or "sAMAccountName"
        email_attr = config.get("email_attr") or "mail"
        display_attr = config.get("display_attr") or "displayName"
        search_filter = config.get("filter") or "(objectClass=person)"
        query = f"(&{search_filter}({user_attr}={_escape_ldap_filter(username)}))"
        connection.search(
            config["base_dn"],
            query,
            search_scope=ldap3.SUBTREE,
            attributes=[display_attr, email_attr],
        )
        if not connection.entries:
            raise ValueError("LDAP 用户不存在或未匹配")
        entry = connection.entries[0]
        user_dn = entry.entry_dn
        try:
            display_name = _clean_str(getattr(entry, display_attr, ""))
        except Exception:
            display_name = ""
        try:
            email = _clean_str(getattr(entry, email_attr, ""))
        except Exception:
            email = ""
        user_connection = ldap3.Connection(
            ldap3.Server(
                config["server_url"],
                use_ssl=bool(config.get("use_tls")),
                connect_timeout=int(config.get("connect_timeout") or 5),
            ),
            user=user_dn,
            password=password,
            auto_bind=False,
            raise_exceptions=True,
        )
        try:
            if not user_connection.bind():
                raise ValueError("LDAP 密码验证失败")
        finally:
            try:
                user_connection.unbind()
            except Exception:
                pass
        if not email:
            raise ValueError(
                f"LDAP 用户 {user_dn} 缺少 {email_attr} 属性，无法建立本地账号",
            )
        return {
            "subject": user_dn.lower(),
            "email": email.lower(),
            "display_name": display_name or email.split("@")[0],
        }
    finally:
        try:
            connection.unbind()
        except Exception:
            pass
