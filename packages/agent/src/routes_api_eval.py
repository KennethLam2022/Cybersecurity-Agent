import json
import asyncio
from fastapi import APIRouter, Body, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, StreamingResponse

from app_state import (
    logger, agent, event_bus,
    _load_llm_config_legacy, _save_llm_config_legacy,
    _get_llm_key, _get_llm_key_mask, _save_llm_key,
    _get_llm_config_card, _save_llm_config_card,
    _save_provider_models, _get_all_llm_configs,
    LLM_PRESETS, _CONFIG_CARD_MODULES, _EXCLUDE_MODEL_KEYWORDS,
    _get_backend_eval_llm, _generate_eval_summary, _load_eval_summary,
    validate_llm_url,
)
import requests as http_requests

router = APIRouter()


# ==================== E2E Eval ====================
@router.get("/api/stats/e2e-eval/items")
async def get_e2e_eval_items():
    try:
        items = agent.memory.get_e2e_eval_items()
        return {"items": items, "total": len(items)}
    except Exception as e:
        logger.error(f"get_e2e_eval_items 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/e2e-eval/items")
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


@router.put("/api/stats/e2e-eval/items")
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


@router.delete("/api/stats/e2e-eval/items/{item_id}")
async def delete_e2e_eval_item(item_id: int):
    try:
        ok = agent.memory.delete_e2e_eval_item(item_id)
        return JSONResponse({"success": ok})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/e2e-eval/items/seed")
async def seed_e2e_eval_items():
    try:
        from eval_e2e import QUESTIONS
        items = [(q["query"], q["domain"], q["difficulty"], q.get("style", "plain"))
                 for q in QUESTIONS]
        count = agent.memory.batch_import_e2e_eval_items(items)
        return {"seeded": count, "success": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/e2e-eval/generate-items")
async def e2e_eval_generate_items(data: dict = None):
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
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            items = json.loads(text.strip())
            if not isinstance(items, list) or len(items) == 0:
                return JSONResponse({"error": "LLM 返回格式异常"}, status_code=500)
        except Exception as e:
            logger.warning(f"E2E LLM 生成测试集失败: {e}")
            return JSONResponse({"error": f"LLM 生成失败: {e}"}, status_code=500)
    else:
        return JSONResponse({"error": "后端评测 LLM 未配置"}, status_code=400)

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


@router.post("/api/stats/e2e-eval/run")
async def e2e_eval_run(data: dict = None):
    try:
        from pathlib import Path as _Path
        from datetime import datetime as _datetime
        from eval_e2e import run_evaluation, generate_html, _save_version, _compute_stats, _EVAL_DIR, _HTML_DIR

        item_limit = None
        if data and isinstance(data, dict) and data.get("limit"):
            item_limit = int(data.get("limit", 0))
            if item_limit <= 0:
                item_limit = None

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

        eval_llm = None
        prompt_eval_cfg = _get_llm_config_card("promptEval")

        if prompt_eval_cfg and prompt_eval_cfg.get("model", "").strip() == "deepseek-chat":
            chat_cfg = _get_llm_config_card("chat")
            if chat_cfg and chat_cfg.get("model"):
                logger.info(
                    "promptEval 卡模型为旧的 deepseek-chat，自动同步为 chat 卡模型: "
                    f"{chat_cfg.get('provider', '?')} / {chat_cfg.get('model', '?')}"
                )
                prompt_eval_cfg["model"] = chat_cfg["model"]
                prompt_eval_cfg["base_url"] = chat_cfg.get("base_url", prompt_eval_cfg["base_url"])
                prompt_eval_cfg["provider"] = chat_cfg.get(
                    "provider", prompt_eval_cfg.get("provider", ""))
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
                logger.info(
                    f"评测 LLM 使用 promptEval 卡片: {prompt_eval_cfg.get('provider', '?')} / {prompt_eval_cfg.get('model', '?')}")
            except Exception as e:
                logger.warning(f"创建评测 LLM 失败: {e}")

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
            answer_llm=eval_llm,
        )

        html_file = _Path(str(_HTML_DIR)) / f"eval_e2e_report_{ts}.html"
        generate_html(results, html_file)

        stats = _compute_stats(results)
        _save_version(output_json, html_file, version_tag, stats)

        try:
            eval_llm = _get_backend_eval_llm()
            if eval_llm:
                _generate_eval_summary(eval_llm, "e2e_quality", stats, results)
        except Exception as e:
            logger.warning(f"生成综合质量分析建议失败: {e}")

        return {
            "ok": True,
            "total": len(results),
            "output_json": str(output_json),
            "html_file": str(html_file),
            "stats": stats,
        }
    except Exception as e:
        logger.error(f"e2e_eval_run 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/stats/e2e-eval/report")
async def e2e_eval_report():
    from eval_e2e import find_latest_results
    _, latest_html = find_latest_results()
    if not latest_html or not latest_html.exists():
        return JSONResponse({"error": "暂无报告，请先运行评测"}, status_code=404)
    return HTMLResponse(latest_html.read_text(encoding="utf-8"), media_type="text/html")


@router.get("/api/stats/e2e-eval/versions")
async def e2e_eval_versions():
    from eval_e2e import _load_versions
    return {"versions": _load_versions()}


@router.get("/api/stats/e2e-eval/latest")
async def e2e_eval_latest():
    from eval_e2e import find_latest_results
    latest_json, _ = find_latest_results()
    if not latest_json or not latest_json.exists():
        return JSONResponse({"error": "暂无评测结果"}, status_code=404)
    return JSONResponse(json.loads(latest_json.read_text(encoding="utf-8")))


@router.get("/api/stats/eval-summary/e2e-quality")
async def get_e2e_quality_summary():
    return _load_eval_summary("e2e_quality")


@router.get("/api/stats/e2e-eval/analysis")
async def get_e2e_eval_analysis():
    from eval_e2e import _load_versions

    versions = _load_versions()
    if not versions:
        return JSONResponse({"summary": "", "time": "", "error": "暂无评测数据"}, status_code=200)

    latest = versions[-1]
    total = latest.get("total", 0)
    errors = latest.get("errors", 0)
    c5_rate = latest.get("c5_truncation_rate", 0)

    def _s(v, k):
        val = v.get("avg_scores", {}).get(k)
        return f"{val*100:.1f}%" if val is not None else "N/A"

    history = versions[-3:] if len(versions) >= 3 else versions
    trend_lines = "\n".join(
        f"- {v.get('version', '?')}: CP={_s(v, 'context_precision')} CR={_s(v, 'context_recall')} "
        f"FT={_s(v, 'faithfulness')} RL={_s(v, 'relevancy')} HC={_s(v, 'hallucination')}"
        for v in history
    )

    prompt = f"""你是一位网络安全 RAG 系统质量分析专家。分析以下综合质量评测结果。

## 最新版本概况
- 版本：{latest.get('version', '?')}（{latest.get('timestamp', '')[:19] if latest.get('timestamp') else '?'}）
- 总题数：{total} | 运行错误：{errors}
- C5 截断影响率：{c5_rate}%

## 各指标得分
- Context Precision（CP，检索精度）：{_s(latest, 'context_precision')}
- Context Recall（CR，检索召回）：{_s(latest, 'context_recall')}
- Faithfulness（FT，回答忠实度）：{_s(latest, 'faithfulness')}
- Relevancy（RL，回答相关性）：{_s(latest, 'relevancy')}
- Hallucination（HC，幻觉率，越低越好）：{_s(latest, 'hallucination')}

## 趋势变化（最近 {"3" if len(history) == 3 else len(history)} 次评测）
{trend_lines}

## 目标阈值参考
- CP ≥ 80%，CR ≥ 70%，FT ≥ 85%，RL ≥ 80%，HC ≤ 10%

请输出以下结构分析报告（500字以内，用中文）：

### 1. 整体评估
### 2. 短板指标深度分析
### 3. 趋势判断
### 4. 优先改进建议"""

    eval_llm = _get_backend_eval_llm()
    if not eval_llm:
        return JSONResponse({"summary": "", "time": latest.get('timestamp', '')[:19] if latest.get('timestamp') else "", "error": "后端评测 LLM 未配置"}, status_code=200)

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


# ==================== LLM 配置 ====================
@router.get("/api/llm/presets")
async def llm_presets():
    return {"presets": LLM_PRESETS}


@router.get("/api/llm/config")
async def llm_get_config():
    full = _load_llm_config_legacy()
    cur = full.get("current")
    providers = full.get("providers", {})

    cfg = providers.get(cur, {}).copy() if cur else {}
    cfg.pop("api_key", None)
    cfg.setdefault("base_url", agent.llm.base_url)
    cfg.setdefault("model", agent.llm.model)

    if cur:
        key_mask = _get_llm_key_mask(cur)
        if key_mask:
            cfg["api_key_mask"] = key_mask
        else:
            cfg["api_key_mask"] = "未配置"

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
        clean_cfg = {k: v for k, v in pcfg.items() if k != "api_key"}
        clean_cfg["api_key_mask"] = key_mask if key_mask else "未配置"
        providers_config[pname] = clean_cfg

    return {
        "config": cfg,
        "current": cur,
        "providers": provider_list,
        "providers_config": providers_config,
    }


@router.post("/api/llm/config")
async def llm_save_config(data: dict = Body(...)):
    provider = data.get("provider", "自定义")
    base_url = data.get("base_url", "").rstrip("/")
    api_key = data.get("api_key", "")
    model = data.get("model", "")

    if not base_url or not model:
        return JSONResponse({"ok": False, "error": "base_url 和 model 不能为空"}, status_code=400)

    full = _load_llm_config_legacy()
    full.setdefault("providers", {})
    existing = full["providers"].get(provider, {})

    cfg = {
        "base_url": base_url,
        "model": model,
    }
    if existing.get("models"):
        cfg["models"] = existing["models"]

    full["providers"][provider] = cfg
    full["current"] = provider
    _save_llm_config_legacy(full)

    if api_key:
        _save_llm_key(provider, api_key)
    else:
        existing_key = _get_llm_key(provider)
        if existing_key:
            api_key = existing_key
        else:
            api_key = agent.llm.api_key if hasattr(agent.llm, 'api_key') else ''
    agent.llm.reconfigure(
        base_url=base_url,
        api_key=api_key,
        model=model,
        provider_name=provider,
    )

    _save_llm_config_card("chat", provider, model, base_url, api_key)

    asyncio.create_task(event_bus.publish("config_update", {
        "source": "frontend",
        "provider": provider,
        "model": model,
    }))

    return {"status": "saved", "message": f"{provider} 配置已保存并生效"}


@router.get("/api/llm/config/current")
async def llm_get_current_config():
    info = agent.llm.get_current_provider() if hasattr(agent.llm, "get_current_provider") else {}
    return {
        "provider": info.get("name", ""),
        "model": info.get("model", ""),
        "base_url": info.get("base_url", ""),
    }


@router.post("/api/llm/test")
async def llm_test_connection(data: dict = Body(...)):
    from llm_provider import LLMProvider

    base_url = data.get("base_url", "")
    api_key = data.get("api_key", "")
    model = data.get("model", "")

    if not api_key and base_url:
        for pname, pcfg in _load_llm_config_legacy().get("providers", {}).items():
            if pcfg.get("base_url", "").rstrip("/") == base_url.rstrip("/"):
                stored_key = _get_llm_key(pname)
                if stored_key:
                    api_key = stored_key
                    break

    if not api_key:
        is_local = any(host in base_url for host in ["localhost", "127.0.0.1", "0.0.0.0"])
        if not is_local:
            return JSONResponse({"ok": False, "error": "未提供 API Key 且未找到已存储的 Key"}, status_code=400)

    if base_url and not validate_llm_url(base_url):
        return JSONResponse({"ok": False, "error": f"不允许的 LLM API 域名: {base_url}"}, status_code=400)

    temp = LLMProvider()
    result = temp.test_connection(
        base_url=base_url,
        api_key=api_key,
        model=model,
    )
    return result


@router.post("/api/llm/refresh-models")
async def llm_refresh_models(data: dict = Body(...)):
    provider = data.get("provider", "硅基流动")
    api_key = data.get("api_key", "")

    if not api_key:
        stored_key = _get_llm_key(provider)
        if stored_key:
            api_key = stored_key

    if not api_key:
        return JSONResponse({"ok": False, "models": [], "message": "请先配置该提供商的 API Key"}, status_code=400)

    if provider == "OpenRouter":
        try:
            r = http_requests.get("https://openrouter.ai/api/v1/models", timeout=30)
            if r.status_code != 200:
                return {"ok": False, "models": [], "message": f"HTTP {r.status_code}"}
            data = r.json()
            all_models = [m["id"]
                          for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
            exclude_modalities = ["image", "audio", "video", "embedding", "rerank"]
            exclude_all = set(exclude_modalities + _EXCLUDE_MODEL_KEYWORDS)
            chat_models = [m for m in all_models if not any(x in m.lower() for x in exclude_all)]
            chat_models.sort()
            _save_provider_models(provider, chat_models)
            return {"ok": True, "models": chat_models, "count": len(chat_models), "total": len(all_models)}
        except Exception as e:
            return {"ok": False, "models": [], "message": str(e)[:100]}

    if provider == "阿里云百炼":
        preset_models = LLM_PRESETS.get("阿里云百炼", {}).get("models", [])
        _save_provider_models(provider, preset_models)
        return {"ok": True, "models": preset_models, "count": len(preset_models)}

    if not api_key:
        return {"ok": False, "models": [], "message": "请先填写 API Key"}
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        r = http_requests.get("https://api.siliconflow.cn/v1/models", headers=headers, timeout=30)
        if r.status_code != 200:
            return {"ok": False, "models": [], "message": f"HTTP {r.status_code}"}
        data = r.json()
        all_models = [m["id"] for m in data.get("data", []) if isinstance(m, dict) and "id" in m]
        channel_models = [m for m in all_models if not any(
            k in m.lower() for k in _EXCLUDE_MODEL_KEYWORDS)]
        channel_models.sort()
        _save_provider_models(provider, channel_models)
        return {"ok": True, "models": channel_models, "count": len(channel_models), "total": len(all_models)}
    except Exception as e:
        return {"ok": False, "models": [], "message": str(e)[:100]}


# ==================== LLM 配置卡片 API ====================
@router.get("/api/llm/configs")
async def llm_configs_get_all():
    configs = _get_all_llm_configs()
    return {"configs": configs}


@router.post("/api/llm/configs/save")
async def llm_configs_save_one(data: dict = Body(...)):
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

    if module_id == "chat":
        agent.llm.reconfigure(
            base_url=base_url,
            api_key=api_key,
            model=model,
            provider_name=provider,
        )
        logger.info(f"♻️ chat 配置热更新: {provider} / {model}")

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


@router.post("/api/llm/configs/test")
async def llm_configs_test(data: dict = Body(...)):
    module_id = data.get("module_id", "").strip()
    base_url = data.get("base_url", "").strip()
    api_key = data.get("api_key", "").strip()
    model = data.get("model", "").strip()

    if not base_url:
        return JSONResponse({"ok": False, "error": "base_url 不能为空"}, status_code=400)
    if not model:
        return JSONResponse({"ok": False, "error": "model 不能为空"}, status_code=400)

    if not api_key and module_id:
        saved = _get_llm_config_card(module_id)
        if saved and saved.get("api_key"):
            api_key = saved["api_key"]

    if not api_key:
        is_local = any(host in base_url.lower() for host in ["localhost", "127.0.0.1", "0.0.0.0"])
        if not is_local:
            return JSONResponse({"ok": False, "error": "未提供 API Key 且未找到已存储的 Key"}, status_code=400)

    if not validate_llm_url(base_url):
        return JSONResponse({"ok": False, "error": f"不允许的 LLM API 域名: {base_url}"}, status_code=400)

    base_url = base_url.rstrip("/")
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    import time as _time
    t0 = _time.time()
    import requests as _requests

    if module_id == "embedding":
        try:
            payload = {"model": model, "input": "测试连接", "encoding_format": "float"}
            resp = _requests.post(f"{base_url}/embeddings", headers=headers,
                                  json=payload, timeout=(10, 30))
            resp.raise_for_status()
            rdata = resp.json()
            dim = len(rdata["data"][0]["embedding"]) if rdata.get("data") else 0
            elapsed = _time.time() - t0
            return {"ok": True, "time_s": round(elapsed, 2), "dimension": dim, "message": f"Embedding 连接成功，维度={dim}"}
        except Exception as e:
            elapsed = _time.time() - t0
            return {"ok": False, "time_s": round(elapsed, 2), "error": str(e)[:200]}

    try:
        payload = {"model": model, "messages": [{"role": "user", "content": "Hi"}], "max_tokens": 5}
        resp = _requests.post(f"{base_url}/chat/completions",
                              headers=headers, json=payload, timeout=(10, 30))
        resp.raise_for_status()
        rdata = resp.json()
        content = rdata["choices"][0]["message"]["content"] if rdata.get("choices") else ""
        elapsed = _time.time() - t0
        return {"ok": True, "time_s": round(elapsed, 2), "reply": content[:50], "message": "连接成功"}
    except Exception as e:
        elapsed = _time.time() - t0
        return {"ok": False, "time_s": round(elapsed, 2), "error": str(e)[:200]}
