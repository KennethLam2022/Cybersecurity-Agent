"""
管理页面路由 — 从 main.py 拆分（H-3 代码质量审计修复）

仅包含 Admin 页面服务路由，不含业务逻辑。
"""
import os
from pathlib import Path
from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

# 路径常量（与 main.py 保持一致）
_BASE = Path(__file__).parent
_STATIC = _BASE / "static"
_TEMPLATES = _BASE / "templates"


@router.get("/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page():
    """Dedicated browser login page for user and administrator sessions."""
    html_path = _TEMPLATES / "login.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>登录页面未找到</h1>", status_code=500)


@router.get("/admin/login", response_class=HTMLResponse, include_in_schema=False)
async def admin_login_page():
    """Dedicated management-console login page."""
    html_path = _TEMPLATES / "admin_login.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>后台登录页面未找到</h1>", status_code=500)


@router.get("/register", response_class=HTMLResponse, include_in_schema=False)
async def register_page():
    """Dedicated public self-registration page."""
    html_path = _TEMPLATES / "register.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>注册页面未找到</h1>", status_code=500)


@router.get("/setup", response_class=HTMLResponse, include_in_schema=False)
async def setup_page():
    """One-time first-run platform administrator activation page."""
    html_path = _TEMPLATES / "setup.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>首次初始化页面未找到</h1>", status_code=500)


@router.get("/admin", response_class=HTMLResponse, include_in_schema=False)
async def admin_dashboard():
    """管理后台看板"""
    html_path = _TEMPLATES / "admin.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
        )
    return HTMLResponse("<h1>管理后台页面未找到</h1><p>请检查 templates/admin.html</p>")


@router.get("/admin/documents", response_class=HTMLResponse, include_in_schema=False)
async def admin_documents():
    """文档入库管理页面"""
    html_path = _STATIC / "data_preview.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
        )
    return HTMLResponse("<h1>页面未找到</h1>")


@router.get("/admin/knowledge-bases", response_class=HTMLResponse, include_in_schema=False)
async def admin_knowledge_bases():
    """Knowledge base governance page."""
    html_path = _STATIC / "knowledge_base_governance.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>页面未找到</h1>")


@router.get("/embed", response_class=HTMLResponse, include_in_schema=False)
async def embedded_chat():
    """受控嵌入式聊天页面；只接受短期 Embed Token，不接收管理凭证。"""
    html_path = _STATIC / "embed_chat.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>页面未找到</h1>")


@router.get("/shared/conversation/{token}", response_class=HTMLResponse, include_in_schema=False)
async def shared_conversation(token: str):
    """Public, read-only conversation view; the token is verified by its API call."""
    html_path = _STATIC / "shared_conversation.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>页面未找到</h1>")


@router.api_route("/admin/model-config", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def admin_model_config():
    """后端模型配置预览页"""
    if not _STATIC.exists():
        return HTMLResponse("<h1>静态文件目录不存在</h1>")
    html_path = _STATIC / "admin_model_preview.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"}
        )
    return HTMLResponse("<h1>页面未找到</h1>")


@router.api_route("/admin/langfuse-config", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def admin_langfuse_config():
    """Langfuse observability configuration page."""
    html_path = _STATIC / "langfuse_config.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>页面未找到</h1>")


@router.api_route("/admin/sso-config", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def admin_sso_config():
    """SSO/LDAP enterprise identity configuration page."""
    html_path = _STATIC / "sso_config.html"
    if html_path.exists():
        return HTMLResponse(
            html_path.read_text(encoding="utf-8"),
            headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"},
        )
    return HTMLResponse("<h1>页面未找到</h1>")


@router.api_route("/admin/email-notifications", methods=["GET", "HEAD"], response_class=HTMLResponse, include_in_schema=False)
async def admin_email_notifications():
    html_path = _STATIC / "email_notifications.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})
    return HTMLResponse("<h1>页面未找到</h1>")


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return HTMLResponse("")
