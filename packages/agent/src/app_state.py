"""共享应用状态 — 网络安全 RAG Agent

包含全局实例（agent、event_bus）、配置管理、帮助函数、启动初始化。
"""
from llm_config_manager import (
    _fernet, _CRYPTO_AVAILABLE,
    _load_llm_config, _save_llm_config,
    _get_db_path, _save_llm_key, _get_llm_key, _get_llm_key_mask, _has_llm_key, _delete_llm_key,
    _save_llm_config_card, _get_all_llm_configs, _get_llm_config_card,
    _hash_api_key, _encrypt_api_key, _decrypt_api_key, _make_key_mask,
    _get_project_root,
)
from auth import is_admin_route, validate_llm_url
from notification_delivery import publish_system_event
from monitoring import record_event
from deduplicator import Deduplicator
import os
import sys
import json
import logging
import time
import hashlib
import base64
import re
import urllib.parse
import shutil
import uuid
import threading
import queue
import subprocess
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, TimeoutError
import requests
from pathlib import Path
from typing import AsyncGenerator, Optional
from fastapi import Request, Body, HTTPException, UploadFile, File, Form
from fastapi.responses import JSONResponse, StreamingResponse
from io import BytesIO
import docx
from docx.shared import Pt, Inches, RGBColor
from docx.enum.table import WD_TABLE_ALIGNMENT
from jinja2 import Environment, FileSystemLoader
import asyncio

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

_PREPROCESSOR_SRC = str(Path(_SRC).parent.parent.parent / "packages" / "preprocessor" / "src")
if _PREPROCESSOR_SRC not in sys.path:
    sys.path.insert(0, _PREPROCESSOR_SRC)


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_START_TIME = time.time()

_EXCLUDE_MODEL_KEYWORDS = ["embedding", "reranker", "image", "video", "audio", "speech", "ocr",
                           "asr", "tts", "bge", "wan", "kolors", "paddleocr", "captioner", "cosyvoice", "sensevoice"]


class EventBus:
    """SSE 事件广播：admin 页面实时监控"""

    def __init__(self):
        self._subscribers: list[asyncio.Queue] = []

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue):
        try:
            self._subscribers.remove(q)
        except ValueError:
            pass

    async def publish(self, event: str, data: dict):
        payload = f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"
        for q in self._subscribers[:]:
            try:
                await q.put(payload)
            except Exception:
                pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


event_bus = EventBus()
_ACTIVE_CONVERSATIONS: dict = {}

_BASE = Path(__file__).parent
_STATIC = _BASE / "static"
_TEMPLATES = _BASE / "templates"
jinja_env = Environment(loader=FileSystemLoader(str(_TEMPLATES)))

class LazyCyberAgent:
    """Defer heavy LangChain/vector dependencies until the first Agent use."""

    def __init__(self):
        self._instance = None
        self._lock = threading.Lock()

    def _get(self):
        if self._instance is None:
            with self._lock:
                if self._instance is None:
                    from agent import CyberAgent
                    self._instance = CyberAgent()
        return self._instance

    def __getattr__(self, name):
        return getattr(self._get(), name)


agent = LazyCyberAgent()

# ---- LLM 配置与密钥管理 ----
_CONFIG_PATH = Path(__file__).parent.parent / "agent_data" / "llm_config.json"


def _load_llm_config_legacy() -> dict:
    if _CONFIG_PATH.exists():
        try:
            return json.loads(_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"current": None, "providers": {}}


def _save_llm_config_legacy(data: dict):
    _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _CONFIG_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def _save_provider_models(provider: str, models: list[str]):
    full = _load_llm_config_legacy()
    full.setdefault("providers", {})
    full["providers"].setdefault(provider, {})
    full["providers"][provider]["models"] = models
    _save_llm_config_legacy(full)


def _cleanup_provider_models():
    full = _load_llm_config_legacy()
    changed = False
    for pname, pcfg in full.get("providers", {}).items():
        preset = LLM_PRESETS.get(pname)
        if preset and not preset.get("can_refresh", True):
            preset_models = preset.get("models", [])
            if pcfg.get("models") != preset_models:
                full["providers"][pname]["models"] = list(preset_models)
                changed = True
                logger.info(
                    f"重置 {pname} 模型列表: {len(pcfg.get('models', []))} → {len(preset_models)} (预设)")
        else:
            models = pcfg.get("models")
            if not models:
                continue
            clean = [m for m in models if not any(k in m.lower() for k in _EXCLUDE_MODEL_KEYWORDS)]
            if len(clean) != len(models):
                full["providers"][pname]["models"] = clean
                changed = True
                logger.info(
                    f"清理 {pname} 模型列表: {len(models)} → {len(clean)} (排除 {len(models)-len(clean)} 个非对话模型)")
    if changed:
        _save_llm_config_legacy(full)


def _migrate_keys_from_json():
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
                pcfg.pop("api_key", None)
                migrated += 1
        if migrated > 0:
            _save_llm_config_legacy(full)
            logger.info(f"🔄 数据迁移完成：{migrated} 个提供商的 API Key 已迁移到 SQLite 加密存储")
    except Exception as e:
        logger.error(f"⚠️ 数据迁移失败: {e}")


def _get_current_config() -> dict:
    full = _load_llm_config_legacy()
    cur = full.get("current")
    if cur and cur in full.get("providers", {}):
        cfg = full["providers"][cur].copy()
        cfg.pop("api_key", None)
        return cfg
    return {}


_CONFIG_CARD_MODULES = [
    "chat", "jailbreak", "scoring", "fallback", "reflection", "chunk", "promptEval",
    "embedding", "reranker",
]


def _get_backend_eval_llm():
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


def _generate_eval_summary(eval_llm, tab_type: str, result: dict, items: list, usage_sink=None) -> str:
    prompt = ""
    if tab_type == "retrieval_quality":
        total = result.get("total", 0)
        passed = result.get("pass", 0)
        failed = result.get("fail", 0)
        recall5_avg = result.get("avg_recall_5", result.get("recall_5_avg", 0))
        recall10_avg = result.get("avg_recall_10", result.get("recall_10_avg", 0))
        mrr_avg = result.get("avg_mrr", result.get("mrr_avg", 0))

        result_items = result.get("items", [])
        fail_items = [it for it in result_items if it.get("recall_5") == 0]
        double_fail = [it for it in result_items if it.get(
            "recall_5") == 0 and it.get("recall_10") == 0]
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
### 2. 失败题目深度分析（重点）
### 3. 改进建议"""

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

            result_items = result.get("items", [])
            mode_details = ""
            for mode_name in ["faiss_only", "bm25_only", "hybrid_no_rerank", "hybrid_rerank"]:
                fails = [it for it in result_items if it.get(f"{mode_name}_recall_5") == 0]
                if fails:
                    mode_details += f"\n#### {mode_name} 模式失败（{len(fails)}题）：\n"
                    for it in fails:
                        mrr_val = it.get(f"{mode_name}_mrr", 0)
                        mode_details += f"- 查询「{it['query']}」期望来源「{it.get('expected', '')}」R@5=× MRR={mrr_val}\n"

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
                    all_mode_detail += f"- 查询「{it['query']}」期望来源「{it.get('expected', '')}」\n"

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
### 2. 失败题目深度分析（重点）
### 3. 实施建议"""
        except Exception:
            return ""

    elif tab_type == "e2e_quality":
        total = result.get("total", 0)
        errors = result.get("errors", 0)
        avg_scores = result.get("avg_scores", {})
        scorable = items

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
                low_detail += f"- 「{it['query']}」[{it.get('difficulty', '')}] 得分 {sc*100:.0f}%{trunc_str}\n"

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
### 2. 薄弱环节深度分析（重点）
### 3. 改进建议"""

    if not prompt:
        return ""

    try:
        resp = eval_llm.chat([{"role": "user", "content": prompt}])
        if usage_sink:
            usage_sink(resp, getattr(eval_llm, "model", ""))
        text = resp.get("content", "") if isinstance(resp, dict) else str(resp)
        text = text.strip()
        if text:
            _save_eval_summary(tab_type, text)
        return text
    except Exception as e:
        logger.warning(f"LLM 总结建议失败: {e}")
        return ""


def _save_eval_summary(tab_type: str, summary: str):
    _summary_dir = Path(__file__).parent.parent.parent.parent / "agent_data"
    _summary_path = _summary_dir / "eval_summaries.json"
    try:
        data = {}
        if _summary_path.exists():
            data = json.loads(_summary_path.read_text(encoding="utf-8"))
        data[tab_type] = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "summary": summary}
        _summary_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"保存总结建议失败: {e}")


def _load_eval_summary(tab_type: str) -> dict:
    _summary_dir = Path(__file__).parent.parent.parent.parent / "agent_data"
    _summary_path = _summary_dir / "eval_summaries.json"
    try:
        if _summary_path.exists():
            data = json.loads(_summary_path.read_text(encoding="utf-8"))
            return data.get(tab_type, {})
    except Exception:
        pass
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

# ---- 项目根目录 & 文档处理相关 ----


def _find_project_root() -> Path:
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

_MAX_FILE_SIZE = 50 * 1024 * 1024

_doc_tasks: dict[str, dict] = {}
_doc_tasks_lock = threading.Lock()


def _record_ingestion_stage(task_id: str, file_name: str, stage: str,
                            status: str, error: str = "", metrics: dict | None = None,
                            error_type: str = "") -> None:
    """Persist per-document pipeline state while keeping the fast in-memory task view."""
    with _doc_tasks_lock:
        task = _doc_tasks.get(task_id, {})
        document_id = (task.get("document_map") or {}).get(file_name, "")
    if document_id:
        try:
            agent.memory.update_ingestion_item(task_id, document_id, stage, status, error)
            agent.memory.record_ingestion_stage_event(
                task_id, document_id, stage, status, metrics=metrics,
                error=error, error_type=error_type,
            )
        except Exception as exc:
            logger.warning("入库步骤状态写入失败: %s", exc)


def _cleanup_staging():
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
    task_dir = _UPLOAD_STAGING / task_id
    if task_dir.exists():
        import shutil
        shutil.rmtree(task_dir)
        logger.info(f"🧹 已清理 staging 任务目录: {task_id}")


def _load_source_map() -> dict:
    sm_path = Path(__file__).resolve().parent.parent.parent.parent / \
        "packages" / "preprocessor" / "src" / "source_map.json"
    if sm_path.exists():
        return json.loads(sm_path.read_text(encoding="utf-8"))
    return {}


def _enabled_source_profiles() -> set[str]:
    raw = os.getenv("CYBER_AGENT_SOURCE_PROFILES", "general")
    profiles = {p.strip() for p in raw.split(",") if p.strip()}
    return profiles or {"general"}


def _source_map_entry_enabled(cat_config: dict, enabled_profiles: set[str]) -> bool:
    if "all" in enabled_profiles:
        return True
    profile = str(cat_config.get("profile") or "").strip()
    scope = str(cat_config.get("scope") or "general").strip()
    if profile:
        return profile in enabled_profiles
    return scope == "general" and "general" in enabled_profiles


def _get_source_dirs() -> list[str]:
    dirs = []
    enabled_profiles = _enabled_source_profiles()
    for cat_name, cat_config in _load_source_map().items():
        if not _source_map_entry_enabled(cat_config, enabled_profiles):
            logger.info(f"跳过未启用资料源 profile: {cat_name} ({cat_config.get('profile') or cat_config.get('scope')})")
            continue
        for d in cat_config.get("source_dirs", []):
            if d and os.path.isdir(d):
                dirs.append(d)
    return dirs


_DEDUP = None
_DEDUP_LOCK = threading.Lock()


def _get_dedup() -> Deduplicator:
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
    dedup = _get_dedup()
    result = dedup.check_file("", file_name)
    d = result.to_dict()
    d["in_source"] = bool(result.matched_files)
    d["in_cleaned"] = result.is_duplicate and result.layer == 1
    return d


def _ensure_preprocessor_imports():
    p = str(Path(__file__).resolve().parent.parent.parent.parent /
            "packages" / "preprocessor" / "src")
    if p not in sys.path:
        sys.path.insert(0, p)


def _parse_single_file(file_path: str, task_id: str, skip_layer2: bool = False) -> tuple[str, str] | None:
    try:
        file_path_obj = Path(file_path)
        _ensure_preprocessor_imports()
        from odl_parser import OdlParser

        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["stage"] = "parsing"
                _doc_tasks[task_id]["current_file"] = file_path_obj.name
        _record_ingestion_stage(task_id, file_path_obj.name, "parsing", "processing")

        parser = OdlParser()
        parse_data = parser.parse(str(file_path))
        raw_text = parse_data["full_markdown"]
        parse_metrics = {"file_type": file_path_obj.suffix.lower(),
                         "raw_characters": len(raw_text)}

        if not skip_layer2:
            dedup = _get_dedup()
            dedup2 = dedup.check_text(raw_text, file_path_obj.name)
            if dedup2.is_duplicate:
                logger.warning(f"  ⚠️ Layer 2 检测到文本重复: {file_path_obj.name} ({dedup2.reason})")
                return None

        _record_ingestion_stage(task_id, file_path_obj.name, "parsing", "completed", metrics=parse_metrics)
        return (raw_text, file_path_obj.stem)
    except Exception as e:
        logger.error(f"  ❌ 解析失败 {file_path}: {e}")
        _record_ingestion_stage(task_id, Path(file_path).name, "parsing", "failed", str(e))
        return None


def _clean_and_save(raw_text: str, file_stem: str, category: str, task_id: str, file_meta: dict | None = None) -> str | None:
    try:
        _ensure_preprocessor_imports()
        from llm_cleaner import LlmCleaner

        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["stage"] = "cleaning"
        _record_ingestion_stage(task_id, str((file_meta or {}).get("source_name") or file_stem),
                                "cleaning", "processing")

        cleaner = LlmCleaner(prompt_memory=agent.memory)
        clean_data = cleaner.clean_document(raw_text, file_stem)
        cleaned = clean_data["cleaned_markdown"]
        clean_metrics = {"cleaned_characters": len(cleaned), "output_format": "markdown"}

        cleaned_dir = _PROJECT_ROOT / "RAG_DATA" / "03_cleaned" / category
        cleaned_dir.mkdir(parents=True, exist_ok=True)
        safe_stem = file_stem.replace(" ", "_").replace("-", "_")
        document_id = str((file_meta or {}).get("document_id") or "").strip()
        # Document IDs keep same-named uploads from different workspaces independent.
        md_name = f"{document_id}__{safe_stem}.md" if document_id else f"{safe_stem}.md"
        md_path = cleaned_dir / md_name
        md_path.write_text(cleaned, encoding="utf-8")
        if file_meta:
            meta_path = md_path.with_suffix(".meta.json")
            meta_payload = {
                "file_name": file_stem,
                "category": category,
                "scope": file_meta.get("scope", "general"),
                "profile": file_meta.get("profile", "general"),
                "industry": file_meta.get("industry", ""),
                "profile_confidence": file_meta.get("profile_confidence", 0),
                "profile_reason": file_meta.get("profile_reason", ""),
                "profile_confirmed": bool(file_meta.get("profile_confirmed", False)),
                "profile_source": file_meta.get("profile_source", "manual_confirmed"),
                "document_id": file_meta.get("document_id", ""),
                "visibility": file_meta.get("visibility", "public"),
                "tenant_id": file_meta.get("tenant_id", ""),
                "owner_user_id": file_meta.get("owner_user_id", ""),
                "agent_id": file_meta.get("agent_id", ""),
                "knowledge_base_id": file_meta.get("knowledge_base_id", ""),
                "source_metadata": file_meta.get("source_metadata", {}),
            }
            meta_path.write_text(json.dumps(meta_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            document_id = str(meta_payload.get("document_id") or "")
            if document_id:
                try:
                    agent.memory.mark_document_indexed(document_id, str(md_path))
                    _record_ingestion_stage(task_id, str(file_meta.get("source_name") or file_stem),
                                            "cleaning", "completed", metrics=clean_metrics)
                except Exception as exc:
                    logger.warning("文档登记状态更新失败: %s", exc)
        logger.info(f"  ✅ 已保存: {md_path}")
        return str(md_path)
    except Exception as e:
        logger.error(f"  ❌ 清洗/保存失败 {file_stem}: {e}")
        _record_ingestion_stage(task_id, str((file_meta or {}).get("source_name") or file_stem),
                                "cleaning", "failed", str(e), error_type=type(e).__name__)
        return None


_INDEX_LOCK = threading.Lock()


def _incremental_index(task_id: str, md_paths: list[str]):
    preprocessor_dir = str(Path(__file__).resolve(
    ).parent.parent.parent.parent / "packages" / "preprocessor" / "src")
    script = "incremental_index.py"
    args = [sys.executable, script] + md_paths

    with _doc_tasks_lock:
        if task_id in _doc_tasks:
            _doc_tasks[task_id]["stage"] = "incremental_index"
    logger.info(f"  ▶️ 运行增量索引 ({len(md_paths)} 个文件)...")
    try:
        with _INDEX_LOCK:
            result = subprocess.run(args, cwd=preprocessor_dir,
                                    capture_output=True, text=True, timeout=600)
        for line in result.stdout.split("\n"):
            if line.strip():
                logger.info(f"  {line.strip()}")
        if result.returncode != 0:
            logger.error(f"  ❌ 增量索引失败: {result.stderr[:500]}")
            with _doc_tasks_lock:
                if task_id in _doc_tasks:
                    _doc_tasks[task_id]["error"] = f"增量索引失败: {result.stderr[:200]}"
            return {"ok": False, "files": len(md_paths), "return_code": result.returncode,
                    "stderr_characters": len(result.stderr or "")}
        logger.info(f"  ✅ 增量索引完成")
        agent.refresh_retriever()
        return {"ok": True, "files": len(md_paths), "return_code": result.returncode,
                "stdout_characters": len(result.stdout or "")}
    except subprocess.TimeoutExpired:
        logger.error(f"  ❌ 增量索引超时")
        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["error"] = f"增量索引超时"
        return {"ok": False, "files": len(md_paths), "error": "timeout"}
    except Exception as exc:
        logger.error(f"  ❌ 增量索引异常: {exc}")
        return {"ok": False, "files": len(md_paths), "error": type(exc).__name__}


def _run_processing_task(task_id: str, files: list[dict], category: str):
    try:
        total = len(files)
        success_count = [0]
        fail_count = [0]

        old_stems_to_clear: list[str] = []
        for f in files:
            if f.get("conflict_action") != "overwrite":
                continue
            file_category = f.get("category", category) or category
            dedup = _get_dedup()
            dr = dedup.check_file("", f["name"])
            if dr.is_duplicate and dr.layer == 1:
                for mf in dr.matched_files:
                    old_stem = Path(mf).stem if mf.endswith(".md") else mf.rsplit(".", 1)[0]
                    old_stem_normalized = old_stem.replace(" ", "_").replace("-", "_")
                    cleaned_dir = _PROJECT_ROOT / "RAG_DATA" / "03_cleaned" / file_category
                    for ext in (".md",):
                        old_md = cleaned_dir / f"{old_stem_normalized}{ext}"
                        if old_md.exists():
                            old_md.unlink()
                            old_meta = old_md.with_suffix(".meta.json")
                            if old_meta.exists():
                                old_meta.unlink()
                            logger.warning(f"  🗑️ 删除旧版 .md: {old_md.name}")
                            old_stems_to_clear.append(old_stem_normalized)
                    old_md2 = cleaned_dir / f"{old_stem}.md"
                    if old_md2.exists() and old_stem != old_stem_normalized:
                        old_md2.unlink()
                        old_meta2 = old_md2.with_suffix(".meta.json")
                        if old_meta2.exists():
                            old_meta2.unlink()
                        logger.warning(f"  🗑️ 删除旧版 .md: {old_md2.name}")
                        old_stems_to_clear.append(old_stem)

        if old_stems_to_clear:
            parent_file = _PROJECT_ROOT / "RAG_DATA" / "04_vector_store" / "parent_texts.json"
            if parent_file.exists():
                try:
                    parent_data = json.loads(parent_file.read_text(encoding="utf-8"))
                    before = len(parent_data)
                    keys_to_delete = [k for k in parent_data if any(
                        s in k for s in old_stems_to_clear)]
                    for k in keys_to_delete:
                        del parent_data[k]
                    parent_file.write_text(json.dumps(
                        parent_data, ensure_ascii=False, indent=2), encoding="utf-8")
                    logger.warning(
                        f"  🗑️ 从 parent_texts 清理 {before - len(parent_data)} 条旧条目 (剩余 {len(parent_data)})")
                except Exception as e:
                    logger.error(f"  ❌ 清理 parent_texts 失败: {e}")

            if old_stems_to_clear:
                try:
                    preprocessor_dir = str(Path(__file__).resolve(
                    ).parent.parent.parent.parent / "packages" / "preprocessor" / "src")
                    remove_args = [sys.executable, "incremental_index.py",
                                   "--remove-stems"] + old_stems_to_clear
                    with _INDEX_LOCK:
                        result = subprocess.run(remove_args, cwd=preprocessor_dir,
                                                capture_output=True, text=True, timeout=600)
                    for line in result.stdout.split("\n"):
                        if line.strip():
                            logger.info(f"  {line.strip()}")
                    if result.returncode != 0:
                        logger.error(f"  ❌ 清理 Chroma/FAISS 失败: {result.stderr[:300]}")
                    agent.refresh_retriever()
                except Exception as e:
                    logger.error(f"  ❌ 清理 Chroma/FAISS 异常: {e}")

        _STAGING_SLOTS = 3
        _STAGING_BATCH = 5
        staging_buffers = [[] for _ in range(_STAGING_SLOTS)]
        staging_locks = [threading.Lock() for _ in range(_STAGING_SLOTS)]
        staging_busy = [False] * _STAGING_SLOTS
        active_workers = [0]

        def _slot_worker(slot_idx: int, batch: list):
            try:
                md_paths = []
                indexed_items = []
                for raw_text, file_stem, file_meta in batch:
                    file_category = file_meta.get("category", category) or category
                    logger.info(f"  [Slot {slot_idx}] 开始清洗: {file_stem}")
                    md_path = _clean_and_save(raw_text, file_stem, file_category, task_id, file_meta)
                    if md_path:
                        md_paths.append(md_path)
                        indexed_items.append(file_meta)
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
                    for file_meta in indexed_items:
                        _record_ingestion_stage(
                            task_id, str(file_meta.get("source_name") or ""),
                            "incremental_index", "processing",
                        )
                    index_result = _incremental_index(task_id, md_paths)
                    index_ok = bool(index_result.get("ok"))
                    for file_meta in indexed_items:
                        _record_ingestion_stage(
                            task_id, str(file_meta.get("source_name") or ""),
                            "done" if index_ok else "incremental_index",
                            "completed" if index_ok else "failed",
                            "增量索引失败" if not index_ok else "",
                            metrics=index_result,
                            error_type="IndexProcessError" if not index_ok else "",
                        )
                    if index_ok:
                        # Indexing is the stable point at which graph extraction can
                        # safely read the cleaned document. Candidates remain pending
                        # review and therefore cannot affect answers yet.
                        from knowledge_graph import extract_document_graph
                        for file_meta in indexed_items:
                            knowledge_base_id = str(file_meta.get("knowledge_base_id") or "")
                            document_id = str(file_meta.get("document_id") or "")
                            if not knowledge_base_id or not document_id:
                                continue
                            source_name = str(file_meta.get("source_name") or "")
                            _record_ingestion_stage(
                                task_id, source_name, "graph_extraction", "processing",
                            )
                            try:
                                document = agent.memory.get_document(
                                    document_id, str(file_meta.get("tenant_id") or "local-default"),
                                )
                                if not document:
                                    raise ValueError("图谱抽取文档不存在")
                                graph_run = extract_document_graph(
                                    agent.memory, document,
                                    str(file_meta.get("owner_user_id") or "system"),
                                )
                                _record_ingestion_stage(
                                    task_id, source_name, "graph_extraction", "completed",
                                    metrics={"run_id": graph_run.get("id", ""),
                                             "entity_count": graph_run.get("entity_count", 0),
                                             "relation_count": graph_run.get("relation_count", 0)},
                                )
                            except Exception as exc:
                                # Graph extraction is additive; it must not turn a
                                # successfully indexed document into a failed upload.
                                logger.warning("自动图谱候选抽取失败 %s: %s", source_name, exc)
                                _record_ingestion_stage(
                                    task_id, source_name, "graph_extraction", "failed",
                                    "图谱候选抽取失败", error_type=type(exc).__name__,
                                )
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
                    _record_ingestion_stage(task_id, f.get("name", ""), "parsing", "failed", "解析或去重失败")
                    continue

                raw_text, file_stem = parsed

                added = False
                while not added:
                    for i in range(_STAGING_SLOTS):
                        with staging_locks[i]:
                            full = False
                            if not staging_busy[i] and len(staging_buffers[i]) < _STAGING_BATCH:
                                staging_buffers[i].append((
                                    raw_text,
                                    file_stem,
                                    {
                                        "source_name": f.get("name", ""),
                                        "category": f.get("category", category) or category,
                                        "profile": f.get("profile", "general"),
                                        "scope": f.get("scope", "general"),
                                        "industry": f.get("industry", ""),
                                        "profile_confidence": f.get("profile_confidence", 0),
                                        "profile_reason": f.get("profile_reason", ""),
                                        "profile_confirmed": f.get("profile_confirmed", False),
                                        "profile_source": f.get("profile_source", "manual_confirmed"),
                                        "document_id": f.get("document_id", ""),
                                        "visibility": f.get("visibility", "public"),
                                        "tenant_id": f.get("tenant_id", ""),
                                        "owner_user_id": f.get("owner_user_id", ""),
                                        "agent_id": f.get("agent_id", ""),
                                        "knowledge_base_id": f.get("knowledge_base_id", ""),
                                    },
                                ))
                                full = len(staging_buffers[i]) >= _STAGING_BATCH
                                added = True
                        if full:
                            with staging_locks[i]:
                                staging_busy[i] = True
                                batch = staging_buffers[i]
                                staging_buffers[i] = []
                            active_workers[0] += 1
                            t = threading.Thread(target=_slot_worker, args=(
                                i, batch), daemon=True, name=f"slot-{i}-{task_id}")
                            t.start()
                        if added:
                            break
                    if not added:
                        time.sleep(2)

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
                    t = threading.Thread(target=_slot_worker, args=(i, batch),
                                         daemon=True, name=f"slot-tail-{i}-{task_id}")
                    t.start()

        t1 = threading.Thread(target=producer, daemon=True, name=f"producer-{task_id}")
        t1.start()
        t1.join()

        while active_workers[0] > 0:
            time.sleep(1)

        _clean_staging_task_dir(task_id)

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

        _get_dedup().invalidate_cache()

        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["status"] = "completed"
                _doc_tasks[task_id]["progress"] = 100
                _doc_tasks[task_id]["stage"] = "done"
                _doc_tasks[task_id]["summary"] = {
                    "total": total, "success": success_count[0], "fail": fail_count[0]
                }
        try:
            agent.memory.finish_ingestion_job(
                task_id, "completed", success_count[0], fail_count[0],
            )
            job = agent.memory.get_ingestion_job(task_id) or {}
            if fail_count[0]:
                record_event(
                    agent.memory, job.get("tenant_id") or "local-default", "ingestion_partial_failure", "P2", "ingestion",
                    {"job_id": task_id, "total": total, "success_count": success_count[0], "fail_count": fail_count[0]},
                    "", "入库任务完成但存在失败文件；文件内容和错误正文不写入监控事件。",
                )
            publish_system_event(agent.memory, event_bus, "ingestion.completed", {
                "tenant_id": job.get("tenant_id") or "local-default",
                "user_id": job.get("user_id") or "",
                "agent_id": job.get("agent_id") or "",
                "job_id": task_id, "success_count": success_count[0],
                "fail_count": fail_count[0],
                "notification_body": f"入库任务完成：成功 {success_count[0]} 篇，失败 {fail_count[0]} 篇。",
            })
        except Exception as exc:
            logger.warning("入库任务状态写入失败: %s", exc)
    except Exception as e:
        logger.error(f"处理任务 {task_id} 异常: {e}")
        _clean_staging_task_dir(task_id)
        with _doc_tasks_lock:
            if task_id in _doc_tasks:
                _doc_tasks[task_id]["status"] = "error"
                _doc_tasks[task_id]["error"] = str(e)
        try:
            agent.memory.finish_ingestion_job(
                task_id, "error", success_count[0], fail_count[0], str(e),
            )
            job = agent.memory.get_ingestion_job(task_id) or {}
            record_event(
                agent.memory, job.get("tenant_id") or "local-default", "ingestion_failed", "P1", "ingestion",
                {"job_id": task_id, "success_count": success_count[0], "fail_count": fail_count[0],
                 "error_type": type(e).__name__},
                "", "入库任务整体失败；错误正文不写入监控事件。",
            )
            publish_system_event(agent.memory, event_bus, "ingestion.failed", {
                "tenant_id": job.get("tenant_id") or "local-default",
                "user_id": job.get("user_id") or "",
                "agent_id": job.get("agent_id") or "",
                "job_id": task_id, "error": str(e)[:500],
                "notification_body": f"入库任务失败：{str(e)[:160]}",
            })
        except Exception as exc:
            logger.warning("入库失败任务状态写入失败: %s", exc)


def _build_report_doc(title: str, date_line: str, summary_cards: list, headers: list, rows: list) -> BytesIO:
    doc_obj = docx.Document()
    style = doc_obj.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(10)

    h = doc_obj.add_heading(title, level=1)
    h.alignment = 1

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
    footer_p.alignment = 2
    for r in footer_p.runs:
        r.font.size = Pt(8)
        r.font.color.rgb = RGBColor(0x6E, 0x6E, 0x73)

    buf = BytesIO()
    doc_obj.save(buf)
    buf.seek(0)
    return buf


def _render_trace_report(trace: dict) -> str:
    """将 RAG 全链路 trace 数据渲染为 HTML"""
    ctx = trace.get("context", {}) if isinstance(trace, dict) else {}
    llm_ctx = ctx.get("llm", {}) if isinstance(ctx, dict) else {}
    chat_llm = llm_ctx.get("chat", {}) if isinstance(llm_ctx, dict) else {}
    kb = ctx.get("knowledge_base", {}) if isinstance(ctx, dict) else {}
    context_html = f"""
<p>执行结果: <strong>{trace.get("outcome", "unknown")}</strong></p>
<p>Prompt 版本: <code>{ctx.get("prompt_version", "")}</code></p>
<p>Taxonomy 版本: <code>{ctx.get("taxonomy_version", "")}</code></p>
<p>Chat 模型: <code>{chat_llm.get("provider", "")} / {chat_llm.get("model", "")}</code></p>
<p>知识库快照: cleaned={kb.get("cleaned_docs", 0)}，parent_sections={kb.get("parent_sections", 0)}，FAISS={'✅' if kb.get("faiss_index_exists") else '❌'}，Chroma={'✅' if kb.get("chroma_exists") else '❌'}</p>
"""
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
                top_html += f"<li>{src.get('file_name', '')} — {src.get('section', '')}</li>"
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
{context_html}
<hr style="border:none;border-top:1px solid #ddd;margin:12px 0">
{steps_html}
</div>"""


# ============================================================
# 启动初始化
# ============================================================
import sqlite3 as _sqlite3


def _wal(db_path: str):
    c = _sqlite3.connect(db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _init_on_startup():
    """启动时执行：数据迁移 + 加载 LLM 配置 + 清理脏数据"""

    _db_path = _get_db_path()
    try:
        with _wal(_db_path) as _conn:
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
            _count = _conn.execute("SELECT COUNT(*) FROM llm_configs").fetchone()[0]
            if _count == 0:
                _full = _load_llm_config_legacy()
                _cur = _full.get("current", "")
                _providers = _full.get("providers", {})
                _default_base_url = "https://api.deepseek.com"
                _default_model = "deepseek-chat"

                if _cur and _cur in _providers:
                    _cfg = _providers[_cur]
                    _api_key = _get_llm_key(_cur)
                    _save_llm_config_card("chat", _cur, _cfg.get("model", "deepseek-chat"),
                                          _cfg.get("base_url", _default_base_url), _api_key)
                else:
                    _save_llm_config_card("chat", "DeepSeek", "deepseek-v4-flash",
                                          _default_base_url, _get_llm_key("DeepSeek"))

                _save_llm_config_card("promptEval", "DeepSeek", "deepseek-v4-flash",
                                      _default_base_url, _get_llm_key("DeepSeek"))
                _save_llm_config_card("fallback", "Ollama", "qwen2.5:7b",
                                      "http://localhost:11434", "")
                _save_llm_config_card("chunk", "DeepSeek", "deepseek-chat", _default_base_url, "")
                _save_llm_config_card("jailbreak", "DeepSeek",
                                      "deepseek-chat", _default_base_url, "")
                _save_llm_config_card("scoring", "DeepSeek", "deepseek-chat", _default_base_url, "")
                logger.info("已创建 llm_configs 表并种子化默认卡片配置")
            else:
                logger.debug(f"llm_configs 表已存在，{_count} 张卡片")
    except Exception as _e:
        logger.error(f"初始化 llm_configs 表失败: {_e}")

    _migrate_keys_from_json()

    from prompt_versions import init_versions_db
    _pv_db = _get_db_path()
    init_versions_db(_pv_db)

    chat_cfg = _get_llm_config_card("chat")
    if chat_cfg and chat_cfg.get("model") and chat_cfg.get("base_url"):
        api_key = chat_cfg.get("api_key", "")
        agent.llm.reconfigure(
            base_url=chat_cfg.get("base_url", ""),
            api_key=api_key,
            model=chat_cfg.get("model", ""),
            provider_name=chat_cfg.get("provider", ""),
        )
        logger.info(
            f"已加载 LLM 配置 (chat卡片): {chat_cfg.get('provider', '?')} / {chat_cfg.get('model', '?')}")
    else:
        full = _load_llm_config_legacy()
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
                logger.info(f"已加载 LLM 配置 (旧版): {cur} / {cfg.get('model', '?')}")
            else:
                logger.warning(f"当前提供商 {cur} 在 SQLite 中未找到 API Key，请重新配置")

    _cleanup_provider_models()

    # 加密密钥一致性校验（防密钥丢失导致数据不可读）
    try:
        from llm_config_manager import validate_encryption_consistency
        db_path = _get_db_path()
        validate_encryption_consistency(db_path)
    except RuntimeError:
        raise
    except Exception:
        pass

    migrated = agent.memory.migrate_builtin_suite()
    if migrated:
        logger.info(f"已迁移内置测试集: {migrated} 条")

    _cleanup_staging()

    # Delayed account deletion removes private document namespaces from every
    # retrieval store after the configured retention period.
    try:
        purged = agent.memory.purge_due_deleted_accounts()
        if purged:
            preprocessor_dir = Path(__file__).resolve().parent.parent.parent.parent / "packages" / "preprocessor" / "src"
            document_ids = [document_id for item in purged for document_id in item["document_ids"]]
            if document_ids:
                subprocess.run(
                    [sys.executable, "incremental_index.py", "--remove-document-ids", *document_ids],
                    cwd=preprocessor_dir, capture_output=True, text=True, timeout=600, check=False,
                )
            agent.refresh_retriever()
            logger.info("已完成 %s 个到期账号的数据清理", len(purged))
    except Exception as exc:
        logger.error("到期账号数据清理失败，将在下次启动重试: %s", exc)

    try:
        import sqlite3
        from prompt_versions import get_active_version_name
        from agent import SystemPromptLoader
        db_path = _get_db_path()
        active_v = get_active_version_name(db_path)
        if active_v:
            rows = None
            with _wal(db_path) as conn:
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
