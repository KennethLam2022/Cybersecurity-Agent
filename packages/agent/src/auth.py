"""会话认证路由辅助 + SSRF 域名白名单 + 管理路由检查

用法：
  from auth import is_admin_route, validate_llm_url
"""
import os

# ---- SSRF 防护：LLM API 域名白名单 ----
_ALLOWED_LLM_DOMAINS = {
    "api.siliconflow.cn",
    "api.deepseek.com",
    "dashscope.aliyuncs.com",
    "open.bigmodel.cn",
    "api.moonshot.cn",
    "qianfan.baidubce.com",
    "openrouter.ai",
    "api.openai.com",
    "api.anthropic.com",
    "api.googleapis.com",
    "generativelanguage.googleapis.com",
    "token.sensenova.cn",
    "api.sensenova.com.cn",
}


def validate_llm_url(url: str) -> bool:
    """校验 LLM API URL 是否在白名单内，防止 SSRF"""
    from urllib.parse import urlparse
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False
        if hostname in _ALLOWED_LLM_DOMAINS:
            return True
        for allowed in _ALLOWED_LLM_DOMAINS:
            if hostname.endswith("." + allowed):
                return True
        return False
    except Exception:
        return False


# ---- 管理路由检查 ----
_ADMIN_PREFIXES = {
    "/admin",
    "/admin/",
    "/api/admin/",
    "/api/documents/",
    "/api/llm/configs/",
    "/api/agent-eval/",
    "/api/prompt/ab/",
}

_ADMIN_ROUTES = [
    "/api/conversations/detail",
    "/api/conversations/stats",
    "/api/conversations/{conv_id}/hard",
    "/api/conversations/{conv_id}/jailbreak-status",
    "/api/conversations/{conv_id}/jailbreak-report",
    "/api/llm/config",
    "/api/llm/presets",
    "/api/llm/test",
    "/api/llm/refresh-models",
]

PUBLIC_ROUTES = {
    "/", "/login", "/admin/login", "/setup", "/chat",
    "/api/rating",
    # Profile registry only exposes taxonomy metadata; document content and
    # all write/migration endpoints remain protected by /api/documents/.
    "/api/documents/profile-registry",
    "/api/llm/config/current",
    "/api/auth/sso/providers",
}

PUBLIC_PREFIXES = {"/static/", "/api/auth/", "/api/shared/", "/shared/"}

# Older administrative domains are retained during the RBAC migration.  They
# are deliberately platform-only until their handlers have resource-level
# tenant checks; this is a fail-closed bridge, not a substitute for the final
# per-resource authorization work.
_PLATFORM_ONLY_ADMIN_PREFIXES = (
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
