"""FastAPI 生命周期管理 — 启动初始化 & 关闭清理

通过 FastAPI lifespan 上下文管理器，在应用启动时执行数据迁移、
LLM 配置加载、环境清理，在关闭时执行有状态资源释放。
"""
import logging
from contextlib import asynccontextmanager
from fastapi import FastAPI

from app_state import _init_on_startup, _cleanup_staging, logger


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期上下文管理器

    启动阶段 (yield 前)：
      同步执行 _init_on_startup() 进行数据迁移/LLM 加载/环境清理
    关闭阶段 (yield 后)：
      清理 upload staging 临时目录
    """
    # ---- 启动初始化 ----
    logger.info("🚀 应用启动中...")
    import asyncio
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _init_on_startup)
    logger.info("✅ 应用启动完成")

    yield

    # ---- 关闭清理 ----
    logger.info("🛑 应用关闭中...")
    _cleanup_staging()
    logger.info("✅ 应用关闭完成")
