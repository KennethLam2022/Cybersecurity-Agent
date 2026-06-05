"""FastAPI Web 应用 — 网络安全 RAG Agent 聊天界面

启动：
  python main.py
"""
import os, sys, json, logging, time, hashlib, base64, re, urllib.parse, shutil, uuid, threading, queue, subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import requests
from pathlib import Path
from typing import AsyncGenerator, Optional
from fastapi import FastAPI, Request, Body, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from io import BytesIO
import docx
from docx.shared import Pt, Inches, RGBColor
from docx.enum.table import WD_TABLE_ALIGNMENT
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader
import asyncio

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# 添加 preprocessor 路径
_PREPROCESSOR_SRC = str(Path(_SRC).parent.parent.parent / "packages" / "preprocessor" / "src")
if _PREPROCESSOR_SRC not in sys.path:
    sys.path.insert(0, _PREPROCESSOR_SRC)

from deduplicator import Deduplicator

# ---- 导入拆分的模块 ----
from auth import verify_admin_token, is_admin_route, validate_llm_url
from llm_config_manager import (
    _fernet, _CRYPTO_AVAILABLE,
    _load_llm_config, _save_llm_config,
    _get_db_path, _save_llm_key, _get_llm_key, _get_llm_key_mask, _has_llm_key, _delete_llm_key,
    _save_llm_config_card, _get_all_llm_configs, _get_llm_config_card,
    _hash_api_key, _encrypt_api_key, _decrypt_api_key, _make_key_mask,
    _get_project_root,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_START_TIME = time.time()

# 非对话/非 LLM 模型关键词过滤
_EXCLUDE_MODEL_KEYWORDS = ["embedding", "reranker", "image", "video", "audio", "speech", "ocr", "asr", "tts", "bge", "wan", "kolors", "paddleocr", "captioner", "cosyvoice", "sensevoice"]

from agent import CyberAgent

class EventBus:
    """SSE 事件广播：admin 页面实时监控"""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        """安全取消订阅：如果队列不在列表中则忽略（防止重复取消）"""
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass  # 已经移除过了

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

# ---- 全局认证中间件 ----
@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """对所有管理路由进行 Token 认证"""
    if is_admin_route(request.url.path):
        try:
            await verify_admin_token(request)
        except HTTPException:
            return JSONResponse(
                status_code=403,
                content={"detail": "未授权访问。请在请求头中设置 X-Admin-Token"},
            )
    response = await call_next(request)
    return response

agent = CyberAgent()

# ---- LLM 配置与密钥管理 ----
_CONFIG_PATH = Path(__file__).parent.parent / "agent_data" / "llm_config.json"

def _load_llm_config() -> dict:
    """读取配置元数据（不含 API Key），返回 {current, providers}"""
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
    """启动时清理已保存的模型列表：
    - can_refresh=False 的提供商（如阿里云百炼）: 用预设列表覆盖缓存
    - 其他: 剔除匹配排除关键词的脏数据
    """
    full = _load_llm_config()
    changed = False
    for pname, pcfg in full.get("providers", {}).items():
        preset = LLM_PRESETS.get(pname)
        if preset and not preset.get("can_refresh", True):
            # 不可刷新的提供商，用预设列表覆盖缓存
            preset_models = preset.get("models", [])
            if pcfg.get("models") != preset_models:
                full["providers"][pname]["models"] = list(preset_models)
                changed = True
                logger.info(f"重置 {pname} 模型列表: {len(pcfg.get('models', []))} → {len(preset_models)} (预设)")
        else:
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

# ---- SQLite 密钥管理 ----
def _get_db_path() -> str:
    """获取 SQLite 数据库路径（与 memory.py 共享同一数据库）

    main.py 在 packages/agent/src/，向上4层到项目根目录，
    然后进入 agent_data/ 目录（项目根目录数据库才有完整的对话记录）
    """
    _DB_DIR = Path(__file__).parent.parent.parent.parent / "agent_data"
    _DB_DIR.mkdir(parents=True, exist_ok=True)
    return str(_DB_DIR / "conversations.db")

def _save_llm_key(provider: str, api_key: str):
    """将 API Key 加密后存入 SQLite"""
    import sqlite3
    db_path = _get_db_path()
    api_key_hash = _hash_api_key(api_key)
    api_key_enc = _encrypt_api_key(api_key) if _CRYPTO_AVAILABLE else api_key
    api_key_mask = _make_key_mask(api_key)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO llm_provider_keys 
               (provider, api_key_hash, api_key_enc, api_key_mask, updated_at)
               VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (provider, api_key_hash, api_key_enc, api_key_mask),
        )
    logger.info(f"✅ API Key 已加密存储到 SQLite: {provider} ({api_key_mask})")

def _get_llm_key(provider: str) -> Optional[str]:
    """从 SQLite 解密读取 API Key"""
    import sqlite3
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT api_key_enc, api_key_hash FROM llm_provider_keys WHERE provider = ?",
            (provider,),
        ).fetchone()
    if not row:
        return None
    api_key_enc, stored_hash = row
    if not api_key_enc:
        return None
    try:
        api_key = _decrypt_api_key(api_key_enc) if _CRYPTO_AVAILABLE else api_key_enc
        # 校验哈希完整性
        if _hash_api_key(api_key) != stored_hash:
            logger.error(f"⚠️ {provider} API Key 哈希校验失败，数据可能已损坏")
            return None
        return api_key
    except Exception as e:
        logger.error(f"⚠️ 解密 {provider} API Key 失败: {e}")
        return None

def _get_llm_key_mask(provider: str) -> Optional[str]:
    """从 SQLite 读取 API Key 掩码（用于前端显示）"""
    import sqlite3
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT api_key_mask FROM llm_provider_keys WHERE provider = ?",
            (provider,),
        ).fetchone()
    return row[0] if row else None

def _has_llm_key(provider: str) -> bool:
    """检查某提供商是否有已存储的 API Key"""
    return _get_llm_key_mask(provider) is not None

def _delete_llm_key(provider: str):
    """从 SQLite 删除某提供商的 API Key"""
    import sqlite3
    db_path = _get_db_path()
    with sqlite3.connect(db_path) as conn:
        conn.execute("DELETE FROM llm_provider_keys WHERE provider = ?", (provider,))
    logger.info(f"🗑️ {provider} API Key 已从 SQLite 删除")


# ---- LLM 配置卡片持久化（6个独立后端模型）----
_CONFIG_CARD_MODULES = [
    # chat 类型 LLM（按 RAG→Agent 流程排序）
    "chat", "jailbreak", "scoring", "fallback", "chunk", "promptEval",
    # 非 chat 类型（专用 API）
    "embedding", "reranker",
]

def _save_llm_config_card(module_id: str, provider: str, model: str, base_url: str, api_key: str):
    """将 LLM 配置卡片加密保存到 llm_configs 表"""
    import sqlite3
    db_path = _get_db_path()
    # Key 为空时从 llm_provider_keys 表补回（前台只改模型没输 Key 的场景）
    if not api_key and provider:
        fallback_key = _get_llm_key(provider)
        if fallback_key:
            api_key = fallback_key
    api_key_hash = _hash_api_key(api_key) if api_key else ""
    api_key_enc = _encrypt_api_key(api_key) if (api_key and _CRYPTO_AVAILABLE) else (api_key or "")
    api_key_mask = _make_key_mask(api_key) if api_key else ""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO llm_configs
               (module_id, provider, model, base_url, api_key_enc, api_key_hash, api_key_mask, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)""",
            (module_id, provider, model, base_url, api_key_enc, api_key_hash, api_key_mask),
        )
    logger.info(f"✅ LLM 配置卡片已保存: {module_id} ({provider}/{model}) [{api_key_mask or '无Key'}]")
    return api_key_mask

def _get_all_llm_configs() -> dict:
    """读取所有 LLM 配置卡片（Key 只返回掩码）"""
    import sqlite3
    db_path = _get_db_path()
    result = {}
    try:
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT module_id, provider, model, base_url, api_key_mask, updated_at FROM llm_configs"
            ).fetchall()
            for row in rows:
                module_id, provider, model, base_url, api_key_mask, updated_at = row
                result[module_id] = {
                    "provider": provider or "",
                    "model": model or "",
                    "base_url": base_url or "",
                    "api_key_mask": api_key_mask or "",
                    "updated_at": updated_at or "",
                }
    except sqlite3.OperationalError:
        pass
    return result

def _get_llm_config_card(module_id: str) -> dict:
    """读取单个 LLM 配置卡片（含解密后 Key，仅后端使用）"""
    import sqlite3
    db_path = _get_db_path()
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT provider, model, base_url, api_key_enc, api_key_hash FROM llm_configs WHERE module_id = ?",
                (module_id,),
            ).fetchone()
        if not row:
            return {}
        provider, model, base_url, api_key_enc, stored_hash = row
        api_key = ""
        if api_key_enc and _CRYPTO_AVAILABLE:
            try:
                decrypted = _decrypt_api_key(api_key_enc)
                if _hash_api_key(decrypted) == stored_hash:
                    api_key = decrypted
            except Exception:
                pass
        return {
            "provider": provider or "",
            "model": model or "",
            "base_url": base_url or "",
            "api_key": api_key,
        }
    except sqlite3.OperationalError:
        return {}


def _get_backend_eval_llm():
    """创建 ⑥ 后端评测 LLM 实例（独立于 agent.llm，不影响主对话）

    Returns:
        LLMProvider 实例，或 None（卡片未配置/出错时）
    """
    cfg = _get_llm_config_card("promptEval")
    if not cfg or not cfg.get("model") or not cfg.get("base_url"):
        return None
    try:
        from llm_provider import LLMProvider
        return LLMProvider(
            base_url=cfg["base_url"],
            api_key=cfg.get("api_key", ""),
            model=cfg["model"],
            use_ollama_fallback=False,
        )
    except Exception as e:
        logger.warning(f"创建后端评测 LLM 失败: {e}")
        return None


def _generate_eval_summary(eval_llm, tab_type: str, result: dict, items: list) -> str:
    """用 LLM 生成测评结果的分析建议

    Args:
        eval_llm: LLMProvider 实例
        tab_type: 测评类型 (retrieval_quality, retrieval_compare)
        result: 测评结果 dict
        items: 测试题列表

    Returns:
        str: 分析建议文本
    """
    prompt = ""
    if tab_type == "retrieval_quality":
        total = result.get("total", 0)
        passed = result.get("pass", 0)
        failed = result.get("fail", 0)
        recall5_avg = result.get("avg_recall_5", result.get("recall_5_avg", 0))
        recall10_avg = result.get("avg_recall_10", result.get("recall_10_avg", 0))
        mrr_avg = result.get("avg_mrr", result.get("mrr_avg", 0))

        # 整理失败题目明细
        result_items = result.get("items", [])
        fail_items = [it for it in result_items if it.get("recall_5") == 0]
        double_fail = [it for it in result_items if it.get("recall_5") == 0 and it.get("recall_10") == 0]
        low_mrr = [it for it in result_items if 0 < it.get("mrr", 1) < 0.5]

        fail_detail = ""
        if fail_items:
            fail_detail += "\n#### ❌ 未通过（Recall@5=0）的题目：\n"
            for it in fail_items:
                r10 = "×" if it.get("recall_10") == 0 else "✓"
                mrr_val = it.get("mrr", 0)
                fail_detail += f"- 查询「{it['query']}」期望来源「{it['expected']}」R@5=× R@10={r10} MRR={mrr_val}\n"

        low_mrr_detail = ""
        if low_mrr:
            low_mrr_detail += "\n#### ⚠️ MRR 偏低（< 0.5）的题目：\n"
            for it in low_mrr:
                low_mrr_detail += f"- 查询「{it['query']}」MRR={it.get('mrr', 0)}（首个相关结果排位靠后）\n"

        prompt = f"""你是一位网络安全检索质量分析专家。分析以下检索测试结果。

## 总体数据
- 总题数：{total} | 通过：{passed} | 失败：{failed}
- 平均 Recall@5：{recall5_avg:.1f}%
- 平均 Recall@10：{recall10_avg:.1f}%
- 平均 MRR：{mrr_avg:.3f}

{fail_detail}
{low_mrr_detail}

## 分析要求
请按以下结构输出分析报告（500字以内）：

### 1. 整体评估
测试集覆盖度、检索质量总体水平。

### 2. 失败题目深度分析（重点）
对每条未通过和 MRR 偏低的题目逐一分析可能的原因：
- 知识库是否缺少对应文档？
- 查询用词与文档用词不一致（语义鸿沟）？
- 期望关键词过于宽泛或过于具体？
- 对于「双×」（R@5 和 R@10 都失败）的题目，必须给出具体可操作的改进方案。

### 3. 改进建议
基于上述分析，给出针对知识库、查询改写、检索算法的具体优化方向。"""

    elif tab_type == "retrieval_compare":
        try:
            faiss_r5 = result.get("faiss_only_recall_5", 0)
            bm25_r5 = result.get("bm25_only_recall_5", 0)
            hybrid_r5 = result.get("hybrid_no_rerank_recall_5", 0)
            rerank_r5 = result.get("hybrid_rerank_recall_5", 0)
            faiss_mrr = result.get("faiss_only_mrr", 0)
            bm25_mrr = result.get("bm25_only_mrr", 0)
            hybrid_mrr = result.get("hybrid_no_rerank_mrr", 0)
            rerank_mrr = result.get("hybrid_rerank_mrr", 0)

            # 找出各模式下失败的题目
            result_items = result.get("items", [])
            mode_details = ""
            for mode_name in ["faiss_only", "bm25_only", "hybrid_no_rerank", "hybrid_rerank"]:
                fails = [it for it in result_items if it.get(f"{mode_name}_recall_5") == 0]
                if fails:
                    mode_details += f"\n#### {mode_name} 模式失败（{len(fails)}题）：\n"
                    for it in fails:
                        mrr_val = it.get(f"{mode_name}_mrr", 0)
                        r10 = it.get(f"{mode_name}_recall_10", 0)  # 从 mode dict 拿不到 recall_10 但 prompt 会传
                        mode_details += f"- 查询「{it['query']}」期望来源「{it.get('expected','')}」R@5=× MRR={mrr_val}\n"

            # 双×：在全部模式下都失败的题目
            all_mode_fails = []
            for it in result_items:
                modes_fail = [m for m in ["faiss_only", "bm25_only", "hybrid_no_rerank", "hybrid_rerank"]
                              if it.get(f"{m}_recall_5") == 0]
                if len(modes_fail) >= 3:
                    all_mode_fails.append(it)

            all_mode_detail = ""
            if all_mode_fails:
                all_mode_detail += "\n#### ⚠️ 全模式都失败的题目（所有检索方案均无效）：\n"
                for it in all_mode_fails:
                    all_mode_detail += f"- 查询「{it['query']}」期望来源「{it.get('expected','')}」\n"

            prompt = f"""你是一位检索系统架构师。分析以下4种检索模式的增益对比结果。

## 总体对比
| 模式 | Recall@5 | MRR |
|:----|:--------:|:---:|
| FAISS-only | {faiss_r5*100:.0f}% | {faiss_mrr:.3f} |
| BM25-only | {bm25_r5*100:.0f}% | {bm25_mrr:.3f} |
| Hybrid (no rerank) | {hybrid_r5*100:.0f}% | {hybrid_mrr:.3f} |
| Hybrid + Rerank | {rerank_r5*100:.0f}% | {rerank_mrr:.3f} |

测试题数：{len(items)}
{mode_details}
{all_mode_detail}

## 分析要求
请按以下结构输出分析报告（600字以内）：

### 1. 最佳模式判定
哪种模式综合表现最好？增益来自 Hybrid（多路召回）还是 Rerank（精排）？

### 2. 失败题目深度分析（重点）
对每条失败（R@5=×）的题目，逐条分析：

**各模式独有的失败：**
- FAISS-only 或 BM25-only 单独失败的题目：分析是语义鸿沟还是字面不匹配导致。
- Hybrid 模式解决了哪些单路模式的失败？为什么 Hybrid 有效？

**全模式均失败的题目（如果有）——必须重点分析：**
- 知识库是否缺失对应文档？
- 查询关键词是否在知识库中完全不存在？
- 期望来源的关键词是否过于宽泛/具体？
- **给出具体可操作的改进方案**（补充文档、查询改写规则、同义词扩展等）。

**MRR 偏低的题目：**
- 虽然命中了但排位靠后，分析重排序是否有优化空间。

### 3. 实施建议
基于各模式的失败差异，给出检索方案选型建议：是否值得开启 Hybrid + Rerank？针对高频失败类型，推荐做什么专项优化？"""
        except Exception:
            return ""

    elif tab_type == "e2e_quality":
        # result = stats dict from _compute_stats; items = results list
        total = result.get("total", 0)
        errors = result.get("errors", 0)
        avg_scores = result.get("avg_scores", {})
        scorable = items  # full results list

        # 整理各指标失败的题目
        keys = ["context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"]
        labels = {
            "context_precision": "Context Precision（上下文精度）",
            "context_recall": "Context Recall（上下文召回）",
            "faithfulness": "Faithfulness（忠实度）",
            "relevancy": "Relevancy（相关性）",
            "hallucination": "Hallucination（幻觉检测）",
        }

        low_items_by_key = {}
        for k in keys:
            threshold = 0.6
            low_items = [
                it for it in scorable
                if it.get("scores", {}).get(k, {}).get("score", 1) < threshold
            ]
            if low_items:
                low_items_by_key[k] = low_items

        # 按域聚合
        domain_scores = {}
        for it in scorable:
            dom = it.get("domain", "通用")
            if dom not in domain_scores:
                domain_scores[dom] = {"total": 0, "sum": {k: 0.0 for k in keys}}
            domain_scores[dom]["total"] += 1
            for k in keys:
                domain_scores[dom]["sum"][k] += it.get("scores", {}).get(k, {}).get("score", 0)

        domain_detail = ""
        for dom, ds in sorted(domain_scores.items(), key=lambda x: x[1]["total"], reverse=True):
            avgs = {k: ds["sum"][k] / ds["total"] for k in keys}
            worst = min(keys, key=lambda k: avgs[k])
            domain_detail += f"\n- **{dom}**（{ds['total']}题）："
            domain_detail += " ".join(f"{labels[k].split('（')[0]}{avgs[k]*100:.0f}%" for k in keys)
            domain_detail += f"，最弱项：{labels[worst].split('（')[0]} {avgs[worst]*100:.0f}%"

        low_detail = ""
        for k, litems in low_items_by_key.items():
            low_detail += f"\n#### {labels[k]} 偏低（< 60%）：共 {len(litems)} 题\n"
            for it in litems[:5]:
                sc = it.get("scores", {}).get(k, {}).get("score", 0)
                trunc = it.get("truncation", {})
                trunc_str = f"，上下文被截断" if trunc.get("truncated_count", 0) > 0 else ""
                low_detail += f"- 「{it['query']}」[{it.get('difficulty','')}] 得分 {sc*100:.0f}%{trunc_str}\n"

        prompt = f"""你是一位网络安全 Agent 综合质量评测分析师。分析以下综合质量评测结果。

## 总体数据
- 总题数：{total} | 已评分：{total - errors} | 错误：{errors}
- 平均 Context Precision：{avg_scores.get('context_precision', 0)*100:.0f}%
- 平均 Context Recall：{avg_scores.get('context_recall', 0)*100:.0f}%
- 平均 Faithfulness：{avg_scores.get('faithfulness', 0)*100:.0f}%
- 平均 Relevancy：{avg_scores.get('relevancy', 0)*100:.0f}%
- 平均 Hallucination：{avg_scores.get('hallucination', 0)*100:.0f}%

## 各域表现{domain_detail}
{low_detail}

## 分析要求
请按以下结构输出分析报告（600字以内）：

### 1. 整体评估
综合质量在5个维度上的整体水平，哪些维度表现良好，哪些维度存在明显问题。

### 2. 薄弱环节深度分析（重点）
对每个得分 < 60% 的维度，逐题分析失败原因：
- Context Precision 低：检索到的文档是否与问题不相关？Reranker 是否失效？
- Context Recall 低：相关文档是否未被召回到 Top-K？知识库是否缺失？
- Faithfulness 低：回答是否出现了检索文档之外的内容？幻觉严重吗？
- Relevancy 低：回答与问题的相关性如何？是否答非所问？
- Hallucination 低：出现幻觉的频率高吗？主要在哪些类型的题目上？
- **对每条低分题目给出根因分析**（知识库缺陷/检索问题/Prompt 问题/Reranker 偏差）。

### 3. 改进建议
基于以上分析，给出针对知识库补充、检索优化、Prompt 调优、Reranker 微调的具体建议。"""

    if not prompt:
        return ""

    try:
        resp = eval_llm.chat([{"role": "user", "content": prompt}])
        text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        text = text.strip()

        # 持久化保存到文件（重启不丢失，下次测评覆盖）
        if text:
            _save_eval_summary(tab_type, text)
        return text
    except Exception as e:
        logger.warning(f"LLM 总结建议失败: {e}")
        return ""


def _save_eval_summary(tab_type: str, summary: str):
    """持久化保存测评总结建议到 JSON 文件"""
    _summary_dir = Path(__file__).parent.parent.parent.parent / "agent_data"
    _summary_path = _summary_dir / "eval_summaries.json"
    try:
        data = {}
        if _summary_path.exists():
            data = json.loads(_summary_path.read_text(encoding="utf-8"))
        from datetime import datetime
        data[tab_type] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "summary": summary}
        _summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存总结建议失败: {e}")


def _load_eval_summary(tab_type: str) -> dict:
    """读取持久化的测评总结建议"""
    _summary_dir = Path(__file__).parent.parent.parent.parent / "agent_data"
    _summary_path = _summary_dir / "eval_summaries.json"
    try:
        if _summary_path.exists():
            data = json.loads(_summary_path.read_text(encoding="utf-8"))
            return data.get(tab_type, {})
    except Exception:
        pass
    return {}

def _migrate_keys_from_json():
    """迁移：将 llm_config.json 中的明文 Key 迁移到 SQLite 加密存储"""
    import sqlite3
    if not _CONFIG_PATH.exists():
        return
    try:
        full = json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        providers = full.get("providers", {})
        migrated = 0
        for pname, pcfg in providers.items():
            api_key = pcfg.get("api_key", "")
            if api_key:
                _save_llm_key(pname, api_key)
                # 从 JSON 中移除明文 Key
                pcfg.pop("api_key", None)
                migrated += 1
        if migrated > 0:
            _save_llm_config(full)
            logger.info(f"🔄 数据迁移完成：{migrated} 个提供商的 API Key 已迁移到 SQLite 加密存储")
    except Exception as e:
        logger.error(f"⚠️ 数据迁移失败: {e}")

def _get_current_config() -> dict:
    """获取当前生效的提供商配置（不含 API Key）"""
    full = _load_llm_config()
    cur = full.get("current")
    if cur and cur in full.get("providers", {}):
        cfg = full["providers"][cur].copy()
        cfg.pop("api_key", None)  # 确保不返回 Key
        return cfg
    return {}


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
        "models": [
            "qwen3.6-max-preview", "qwen3-max", "qwen3-max-preview",
            "qwen3.6-plus", "qwen3.5-plus",
            "qwen3.6-flash", "qwen3.5-flash",
            "qwen3.6-35b-a3b", "qwen3.5-397b-a17b", "qwen3.5-122b-a10b", "qwen3.5-27b", "qwen3.5-35b-a3b",
            "qwen3-next-80b-a3b-thinking", "qwen3-next-80b-a3b-instruct",
            "qwen3-30b-a3b-thinking", "qwen3-30b-a3b-instruct",
            "qwen3-32b-thinking", "qwen3-32b-instruct",
            "qwen3-14b-thinking", "qwen3-14b-instruct",
            "qwen3-8b-thinking", "qwen3-8b-instruct",
            "qwen3-1.7b-thinking", "qwen3-1.7b-instruct",
            "qwen3-0.5b-thinking", "qwen3-0.5b-instruct",
        ],
        "can_refresh": False,
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

# ---- 环境清理 ----
def _cleanup_staging():
    """启动时清理：清空 upload_staging 残留任务 + 重置 _doc_tasks。"""
    import shutil
    if _UPLOAD_STAGING.exists():
        count = 0
        for item in _UPLOAD_STAGING.iterdir():
            if item.is_dir():
                shutil.rmtree(item)
            else:
                item.unlink()
            count += 1
        if count:
            logger.warning(f"🧹 清理 upload_staging: 移除 {count} 个残留任务")
    with _doc_tasks_lock:
        _doc_tasks.clear()
    logger.info("🧹 环境已清理，管道可正常运行")


def _clean_staging_task_dir(task_id: str):
    """处理完毕后清理 staging 中该 task_id 对应的目录。"""
    task_dir = _UPLOAD_STAGING / task_id
    if task_dir.exists():
        import shutil
        shutil.rmtree(task_dir)
        logger.info(f"🧹 已清理 staging 任务目录: {task_id}")


# ---- 启动时初始化 ----
def _init_on_startup():
    """启动时执行：数据迁移 + 加载 LLM 配置 + 清理脏数据"""
    # 0. 确保 llm_configs 表存在（所有卡片配置的持久化存储）
    import sqlite3 as _sqlite3
    _db_path = _get_db_path()
    try:
        with _sqlite3.connect(_db_path) as _conn:
            _conn.execute("""
                CREATE TABLE IF NOT EXISTS llm_configs (
                    module_id TEXT PRIMARY KEY,
                    provider TEXT NOT NULL DEFAULT '',
                    model TEXT NOT NULL DEFAULT '',
                    base_url TEXT NOT NULL DEFAULT '',
                    api_key_enc TEXT DEFAULT '',
                    api_key_hash TEXT DEFAULT '',
                    api_key_mask TEXT DEFAULT '',
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            _conn.commit()
            # 检查是否有任何卡片数据
            _count = _conn.execute("SELECT COUNT(*) FROM llm_configs").fetchone()[0]
            if _count == 0:
                # 从旧版 llm_config.json 种子化默认卡片
                _full = _load_llm_config()
                _cur = _full.get("current", "")
                _providers = _full.get("providers", {})
                _default_base_url = "https://api.deepseek.com"
                _default_model = "deepseek-chat"

                # chat 卡片：用当前选中的提供商
                if _cur and _cur in _providers:
                    _cfg = _providers[_cur]
                    _api_key = _get_llm_key(_cur)
                    _save_llm_config_card("chat", _cur, _cfg.get("model", "deepseek-chat"),
                                          _cfg.get("base_url", _default_base_url), _api_key)
                else:
                    _save_llm_config_card("chat", "DeepSeek", "deepseek-v4-flash", _default_base_url, _get_llm_key("DeepSeek"))

                # promptEval 卡片：默认用同款模型
                _save_llm_config_card("promptEval", "DeepSeek", "deepseek-v4-flash", _default_base_url, _get_llm_key("DeepSeek"))

                # 其他卡片：fallback/chunk 用默认值
                _save_llm_config_card("fallback", "Ollama", "qwen2.5:7b", "http://localhost:11434", "")
                _save_llm_config_card("chunk", "DeepSeek", "deepseek-chat", _default_base_url, "")
                _save_llm_config_card("jailbreak", "DeepSeek", "deepseek-chat", _default_base_url, "")
                _save_llm_config_card("scoring", "DeepSeek", "deepseek-chat", _default_base_url, "")
                logger.info("已创建 llm_configs 表并种子化默认卡片配置")
            else:
                logger.debug(f"llm_configs 表已存在，{_count} 张卡片")
    except Exception as _e:
        logger.error(f"初始化 llm_configs 表失败: {_e}")

    # 1. 迁移：将 llm_config.json 中的明文 Key 迁移到 SQLite 加密存储
    _migrate_keys_from_json()

    # 2. 注册 Prompt 版本管理数据库
    from prompt_versions import init_versions_db
    _pv_db = _get_db_path()
    init_versions_db(_pv_db)

    # 3. 从 SQLite 读取 LLM 配置并配置 agent
    #    优先读取新版的 chat 配置卡片（SQLite llm_configs 表），
    #    无 chat 卡片时回退到旧版 llm_config.json
    chat_cfg = _get_llm_config_card("chat")
    if chat_cfg and chat_cfg.get("model") and chat_cfg.get("base_url"):
        api_key = chat_cfg.get("api_key", "")
        agent.llm.reconfigure(
            base_url=chat_cfg.get("base_url", ""),
            api_key=api_key,
            model=chat_cfg.get("model", ""),
            provider_name=chat_cfg.get("provider", ""),
        )
        logger.info(f"已加载 LLM 配置 (chat卡片): {chat_cfg.get('provider','?')} / {chat_cfg.get('model','?')}")
    else:
        full = _load_llm_config()
        cur = full.get("current")
        if cur:
            api_key = _get_llm_key(cur)
            if api_key:
                cfg = full.get("providers", {}).get(cur, {})
                agent.llm.reconfigure(
                    base_url=cfg.get("base_url", ""),
                    api_key=api_key,
                    model=cfg.get("model", ""),
                    provider_name=cur,
                )
                logger.info(f"已加载 LLM 配置 (旧版): {cur} / {cfg.get('model','?')}")
            else:
                logger.warning(f"当前提供商 {cur} 在 SQLite 中未找到 API Key，请重新配置")

    # 4. 清理已保存模型列表中的脏数据
    _cleanup_provider_models()

    # 5. 迁移内置测试集（prompt_test_suite.json → SQLite）
    migrated = agent.memory.migrate_builtin_suite()
    if migrated:
        logger.info(f"已迁移内置测试集: {migrated} 条")

    # 6. 清理环境：清空 upload_staging + 重置 _doc_tasks
    _cleanup_staging()
    
    # 7. 同步 DB 活跃版本 → active_prompt.txt
    try:
        import sqlite3
        from prompt_versions import get_active_version_name
        from agent import SystemPromptLoader
        db_path = _get_db_path()
        active_v = get_active_version_name(db_path)
        if active_v:
            rows = None
            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT id, system_prompt FROM prompt_versions WHERE version_name = ? AND is_active = 1 LIMIT 1",
                    (active_v,)
                ).fetchone()
            if rows and rows[1]:
                SystemPromptLoader._path.write_text(rows[1], encoding="utf-8")
                SystemPromptLoader._cache = None
                SystemPromptLoader._mtime = 0
                logger.info(f"  ✅ 启动同步: active_prompt.txt ← DB 版本 {rows[0]} ({active_v})")
    except Exception as e:
        logger.warning(f"启动同步 active_prompt.txt 失败: {e}")


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    template = jinja_env.get_template("index.html")
    content = template.render({"request": request})
    return HTMLResponse(content)


@app.get("/api/conversations")
async def list_conversations(include_deleted: bool = False, include_test: bool = False, jailbreak: str = "all"):
    convs = agent.memory.get_conversations(include_deleted=include_deleted, include_test=include_test, jailbreak=jailbreak)
    return JSONResponse(convs)


@app.post("/api/conversations")
async def new_conversation():
    conv = agent.memory.create_conversation()
    return JSONResponse(conv)


@app.put("/api/conversations/{conv_id}")
async def rename_conversation(conv_id: str, data: dict = Body(...)):
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    agent.memory.update_title(conv_id, title)
    return JSONResponse({"ok": True, "title": title})


@app.put("/api/conversations/{conv_id}/jailbreak-status")
async def set_jailbreak_status(conv_id: str, data: dict = Body(...)):
    status = data.get("status", "").strip()
    if status not in ("false_alarm", "handled"):
        raise HTTPException(status_code=400, detail="无效状态，仅支持 false_alarm 或 handled")
    agent.memory.update_jailbreak_status(conv_id, status)
    return JSONResponse({"ok": True, "status": status})


@app.get("/api/conversations/{conv_id}/jailbreak-report")
async def jailbreak_report(conv_id: str):
    data = agent.memory.get_jailbreak_report_data(conv_id)
    if not data:
        raise HTTPException(status_code=404, detail="对话不存在")

    filename = f"越狱报告_{conv_id}.doc"

    # 构建 Word (HTML格式)
    reason = data.get("jailbreak_reason") or "无"
    jb_status_labels = {"pending": "待处理", "downloaded": "已下载", "false_alarm": "误报", "handled": "已处理"}
    jb_label = jb_status_labels.get(data.get("jailbreak_status"), "未知")

    msgs_html = ""
    jailbreak_msg_id = data.get("jailbreak_message_id")
    if jailbreak_msg_id:
        # 找到触发消息及其在列表中的索引
        jb_idx = -1
        for i, m in enumerate(data.get("messages", [])):
            if m.get("id") == jailbreak_msg_id:
                jb_idx = i
                break
        if jb_idx >= 0:
            # 显示触发轮次 + 前 2 轮作为上下文
            start = max(0, jb_idx - 3)  # 只保留触发用户消息前2.5轮的上下文
            trigger_msgs = data["messages"][start:jb_idx+2]  # 到触发后的 Agent 回复
        else:
            trigger_msgs = data.get("messages", [])[-4:]
    else:
        trigger_msgs = data.get("messages", [])[-4:]

    for m in trigger_msgs:
        role_label = "👤 用户" if m["role"] == "user" else "🤖 助手"
        content = m.get("content", "")[:500]
        rating_html = ""
        if m.get("user_rating") is not None:
            rating_html = f'<span style="color:#0071e3;font-weight:600">用户评分: {"⭐" * m["user_rating"]}</span>'
        elif m.get("semantic_rating") is not None:
            rating_html = f'<span style="color:#999">语义评分: {"⭐" * m["semantic_rating"]}</span>'

        is_trigger = m.get("id") == jailbreak_msg_id
        row_style = ' style="background:#fff0f0;font-weight:600"' if is_trigger else ""
        msgs_html += f"""<tr{row_style}>
            <td style="border:1px solid #ddd;padding:8px;font-size:12px">{role_label}</td>
            <td style="border:1px solid #ddd;padding:8px;font-size:13px;white-space:pre-wrap">{content}</td>
            <td style="border:1px solid #ddd;padding:8px;font-size:12px">{rating_html}</td>
        </tr>"""

    trace = data.get("trace_data")
    if trace and trace.get("steps"):
        trace_html = _render_trace_report(trace)
    else:
        trace_html = f"""<div style="background:#f9f9f9;border-radius:8px;padding:16px;margin-bottom:20px">
<p style="color:#999">该越狱由前置规则拦截（越狱/离题/社交寒暄检测），未进入完整 RAG 管线，无法提供检索→生成全链路 trace 数据。</p>
</div>"""

    # ---- 每轮过程数据 ----
    msgs = data.get("messages", [])
    rounds_html = ""
    for log in data.get("usage_logs", []):
        if not log.get("answer_jailbreak") and not log.get("off_topic"):
            continue
        asst_id = log["message_id"]
        # 找到 assistant 消息的位置
        asst_idx = None
        for idx, m in enumerate(msgs):
            if m.get("id") == asst_id:
                asst_idx = idx
                break
        if asst_idx is None:
            continue
        # 从 asst_idx 往前找最近的一条 user 消息
        user_msg = None
        for idx in range(asst_idx - 1, -1, -1):
            if msgs[idx]["role"] == "user":
                user_msg = msgs[idx]
                break
        if not user_msg:
            continue
        user_q = user_msg.get("content", "")[:80]
        jb_flag = "🔴" if log.get("answer_jailbreak") else ""
        ot_flag = "⚠️ 离题" if log.get("off_topic") else ""

        retrieval_detail = f"FAISS={log.get('faiss_count',0)}条 BM25={log.get('bm25_count',0)}条 → 最终={log.get('returned_count',0)}条" if log else "无数据"
        if log and log.get("chroma_count"):
            retrieval_detail += f" Chroma={log['chroma_count']}条"
        search_t = round(log.get("faiss_time",0) + log.get("chroma_time",0) + log.get("rerank_time",0), 2) if log else 0
        rewrite_t = log.get("rewrite_time", 0) if log else 0
        llm_t = log.get("llm_time", 0) if log else 0
        total_t = log.get("total_time", 0) if log else 0
        docs = log.get("documents") or [] if log else []
        top_docs = "".join(f"<li>{d.get('file_name','')[:40]} — {d.get('section','')[:20]}</li>" for d in docs[:3])

        trace_steps_html = ""
        if log and log.get("trace") and log["trace"].get("steps"):
            for st in log["trace"]["steps"]:
                trace_steps_html += f"<li><strong>{st['step']}</strong>: {json.dumps({k:v for k,v in st.items() if k!='step'}, ensure_ascii=False)[:100]}</li>"

        rounds_html += f"""<div style="background:#f9f9f9;border-radius:8px;padding:12px;margin-bottom:12px;{'border-left:4px solid #ff3b30' if jb_flag else 'border-left:4px solid #3498db'}">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <strong>🔴 越狱检测轮次 {jb_flag} {ot_flag}</strong>
        <span style="font-size:12px;color:#999">总耗时 {total_t}s</span>
    </div>
    <p style="font-size:12px;color:#333;margin:4px 0">用户: {user_q}</p>
    <table style="border-collapse:collapse;width:100%;font-size:12px;margin-top:6px">
        <tr><td style="padding:2px 6px;width:80px">Query 改写</td><td style="padding:2px 6px">{rewrite_t}s</td><td style="padding:2px 6px;width:80px">检索</td><td style="padding:2px 6px">{search_t}s ({retrieval_detail})</td></tr>
        <tr><td style="padding:2px 6px">LLM 生成</td><td style="padding:2px 6px">{llm_t}s</td><td style="padding:2px 6px">Token</td><td style="padding:2px 6px">prompt={log.get('prompt_tokens',0) if log else 0} / completion={log.get('completion_tokens',0) if log else 0}</td></tr>
        <tr><td style="padding:2px 6px">截断</td><td style="padding:2px 6px">{'✅' if log and log.get('was_truncated') else '❌'}</td><td style="padding:2px 6px">熔断</td><td style="padding:2px 6px">{'✅' if log and log.get('was_circuit_break') else '❌'}</td></tr>
    </table>
    {f'<p style="font-size:12px;margin:4px 0">Top 来源:</p><ul style="font-size:11px;margin:2px 0">{top_docs}</ul>' if top_docs else ''}
    {f'<details style="margin-top:4px"><summary style="font-size:12px;cursor:pointer;color:#666">Trace 步骤详情</summary><ul style="font-size:11px;color:#555">{trace_steps_html}</ul></details>' if trace_steps_html else ''}
</div>"""

    html = f"""<html>
<head><meta charset="utf-8"><title>越狱检测报告</title></head>
<body style="font-family:sans-serif;padding:20px;max-width:800px">
<h1 style="color:#ff3b30">🔴 越狱检测报告</h1>
<table style="border-collapse:collapse;width:100%;margin-bottom:20px">
    <tr><td style="padding:6px;font-weight:600;width:100px">对话标题</td><td style="padding:6px">{data.get("title","")}</td></tr>
    <tr><td style="padding:6px;font-weight:600">对话ID</td><td style="padding:6px">{data.get("id","")}</td></tr>
    <tr><td style="padding:6px;font-weight:600">越狱原因</td><td style="padding:6px;color:#ff3b30">{reason}</td></tr>
    <tr><td style="padding:6px;font-weight:600">当前状态</td><td style="padding:6px">{jb_label}</td></tr>
    <tr><td style="padding:6px;font-weight:600">触发消息ID</td><td style="padding:6px">{jailbreak_msg_id or "未知"}</td></tr>
    <tr><td style="padding:6px;font-weight:600">总对话轮次</td><td style="padding:6px">{data.get("stats",{}).get("rounds","")} 轮</td></tr>
</table>

<h2>🔍 越狱触发对话（标红行为越狱消息）</h2>
<table style="border-collapse:collapse;width:100%">
    <tr style="background:#f5f5f5">
        <th style="border:1px solid #ddd;padding:8px;text-align:left">角色</th>
        <th style="border:1px solid #ddd;padding:8px;text-align:left">内容</th>
        <th style="border:1px solid #ddd;padding:8px;text-align:left">评分</th>
    </tr>
    {msgs_html}
    {f'<tr><td colspan="3" style="color:#ff3b30;font-size:13px;padding:8px;text-align:center">⬆️ 标红行为触发越狱的消息</td></tr>' if len(trigger_msgs) > 0 else ''}
</table>

<h2>🧠 全链路过程（按轮次）</h2>
    <p style="font-size:12px;color:#666;margin-bottom:12px">每轮展示 Query 改写 → 检索 → LLM 生成 → 后处理 的真实耗时和数据</p>
    {rounds_html}

<p style="color:#999;font-size:11px;margin-top:20px">生成时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}</p>
<p style="color:#999;font-size:11px">本报告由 AI Security Agent 自动生成</p>
</body></html>"""

    ascii_name = filename.encode("ascii", errors="replace").decode("ascii")
    if ascii_name != filename:
        encoded_name = urllib.parse.quote(filename)
        disp = f'attachment; filename="{ascii_name}"; filename*=UTF-8\'\'{encoded_name}'
    else:
        disp = f'attachment; filename="{filename}"'
    return HTMLResponse(content=html, headers={"Content-Disposition": disp})


def _render_trace_report(trace: dict) -> str:
    """将 RAG 全链路 trace 数据渲染为 HTML"""
    steps_html = ""
    step_num = 0
    for s in trace.get("steps", []):
        step_num += 1
        step_name = s.get("step", "unknown")
        if step_name == "query_rewrite":
            orig = s.get("original", "")
            rewritten = s.get("rewritten", "")
            t = s.get("time_s", 0)
            if rewritten:
                steps_html += f"""<h3>#{step_num} Query 改写</h3>
<p>原始查询: <code>{orig}</code></p>
<p>改写后: <code>{rewritten}</code>（耗时 {t}s）</p>"""
            else:
                steps_html += f"""<h3>#{step_num} Query 改写</h3>
<p>查询无需改写: <code>{orig}</code></p>"""
        elif step_name == "retrieval":
            cnt = s.get("total_results", 0)
            t = s.get("time_s", 0)
            top = s.get("top_sources", [])
            top_html = ""
            for src in top[:5]:
                top_html += f"<li>{src.get('file_name','')} — {src.get('section','')}</li>"
            steps_html += f"""<h3>#{step_num} 检索（FAISS + BM25 混合）</h3>
<p>检索返回 <strong>{cnt}</strong> 条结果，耗时 {t}s</p>
<p>Top 来源:</p><ul>{top_html}</ul>"""
        elif step_name == "jailbreak_detection":
            triggered = s.get("triggered", False)
            reason = s.get("reason", "")
            steps_html += f"""<h3>#{step_num} 越狱检测</h3>
<p>触发: {'✅ 是' if triggered else '❌ 否'}</p>
<p>原因: {reason}</p>
<p>用户查询: {s.get('user_query', '')}</p>"""
        elif step_name == "greeting_detection":
            steps_html += f"""<h3>#{step_num} 社交寒暄检测</h3>
<p>检测到寒暄，未进入 RAG 管线。</p>"""
        elif step_name == "llm_generation":
            model = s.get("model", "unknown")
            t = s.get("time_s", 0)
            total = s.get("total_time_s", 0)
            rsp_len = s.get("response_length", 0)
            rsn_len = s.get("reasoning_length", 0)
            preview = s.get("prompt_preview", "")
            steps_html += f"""<h3>#{step_num} LLM 生成</h3>
<p>模型: {model}</p>
<p>生成耗时: {t}s | 总耗时: {total}s</p>
<p>回答长度: {rsp_len} 字符 | 思考过程: {rsn_len} 字符</p>
<p>Prompt 预览（末2轮）:</p><pre style="background:#fff;padding:8px;border-radius:4px;font-size:12px">{preview}</pre>"""
        elif step_name == "post_processing":
            corrected = s.get("source_check_corrected", False)
            actions = s.get("actions", [])
            steps_html += f"""<h3>#{step_num} 后处理</h3>
<p>来源核验修正: {'✅ 是' if corrected else '❌ 否'}</p>
<p>执行动作: {', '.join(actions) if actions else '无'}</p>"""
        elif step_name == "output_filter":
            was_filtered = s.get("was_filtered", False)
            steps_html += f"""<h3>#{step_num} 输出越界过滤</h3>
<p>拦截越界内容: {'✅ 是' if was_filtered else '❌ 否'}</p>"""
        elif step_name == "annotation_validation":
            was_annotated = s.get("was_annotated", False)
            steps_html += f"""<h3>#{step_num} 来源标注校验</h3>
<p>补充标注: {'✅ 是' if was_annotated else '❌ 否'}</p>"""
        else:
            steps_html += f"""<h3>#{step_num} {step_name}</h3>
<p><pre style="background:#fff;padding:8px;border-radius:4px;font-size:12px">{json.dumps(s, ensure_ascii=False, indent=2)}</pre></p>"""

    return f"""<div style="background:#f9f9f9;border-radius:8px;padding:16px;margin-bottom:20px">
<p>原始查询: <code>{trace.get("original_query", "")}</code></p>
<p>改写启用: {'✅ 是' if trace.get("rewrite_enabled") else '❌ 否'}</p>
<hr style="border:none;border-top:1px solid #ddd;margin:12px 0">
{steps_html}
</div>"""


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

@app.post("/api/admin/cleanup")
async def admin_cleanup():
    """清理环境：清空 upload_staging + 重置 _doc_tasks。"""
    _cleanup_staging()
    return JSONResponse({"status": "ok", "message": "环境已清理"})

@app.get("/api/admin/stream")
async def admin_event_stream():
    """SSE 实时监控：对话列表变动、新消息、进行中对话"""
    q = event_bus.subscribe()
    convs = agent.memory.get_conversations(include_deleted=True, include_test=True)

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
        try:
            # 发送初始状态
            async for chunk in _send_initial_state():
                yield chunk
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=30)
                    yield payload
                except asyncio.TimeoutError:
                    yield f"event: heartbeat\ndata: {json.dumps({'t': 'keep-alive'})}\n\n"
                except Exception as e:
                    logger.warning(f"SSE 连接异常: {e}")
                    break
        finally:
            # 连接关闭时自动取消订阅，防止内存泄漏
            event_bus.unsubscribe(q)
            logger.info(f"SSE 连接关闭，当前订阅数: {event_bus.subscriber_count}")

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
        return HTMLResponse(html_path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})
    return HTMLResponse("<h1>管理后台页面未找到</h1><p>请检查 templates/admin.html</p>")


@app.get("/admin/documents", response_class=HTMLResponse)
async def admin_documents():
    """文档入库管理页面"""
    html_path = _STATIC / "data_preview.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})
    return HTMLResponse("<h1>页面未找到</h1>")


@app.api_route("/admin/model-config", methods=["GET", "HEAD"], response_class=HTMLResponse)
async def admin_model_config():
    """后端模型配置预览页
    支持 HEAD 请求（浏览器探测用）
    """
    if not _STATIC.exists():
        return HTMLResponse("<h1>静态文件目录不存在</h1>")
    html_path = _STATIC / "admin_model_preview.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"), headers={"Cache-Control": "no-store, must-revalidate", "Pragma": "no-cache"})
    return HTMLResponse("<h1>页面未找到</h1>")


@app.get("/favicon.ico")
async def favicon():
    return HTMLResponse("")


# ============================================================
# 文档入库处理（upload → parse → clean → vector rebuild）
# ============================================================

# 项目根目录（用于找到 preprocessor 脚本和 RAG_DATA）
def _find_project_root() -> Path:
    """从 main.py 向上回溯到项目根目录"""
    p = Path(__file__).resolve()
    for _ in range(10):
        if (p / "packages").is_dir() and (p / "RAG_DATA").is_dir():
            return p
        p = p.parent
    return Path(__file__).resolve().parent.parent.parent.parent

_PROJECT_ROOT = _find_project_root()
logger.info(f"📁 项目根目录: {_PROJECT_ROOT}")

_UPLOAD_STAGING = _PROJECT_ROOT / "upload_staging"
_UPLOAD_STAGING.mkdir(exist_ok=True)

_MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB 上限，超过的文件跳过不处理

# 处理任务状态存储
_doc_tasks: dict[str, dict] = {}
_doc_tasks_lock = threading.Lock()

# 启动初始化：LLM 配置、数据迁移、清理环境
_init_on_startup()


def _load_source_map() -> dict:
    """加载 source_map.json（返回 dict，key=分类名, value=分类配置）"""
    sm_path = Path(__file__).resolve().parent.parent.parent.parent / "packages" / "preprocessor" / "src" / "source_map.json"
    if sm_path.exists():
        return json.loads(sm_path.read_text(encoding="utf-8"))
    return {}


def _get_source_dirs() -> list[str]:
    """获取所有源目录路径"""
    dirs = []
    for cat_name, cat_config in _load_source_map().items():
        for d in cat_config.get("source_dirs", []):
            if d and os.path.isdir(d):
                dirs.append(d)
    return dirs


# ── 三层去重引擎 ──────────────────────────────────────────
_DEDUP = None
_DEDUP_LOCK = threading.Lock()

def _get_dedup() -> Deduplicator:
    """获取/初始化全局去重引擎"""
    global _DEDUP
    if _DEDUP is not None:
        return _DEDUP
    with _DEDUP_LOCK:
        if _DEDUP is not None:
            return _DEDUP
        _DEDUP = Deduplicator(
            source_dirs=_get_source_dirs(),
            rag_data=str(_PROJECT_ROOT / "RAG_DATA"),
        )
        return _DEDUP


def _scan_duplicates(file_name: str) -> dict:
    """检查文件名是否在已有数据中存在重复（代理到 Deduplicator）"""
    dedup = _get_dedup()
    # 构造一个虚拟路径让 Deduplicator 做 layer 1 检测
    result = dedup.check_file("", file_name)
    d = result.to_dict()
    # 兼容旧字段
    d["in_source"] = bool(result.matched_files)
    d["in_cleaned"] = result.is_duplicate and result.layer == 1
    return d


def _ensure_preprocessor_imports():
    """确保 preprocessor 模块可导入（线程安全）"""
    p = str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "preprocessor" / "src")
    if p not in sys.path:
        sys.path.insert(0, p)


def _parse_single_file(file_path: str, task_id: str, skip_layer2: bool = False) -> tuple[str, str] | None:
    """生产者：解析文件 → 返回 (raw_text, file_stem)，Layer 2 去重在此完成"""
    try:
        file_path_obj = Path(file_path)
        _ensure_preprocessor_imports()
        from odl_parser import OdlParser

        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["stage"] = "parsing"
                _doc_tasks[task_id]["current_file"] = file_path_obj.name

        parser = OdlParser()
        parse_data = parser.parse(str(file_path))
        raw_text = parse_data["full_markdown"]

        if not skip_layer2:
            # Layer 2: SimHash 文本指纹软去重
            dedup = _get_dedup()
            dedup2 = dedup.check_text(raw_text, file_path_obj.name)
            if dedup2.is_duplicate:
                logger.warning(f"  ⚠️ Layer 2 检测到文本重复: {file_path_obj.name} ({dedup2.reason})")
                return None

        return (raw_text, file_path_obj.stem)
    except Exception as e:
        logger.error(f"  ❌ 解析失败 {file_path}: {e}")
        return None


def _clean_and_save(raw_text: str, file_stem: str, category: str, task_id: str) -> str | None:
    """消费者：LLM 清洗 → 保存 .md → 返回 md_path"""
    try:
        _ensure_preprocessor_imports()
        from llm_cleaner import LlmCleaner

        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["stage"] = "cleaning"

        cleaner = LlmCleaner()
        clean_data = cleaner.clean_document(raw_text, file_stem)
        cleaned = clean_data["cleaned_markdown"]

        cleaned_dir = _PROJECT_ROOT / "RAG_DATA" / "03_cleaned" / category
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        md_name = file_stem.replace(" ", "_").replace("-", "_") + ".md"
        md_path = cleaned_dir / md_name
        md_path.write_text(cleaned, encoding="utf-8")
        logger.info(f"  ✅ 已保存: {md_path}")
        return str(md_path)
    except Exception as e:
        logger.error(f"  ❌ 清洗/保存失败 {file_stem}: {e}")
        return None


# FAISS/Chroma 写锁（增量索引子进程不能并发）
_INDEX_LOCK = threading.Lock()


def _incremental_index(task_id: str, md_paths: list[str]):
    """增量索引：将新增的 .md 文件追加到父文档索引 + FAISS + Chroma"""
    preprocessor_dir = str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "preprocessor" / "src")
    script = "incremental_index.py"
    args = [sys.executable, script] + md_paths

    with _doc_tasks_lock:
        if task_id in _doc_tasks:
            _doc_tasks[task_id]["stage"] = "incremental_index"
    logger.info(f"  ▶️ 运行增量索引 ({len(md_paths)} 个文件)...")
    try:
        with _INDEX_LOCK:
            result = subprocess.run(args, cwd=preprocessor_dir, capture_output=True, text=True, timeout=600)
        for line in result.stdout.split("\n"):
            if line.strip():
                logger.info(f"  {line.strip()}")
        if result.returncode != 0:
            logger.error(f"  ❌ 增量索引失败: {result.stderr[:500]}")
            with _doc_tasks_lock:
                if task_id in _doc_tasks:
                    _doc_tasks[task_id]["error"] = f"增量索引失败: {result.stderr[:200]}"
            return False
        logger.info(f"  ✅ 增量索引完成")
        agent.refresh_retriever()
        return True
    except subprocess.TimeoutExpired:
        logger.error(f"  ❌ 增量索引超时")
        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["error"] = f"增量索引超时"
        return False


def _run_processing_task(task_id: str, files: list[dict], category: str):
    """多 staging 管道：解析(CPU) 与 清洗/入库(IO) 并行（最多3路并发）"""
    try:
        total = len(files)
        success_count = [0]       # 用 list 包裹以便闭包修改
        fail_count = [0]

        # ── 新版本覆盖：删除旧版 .md + parent_texts 条目 ─────
        old_stems_to_clear: list[str] = []
        for f in files:
            if f.get("conflict_action") != "overwrite":
                continue
            dedup = _get_dedup()
            dr = dedup.check_file("", f["name"])
            if dr.is_duplicate and dr.layer == 1:
                # matched_files 中的 old stem 用于清理
                for mf in dr.matched_files:
                    old_stem = Path(mf).stem if mf.endswith(".md") else mf.rsplit(".", 1)[0]
                    old_stem_normalized = old_stem.replace(" ", "_").replace("-", "_")
                    # 删除旧 .md
                    cleaned_dir = _PROJECT_ROOT / "RAG_DATA" / "03_cleaned" / category
                    for ext in (".md",):
                            old_md = cleaned_dir / f"{old_stem_normalized}{ext}"
                            if old_md.exists():
                                old_md.unlink()
                                logger.warning(f"  🗑️ 删除旧版 .md: {old_md.name}")
                                old_stems_to_clear.append(old_stem_normalized)
                    # 也试试原始 stem
                    old_md2 = cleaned_dir / f"{old_stem}.md"
                    if old_md2.exists() and old_stem != old_stem_normalized:
                        old_md2.unlink()
                        logger.warning(f"  🗑️ 删除旧版 .md: {old_md2.name}")
                        old_stems_to_clear.append(old_stem)

        # 从 parent_texts.json 清理旧条目
        if old_stems_to_clear:
            parent_file = _PROJECT_ROOT / "RAG_DATA" / "04_vector_store" / "parent_texts.json"
            if parent_file.exists():
                try:
                    parent_data = json.loads(parent_file.read_text(encoding="utf-8"))
                    before = len(parent_data)
                    keys_to_delete = [k for k in parent_data if any(s in k for s in old_stems_to_clear)]
                    for k in keys_to_delete:
                        del parent_data[k]
                    parent_file.write_text(json.dumps(parent_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.warning(f"  🗑️ 从 parent_texts 清理 {before - len(parent_data)} 条旧条目 (剩余 {len(parent_data)})")
                except Exception as e:
                    logger.error(f"  ❌ 清理 parent_texts 失败: {e}")

            # 从 Chroma + FAISS 清理旧版向量
            if old_stems_to_clear:
                try:
                    preprocessor_dir = str(Path(__file__).resolve().parent.parent.parent.parent / "packages" / "preprocessor" / "src")
                    remove_args = [sys.executable, "incremental_index.py", "--remove-stems"] + old_stems_to_clear
                    with _INDEX_LOCK:
                        result = subprocess.run(remove_args, cwd=preprocessor_dir, capture_output=True, text=True, timeout=600)
                    for line in result.stdout.split("\n"):
                        if line.strip():
                            logger.info(f"  {line.strip()}")
                    if result.returncode != 0:
                        logger.error(f"  ❌ 清理 Chroma/FAISS 失败: {result.stderr[:300]}")
                    agent.refresh_retriever()
                except Exception as e:
                    logger.error(f"  ❌ 清理 Chroma/FAISS 异常: {e}")

        # ── 多 staging 管道（3个槽，每槽5个，独立清洗+索引） ──
        _STAGING_SLOTS = 3
        _STAGING_BATCH = 5
        staging_buffers = [[] for _ in range(_STAGING_SLOTS)]
        staging_locks = [threading.Lock() for _ in range(_STAGING_SLOTS)]
        staging_busy = [False] * _STAGING_SLOTS
        active_workers = [0]

        def _slot_worker(slot_idx: int, batch: list):
            """单个 staging 的处理线程：LLM 清洗 → 存 .md → 增量索引"""
            try:
                md_paths = []
                for raw_text, file_stem in batch:
                    logger.info(f"  [Slot {slot_idx}] 开始清洗: {file_stem}")
                    md_path = _clean_and_save(raw_text, file_stem, category, task_id)
                    if md_path:
                        md_paths.append(md_path)
                        success_count[0] += 1
                        logger.info(f"  [Slot {slot_idx}] ✅ 清洗完成: {file_stem}")
                    else:
                        fail_count[0] += 1
                        logger.warning(f"  [Slot {slot_idx}] ❌ 清洗失败: {file_stem}")
                    with _doc_tasks_lock:
                        if task_id in _doc_tasks:
                            done = success_count[0] + fail_count[0]
                            pct = 50 + int((done / total) * 40)
                            _doc_tasks[task_id]["progress"] = min(pct, 90)
                if md_paths:
                    logger.info(f"  [Slot {slot_idx}] 开始增量索引 ({len(md_paths)} 个文件)")
                    _incremental_index(task_id, md_paths)
                    logger.info(f"  [Slot {slot_idx}] ✅ 增量索引完成")
            finally:
                with staging_locks[slot_idx]:
                    staging_busy[slot_idx] = False
                active_workers[0] -= 1
                logger.info(f"  [Slot {slot_idx}] 释放（还剩 {active_workers[0]} 个活跃）")

        def producer():
            for idx, f in enumerate(files):
                with _doc_tasks_lock:
                    if task_id in _doc_tasks:
                        _doc_tasks[task_id]["progress"] = int((idx / total) * 50)
                        _doc_tasks[task_id]["current_file"] = f["name"]
                skip_l2 = f.get("conflict_action") == "overwrite"
                parsed = _parse_single_file(f["path"], task_id, skip_layer2=skip_l2)

                if parsed is None:
                    fail_count[0] += 1
                    continue

                raw_text, file_stem = parsed

                # 轮询找可用 staging，全部满则等
                added = False
                while not added:
                    for i in range(_STAGING_SLOTS):
                        with staging_locks[i]:
                            full = False
                            if not staging_busy[i] and len(staging_buffers[i]) < _STAGING_BATCH:
                                staging_buffers[i].append((raw_text, file_stem))
                                full = len(staging_buffers[i]) >= _STAGING_BATCH
                                added = True
                        if full:
                            with staging_locks[i]:
                                staging_busy[i] = True
                                batch = staging_buffers[i]
                                staging_buffers[i] = []
                            active_workers[0] += 1
                            t = threading.Thread(target=_slot_worker, args=(i, batch), daemon=True, name=f"slot-{i}-{task_id}")
                            t.start()
                        if added:
                            break
                    if not added:
                        time.sleep(2)

            # 处理尾巴：不满 batch 的 staging
            for i in range(_STAGING_SLOTS):
                with staging_locks[i]:
                    if not staging_busy[i] and staging_buffers[i]:
                        batch = staging_buffers[i]
                        staging_buffers[i] = []
                        staging_busy[i] = True
                    else:
                        batch = None
                if batch:
                    active_workers[0] += 1
                    t = threading.Thread(target=_slot_worker, args=(i, batch), daemon=True, name=f"slot-tail-{i}-{task_id}")
                    t.start()

        t1 = threading.Thread(target=producer, daemon=True, name=f"producer-{task_id}")
        t1.start()
        t1.join()

        # 等所有 slot worker 完成
        while active_workers[0] > 0:
            time.sleep(1)

        # 清理 staging 目录：处理完成的任务目录删掉
        _clean_staging_task_dir(task_id)

        # 入库质量统计持久化
        try:
            st = agent.stats()
            agent.memory.save_pipeline_stats(
                task_id=task_id,
                total_files=total,
                success_count=success_count[0],
                fail_count=fail_count[0],
                faiss_after=st.get("faiss_vectors", 0),
                chroma_after=st.get("chroma_chunks", 0),
            )
        except Exception as e:
            logger.warning(f"pipeline_stats 写入失败: {e}")

        # 去重缓存失效
        _get_dedup().invalidate_cache()

        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["status"] = "completed"
                _doc_tasks[task_id]["progress"] = 100
                _doc_tasks[task_id]["stage"] = "done"
                _doc_tasks[task_id]["summary"] = {
                    "total": total, "success": success_count[0], "fail": fail_count[0]
                }
    except Exception as e:
        logger.error(f"处理任务 {task_id} 异常: {e}")
        # 清理 staging 目录：失败的任务目录也删掉
        _clean_staging_task_dir(task_id)
        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["status"] = "error"
                _doc_tasks[task_id]["error"] = str(e)


@app.post("/api/documents/scan")
async def documents_scan(files: list[UploadFile] = File(...)):
    """扫描上传的文件元数据 + 三层去重校验 + 批次内新旧比对"""
    dedup = _get_dedup()
    results = []
    for f in files:
        file_bytes = await f.read()
        checksum = hashlib.md5(file_bytes).hexdigest()

        # 文件大小上限检查
        if len(file_bytes) > _MAX_FILE_SIZE:
            results.append({
                "name": f.filename,
                "size": len(file_bytes),
                "checksum": checksum,
                "duplicate": True,
                "reason": f"文件超过50MB限制（{len(file_bytes)/1024/1024:.1f}MB），跳过处理",
                "dedup_layer": 0,
                "standard_id": None,
                "incoming_year": None,
                "existing_year": None,
                "matched_files": [],
                "in_source": False,
                "in_cleaned": False,
            })
            continue

        # Layer 1: 文件级去重（毫秒级）
        dr = dedup.check_file("", f.filename)
        dup_info = dr.to_dict()
        dup_info["in_source"] = dr.is_duplicate and dr.layer == 1
        dup_info["in_cleaned"] = dr.is_duplicate and dr.layer == 1
        results.append({
            "name": f.filename,
            "size": len(file_bytes),
            "checksum": checksum,
            "duplicate": dr.is_duplicate,
            "reason": dr.reason,
            "dedup_layer": dr.layer,
            "standard_id": dr.existing_standard,
            "incoming_year": dr.incoming_year,
            "existing_year": dr.existing_year,
            "matched_files": dr.matched_files[:5],
            "in_source": dup_info["in_source"],
            "in_cleaned": dup_info["in_cleaned"],
        })

    # 批次内交叉比对：同标准号不同年份，旧版标重复，新版保留
    from deduplicator import extract_standard_id
    batch_standards: dict = {}
    for idx, r in enumerate(results):
        sid = extract_standard_id(r["name"])
        if sid:
            key = f"{sid['prefix'].replace('_', '/')} {sid['number']}"
            year = sid.get("year_int") or 0
            if key not in batch_standards:
                batch_standards[key] = []
            batch_standards[key].append((year, idx))

    for key, entries in batch_standards.items():
        if len(entries) < 2:
            continue
        years = [e[0] for e in entries if e[0] > 0]
        if not years:
            continue
        max_year = max(years)
        for year, idx in entries:
            if 0 < year < max_year and not results[idx]["duplicate"]:
                results[idx]["duplicate"] = True
                results[idx]["reason"] = f"批次内有新版（{max_year}），当前旧版（{year}）不处理"
                results[idx]["dedup_layer"] = 1
                results[idx]["existing_year"] = max_year
            elif year == max_year and any(y < max_year for y in years):
                results[idx]["reason"] = f"批次内检测到旧版，当前为新版本（{year}）"
                results[idx]["existing_year"] = max_year

    return JSONResponse({"status": "ok", "files": results})


@app.post("/api/documents/start-processing")
async def documents_start(data: dict = Body(...)):
    """开始处理上传的文件"""
    files = data.get("files", [])
    category = data.get("category", "通用")
    conflict_actions = data.get("conflict_actions", {})
    if not files:
        return JSONResponse({"status": "error", "message": "没有文件"})

    task_id = uuid.uuid4().hex[:12]
    staging_files = []

    for f in files:
        name = f.get("name", "")
        action = conflict_actions.get(name, "overwrite")
        if action == "skip":
            continue
        content_b64 = f.get("content", "")
        if not content_b64:
            continue
        file_bytes = base64.b64decode(content_b64)
        # 文件大小上限检查（二次防护）
        if len(file_bytes) > _MAX_FILE_SIZE:
            logger.warning(f"  ⏭️ 跳过超大文件: {name} ({len(file_bytes)/1024/1024:.1f}MB)")
            continue
        file_dir = _UPLOAD_STAGING / task_id
        file_dir.mkdir(parents=True, exist_ok=True)
        file_path = file_dir / name
        file_path.write_bytes(file_bytes)
        staging_files.append({"name": name, "path": str(file_path), "conflict_action": action})

    if not staging_files:
        return JSONResponse({"status": "error", "message": "没有有效的文件"})

    with _doc_tasks_lock:
        _doc_tasks[task_id] = {
            "status": "processing",
            "progress": 0,
            "stage": "starting",
            "current_file": "",
            "summary": None,
            "error": None,
        }

    # 后台线程处理
    t = threading.Thread(
        target=_run_processing_task,
        args=(task_id, staging_files, category),
        daemon=True,
    )
    t.start()

    return JSONResponse({"status": "ok", "task_id": task_id, "files": len(staging_files)})


@app.get("/api/documents/status/{task_id}")
async def documents_status(task_id: str):
    """获取处理状态"""
    with _doc_tasks_lock:
        task = _doc_tasks.get(task_id)
    if not task:
        return JSONResponse({"status": "unknown", "progress": 0})
    return JSONResponse(task)


@app.get("/api/documents/debug-dedup")
async def debug_dedup():
    """调试：查看去重引擎状态"""
    import traceback
    info = {}
    try:
        dedup = _get_dedup()
        info["source_dirs"] = dedup._source_dirs
        info["rag_data"] = str(dedup._rag_data) if dedup._rag_data else None

        from deduplicator import normalize_stem, extract_standard_id
        test_name = "信息安全技术_信息安全风险评估规范(1).pdf"
        test_norm = normalize_stem(test_name)
        info["test_normalize"] = test_norm

        # 检查 03_cleaned/测试入库 下的文件
        test_file_path = dedup._rag_data / "03_cleaned" / "测试入库"
        if test_file_path.is_dir():
            test_files = []
            for f in test_file_path.iterdir():
                norm = normalize_stem(f.stem)
                test_files.append({"name": f.name, "normalized": norm})
            info["test_dir_files"] = test_files

        # 检查 _collect_existing_files 结果
        existing = dedup._collect_existing_files()
        info["existing_keys_count"] = len(existing)

        # 搜索 "信息安全风险" 相关的 key
        matching_keys = [k for k in existing if "信息安全风险" in k]
        info["security_risk_keys"] = matching_keys

        # 手动运行 check_file
        result = dedup.check_file("", test_name)
        info["check_result"] = result.to_dict()
    except Exception as e:
        info["error"] = str(e)
        info["traceback"] = traceback.format_exc()
    return JSONResponse(info)


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
                    real_conv_id = event.get("conversation_id", conv_id)
                    if real_conv_id:
                        if real_conv_id in _ACTIVE_CONVERSATIONS:
                            del _ACTIVE_CONVERSATIONS[real_conv_id]
                            asyncio.create_task(event_bus.publish("active_removed", {
                                "conv_id": real_conv_id,
                            }))
                        asyncio.create_task(event_bus.publish("conversation_updated", {
                            "conv_id": real_conv_id,
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
async def drill_down(type: str, key: str, limit: int = 50, category: str = "all"):
    try:
        results = agent.memory.drill_down(type, key, limit, category=category)
        return JSONResponse(results)
    except Exception as e:
        logger.error(f"钻取查询失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/dashboard")
async def dashboard_stats(category: str = "all"):
    try:
        data = agent.memory.get_dashboard_stats(category=category)
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
        "faiss_ready": os.path.exists(os.path.join(str(Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "04_vector_store" / "faiss_index"), "index.faiss")),
        "chroma_ready": os.path.exists(str(Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "04_vector_store" / "chroma_db")),
    }


# ========== 入库质量 / 检索质量 API ==========
@app.get("/api/stats/pipeline")
async def pipeline_stats(limit: int = 30):
    try:
        data = agent.memory.get_pipeline_stats(limit=limit)
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"pipeline_stats 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/retrieval-eval")
async def retrieval_eval(limit: int = 100):
    try:
        data = agent.memory.get_retrieval_eval(limit=limit)
        data["eval_summary"] = _load_eval_summary("retrieval_quality")
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"retrieval_eval 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ====== Retrieval Eval 测试集管理 CRUD ======

@app.get("/api/stats/retrieval-eval/items")
async def get_retrieval_eval_items():
    try:
        items = agent.memory.get_retrieval_eval_items()
        return JSONResponse({"items": items, "total": len(items)})
    except Exception as e:
        logger.error(f"get_retrieval_eval_items 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/stats/retrieval-eval/items")
async def add_retrieval_eval_item(data: dict):
    try:
        query = data.get("query", "").strip()
        expected = data.get("expected", "").strip()
        category = data.get("category", "")
        difficulty = data.get("difficulty", "medium")
        if not query or not expected:
            return JSONResponse({"error": "query 和 expected 不能为空"}, status_code=400)
        item_id = agent.memory.add_retrieval_eval_item(query, expected, category, difficulty)
        return JSONResponse({"id": item_id, "success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.put("/api/stats/retrieval-eval/items")
async def update_retrieval_eval_item(data: dict):
    try:
        item_id = data.get("id")
        if not item_id:
            return JSONResponse({"error": "id 不能为空"}, status_code=400)
        ok = agent.memory.update_retrieval_eval_item(
            item_id,
            data.get("query", ""),
            data.get("expected", ""),
            data.get("category", ""),
            data.get("difficulty", "medium"),
            data.get("is_active", 1)
        )
        return JSONResponse({"success": ok})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.delete("/api/stats/retrieval-eval/items/{item_id}")
async def delete_retrieval_eval_item(item_id: int):
    try:
        ok = agent.memory.delete_retrieval_eval_item(item_id)
        return JSONResponse({"success": ok})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/stats/retrieval-eval/items/batch-import")
async def batch_import_retrieval_eval_items(data: dict):
    """批量导入测试用例: {items: [[query, expected, category, difficulty], ...]}"""
    try:
        raw = data.get("items", [])
        count = agent.memory.batch_import_retrieval_eval_items(raw)
        return JSONResponse({"imported": count, "success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/stats/retrieval-eval/run-single")
async def retrieval_eval_run_single(data: dict):
    """单条检索质量跑分"""
    try:
        query = data.get("query", "").strip()
        expected = data.get("expected", "").strip()
        if not query or not expected:
            return JSONResponse({"error": "query 和 expected 不能为空"}, status_code=400)
        
        # 利用已有 eval 逻辑进行单条评估
        from _eval_retrieval import evaluate_single_query
        result = evaluate_single_query(agent, query, expected)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"run-single 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/stats/retrieval-eval/generate")
async def retrieval_eval_generate(data: dict):
    """AI 生成检索质量测试集、自动跑分、并存入测试用例表"""
    keywords = data.get("keywords", "网络安全")
    from _eval_retrieval import generate_test_set_from_keywords, evaluate_with_items
    items = generate_test_set_from_keywords(keywords, llm=getattr(agent, "llm", None))
    result = evaluate_with_items(items, agent.memory)

    # 自动将生成的测试用例存入 retrieval_eval_items 表
    saved = 0
    for item in items:
        try:
            agent.memory.add_retrieval_eval_item(
                query=item.get("query", ""),
                expected=item.get("expected", ""),
                category=item.get("category", ""),
                difficulty=item.get("difficulty", "medium"),
            )
            saved += 1
        except Exception:
            continue
    logger.info(f"生成并跑分: {len(items)} 条, 已存入测试集 {saved} 条")

    # 用 ⑥ 后端评测 LLM 生成分析建议
    summary_text = ""
    try:
        eval_llm = _get_backend_eval_llm()
        if eval_llm:
            summary_text = _generate_eval_summary(eval_llm, "retrieval_quality", result, items)
    except Exception as e:
        logger.warning(f"生成分析建议失败: {e}")

    return {"ok": True, "items": items, "result": result, "saved_to_items": saved, "summary": summary_text}


def _build_report_doc(title: str, date_line: str, summary_cards: list, headers: list, rows: list) -> BytesIO:
    """构建 Word 报告文档"""
    doc_obj = docx.Document()
    style = doc_obj.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(10)

    h = doc_obj.add_heading(title, level=1)
    h.alignment = 1  # center

    p = doc_obj.add_paragraph(date_line)
    p.alignment = 0
    for r in p.runs:
        r.font.size = Pt(9)
        r.font.color.rgb = RGBColor(0x6E, 0x6E, 0x73)

    t = doc_obj.add_table(rows=2, cols=len(summary_cards))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    for ci, (num_val, label_text) in enumerate(summary_cards):
        cell_num = t.cell(0, ci)
        cell_num.text = str(num_val)
        for par in cell_num.paragraphs:
            par.alignment = 1
            for r in par.runs:
                r.bold = True
                r.font.size = Pt(18)
                r.font.color.rgb = RGBColor(0x00, 0x71, 0xE3)
        cell_lbl = t.cell(1, ci)
        cell_lbl.text = label_text
        for par in cell_lbl.paragraphs:
            par.alignment = 1
            for r in par.runs:
                r.font.size = Pt(9)
                r.font.color.rgb = RGBColor(0x6E, 0x6E, 0x73)

    doc_obj.add_paragraph()

    if rows:
        tbl = doc_obj.add_table(rows=1 + len(rows), cols=len(headers))
        tbl.style = 'Table Grid'
        for ci, h_text in enumerate(headers):
            cell = tbl.cell(0, ci)
            cell.text = h_text
            for par in cell.paragraphs:
                for r in par.runs:
                    r.bold = True
                    r.font.size = Pt(9)
        for ri, row in enumerate(rows):
            for ci, val in enumerate(row):
                cell = tbl.cell(1 + ri, ci)
                cell.text = str(val)
                for par in cell.paragraphs:
                    for r in par.runs:
                        r.font.size = Pt(9)

    doc_obj.add_paragraph()
    footer_p = doc_obj.add_paragraph("网络安全智能 Agent - 自动生成")
    footer_p.alignment = 2  # right
    for r in footer_p.runs:
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x6E, 0x6E, 0x73)

    buf = BytesIO()
    doc_obj.save(buf)
    buf.seek(0)
    return buf


@app.get("/api/stats/retrieval-eval/report")
async def retrieval_eval_report(limit: int = 60):
    """导出检索质量评估报告 (Word .docx，最新60条)"""
    try:
        data = agent.memory.get_retrieval_eval(limit)
        items = data.get("items", [])
        summary = data.get("summary", {})
        stats = agent.stats()

        summary_cards = [
            (summary.get('count', 0), "测试查询数"),
            (f"{summary.get('avg_recall_5', 0)*100:.0f}%", "平均 Recall@5"),
            (f"{summary.get('avg_recall_10', 0)*100:.0f}%", "平均 Recall@10"),
            (f"{summary.get('avg_mrr', 0):.3f}", "平均 MRR"),
            (stats.get('faiss_vectors', '?'), "FAISS 向量"),
            (stats.get('chroma_chunks', '?'), "Chroma 向量"),
        ]

        headers = ["#", "查询", "期望来源", "R@5", "R@10", "MRR", "评估时间"]
        rows = []
        for i, item in enumerate(items[:60], 1):
            r5 = "✓" if item.get("recall_5") else "✗"
            r10 = "✓" if item.get("recall_10") else "✗"
            rows.append([
                i, item.get('query', ''), item.get('expected_source', ''),
                r5, r10, f"{item.get('mrr', 0):.3f}",
                str(item.get('eval_at', ''))[:16]
            ])

        date_line = f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 已入库: {stats.get('cleaned_docs', '?')}"
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        buf = _build_report_doc("检索质量评估报告", date_line, summary_cards, headers, rows)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 headers={"Content-Disposition": f"attachment; filename=retrieval_eval_report_{ts}.docx"})
    except Exception as e:
        logger.error(f"报告生成失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/stats/retrieval-eval/compare")
async def retrieval_eval_compare(limit: int = 100):
    """获取检索模式对比结果"""
    try:
        data = agent.memory.get_eval_comparison(limit=limit)
        data["eval_summary"] = _load_eval_summary("retrieval_compare")
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"eval_comparison 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/stats/retrieval-eval/compare")
async def retrieval_eval_compare_run(data: dict):
    """运行检索模式对比（4种模式跑同一测试集）"""
    keywords = data.get("keywords", "网络安全")
    from _eval_retrieval import generate_test_set_from_keywords, evaluate_with_items_compare
    items = generate_test_set_from_keywords(keywords, llm=_get_backend_eval_llm())
    result = evaluate_with_items_compare(items, agent.memory)

    # 用 ⑥ 后端评测 LLM 生成分析建议
    summary_text = ""
    try:
        eval_llm = _get_backend_eval_llm()
        if eval_llm:
            summary_text = _generate_eval_summary(eval_llm, "retrieval_compare", result, items)
    except Exception as e:
        logger.warning(f"生成增益对比分析建议失败: {e}")

    return {"ok": True, "items": items, "result": result, "summary": summary_text}


@app.get("/api/stats/eval-summary/retrieval-quality")
async def get_retrieval_quality_summary():
    """获取持久化的检索质量测评总结建议"""
    return _load_eval_summary("retrieval_quality")


@app.get("/api/stats/eval-summary/retrieval-compare")
async def get_retrieval_compare_summary():
    """获取持久化的增益对比总结建议"""
    return _load_eval_summary("retrieval_compare")


@app.get("/api/stats/retrieval-eval/compare/report")
async def retrieval_eval_compare_report(limit: int = 60):
    """导出检索模式增益对比报告 (Word .docx)

    eval_comparison 表每条记录存放 4 种模式的平铺字段，需要展开为多行。
    """
    try:
        data = agent.memory.get_eval_comparison(limit=limit)
        items = data.get("items", [])
        summary = data.get("summary", {})
        stats = agent.stats()

        # 从 summary 中提取各模式均值（memory.py 已经算好了）
        mode_keys = [
            ("faiss_only",       "faiss_only_recall_5",       "faiss_only_mrr"),
            ("bm25_only",        "bm25_only_recall_5",        "bm25_only_mrr"),
            ("hybrid_no_rerank", "hybrid_no_rerank_recall_5", "hybrid_no_rerank_mrr"),
            ("hybrid_rerank",    "hybrid_rerank_recall_5",    "hybrid_rerank_mrr"),
        ]
        summary_cards = []
        for label, r5_key, mrr_key in mode_keys:
            avg_r5 = summary.get(r5_key, 0)
            avg_mrr = summary.get(mrr_key, 0)
            summary_cards.append((
                f"R@{avg_r5*100:.0f}%\nMRR{avg_mrr:.3f}",
                f"{label}"
            ))

        # 增益卡片
        hybrid_gain_r5 = summary.get("hybrid_gain_recall_5", 0)
        rerank_gain_r5 = summary.get("rerank_gain_recall_5", 0)
        hybrid_gain_mrr = summary.get("hybrid_gain_mrr", 0)
        rerank_gain_mrr = summary.get("rerank_gain_mrr", 0)
        summary_cards.append((
            f"{hybrid_gain_r5*100:+.0f}pp\n{hybrid_gain_mrr:+.3f}",
            "Hybrid 增益"
        ))
        summary_cards.append((
            f"{rerank_gain_r5*100:+.0f}pp\n{rerank_gain_mrr:+.3f}",
            "Rerank 增益"
        ))

        # 展开每条记录中的 4 种模式为独立行
        headers = ["#", "查询", "模式", "R@5", "MRR", "评估时间"]
        rows = []
        unfolded_modes = [
            ("FAISS-only",       "faiss_only_recall_5",       "faiss_only_mrr"),
            ("BM25-only",        "bm25_only_recall_5",        "bm25_only_mrr"),
            ("Hybrid no rerank", "hybrid_no_rerank_recall_5", "hybrid_no_rerank_mrr"),
            ("Hybrid+rerank",    "hybrid_rerank_recall_5",    "hybrid_rerank_mrr"),
        ]
        for idx, it in enumerate(items, 1):
            query = it.get('query', '')
            ts = str(it.get('eval_at', ''))[:16]
            for mode_label, r5_key, mrr_key in unfolded_modes:
                r5_val = it.get(r5_key, 0)
                mrr_val = it.get(mrr_key, 0)
                rows.append([
                    idx, query, mode_label,
                    f"{'✓' if r5_val else '✗'} ({r5_val})",
                    f"{mrr_val:.3f}",
                    ts,
                ])

        date_line = f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 测试 {summary.get('count', 0)} 条 | FAISS: {stats.get('faiss_vectors', '?')} | Chroma: {stats.get('chroma_chunks', '?')}"
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        buf = _build_report_doc("检索模式增益对比报告", date_line, summary_cards, headers, rows)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 headers={"Content-Disposition": f"attachment; filename=retrieval_compare_report_{ts}.docx"})
    except Exception as e:
        logger.error(f"增益对比报告生成失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/stats/retrieval-eval/generate-items")
async def retrieval_eval_generate_items(data: dict):
    """仅生成测试集并存入用例表（清空旧的，替换为新的），不跑分"""
    keywords = data.get("keywords", "网络安全")
    from _eval_retrieval import generate_test_set_from_keywords
    items = generate_test_set_from_keywords(keywords, llm=_get_backend_eval_llm())

    # 清空旧用例，替换为新生成的
    agent.memory.clear_retrieval_eval_items()
    saved = 0
    for item in items:
        try:
            agent.memory.add_retrieval_eval_item(
                query=item.get("query", ""),
                expected=item.get("expected", ""),
                category=item.get("category", ""),
                difficulty=item.get("difficulty", "medium"),
            )
            saved += 1
        except Exception:
            continue
    logger.info(f"生成测试集: {len(items)} 条, 已替换 {saved} 条")
    return {"ok": True, "items": items, "saved": saved}


# ========== E2E Eval 综合质量评测 API ==========

@app.get("/api/stats/e2e-eval/items")
async def get_e2e_eval_items():
    try:
        items = agent.memory.get_e2e_eval_items()
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.error(f"get_e2e_eval_items 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/stats/e2e-eval/items")
async def add_e2e_eval_item(data: dict):
    try:
        query = data.get("query", "").strip()
        if not query:
            return JSONResponse({"error": "query 不能为空"}, status_code=400)
        item_id = agent.memory.add_e2e_eval_item(
            query=query,
            domain=data.get("domain", ""),
            difficulty=data.get("difficulty", "中等"),
            style=data.get("style", "plain"),
        )
        return JSONResponse({"id": item_id, "success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.put("/api/stats/e2e-eval/items")
async def update_e2e_eval_item(data: dict):
    try:
        item_id = data.get("id")
        if not item_id:
            return JSONResponse({"error": "id 不能为空"}, status_code=400)
        ok = agent.memory.update_e2e_eval_item(
            item_id,
            data.get("query", ""),
            data.get("domain", ""),
            data.get("difficulty", "中等"),
            data.get("style", "plain"),
            data.get("is_active", 1),
        )
        return JSONResponse({"success": ok})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.delete("/api/stats/e2e-eval/items/{item_id}")
async def delete_e2e_eval_item(item_id: int):
    try:
        ok = agent.memory.delete_e2e_eval_item(item_id)
        return JSONResponse({"success": ok})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/api/stats/e2e-eval/items/seed")
async def seed_e2e_eval_items():
    """用内置 30 题种子数据填充测试集"""
    try:
        from eval_e2e import QUESTIONS
        items = [(q["query"], q["domain"], q["difficulty"], q.get("style", "plain")) for q in QUESTIONS]
        count = agent.memory.batch_import_e2e_eval_items(items)
        return {"seeded": count, "success": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/stats/e2e-eval/generate-items")
async def e2e_eval_generate_items(data: dict = None):
    """用 LLM 根据关键词生成综合质量评测测试集（清空旧的，替换为新的）"""
    keywords = (data or {}).get("keywords", "网络安全 等保 数据安全")
    eval_llm = _get_backend_eval_llm()

    prompt = f"""你是一个网络安全 RAG 系统综合质量评估专家。根据以下关键词，生成 20 条综合质量评测测试用例。

关键词：{keywords}

要求：
1. 每条包含 query（查询语句）、domain（领域分类）、difficulty（难度：基础/中等/困难）、style（风格：plain/role）
2. 覆盖不同难度（基础约30%、中等约40%、困难约30%），随机分配
3. domain 根据关键词的语义自动判断，从以下选取一个最匹配的标签（不要编号）：
   - 等保合规（等保、国标、网络安全法相关）
   - 数据安全（数据安全法、个人信息保护、隐私、数据分类分级相关）
   - 安全运营（安全运营、应急响应、SOC、SoC相关）
   - 管理体系（安全管理组织、制度体系、培训相关）
   - 基础设施安全（CII、关键基础设施、供应链安全相关）
4. 角色扮演（role）风格约占一半，自然语言（plain）约占一半
5. query 用中文，长度 15-50 字，贴合网络安全管理场景

只输出 JSON 数组，不要多余文字，格式：
[
  {{"query": "等保三级对访问控制有什么要求？", "domain": "等保合规", "difficulty": "中等", "style": "plain"}},
  {{"query": "我是安全管理员，CII安全检测评估每年要做几次？", "domain": "基础设施安全", "difficulty": "困难", "style": "role"}}
]"""

    if eval_llm:
        try:
            resp = eval_llm.chat([{"role": "user", "content": prompt}])
            text = resp.get("content", "")
            text = text.strip()
            if text.startswith("```"): text = text.split("\n", 1)[1]
            if text.endswith("```"): text = text.rsplit("```", 1)[0]
            import json
            items = json.loads(text.strip())
            if not isinstance(items, list) or len(items) == 0:
                return JSONResponse({"error": "LLM 返回格式异常"}, status_code=500)
        except Exception as e:
            logger.warning(f"E2E LLM 生成测试集失败: {e}")
            return JSONResponse({"error": f"LLM 生成失败: {e}"}, status_code=500)
    else:
        return JSONResponse({"error": "后端评测 LLM 未配置"}, status_code=400)

    # 清空旧用例，替换为新生成的
    agent.memory.clear_e2e_eval_items()
    saved = 0
    for item in items:
        try:
            agent.memory.add_e2e_eval_item(
                query=item.get("query", ""),
                domain=item.get("domain", ""),
                difficulty=item.get("difficulty", "中等"),
                style=item.get("style", "plain"),
            )
            saved += 1
        except Exception:
            continue

    logger.info(f"E2E 测试集已生成: {saved} 条（关键词: {keywords}）")
    return {"ok": True, "count": saved, "items": items}


@app.post("/api/stats/e2e-eval/run")
async def e2e_eval_run(data: dict = None):
    """运行综合质量评测"""
    try:
        import json as _json
        from pathlib import Path as _Path
        from datetime import datetime as _datetime
        from eval_e2e import run_evaluation, generate_html, _save_version, _compute_stats, _EVAL_DIR, _HTML_DIR

        # 0. 题目数量限制（不传则跑全部）
        item_limit = None
        if data and isinstance(data, dict) and data.get("limit"):
            item_limit = int(data.get("limit", 0))
            if item_limit <= 0:
                item_limit = None

        # 1. 从 DB 加载测试题（最多取 item_limit 条）
        items = agent.memory.get_e2e_eval_items()
        if not items:
            return JSONResponse({"error": "测试集为空，请先添加测试题"}, status_code=400)
        if item_limit:
            items = items[:item_limit]

        questions = []
        for i, item in enumerate(items, 1):
            prefix = (item.get("domain", "通用")[:2]).upper()
            questions.append({
                "id": f"E{prefix}{i:02d}",
                "domain": item.get("domain", "通用"),
                "difficulty": item.get("difficulty", "中等"),
                "query": item["query"],
                "style": item.get("style", "plain"),
            })

        # 2. 评测 LLM — 从 promptEval 卡读取配置（仅用于评分，不影响 agent.ask）
        eval_llm = None
        prompt_eval_cfg = _get_llm_config_card("promptEval")

        # 如果 promptEval 卡是旧的 deepseek-chat，自动用 chat 卡的同款模型
        if prompt_eval_cfg and prompt_eval_cfg.get("model", "").strip() == "deepseek-chat":
            chat_cfg = _get_llm_config_card("chat")
            if chat_cfg and chat_cfg.get("model"):
                logger.info(
                    "promptEval 卡模型为旧的 deepseek-chat，自动同步为 chat 卡模型: "
                    f"{chat_cfg.get('provider','?')} / {chat_cfg.get('model','?')}"
                )
                prompt_eval_cfg["model"] = chat_cfg["model"]
                prompt_eval_cfg["base_url"] = chat_cfg.get("base_url", prompt_eval_cfg["base_url"])
                prompt_eval_cfg["provider"] = chat_cfg.get("provider", prompt_eval_cfg.get("provider", ""))
                # 一并写入数据库，后续直接命中
                _save_llm_config_card(
                    "promptEval", chat_cfg.get("provider", "DeepSeek"),
                    chat_cfg["model"], chat_cfg.get("base_url", prompt_eval_cfg["base_url"]),
                    chat_cfg.get("api_key", prompt_eval_cfg.get("api_key", "")),
                )

        if prompt_eval_cfg and prompt_eval_cfg.get("model") and prompt_eval_cfg.get("base_url"):
            try:
                from llm_provider import LLMProvider as _LLMProvider
                eval_llm = _LLMProvider(
                    base_url=prompt_eval_cfg["base_url"],
                    api_key=prompt_eval_cfg.get("api_key", ""),
                    model=prompt_eval_cfg["model"],
                    use_ollama_fallback=False,
                )
                logger.info(f"评测 LLM 使用 promptEval 卡片: {prompt_eval_cfg.get('provider','?')} / {prompt_eval_cfg.get('model','?')}")
            except Exception as e:
                logger.warning(f"创建评测 LLM 失败: {e}")

        # 3. 跑评估
        ts = _datetime.now().strftime("%Y%m%d_%H%M%S")
        output_json = _Path(str(_EVAL_DIR)) / f"eval_e2e_results_{ts}.json"
        version_tag = ts

        use_llm = True
        if data:
            use_llm = not data.get("heuristic", False)

        results = run_evaluation(
            agent=agent,
            questions=questions,
            use_llm=use_llm,
            output_file=output_json,
            eval_llm=eval_llm,
            answer_llm=eval_llm,  # 回答也用评测 LLM（硅基流动），避免直连 DeepSeek 超时
        )

        # 4. 生成 HTML
        html_file = _Path(str(_HTML_DIR)) / f"eval_e2e_report_{ts}.html"
        generate_html(results, html_file)

        # 5. 保存版本
        stats = _compute_stats(results)
        _save_version(output_json, html_file, version_tag, stats)

        return {
            "ok": True,
            "total": len(results),
            "output_json": str(output_json),
            "html_file": str(html_file),
            "stats": stats,
        }

        # 用 ⑥ 后端评测 LLM 生成分析建议
        try:
            eval_llm = _get_backend_eval_llm()
            if eval_llm:
                _generate_eval_summary(eval_llm, "e2e_quality", stats, results)
        except Exception as e:
            logger.warning(f"生成综合质量分析建议失败: {e}")

    except Exception as e:
        logger.error(f"e2e_eval_run 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/api/stats/e2e-eval/report")
async def e2e_eval_report():
    """获取最新综合质量评测 HTML 报告"""
    from eval_e2e import find_latest_results
    _, latest_html = find_latest_results()
    if not latest_html or not latest_html.exists():
        return JSONResponse({"error": "暂无报告，请先运行评测"}, status_code=404)
    from fastapi.responses import HTMLResponse
    return HTMLResponse(latest_html.read_text(encoding="utf-8"), media_type="text/html")

@app.get("/api/stats/e2e-eval/versions")
async def e2e_eval_versions():
    """获取版本列表"""
    from eval_e2e import _load_versions
    return {"versions": _load_versions()}


@app.get("/api/stats/e2e-eval/latest")
async def e2e_eval_latest():
    """获取最新综合质量评测的完整 JSON 结果"""
    from eval_e2e import find_latest_results
    latest_json, _ = find_latest_results()
    if not latest_json or not latest_json.exists():
        return JSONResponse({"error": "暂无评测结果"}, status_code=404)
    import json
    return JSONResponse(json.loads(latest_json.read_text(encoding="utf-8")))


@app.get("/api/stats/eval-summary/e2e-quality")
async def get_e2e_quality_summary():
    """获取持久化的综合质量评测总结建议"""
    return _load_eval_summary("e2e_quality")


@app.get("/api/stats/e2e-eval/analysis")
async def get_e2e_eval_analysis():
    """用 LLM 实时分析最新综合质量评测结果，生成改进建议

    读取 eval_results/versions/eval_versions.json 最新版本的评分数据，
    构造分析 prompt，调用后端评测 LLM，返回分析文本。
    """
    from eval_e2e import _load_versions

    versions = _load_versions()
    if not versions:
        return JSONResponse({"summary": "", "time": "", "error": "暂无评测数据"}, status_code=200)

    latest = versions[-1]
    scores = latest.get("avg_scores", {})
    total = latest.get("total", 0)
    errors = latest.get("errors", 0)
    c5_rate = latest.get("c5_truncation_rate", 0)

    def _s(v, k):
        val = v.get("avg_scores", {}).get(k)
        return f"{val*100:.1f}%" if val is not None else "N/A"

    # 取最近3个版本做趋势参考
    history = versions[-3:] if len(versions) >= 3 else versions
    trend_lines = "\n".join(
        f"- {v.get('version','?')}: CP={_s(v,'context_precision')} CR={_s(v,'context_recall')} "
        f"FT={_s(v,'faithfulness')} RL={_s(v,'relevancy')} HC={_s(v,'hallucination')}"
        for v in history
    )

    prompt = f"""你是一位网络安全 RAG 系统质量分析专家。分析以下综合质量评测结果。

## 最新版本概况
- 版本：{latest.get('version','?')}（{latest.get('timestamp','')[:19] if latest.get('timestamp') else '?'}）
- 总题数：{total} | 运行错误：{errors}
- C5 截断影响率：{c5_rate}%

## 各指标得分
- Context Precision（CP，检索精度）：{_s(latest,'context_precision')}
- Context Recall（CR，检索召回）：{_s(latest,'context_recall')}
- Faithfulness（FT，回答忠实度）：{_s(latest,'faithfulness')}
- Relevancy（RL，回答相关性）：{_s(latest,'relevancy')}
- Hallucination（HC，幻觉率，越低越好）：{_s(latest,'hallucination')}

## 趋势变化（最近 {"3" if len(history)==3 else len(history)} 次评测）
{trend_lines}

## 目标阈值参考
- CP ≥ 80%，CR ≥ 70%，FT ≥ 85%，RL ≥ 80%，HC ≤ 10%

请输出以下结构分析报告（500字以内，用中文）：

### 1. 整体评估
评测系统目前综合质量水平，处于哪个阶段（初建/可用/优秀）。

### 2. 短板指标深度分析
对低于目标阈值的指标，分析可能原因（知识库缺失/检索策略问题/回答生成问题）。

### 3. 趋势判断
结合历史趋势，当前质量是稳定/上升/下降，判断依据。

### 4. 优先改进建议
给出 3 条最具体、最可操作的改进建议，附上预期收益。"""

    eval_llm = _get_backend_eval_llm()
    if not eval_llm:
        return JSONResponse({"summary": "", "time": latest.get('timestamp','')[:19] if latest.get('timestamp') else "", "error": "后端评测 LLM 未配置"}, status_code=200)

    try:
        loop = asyncio.get_event_loop()
        resp = await loop.run_in_executor(None, lambda: eval_llm.chat([{"role": "user", "content": prompt}]))
        text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        text = text.strip()
        ts = latest.get("timestamp", "")[:19] if latest.get("timestamp") else ""
        return {"summary": text, "time": ts}
    except Exception as e:
        logger.warning(f"E2E eval LLM 分析失败: {e}")
        return JSONResponse({"summary": "", "time": "", "error": str(e)}, status_code=200)


# ========== LLM 配置接口 ==========
@app.get("/api/llm/presets")
async def llm_presets():
    return {"presets": LLM_PRESETS}


@app.get("/api/llm/config")
async def llm_get_config():
    """获取 LLM 配置（API Key 只返回掩码，不返回完整 Key）"""
    full = _load_llm_config()
    cur = full.get("current")
    providers = full.get("providers", {})

    # 构建返回配置：从 JSON 元数据 + SQLite 掩码
    cfg = providers.get(cur, {}).copy() if cur else {}
    cfg.pop("api_key", None)  # 确保不返回完整 Key
    cfg.setdefault("base_url", agent.llm.base_url)
    cfg.setdefault("model", agent.llm.model)

    # 从 SQLite 获取当前提供商的 API Key 掩码
    if cur:
        key_mask = _get_llm_key_mask(cur)
        if key_mask:
            cfg["api_key_mask"] = key_mask
        else:
            cfg["api_key_mask"] = "未配置"

    # 构建提供商列表（只返回名称和掩码，不返回完整 Key）+ 完整 providers_config（含 models，用于前端刷新后展示）
    provider_list = []
    providers_config = {}
    for pname, pcfg in providers.items():
        info = {"name": pname}
        key_mask = _get_llm_key_mask(pname)
        if key_mask:
            info["has_key"] = True
            info["api_key_mask"] = key_mask
        else:
            info["has_key"] = False
        provider_list.append(info)
        # 返回除 api_key 外的完整配置（含刷新后的模型列表），加上掩码用于前端显示
        clean_cfg = {k: v for k, v in pcfg.items() if k != "api_key"}
        clean_cfg["api_key_mask"] = key_mask if key_mask else "未配置"
        providers_config[pname] = clean_cfg

    return {
        "config": cfg,
        "current": cur,
        "providers": provider_list,
        "providers_config": providers_config,
    }


@app.post("/api/llm/config")
async def llm_save_config(data: dict = Body(...)):
    """保存 LLM 配置：API Key 写入 SQLite 加密存储，JSON 只存元数据"""
    provider = data.get("provider", "自定义")
    base_url = data.get("base_url", "").rstrip("/")
    api_key = data.get("api_key", "")
    model = data.get("model", "")

    if not base_url or not model:
        return JSONResponse({"ok": False, "error": "base_url 和 model 不能为空"}, status_code=400)

    full = _load_llm_config()
    full.setdefault("providers", {})
    existing = full["providers"].get(provider, {})

    # 构建配置元数据（不含 api_key）
    cfg = {
        "base_url": base_url,
        "model": model,
    }
    if existing.get("models"):
        cfg["models"] = existing["models"]

    full["providers"][provider] = cfg
    full["current"] = provider
    _save_llm_config(full)

    # API Key 加密写入 SQLite + 热更新 agent.llm
    if api_key:
        _save_llm_key(provider, api_key)
    else:
        existing_key = _get_llm_key(provider)
        if existing_key:
            api_key = existing_key
        else:
            # 都没有则用当前内存中的 Key 兜底
            api_key = agent.llm.api_key if hasattr(agent.llm, 'api_key') else ''
    agent.llm.reconfigure(
        base_url=base_url,
        api_key=api_key,
        model=model,
        provider_name=provider,
    )

    # 同步到后台 chat 配置卡片（llm_configs 表）
    _save_llm_config_card("chat", provider, model, base_url, api_key)

    # 广播配置变更事件（admin SSE 实时显示）
    asyncio.create_task(event_bus.publish("config_update", {
        "source": "frontend",
        "provider": provider,
        "model": model,
    }))

    return {"status": "saved", "message": f"{provider} 配置已保存并生效"}


@app.get("/api/llm/config/current")
async def llm_get_current_config():
    """返回当前生效的 LLM 配置（供前台轮询检测变更）"""
    from llm_provider import LLMProvider
    info = agent.llm.get_current_provider() if hasattr(agent.llm, "get_current_provider") else {}
    return {
        "provider": info.get("name", ""),
        "model": info.get("model", ""),
        "base_url": info.get("base_url", ""),
    }


@app.post("/api/llm/test")
async def llm_test_connection(data: dict = Body(...)):
    """测试 LLM 提供商连接"""
    from llm_provider import LLMProvider

    base_url = data.get("base_url", "")
    api_key = data.get("api_key", "")
    model = data.get("model", "")

    # 如果没传 api_key，尝试从 SQLite 读取
    if not api_key and base_url:
        # 根据 base_url 推断 provider
        for pname, pcfg in _load_llm_config().get("providers", {}).items():
            if pcfg.get("base_url", "").rstrip("/") == base_url.rstrip("/"):
                stored_key = _get_llm_key(pname)
                if stored_key:
                    api_key = stored_key
                    break

    if not api_key:
        # 本地地址（Ollama 等）不需要 API Key
        is_local = any(host in base_url for host in ["localhost", "127.0.0.1", "0.0.0.0"])
        if not is_local:
            return JSONResponse({"ok": False, "error": "未提供 API Key 且未找到已存储的 Key"}, status_code=400)

    # SSRF 防护：校验 URL 在白名单内
    if base_url and not validate_llm_url(base_url):
        return JSONResponse({"ok": False, "error": f"不允许的 LLM API 域名: {base_url}"}, status_code=400)

    temp = LLMProvider()
    result = temp.test_connection(
        base_url=base_url,
        api_key=api_key,
        model=model,
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

    # 如果没传 api_key，尝试从 SQLite 读取
    if not api_key:
        stored_key = _get_llm_key(provider)
        if stored_key:
            api_key = stored_key

    if not api_key:
        return JSONResponse({"ok": False, "models": [], "message": "请先配置该提供商的 API Key"}, status_code=400)

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
        # 百炼的 /v1/models 接口会返回大量在 OpenAI 兼容模式下不可用的模型名，
        # 直接返回官方已验证的预设列表，确保用户只看到可用的模型
        preset_models = LLM_PRESETS.get("阿里云百炼", {}).get("models", [])
        _save_provider_models(provider, preset_models)
        return {"ok": True, "models": preset_models, "count": len(preset_models)}

    # 默认: 硅基流动
    if not api_key:
        return {"ok": False, "models": [], "message": "请先填写 API Key"}
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


# ========== LLM 配置卡片 API（6个独立后端模型持久化）==========
@app.get("/api/llm/configs")
async def llm_configs_get_all():
    """获取所有 6 个 LLM 配置卡片的已保存配置"""
    configs = _get_all_llm_configs()
    return {"configs": configs}


@app.post("/api/llm/configs/save")
async def llm_configs_save_one(data: dict = Body(...)):
    """保存单个 LLM 配置卡片（API Key 加密存储到 llm_configs 表）"""
    module_id = data.get("module_id", "").strip()
    provider = data.get("provider", "").strip()
    model = data.get("model", "").strip()
    base_url = data.get("base_url", "").strip()
    api_key = data.get("api_key", "")

    if not module_id:
        return JSONResponse({"ok": False, "error": "module_id 不能为空"}, status_code=400)
    if module_id not in _CONFIG_CARD_MODULES:
        return JSONResponse({"ok": False, "error": f"无效的 module_id: {module_id}"}, status_code=400)
    if not model:
        return JSONResponse({"ok": False, "error": "model 不能为空"}, status_code=400)

    api_key_mask = _save_llm_config_card(module_id, provider, model, base_url, api_key)

    # 如果是 chat 卡片，热更新 agent.llm 配置（保存即生效，无需重启）
    if module_id == "chat":
        agent.llm.reconfigure(
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider_name=provider,
        )
        logger.info(f"♻️ chat 配置热更新: {provider} / {model}")

        # 广播配置变更事件（admin SSE 实时显示 + 前台可轮询）
        asyncio.create_task(event_bus.publish("config_update", {
            "source": "backend",
            "provider": provider,
            "model": model,
        }))

    return {
        "ok": True,
        "module_id": module_id,
        "api_key_mask": api_key_mask,
        "message": f"「{module_id}」配置已保存",
    }


@app.post("/api/llm/configs/test")
async def llm_configs_test(data: dict = Body(...)):
    """测试单个 LLM 配置卡片的连接

    如果传入了 module_id 且未传 api_key，尝试从数据库读取已保存的 Key。
    根据 module_id 自动选择测试方式：embedding 模型走 /embeddings，其余走 /chat/completions。
    """
    module_id = data.get("module_id", "").strip()
    base_url = data.get("base_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()

    if not base_url:
        return JSONResponse({"ok": False, "error": "base_url 不能为空"}, status_code=400)
    if not model:
        return JSONResponse({"ok": False, "error": "model 不能为空"}, status_code=400)

    # 如果没传 Key 但有 module_id，尝试从数据库读取
    if not api_key and module_id:
        saved = _get_llm_config_card(module_id)
        if saved and saved.get("api_key"):
            api_key = saved["api_key"]

    if not api_key:
        # 本地地址（Ollama 等）不需要 API Key
        is_local = any(host in base_url.lower() for host in ["localhost", "127.0.0.1", "0.0.0.0"])
        if not is_local:
            return JSONResponse({"ok": False, "error": "未提供 API Key 且未找到已存储的 Key"}, status_code=400)

    # SSRF 防护
    if not validate_llm_url(base_url):
        return JSONResponse({"ok": False, "error": f"不允许的 LLM API 域名: {base_url}"}, status_code=400)

    base_url = base_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    import time
    t0 = time.time()
    import requests

    # Embedding 模型走 /embeddings
    if module_id == "embedding":
        try:
            payload = {
                "model": model,
                "input": "测试连接",
                "encoding_format": "float",
            }
            resp = requests.post(
                f"{base_url}/embeddings",
                headers=headers,
                json=payload,
                timeout=(10, 30),
            )
            resp.raise_for_status()
            data = resp.json()
            dim = len(data["data"][0]["embedding"]) if data.get("data") else 0
            elapsed = time.time() - t0
            return {"ok": True, "message": f"连接成功 ({elapsed:.1f}s, 维度={dim})", "model": model}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "message": "无法连接，请检查 BASE URL 是否正确"}
        except requests.exceptions.Timeout:
            return {"ok": False, "message": "连接超时，请检查网络或提供商状态"}
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                return {"ok": False, "message": "认证失败，请检查 API Key 是否正确"}
            elif status == 404:
                return {"ok": False, "message": f"模型 '{model}' 不存在，请检查模型名称"}
            else:
                body = e.response.text[:200]
                return {"ok": False, "message": f"HTTP {status}: {body}"}
        except Exception as e:
            return {"ok": False, "message": f"测试失败: {e}"}

    # Reranker 模型走 /rerank
    if module_id == "reranker":
        try:
            payload = {
                "model": model,
                "query": "测试连接",
                "documents": ["这是一段用于测试的文本"],
            }
            resp = requests.post(
                f"{base_url}/rerank",
                headers=headers,
                json=payload,
                timeout=(10, 30),
            )
            resp.raise_for_status()
            data = resp.json()
            elapsed = time.time() - t0
            results_count = len(data.get("results", []))
            return {"ok": True, "message": f"连接成功 ({elapsed:.1f}s, 结果数={results_count})", "model": model}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "message": "无法连接，请检查 BASE URL 是否正确"}
        except requests.exceptions.Timeout:
            return {"ok": False, "message": "连接超时，请检查网络或提供商状态"}
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                return {"ok": False, "message": "认证失败，请检查 API Key 是否正确"}
            elif status == 404:
                return {"ok": False, "message": f"模型 '{model}' 不存在，请检查模型名称"}
            else:
                body = e.response.text[:200]
                return {"ok": False, "message": f"HTTP {status}: {body}"}
        except Exception as e:
            return {"ok": False, "message": f"测试失败: {e}"}

    # LLM 模型走 /chat/completions
    from llm_provider import LLMProvider
    temp = LLMProvider()
    result = temp.test_connection(
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    return result


# ========== Prompt 评测接口 ==========
@app.post("/api/prompt/test/run")
async def prompt_test_run():
    """运行全部 Prompt 测试用例（使用 promptEval 卡片配置的 LLM）"""
    from prompt_tester import run_all_tests, save_result, get_test_history

    prompt_eval_cfg = _get_llm_config_card("promptEval")
    if not prompt_eval_cfg or not prompt_eval_cfg.get("model") or not prompt_eval_cfg.get("base_url"):
        logger.warning("promptEval 卡片未配置，使用 chat 卡片 LLM 运行 Prompt 测试")

    saved_config = {
        "base_url": getattr(agent.llm, "base_url", ""),
        "api_key": getattr(agent.llm, "api_key", ""),
        "model": getattr(agent.llm, "model", ""),
        "provider_name": getattr(agent.llm, "_provider_name", ""),
    }
    try:
        if prompt_eval_cfg and prompt_eval_cfg.get("model") and prompt_eval_cfg.get("base_url"):
            agent.llm.reconfigure(
                base_url=prompt_eval_cfg["base_url"],
                api_key=prompt_eval_cfg.get("api_key", ""),
                model=prompt_eval_cfg["model"],
                provider_name=prompt_eval_cfg.get("provider", ""),
            )
            logger.info(f"Prompt 测试切换至 promptEval 卡片: {prompt_eval_cfg.get('provider','?')} / {prompt_eval_cfg.get('model','?')}")

        report = run_all_tests(agent)
        db_path = _get_db_path()
        save_result(report, db_path)
        return report
    finally:
        agent.llm.reconfigure(**saved_config)
        logger.info("Prompt 测试完成，已恢复 chat 卡片 LLM")


@app.get("/api/prompt/test/history")
async def prompt_test_history(limit: int = 20):
    """获取历史测试结果"""
    from prompt_tester import get_test_history
    db_path = _get_db_path()
    return {"history": get_test_history(db_path, limit)}


@app.get("/api/prompt/test/suite")
async def prompt_test_suite():
    """获取测试集定义"""
    from prompt_tester import load_suite

    return load_suite()


@app.get("/api/prompt/elastic/{query}")
async def prompt_elastic_test(query: str):
    """弹性测试：同一问题的不同表达方式"""
    from prompt_tester import run_elastic_test

    results = run_elastic_test(agent, query)
    return {"base_query": query, "results": results}


# ========== Prompt 测试管理器 API（新增） ==========
@app.post("/api/prompt/test/generate")
async def prompt_test_generate(data: dict):
    """AI 生成测试集（关键词+模式→20条/15条）"""
    from prompt_test_manager import generate_test_set

    keywords = data.get("keywords", "随机")
    items = generate_test_set(keywords, llm=getattr(agent, "llm", None))
    # 保存到 DB
    set_id = agent.memory.save_ai_test_set(keywords, items)
    return {"ok": True, "set_id": set_id, "items": items}


@app.get("/api/prompt/test/items")
async def prompt_test_items(set_id: str = "builtin"):
    """获取测试集列表"""
    items = agent.memory.get_test_items(set_id)
    return {"items": items}


@app.put("/api/prompt/test/items/{item_id}")
async def prompt_test_update_item(item_id: int, data: dict):
    """编辑单条测试题"""
    ok = agent.memory.update_test_item(
        item_id,
        query=data.get("query"),
        category=data.get("category"),
    )
    return {"ok": ok}


@app.post("/api/prompt/test/run-single/{item_id}")
async def prompt_test_run_single(item_id: int, data: dict = None):
    """单条测试 ▶"""
    from prompt_test_manager import run_single_test

    # 从 DB 获取测试项
    items = agent.memory.get_test_items()
    item = next((i for i in items if i["id"] == item_id), None)
    if not item:
        # 允许从 body 传入完整 item
        if data and "query" in data:
            item = {"id": item_id, "query": data["query"], "category": data.get("category", ""),
                    "difficulty": data.get("difficulty", "medium"),
                    "expected": data.get("expected", {})}
        else:
            return {"ok": False, "error": "未找到测试项"}
    result = run_single_test(item, agent)
    return {"ok": True, "result": result}


@app.post("/api/prompt/test/run-all")
async def prompt_test_run_all(data: dict = None):
    """全部测试 ▶"""
    from prompt_test_manager import run_test_set, run_single_test

    set_id = (data or {}).get("set_id", "builtin")
    items = agent.memory.get_test_items(set_id)
    if not items:
        return {"ok": False, "error": "测试集为空,请先生成或切换到内置测试集"}
    # 异步非 SSE 版本：批量运行
    results = run_test_set(items, agent)
    passed = sum(1 for r in results if r["passed"])
    return {
        "ok": True,
        "total": len(results),
        "passed": passed,
        "failed": len(results) - passed,
        "pass_rate": round(passed / len(results) * 100, 1),
        "results": results,
    }


@app.post("/api/prompt/test/suggest-fix/{item_id}")
async def prompt_test_suggest_fix(item_id: int, data: dict = None):
    """LLM 分析失败→生成修复建议"""
    from prompt_test_manager import suggest_fix
    from agent import SystemPromptLoader

    current_sp = SystemPromptLoader.get()
    payload = (data or {})
    result = suggest_fix(payload, current_sp, llm=getattr(agent, "llm", None))
    return {"ok": True, "suggestion": result}


@app.post("/api/prompt/versions/restore")
async def prompt_version_restore(data: dict):
    """还原版本→覆盖 active_prompt.txt"""
    version_name = data.get("version_name", "")
    prompt_text = agent.memory.restore_prompt_version(version_name)
    if prompt_text is None:
        return {"ok": False, "error": f"版本 {version_name} 不存在"}
    return {"ok": True, "version": version_name, "prompt_preview": prompt_text[:200]}


@app.post("/api/prompt/versions/create")
async def prompt_version_create(data: dict):
    """创建新 Prompt 版本（覆盖扩展字段版本）"""
    return agent.memory.create_prompt_version(
        name=data.get("name", "v_new"),
        description=data.get("description", ""),
        system_prompt=data.get("system_prompt", ""),
        changed_by=data.get("changed_by", "管理员"),
        change_log=data.get("change_log", ""),
        prompt_diff=data.get("prompt_diff", ""),
    )


@app.get("/api/prompt/versions")
async def prompt_versions_list_v2():
    """列出所有 Prompt 版本（扩展字段）"""
    from prompt_versions import list_versions
    db_path = _get_db_path()
    return {"versions": list_versions(db_path)}


@app.get("/api/prompt/versions/active")
async def prompt_versions_active_v2():
    """获取当前激活版本"""
    from prompt_versions import get_active_version
    db_path = _get_db_path()
    return {"active": get_active_version(db_path)}


@app.post("/api/prompt/versions/switch")
async def prompt_version_switch_v2(data: dict):
    """切换激活版本"""
    from prompt_versions import switch_version
    db_path = _get_db_path()
    return switch_version(data.get("version_name", ""), db_path)


@app.get("/api/prompt/versions/compare/{version_a}/{version_b}")
async def prompt_version_compare(version_a: str, version_b: str):
    """对比两个版本"""
    from prompt_versions import compare_versions
    db_path = _get_db_path()
    return compare_versions(version_a, version_b, db_path)


@app.get("/api/prompt/test/latest")
async def prompt_test_latest():
    """获取最新完整测试报告"""
    from prompt_tester import get_latest_full_result
    db_path = _get_db_path()
    result = get_latest_full_result(db_path)
    if result:
        return result
    return {"error": "暂无测评记录"}


# ========== Prompt 版本管理 API ==========

@app.get("/api/prompt/versions")
async def prompt_list_versions():
    """列出所有 Prompt 版本"""
    from prompt_versions import list_versions
    return {"versions": list_versions(_get_db_path())}


@app.get("/api/prompt/versions/{version_id}")
async def prompt_get_version(version_id: int):
    """获取版本详情"""
    from prompt_versions import get_version_prompt
    v = get_version_prompt(version_id, _get_db_path())
    if not v:
        return JSONResponse({"error": "版本不存在"}, status_code=404)
    return v


@app.post("/api/prompt/versions")
async def prompt_create_version(data: dict):
    """创建新版本"""
    from prompt_versions import create_version
    result = create_version(
        version_name=data["name"],
        description=data.get("description", ""),
        system_prompt=data["system_prompt"],
        db_path=_get_db_path(),
    )
    if result.get("ok"):
        try:
            # 写入 active_prompt.txt 并清除缓存
            from agent import SystemPromptLoader
            SystemPromptLoader._path.write_text(data["system_prompt"], encoding="utf-8")
            SystemPromptLoader._cache = None
            SystemPromptLoader._mtime = 0
            logger.info(f"  ✅ 版本 {data['name']} 已激活并写入 active_prompt.txt")
        except Exception as e:
            logger.warning(f"创建版本后更新 active_prompt.txt 失败: {e}")
    return result


@app.put("/api/prompt/versions/{version_id}/activate")
async def prompt_activate_version(version_id: int):
    """设置活跃版本"""
    from prompt_versions import activate_version
    result = activate_version(version_id, _get_db_path())
    if result.get("ok"):
        from agent import SystemPromptLoader
        SystemPromptLoader._path.write_text(result["system_prompt"], encoding="utf-8")
        SystemPromptLoader._cache = None
        SystemPromptLoader._mtime = 0
        logger.info(f"  ✅ 版本 {result['version_name']} 已激活并写入 active_prompt.txt")
    return result


@app.get("/api/prompt/versions/{version_id}/results")
async def prompt_version_results(version_id: int, limit: int = 5):
    """获取版本跑分结果"""
    from prompt_versions import get_version_results
    return get_version_results(version_id, _get_db_path(), limit)


@app.get("/api/prompt/versions/{id1}/compare/{id2}")
async def prompt_compare_versions(id1: int, id2: int):
    """对比两个版本"""
    from prompt_versions import ab_test_versions
    return ab_test_versions(id1, id2, _get_db_path())


@app.get("/api/prompt/versions/{version_id}/regression")
async def prompt_regression_check(version_id: int):
    """退化检测"""
    from prompt_versions import regression_check
    return regression_check(version_id, _get_db_path())


@app.get("/api/prompt/test/export")
async def prompt_export_report():
    """导出 Prompt 评测报告 Word 文档"""
    from prompt_versions import list_versions, get_version_results
    from prompt_tester import load_suite
    import docx
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from io import BytesIO
    from datetime import datetime

    db_path = _get_db_path()
    suite = load_suite()
    cases = suite.get("test_cases", [])
    versions = list_versions(db_path)

    doc_obj = docx.Document()
    style = doc_obj.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(10)

    h = doc_obj.add_heading("Prompt 评测报告", level=1)
    h.alignment = 1

    p = doc_obj.add_paragraph(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for r in p.runs:
        r.font.size = Pt(9)

    # 测试集概览
    doc_obj.add_heading("测试集概览", level=2)
    categories = {}
    for c in cases:
        cat = c.get("category", "其他")
        categories[cat] = categories.get(cat, 0) + 1
    summary_text = f"测试题总数: {len(cases)} | "
    for cat, cnt in categories.items():
        summary_text += f"{cat}: {cnt} | "
    doc_obj.add_paragraph(summary_text)

    # 版本信息
    doc_obj.add_heading("版本信息", level=2)
    if versions:
        tbl = doc_obj.add_table(rows=1 + len(versions), cols=4)
        tbl.style = 'Table Grid'
        for ci, h_text in enumerate(["版本名", "描述", "活跃", "创建时间"]):
            cell = tbl.cell(0, ci)
            cell.text = h_text
            for par in cell.paragraphs:
                for r in par.runs:
                    r.bold = True
                    r.font.size = Pt(9)
        for ri, v in enumerate(versions):
            tbl.cell(1+ri, 0).text = v.get("name", "")
            tbl.cell(1+ri, 1).text = v.get("description", "")[:30]
            tbl.cell(1+ri, 2).text = "✅" if v.get("is_active") else ""
            tbl.cell(1+ri, 3).text = str(v.get("created_at", ""))[:16]

    # 活跃版本跑分
    active_v = [v for v in versions if v.get("is_active")]
    if active_v:
        doc_obj.add_heading(f"活跃版本跑分结果", level=2)
        results = get_version_results(active_v[0]["id"], db_path, 100)
        if results.get("results"):
            r_tbl = doc_obj.add_table(rows=1+len(results["results"]), cols=5)
            r_tbl.style = 'Table Grid'
            for ci, h_text in enumerate(["测试ID", "分类", "查询", "加权得分", "评估时间"]):
                cell = r_tbl.cell(0, ci)
                cell.text = h_text
                for par in cell.paragraphs:
                    for r in par.runs:
                        r.bold = True
                        r.font.size = Pt(9)
            for ri, rr in enumerate(results["results"]):
                r_tbl.cell(1+ri, 0).text = rr.get("test_id", "")
                r_tbl.cell(1+ri, 1).text = rr.get("category", "")
                r_tbl.cell(1+ri, 2).text = rr.get("query", "")
                r_tbl.cell(1+ri, 3).text = str(rr.get("weighted_score", ""))
                r_tbl.cell(1+ri, 4).text = str(rr.get("evaluated_at", ""))[:16]

    doc_obj.add_paragraph()
    footer_p = doc_obj.add_paragraph("网络安全智能 Agent - 自动生成")
    footer_p.alignment = 2
    for r in footer_p.runs:
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x6E, 0x6E, 0x73)

    buf = BytesIO()
    doc_obj.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             headers={"Content-Disposition": "attachment; filename=prompt_eval_report.docx"})


@app.get("/api/prompt/system-prompt")
async def prompt_system_prompt():
    """获取当前 system prompt（从 active_prompt.txt 或内存）"""
    try:
        from agent import agent_instance
        if hasattr(agent_instance, 'system_prompt') and agent_instance.system_prompt:
            return {"system_prompt": agent_instance.system_prompt.strip()}
    except Exception:
        pass
    try:
        p = Path(__file__).parent.parent / "agent_data" / "active_prompt.txt"
        if p.exists():
            return {"system_prompt": p.read_text(encoding="utf-8").strip()}
    except Exception:
        pass
    from agent import SYSTEM_PROMPT_SOURCE
    return {"system_prompt": SYSTEM_PROMPT_SOURCE.strip()}


# ========== 服务重启 ==========
@app.post("/api/llm/configs/restart")
async def restart_server():
    """重启服务（CLI 替代方案，管理员在页面直接点击重启）"""
    import subprocess, sys, os, threading, time

    def _do_restart():
        time.sleep(2.0)
        try:
            subprocess.Popen(
                f'start /B python main.py',
                shell=True,
                cwd=os.getcwd(),
            )
        except Exception as e:
            logger.error(f"重启失败: {e}")
        os._exit(0)

    threading.Thread(target=_do_restart, daemon=True).start()
    return {"ok": True, "message": "服务器正在重启中，请稍候…"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)