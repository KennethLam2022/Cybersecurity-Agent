"""FastAPI Web 应用入口 — 网络安全 RAG Agent 聊天界面

启动：
  python main.py
"""
import os
import sys
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
from routes_admin_pages import router as admin_pages_router
from auth import verify_admin_token, is_admin_route
import logging
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_BASE = Path(__file__).parent
_STATIC = _BASE / "static"
_TEMPLATES = _BASE / "templates"

app = FastAPI(title="网络安全智能Agent", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
app.include_router(admin_pages_router)
app.include_router(api_router)
app.include_router(api_eval_router)
app.include_router(api_agent_eval_router)


# ---- 全局认证中间件 ----
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """对所有管理路由进行 Token 认证"""
    if is_admin_route(request.url.path):
        try:
            await verify_admin_token(request)
        except Exception:
            return JSONResponse(
                status_code=403,
                content={"detail": "未授权访问。请在请求头中设置 X-Admin-Token"},
            )
    response = await call_next(request)
    return response


if __name__ == "__main__":
    import uvicorn
    import os
    # 单 worker + 线程池（run_in_executor）：避免 Windows 下 FAISS multiprocessing 冲突，
    # 同时确保同步 agent.ask() 不会阻塞事件循环
    workers = int(os.environ.get("UVICORN_WORKERS", "1"))
    uvicorn.run("main:app", host="127.0.0.1", port=8000, workers=workers)
