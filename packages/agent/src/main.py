"""FastAPI Web 应用入口 — 网络安全 RAG Agent 聊天界面

启动：
  python main.py
"""
import os
import sys
import secrets
import urllib.parse
from pathlib import Path

# 必须在任何应用模块导入前加入 sys.path，确保 retriever 等模块可被找到
_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
_PREPROCESSOR_SRC = str(Path(_SRC).parent.parent.parent / "packages" / "preprocessor" / "src")
if _PREPROCESSOR_SRC not in sys.path:
    sys.path.insert(0, _PREPROCESSOR_SRC)

from app_lifespan import lifespan
from routes_api_eval import router as api_eval_router
from routes_api_agent_eval import router as api_agent_eval_router
from routes_api import router as api_router
from routes_api_governance import router as api_p8_router
from routes_admin_pages import router as admin_pages_router
from auth import is_admin_route, is_platform_only_admin_route
from app_state import agent
from identity import principal_from_request, has_permission
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _validate_security_configuration() -> None:
    """Reject development-only identity defaults when explicitly running in production."""
    environment = os.environ.get("APP_ENV", "development").strip().lower()
    if environment not in {"production", "prod"}:
        return
    if os.environ.get("ALLOW_LEGACY_LOCAL_WORKSPACE", "0").strip().lower() in {"1", "true", "yes", "on"}:
        raise RuntimeError("生产环境不得启用 ALLOW_LEGACY_LOCAL_WORKSPACE")


def _csrf_is_valid(request: Request) -> bool:
    expected = request.cookies.get("securenexus_csrf", "")
    supplied = request.headers.get("X-CSRF-Token", "")
    return bool(expected and supplied and secrets.compare_digest(expected, supplied))


def _wants_html(request: Request) -> bool:
    """Keep browser navigation friendly while APIs retain JSON auth errors."""
    return request.method.upper() in {"GET", "HEAD"} and "text/html" in request.headers.get("accept", "")


def _login_redirect(path: str) -> RedirectResponse:
    login_path = "/admin/login" if path.startswith("/admin") else "/login"
    return RedirectResponse(url=login_path + "?next=" + urllib.parse.quote(path, safe="/?=&"), status_code=303)

_BASE = Path(__file__).parent
_STATIC = _BASE / "static"
_TEMPLATES = _BASE / "templates"

_validate_security_configuration()
app = FastAPI(title="安枢 SecureNexus", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
app.include_router(admin_pages_router)
app.include_router(api_router)
app.include_router(api_p8_router)
app.include_router(api_eval_router)
app.include_router(api_agent_eval_router)


# ---- 全局认证中间件 ----
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """Authenticate administration through individual user sessions and RBAC."""
    unsafe = request.method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    cookie_session = bool(request.cookies.get("securenexus_session"))
    csrf_exempt = request.url.path.startswith(("/api/auth/", "/api/shared/", "/shared/"))
    if unsafe and cookie_session and not csrf_exempt and not _csrf_is_valid(request):
        return JSONResponse(status_code=403, content={"detail": "CSRF 校验失败"})
    if is_admin_route(request.url.path):
        try:
            principal = principal_from_request(request, agent.memory)
            if not principal.authenticated:
                raise PermissionError("authenticated session required")
        except Exception:
            if _wants_html(request):
                return _login_redirect(request.url.path)
            return JSONResponse(
                status_code=401,
                content={"detail": "请使用已登录的管理员账号访问管理功能"},
            )
        if is_platform_only_admin_route(request.url.path):
            try:
                if not has_permission(principal, "platform.manage", agent.memory):
                    raise PermissionError("platform administrator required")
            except Exception:
                if _wants_html(request):
                    return RedirectResponse(url="/chat?forbidden=admin", status_code=303)
                return JSONResponse(
                    status_code=403,
                    content={"detail": "该平台级管理功能仅限平台管理员"},
                )
    response = await call_next(request)
    return response


if __name__ == "__main__":
    import uvicorn
    import os
    # 单 worker + 线程池：避免 FAISS 多进程冲突，同时确保同步 agent.ask()
    # 不会阻塞事件循环。容器/反向代理部署时通过环境变量暴露监听地址。
    workers = int(os.environ.get("UVICORN_WORKERS", "1"))
    host = os.environ.get("APP_HOST", "127.0.0.1")
    port = int(os.environ.get("APP_PORT", "8000"))
    uvicorn.run("main:app", host=host, port=port, workers=workers)
