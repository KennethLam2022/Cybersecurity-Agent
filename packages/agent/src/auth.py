"""认证中间件 + SSRF 域名白名单 + 管理路由检查

用法：
  from auth import verify_admin_token, is_admin_route, validate_llm_url
"""
import os
import json
import logging
from fastapi import Request, HTTPException

logger = logging.getLogger(__name__)

# ---- 管理 Token ----
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me-in-production")


async def verify_admin_token(request: Request) -> None:
    """验证管理端点 Token"""
    token = request.headers.get("X-Admin-Token", "")
    if not token or token != _ADMIN_TOKEN:
        logger.warning(f"⚠️ 未授权访问: {request.url.path}")
        raise HTTPException(status_code=403, detail="未授权访问")

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
    "/api/documents/",
    "/api/llm/configs/",
}

_ADMIN_ROUTES = [
    "/api/conversations/detail",
    "/api/conversations/stats",
    "/api/conversations/{conv_id}/hard",
    "/api/llm/config",
    "/api/llm/presets",
    "/api/llm/test",
    "/api/llm/refresh-models",
]

PUBLIC_ROUTES = {
    "/", "/admin",
    "/api/conversations",
    "/api/chat/stream",
    "/api/rating",
    "/api/llm/config/current",
    "/admin/model-config",
    "/api/admin/stream",
}

PUBLIC_PREFIXES = {"/api/conversations/", "/static/"}


def is_admin_route(path: str) -> bool:
    """判断是否为管理路由（需要 X-Admin-Token）"""
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

    if path in PUBLIC_ROUTES:
        return False
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return False

    return False
