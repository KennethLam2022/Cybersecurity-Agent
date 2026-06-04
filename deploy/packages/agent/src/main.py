"""FastAPI Web 应用 — 网络安全 RAG Agent 聊天界面

启动：
  python main.py
"""
import os, sys, json, logging, time, datetime
import requests
from pathlib import Path
from typing import AsyncGenerator
from fastapi import FastAPI, Request, Body, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
import asyncio

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from agent import CyberAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_START_TIME = time.time()

# 非对话/非 LLM 模型关键词过滤（刷新模型列表时排除掉）
_EXCLUDE_MODEL_KEYWORDS = ["embedding", "reranker", "image", "video", "audio", "speech", "ocr", "asr", "tts", "bge", "wan", "kolors", "paddleocr", "captioner", "cosyvoice", "sensevoice"]

# ---- 实时监控事件总线 ----
class EventBus:
    """SSE 事件广播：admin 页面实时监控"""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def publish(self, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in self._subscribers[:]:
            try:
                await q.put(payload)
            except:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


event_bus = EventBus()
_ACTIVE_CONVERSATIONS: dict = {}  # conv_id -> info

_BASE = Path(__file__).parent
_STATIC = _BASE / "static"
_TEMPLATES = _BASE / "templates"

app = FastAPI(title="网络安全智能Agent")
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")
jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATES)))

agent = CyberAgent()

# ---- 启动时加载 LLM 配置（多提供商存储） ----
_CONFIG_PATH = Path(__file__).parent.parent / "agent_data" / "llm_config.json"


def _load_llm_config() -> dict:
    """读取完整配置，返回 {current, providers}"""
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except:
            pass
    return {"current": None, "providers": {}}


def _save_llm_config(data: dict):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_provider_models(provider: str, models: list[str]):
    """将刷新得到的模型列表保存到 llm_config.json"""
    full = _load_llm_config()
    full.setdefault("providers", {})
    full["providers"].setdefault(provider, {})
    full["providers"][provider]["models"] = models
    _save_llm_config(full)


def _cleanup_provider_models():
    """启动时清理已保存的模型列表：剔除匹配排除关键词的脏数据"""
    full = _load_llm_config()
    changed = False
    for pname, pcfg in full.get("providers", {}).items():
        models = pcfg.get("models")
        if not models:
            continue
        clean = [m for m in models if not any(k in m.lower() for k in _EXCLUDE_MODEL_KEYWORDS)]
        if len(clean) != len(models):
            full["providers"][pname]["models"] = clean
            changed = True
            logger.info(f"清理 {pname} 模型列表: {len(models)} → {len(clean)} (排除 {len(models)-len(clean)} 个非对话模型)")
    if changed:
        _save_llm_config(full)


def _get_current_config() -> dict:
    """获取当前生效的提供商配置"""
    full = _load_llm_config()
    cur = full.get("current")
    if cur and cur in full.get("providers", {}):
        return full["providers"][cur]
    return {}


if _CONFIG_PATH.exists():
    try:
        cfg = _get_current_config()
        if cfg.get("base_url") and cfg.get("api_key") and cfg.get("model"):
            full = _load_llm_config()
            pname = full.get("current", "")
            agent.llm.reconfigure(
                base_url=cfg["base_url"],
                api_key=cfg["api_key"],
                model=cfg["model"],
                provider_name=pname,
            )
            logger.info(f"已加载 LLM 配置: {pname} / {cfg.get('model','?')}")
        # 清理已保存模型列表中的脏数据（如旧的 bge/wan 等非对话模型）
        _cleanup_provider_models()
    except Exception as e:
        logger.warning(f"加载 LLM 配置失败: {e}")


LLM_PRESETS = {
    "硅基流动": {
        "base_url": "https://api.siliconflow.cn/v1",
        "models": ["deepseek-ai/DeepSeek-V4-Pro", "deepseek-ai/DeepSeek-V4-Flash", "deepseek-ai/DeepSeek-V3.2", "Pro/moonshotai/Kimi-K2.6", "Pro/zai-org/GLM-5.1", "MiniMaxAI/MiniMax-M2.5", "Pro/Qwen/Qwen3.6-27B", "Qwen/Qwen3-8B"],
        "can_refresh": True,
    },
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
    },
    "阿里云百炼": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "models": ["qwen3.6-plus", "qwen3.6-flash", "qwen3.6-max-preview", "qwen3-max", "qwen-plus", "qwen-turbo", "qwq-plus", "qwen-long"],
        "can_refresh": True,
    },
    "智谱AI": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": ["glm-4-plus", "glm-4-air", "glm-4-flash"],
    },
    "月之暗面(Kimi)": {
        "base_url": "https://api.moonshot.cn/v1",
        "models": ["moonshot-v1-8k", "moonshot-v1-32k", "moonshot-v1-128k"],
    },
    "百度千帆": {
        "base_url": "https://qianfan.baidubce.com/v2",
        "models": ["ernie-4.0-8k", "ernie-3.5-8k", "ernie-speed-128k"],
    },
    "OpenRouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "models": ["openai/gpt-oss-120b:free", "openai/gpt-5.2", "openai/gpt-4o", "google/gemini-2.5-pro-preview", "google/gemini-2.0-flash", "anthropic/claude-3.5-sonnet", "qwen/qwen3.7-max", "qwen/qwen-plus", "deepseek/deepseek-chat", "mistral/mistral-large"],
        "can_refresh": True,
    },
}


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = jinja_env.get_template("index.html")
    content = template.render({"request": request})
    return HTMLResponse(content)


@app.get("/api/conversations")
async def list_conversations(include_deleted: bool = False):
    convs = agent.memory.get_conversations(include_deleted=include_deleted)
    return JSONResponse(convs)


@app.post("/api/conversations")
async def new_conversation():
    conv = agent.memory.create_conversation()
    return JSONResponse(conv)


@app.delete("/api/conversations/{conv_id}")
async def delete_conversation(conv_id: str):
    agent.memory.delete_conversation(conv_id)
    return JSONResponse({"ok": True, "soft_delete": True})


@app.delete("/api/conversations/{conv_id}/hard")
async def hard_delete_conversation(conv_id: str):
    agent.memory.hard_delete_conversation(conv_id)
    return JSONResponse({"ok": True})


@app.get("/api/conversations/{conv_id}/messages")
async def get_messages(conv_id: str):
    return JSONResponse(agent.memory.get_history(conv_id))


@app.get("/api/conversations/detail")
async def get_conversation_detail(conv_id: str):
    """获取单个对话详情（含所有消息及来源）"""
    detail = agent.memory.get_conversation_detail(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return JSONResponse(detail)


@app.get("/api/conversations/stats")
async def get_conversation_stats(conv_id: str):
    """获取对话统计（来源分布、置信度分布）"""
    detail = agent.memory.get_conversation_detail(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")

    all_sources = []
    for msg in detail["messages"]:
        for s in msg.get("sources", []):
            all_sources.append(s)

    file_stats = {}
    for s in all_sources:
        fn = s.get("file_name", "未知")
        if fn not in file_stats:
            file_stats[fn] = {"count": 0, "confidences": [], "category": s.get("category", "")}
        file_stats[fn]["count"] += 1
        if s.get("confidence") is not None:
            file_stats[fn]["confidences"].append(s["confidence"])

    from collections import Counter
    conf_labels = Counter(s.get("label", "未知") for s in all_sources)

    return JSONResponse({
        "conversation_id": conv_id,
        "total_messages": len(detail["messages"]),
        "total_rounds": detail["stats"]["rounds"],
        "total_sources": detail["stats"]["total_sources"],
        "unique_files": len(file_stats),
        "file_stats": file_stats,
        "confidence_distribution": dict(conf_labels),
    })


@app.get("/api/admin/stream")
async def admin_event_stream():
    """SSE 实时监控：对话列表变动、新消息、进行中对话"""
    q = event_bus.subscribe()
    convs = agent.memory.get_conversations(include_deleted=True)

    async def _send_initial_state():
        active_list = []
        for conv_id, info in _ACTIVE_CONVERSATIONS.items():
            active_list.append({
                "conv_id": conv_id,
                "title": info.get("title", ""),
                "stage": info.get("stage", ""),
                "started_at": info.get("started_at", ""),
            })
        yield f"event: init\ndata: {json.dumps({'conversations': convs, 'active': active_list, 'subscribers': event_bus.subscriber_count}, ensure_ascii=False)}\n\n"

    async def _event_generator():
        # 发送初始状态
        async for chunk in _send_initial_state():
            yield chunk
        while True:
            try:
                payload = await asyncio.wait_for(q.get(), timeout=30)
                yield payload
            except asyncio.TimeoutError:
                yield f"event: heartbeat\ndata: {json.dumps({'t': 'keep-alive'})}\n\n"
            except Exception:
                break

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/admin", response_class=HTMLResponse)
async def admin_dashboard():
    """管理后台看板"""
    html_path = _TEMPLATES / "admin.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>管理后台页面未找到</h1><p>请检查 templates/admin.html</p>")


@app.post("/api/chat")
async def chat(data: dict = Body(...)):
    query = data.get("query", "").strip()
    conv_id = data.get("conversation_id")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    result = agent.ask(query, conversation_id=conv_id)
    asyncio.create_task(event_bus.publish("conversation_updated", {
        "conv_id": conv_id or result.get("conversation_id", ""),
        "action": "chat",
    }))
    return JSONResponse(result)


@app.post("/api/chat/stream")
async def chat_stream(data: dict = Body(...)):
    """SSE 流式聊天端点

    客户端用 fetch + ReadableStream 读取 SSE 事件：
      data: {"type": "status", "stage": "retrieving", "message": "..."}
      data: {"type": "token", "content": "..."}
      data: {"type": "done", "sources": [...], "conversation_id": "..."}
    """
    query = data.get("query", "").strip()
    conv_id = data.get("conversation_id")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

    # 注册到进行中对话
    conv_info = _ACTIVE_CONVERSATIONS.get(conv_id)
    if conv_info is None and conv_id:
        convs = agent.memory.get_conversations()
        title = query[:40]
        for c in convs:
            if c["id"] == conv_id:
                title = c["title"]
                break
        _ACTIVE_CONVERSATIONS[conv_id] = {
            "title": title,
            "stage": "retrieving",
            "started_at": time.strftime("%H:%M:%S"),
        }
        asyncio.create_task(event_bus.publish("active_updated", {
            "conv_id": conv_id,
            "title": title,
            "stage": "retrieving",
        }))

    async def event_generator() -> AsyncGenerator[bytes, None]:
        try:
            async for event in agent.ask_stream(query=query, conversation_id=conv_id):
                etype = event.get("type", "")
                if etype == "status":
                    stage = event.get("stage", "")
                    if conv_id and conv_id in _ACTIVE_CONVERSATIONS:
                        _ACTIVE_CONVERSATIONS[conv_id]["stage"] = stage
                        asyncio.create_task(event_bus.publish("active_updated", {
                            "conv_id": conv_id,
                            "stage": stage,
                        }))
                elif etype == "done":
                    if conv_id and conv_id in _ACTIVE_CONVERSATIONS:
                        del _ACTIVE_CONVERSATIONS[conv_id]
                        asyncio.create_task(event_bus.publish("active_removed", {
                            "conv_id": conv_id,
                        }))
                    asyncio.create_task(event_bus.publish("conversation_updated", {
                        "conv_id": conv_id,
                        "action": "done",
                    }))
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
        except Exception as e:
            logger.error(f"流式生成异常: {e}", exc_info=True)
            err_event = {"type": "error", "content": f"生成回答时出现异常，请重试。错误: {str(e)[:100]}"}
            yield f"data: {json.dumps(err_event, ensure_ascii=False)}\n\n".encode("utf-8")

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/stats")
async def stats():
    return JSONResponse(agent.stats())


@app.post("/api/rating")
async def submit_rating(data: dict = Body(...)):
    message_id = data.get("message_id")
    rating = data.get("rating")
    if not message_id or not rating:
        return JSONResponse({"ok": False, "error": "缺少 message_id 或 rating"}, status_code=400)
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        return JSONResponse({"ok": False, "error": "rating 需为 1-5"}, status_code=400)
    try:
        agent.memory.update_rating(int(message_id), int(rating))
        return {"ok": True}
    except Exception as e:
        logger.error(f"评分写入失败: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@app.get("/api/stats/drill-down")
async def drill_down(type: str, key: str, limit: int = 50):
    try:
        results = agent.memory.drill_down(type, key, limit)
        return JSONResponse(results)
    except Exception as e:
        logger.error(f"钻取查询失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/dashboard")
async def dashboard_stats():
    try:
        data = agent.memory.get_dashboard_stats()
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"看板数据聚合失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/health")
async def health():
    llm_info = agent.llm.get_current_provider() if hasattr(agent.llm, "get_current_provider") else {}
    today_count = 0
    try:
        import sqlite3
        c = sqlite3.connect(agent.memory._db_path)
        today_count = c.execute("SELECT COUNT(*) FROM usage_logs WHERE DATE(created_at) = DATE('now')").fetchone()[0]
    except:
        pass
    provider_name = llm_info.get("name", "")
    model_name = llm_info.get("model", "")
    display_name = provider_name if provider_name and provider_name != "api" else model_name.split("/")[0] if "/" in model_name else model_name[:20] if model_name else "未知"
    return {
        "llm_provider": display_name,
        "llm_model": llm_info.get("model", "未知"),
        "uptime_seconds": int(time.time() - _START_TIME),
        "today_queries": today_count,
        "faiss_ready": os.path.exists(os.path.join(str(Path(__file__).parent.parent.parent / "vector_store" / "faiss_index"), "index.faiss")),
        "chroma_ready": os.path.exists(str(Path(__file__).parent.parent.parent / "vector_store" / "chroma_db")),
    }


# ========== LLM 配置接口 ==========
@app.get("/api/llm/presets")
async def llm_presets():
    return {"presets": LLM_PRESETS}


@app.get("/api/llm/config")
async def llm_get_config():
    full = _load_llm_config()
    cur = full.get("current")
    providers = full.get("providers", {})
    cfg = providers.get(cur, {}) if cur else {}
    cfg.setdefault("base_url", agent.llm.base_url)
    cfg.setdefault("model", agent.llm.model)
    cfg.setdefault("api_key", "")
    # 返回所有提供商的完整配置（含 Key），供前端切换时自动填充
    return {"config": cfg, "current": cur, "providers": list(providers.keys()), "providers_config": providers}


@app.post("/api/llm/config")
async def llm_save_config(data: dict = Body(...)):
    provider = data.get("provider", "自定义")
    cfg = {
        "base_url": data.get("base_url", "").rstrip("/"),
        "api_key": data.get("api_key", ""),
        "model": data.get("model", ""),
    }
    full = _load_llm_config()
    full.setdefault("providers", {})
    existing = full["providers"].get(provider, {})
    if existing.get("models"):
        cfg["models"] = existing["models"]
    full["providers"][provider] = cfg
    full["current"] = provider
    _save_llm_config(full)
    agent.llm.reconfigure(
        base_url=cfg["base_url"],
        api_key=cfg["api_key"],
        model=cfg["model"],
        provider_name=provider,
    )
    return {"status": "saved", "message": f"{provider} 配置已保存并生效"}


@app.post("/api/llm/test")
async def llm_test_connection(data: dict = Body(...)):
    """测试 LLM 提供商连接"""
    from llm_provider import LLMProvider

    temp = LLMProvider()
    result = temp.test_connection(
        base_url=data.get("base_url", ""),
        api_key=data.get("api_key", ""),
        model=data.get("model", ""),
    )
    return result


@app.post("/api/llm/refresh-models")
async def llm_refresh_models(data: dict = Body(...)):
    """动态刷新模型列表 (硅基流动/OpenRouter/阿里云百炼)

    - 硅基流动: 需要 API Key，过滤非对话模型
    - OpenRouter: 无需 API Key（公开接口）
    - 阿里云百炼: 需要 API Key，通过 OpenAI 兼容接口获取
    """
    provider = data.get("provider", "硅基流动")
    api_key = data.get("api_key", "")
    if not api_key:
        full = _load_llm_config()
        providers = full.get("providers", {})
        if provider in providers:
            api_key = providers[provider].get("api_key", "")
        elif full.get("current") and providers.get(full["current"]):
            api_key = providers[full["current"]].get("api_key", "")

    if provider == "OpenRouter":
        try:
            r = requests.get("https://openrouter.ai/api/v1/models", timeout=30)
            if r.status_code != 200:
                return {"ok": False, "models": [], "message": f"HTTP {r.status_code}"}
            data = r.json()
            all_models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
            exclude_modalities = ["image", "audio", "video", "embedding", "rerank"]
            # 合并通用排除词
            exclude_all = set(exclude_modalities + _EXCLUDE_MODEL_KEYWORDS)
            chat_models = [m for m in all_models if not any(x in m.lower() for x in exclude_all)]
            chat_models.sort()
            _save_provider_models(provider, chat_models)
            return {"ok": True, "models": chat_models, "count": len(chat_models), "total": len(all_models)}
        except Exception as e:
            return {"ok": False, "models": [], "message": str(e)[:100]}

    if provider == "阿里云百炼":
        if not api_key:
            return {"ok": False, "models": [], "message": "请先填写 API Key"}
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            r = requests.get("https://dashscope.aliyuncs.com/compatible-mode/v1/models", headers=headers, timeout=30)
            if r.status_code != 200:
                return {"ok": False, "models": [], "message": f"HTTP {r.status_code}"}
            data = r.json()
            if "data" in data and isinstance(data["data"], list):
                all_models = [m["id"] for m in data["data"] if isinstance(m, dict) and "id" in m]
                chat_models = [m for m in all_models if not any(k in m.lower() for k in _EXCLUDE_MODEL_KEYWORDS)]
                chat_models.sort()
                _save_provider_models(provider, chat_models)
                return {"ok": True, "models": chat_models, "count": len(chat_models), "total": len(all_models)}
            return {"ok": False, "models": [], "message": "响应格式异常"}
        except Exception as e:
            return {"ok": False, "models": [], "message": str(e)[:100]}

    # 默认: 硅基流动
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = requests.get("https://api.siliconflow.cn/v1/models", headers=headers, timeout=30)
        if r.status_code != 200:
            return {"ok": False, "models": [], "message": f"HTTP {r.status_code}"}
        data = r.json()
        all_models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        channel_models = [m for m in all_models if not any(k in m.lower() for k in _EXCLUDE_MODEL_KEYWORDS)]
        channel_models.sort()
        _save_provider_models(provider, channel_models)
        return {"ok": True, "models": channel_models, "count": len(channel_models), "total": len(all_models)}
    except Exception as e:
        return {"ok": False, "models": [], "message": str(e)[:100]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)