"""P4 identity helpers for local users, organizations, and agent workspaces."""
from __future__ import annotations

from dataclasses import dataclass
import os
from auth import is_admin_route
from fastapi import HTTPException, Request


DEFAULT_TENANT_ID = "local-default"
DEFAULT_USER_ID = "local-owner"
DEFAULT_AGENT_ID = "default-agent"
FRONT_SESSION_COOKIE = "securenexus_session"
ADMIN_SESSION_COOKIE = "securenexus_admin_session"
ADMIN_CSRF_COOKIE = "securenexus_admin_csrf"
ADMIN_CONTEXT_HEADER = "X-Admin-Context"


# Monitoring is intentionally review-first: only the platform administrator can
# execute a change; organization administrators may prepare and review plans.
ROLE_PERMISSIONS = {
    "platform_admin": {"monitoring.read", "monitoring.plan.edit", "monitoring.review", "monitoring.change.execute", "monitoring.validation.run", "memory.profile.propose", "memory.profile.review", "memory.conflict.propose", "memory.conflict.review", "graph.read", "graph.extract", "graph.review", "workflow.read", "workflow.write", "workflow.execute", "audit.read", "tenant.manage", "platform.manage", "secrets.read", "secrets.manage", "mcp.read", "mcp.manage", "mcp.execute", "extension.execute", "evaluation.manage"},
    "org_admin": {"monitoring.read", "monitoring.plan.edit", "monitoring.review", "memory.profile.propose", "memory.profile.review", "memory.conflict.propose", "memory.conflict.review", "graph.read", "graph.extract", "graph.review", "workflow.read", "workflow.write", "workflow.execute", "audit.read", "tenant.manage", "secrets.read", "secrets.manage", "mcp.read", "mcp.manage", "mcp.execute", "extension.execute", "evaluation.manage"},
    "auditor": {"monitoring.read", "graph.read", "workflow.read", "audit.read"},
    "agent_admin": {"monitoring.read"},
    "user": {"memory.profile.propose", "memory.conflict.propose"},
}

# Custom roles are tenant-scoped and can never become a substitute for the
# platform security boundary. These permissions remain system-role-only.
CUSTOM_ROLE_DENIED_PERMISSIONS = {
    "platform.manage", "secrets.read", "secrets.manage",
    "monitoring.change.execute",
}


@dataclass(frozen=True)
class Principal:
    tenant_id: str
    user_id: str
    agent_id: str
    role: str = "user"
    authenticated: bool = False


def default_principal() -> Principal:
    """Identify legacy local data only; it is never an authenticated user session."""
    return Principal(DEFAULT_TENANT_ID, DEFAULT_USER_ID, DEFAULT_AGENT_ID, "platform_admin", False)


def uses_admin_session(request: Request) -> bool:
    """Whether this request belongs to the admin console session context."""
    if request.headers.get(ADMIN_CONTEXT_HEADER, "").lower() in {"1", "true", "admin"}:
        return True
    # /api/auth/me is shared by both UIs, so the admin UI explicitly marks it.
    request_url = getattr(request, "url", None)
    return is_admin_route(getattr(request_url, "path", ""))


def session_token_from_request(request: Request) -> str:
    """Resolve a browser cookie after honoring an explicit bearer token."""
    authorization = request.headers.get("Authorization", "").strip()
    if authorization:
        return authorization
    cookies = getattr(request, "cookies", {})
    if uses_admin_session(request):
        return str(cookies.get(ADMIN_SESSION_COOKIE, "") or "")
    return str(cookies.get(FRONT_SESSION_COOKIE, "") or "")


def principal_from_request(request: Request, memory) -> Principal:
    authorization = session_token_from_request(request)
    token_from_cookie = bool(authorization and not authorization.lower().startswith("bearer "))
    if not authorization:
        allow_legacy = os.environ.get("ALLOW_LEGACY_LOCAL_WORKSPACE", "0").strip().lower()
        if allow_legacy not in {"1", "true", "yes", "on"}:
            raise HTTPException(status_code=401, detail="请先登录")
        return default_principal()
    if token_from_cookie:
        token = authorization
    else:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise HTTPException(status_code=401, detail="无效的登录凭证")
    session = memory.get_auth_session(token)
    if not session:
        raise HTTPException(status_code=401, detail="登录已过期或无效")
    return Principal(
        tenant_id=session["tenant_id"], user_id=session["user_id"],
        agent_id=session["agent_id"], role=session["role"], authenticated=True,
    )


def has_permission(principal: Principal, permission: str, memory=None) -> bool:
    """Return whether a system or tenant custom role grants the permission."""
    permissions = ROLE_PERMISSIONS.get(principal.role)
    if permissions is None and memory is not None:
        custom = memory.get_custom_role_permissions(principal.role, principal.tenant_id)
        permissions = set(custom or []) if custom else set()
    return permission in (permissions or set())


def require_permission(request: Request, memory, permission: str,
                       tenant_id: str = "") -> Principal:
    """Authenticate, authorize, and enforce tenant scope for an admin action."""
    principal = principal_from_request(request, memory)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="请先使用已登录账号访问管理功能")
    if not has_permission(principal, permission, memory):
        raise HTTPException(status_code=403, detail="当前角色无权执行此操作")
    requested_tenant = str(tenant_id or principal.tenant_id)
    if requested_tenant != principal.tenant_id and principal.role != "platform_admin":
        raise HTTPException(status_code=403, detail="无权访问其他租户的监控数据")
    return principal


def require_platform_permission(request: Request, memory) -> Principal:
    """Require a platform-wide administrative action, not merely an admin token."""
    return require_permission(request, memory, "platform.manage")
