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


@router.get("/favicon.ico", include_in_schema=False)
async def favicon():
    return HTMLResponse("")
