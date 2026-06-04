"""Agent API 服务 — FastAPI

启动：
  python api.py                # 开发模式，http://localhost:8000
  uvicorn api:app --host 0.0.0.0 --port 8000  # 生产模式

接口：
  GET  /                        → HTML 聊天页面
  POST /api/chat                → 新对话提问
  GET  /api/conversations       → 对话列表
  GET  /api/conversations/detail?conv_id= → 对话详情
  GET  /api/conversations/stats?conv_id=  → 对话统计
  POST /api/conversations/{id}/chat → 继续对话
  DELETE /api/conversations/{id}  → 删除对话
  GET  /admin                   → 管理后台看板
"""
import sys, os, json, logging
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

_SRC = Path(__file__).parent
_PREPROC = _SRC.parent.parent / "preprocessor" / "src"
for p in [_SRC, _PREPROC]:
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

from agent import CyberAgent

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

# ---- 单例 Agent（常驻内存，不重复加载检索器） ----
_agent: Optional[CyberAgent] = None

# ---- LLM 配置 ----
_CONFIG_DIR = _SRC.parent / "agent_data"
_CONFIG_PATH = _CONFIG_DIR / "llm_config.json"

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

def _load_llm_config() -> dict:
    """从本地文件加载 LLM 配置"""
    if _CONFIG_PATH.exists():
        try:
            data = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
            logger.info(f"已加载 LLM 配置: provider={data.get('provider','?')}, model={data.get('model','?')}")
            return data
        except Exception as e:
            logger.warning(f"加载 LLM 配置失败: {e}")
    return {}

def _save_llm_config(config: dict):
    """保存 LLM 配置到本地文件"""
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"LLM 配置已保存: provider={config.get('provider','?')}, model={config.get('model','?')}")

def _apply_llm_config(config: dict):
    """将配置应用到全局 Agent 的 LLMProvider"""
    if not config.get("base_url") or not config.get("api_key") or not config.get("model"):
        logger.warning("LLM 配置不完整，跳过应用")
        return
    agent = get_agent()
    agent.llm.reconfigure(
        base_url=config["base_url"],
        api_key=config["api_key"],
        model=config["model"],
    )

def get_agent() -> CyberAgent:
    global _agent
    if _agent is None:
        logger.info("首次初始化 Agent（加载检索器...）")
        _agent = CyberAgent(
            use_rerank=True,
            use_history=True,
            use_query_rewrite=True,
            use_verification=True,
        )
        logger.info("Agent 初始化完成")
    return _agent

# ---- FastAPI ----
app = FastAPI(title="网络安全 Agent API", version="1.0.0")


# ---- 请求/响应模型 ----
class ChatRequest(BaseModel):
    query: str
    temperature: float = 0.2

class ChatResponse(BaseModel):
    answer: str
    conversation_id: str
    sources: list = []
    stats: dict = {}
    rewritten_query: Optional[str] = None


# ---- 接口 ----
@app.get("/", response_class=HTMLResponse)
def chat_page():
    html_path = _SRC / "templates" / "chat.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>聊天页面未找到</h1><p>请检查 templates/chat.html</p>")


@app.post("/api/chat", response_model=ChatResponse)
def new_chat(req: ChatRequest):
    agent = get_agent()
    try:
        result = agent.ask(req.query, temperature=req.temperature)
        return ChatResponse(
            answer=result["answer"],
            conversation_id=result["conversation_id"],
            sources=result.get("sources", []),
            stats=result.get("stats", {}),
            rewritten_query=result.get("rewritten_query"),
        )
    except Exception as e:
        logger.exception(f"/api/chat 错误")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/conversations")
def list_conversations():
    agent = get_agent()
    try:
        convs = agent.memory.get_conversations()
        return convs
    except Exception as e:
        logger.exception("/api/conversations 错误")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/conversations/detail")
def get_conversation_detail(conv_id: str):
    """获取单个对话详情（含所有消息及来源）

    查询参数：conv_id 对话ID
    """
    agent = get_agent()
    try:
        detail = agent.memory.get_conversation_detail(conv_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="对话不存在")
        return detail
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"/api/conversations/detail 错误")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/conversations/stats")
def get_conversation_stats(conv_id: str):
    """获取对话统计（来源分布、置信度分布等）

    查询参数：conv_id 对话ID
    """
    agent = get_agent()
    try:
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

        return {
            "conversation_id": conv_id,
            "total_messages": len(detail["messages"]),
            "total_rounds": detail["stats"]["rounds"],
            "total_sources": detail["stats"]["total_sources"],
            "unique_files": len(file_stats),
            "file_stats": file_stats,
            "confidence_distribution": dict(conf_labels),
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.exception(f"/api/conversations/stats 错误")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/conversations/{conv_id}/chat", response_model=ChatResponse)
def continue_chat(conv_id: str, req: ChatRequest):
    agent = get_agent()
    try:
        result = agent.ask(req.query, conversation_id=conv_id, temperature=req.temperature)
        return ChatResponse(
            answer=result["answer"],
            conversation_id=result["conversation_id"],
            sources=result.get("sources", []),
            stats=result.get("stats", {}),
            rewritten_query=result.get("rewritten_query"),
        )
    except Exception as e:
        logger.exception(f"/api/conversations/{conv_id}/chat 错误")
        raise HTTPException(status_code=500, detail=str(e))


@app.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    agent = get_agent()
    try:
        agent.memory.delete_conversation(conv_id)
        return {"status": "deleted", "conversation_id": conv_id}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard():
    """管理后台看板"""
    html_path = _SRC / "templates" / "admin.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>管理后台页面未找到</h1><p>请检查 templates/admin.html</p>")


# ---- LLM 配置接口 ----
@app.get("/api/llm/presets")
def llm_presets():
    """获取大模型提供商预设列表"""
    return {"presets": LLM_PRESETS}


@app.get("/api/llm/config")
def llm_get_config():
    """获取当前 LLM 配置"""
    config = _load_llm_config()
    agent = get_agent()
    config.setdefault("base_url", agent.llm.base_url)
    config.setdefault("model", agent.llm.model)
    config.setdefault("provider", "自定义")
    return {"config": config}


class LLMConfigRequest(BaseModel):
    base_url: str
    api_key: str
    model: str
    provider: str = "自定义"


@app.post("/api/llm/config")
def llm_save_config(req: LLMConfigRequest):
    """保存并应用 LLM 配置"""
    config = {
        "base_url": req.base_url,
        "api_key": req.api_key,
        "model": req.model,
        "provider": req.provider,
    }
    _save_llm_config(config)
    _apply_llm_config(config)
    return {"status": "saved", "message": "配置已保存并生效"}


class LLMTestRequest(BaseModel):
    base_url: str
    api_key: str
    model: str


@app.post("/api/llm/test")
def llm_test_connection(req: LLMTestRequest):
    """测试 LLM 提供商连接"""
    from llm_provider import LLMProvider

    temp_provider = LLMProvider()
    result = temp_provider.test_connection(
        base_url=req.base_url,
        api_key=req.api_key,
        model=req.model,
    )
    return result


# ---- 命令行启动 ----
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    logger.info(f"启动 API 服务: http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port, reload=False)