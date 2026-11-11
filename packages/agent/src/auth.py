"""会话认证路由辅助 + LLM 公网地址校验 + 管理路由检查

用法：
  from auth import is_admin_route, validate_llm_url
"""
import ipaddress
import socket
from urllib.parse import urlparse


def validate_llm_url(url: str, *, allow_local: bool = False) -> bool:
    """Check a public HTTPS endpoint; return False on malformed or unsafe input.

    Args:
        url: Provider base URL without credentials, query or fragment.
        allow_local: Also permit explicit loopback Ollama on port 11434.
    Returns:
        Whether every resolved address is public, or the Ollama exception applies.
    Raises:
        No URL/DNS parsing errors escape this function.
    Example:
        validate_llm_url("https://provider.example/v1")
    """
    try:
        if not isinstance(url, str) or any(c.isspace() for c in url) or "\\" in url:
            return False
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").lower().rstrip(".")
        port = parsed.port
        if not hostname or parsed.username is not None or parsed.password is not None:
            return False
        if parsed.query or parsed.fragment or port == 0:
            return False
        if (allow_local and hostname in {"localhost", "127.0.0.1", "::1"}
                and port == 11434 and parsed.scheme in {"http", "https"}):
            return True
        if parsed.scheme != "https" or hostname in {"localhost", "localhost.localdomain"}:
            return False
        if hostname.endswith((".localhost", ".local", ".internal")):
            return False
        addresses = socket.getaddrinfo(hostname, port or 443, type=socket.SOCK_STREAM)
        # shortcut: DNS is rechecked, not pinned; hostile DNS needs egress isolation.
        return bool(addresses) and all(
            ipaddress.ip_address(item[4][0]).is_global
            and not ipaddress.ip_address(item[4][0]).is_multicast
            for item in addresses
        ) and _is_public_literal(hostname)
    except (OSError, ValueError, TypeError):
        return False


def _is_public_literal(hostname: str) -> bool:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global and not address.is_multicast


# ---- 管理路由检查 ----
_ADMIN_PREFIXES = {
    "/admin",
    "/admin/",
    "/api/admin/",
    "/api/documents/",
    "/api/llm/configs/",
    "/api/agent-eval/",
    "/api/prompt/ab/",
    "/api/stats/",
    "/api/prompt/",
}

_ADMIN_ROUTES = [
    "/api/conversations/detail",
    "/api/conversations/stats",
    "/api/conversations/{conv_id}/hard",
    "/api/conversations/{conv_id}/jailbreak-status",
    "/api/conversations/{conv_id}/jailbreak-report",
    "/api/llm/config",
    "/api/llm/config/current",
    "/api/llm/configs",
    "/api/llm/presets",
    "/api/llm/test",
    "/api/llm/refresh-models",
    "/api/stats",
]

PUBLIC_ROUTES = {
    "/", "/login", "/admin/login", "/setup", "/chat",
    "/api/rating",
    "/api/auth/sso/providers",
}

PUBLIC_PREFIXES = {"/static/", "/api/auth/", "/api/shared/", "/shared/"}

# Older administrative domains are retained during the RBAC migration.  They
# are deliberately platform-only until their handlers have resource-level
# tenant checks; this is a fail-closed bridge, not a substitute for the final
# per-resource authorization work.
_PLATFORM_ONLY_ADMIN_PREFIXES = (
    "/api/stats",
    "/api/prompt/",
    "/admin/model-config",
    "/admin/langfuse-config",
    "/admin/sso-config",
    "/admin/email-notifications",
    "/api/admin/email-notifications",
    "/api/admin/sso/approvals",
    "/api/admin/external-retrieval",
    "/api/admin/reflection-rules",
    "/api/admin/cleanup",
    "/api/admin/stream",
    "/api/admin/semantic-cache",
    "/api/admin/knowledge-gaps",
)


def is_platform_only_admin_route(path: str) -> bool:
    """Return whether a legacy admin route is temporarily platform-only."""
    return (any(path.startswith(prefix) for prefix in _PLATFORM_ONLY_ADMIN_PREFIXES)
            or path.endswith("/retry-ingestion")
            or path.startswith(("/api/prompt/ab/", "/api/llm/", "/api/documents/")))


def is_admin_route(path: str) -> bool:
    """Return whether a route requires an authenticated management principal."""
    if path in PUBLIC_ROUTES:
        return False

    for prefix in _ADMIN_PREFIXES:
        if path.startswith(prefix):
            return True

    for route in _ADMIN_ROUTES:
        if path == route:
            return True
        if "{" in route:
            base = route.split("{")[0].rstrip("/")
            suffix = route.split("}")[-1]
            if suffix:
                if path.startswith(base + "/") and path.endswith(suffix):
                    middle = path[len(base) + 1: -len(suffix)] if suffix else path[len(base) + 1:]
                    if middle and "/" not in middle:
                        return True
            else:
                if path.startswith(base + "/") and "/" not in path[len(base) + 1:]:
                    return True

    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return False

    return False
