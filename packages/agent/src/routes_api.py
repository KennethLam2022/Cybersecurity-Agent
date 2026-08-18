import os
import json
import time
import hashlib
import base64
import urllib.parse
import uuid
import threading
from datetime import datetime
from pathlib import Path
from typing import AsyncGenerator, Optional
from fastapi import APIRouter, Request, Body, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
import asyncio
import sqlite3
import html

# ---- WAL 连接辅助 (防并发锁) ----
def _db():
    c = sqlite3.connect(agent.memory._db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _publish_event(bus, event_type: str, data: dict) -> None:
    """在线程池/同步上下文中安全发布事件"""
    try:
        asyncio.get_running_loop()
        asyncio.create_task(bus.publish(event_type, data))
    except RuntimeError:
        asyncio.run(bus.publish(event_type, data))

from app_state import (
    logger, _START_TIME, _EXCLUDE_MODEL_KEYWORDS,
    agent, event_bus, _ACTIVE_CONVERSATIONS, jinja_env,
    _get_llm_key, _get_llm_key_mask, _save_llm_key,
    _get_llm_config_card, _save_llm_config_card,
    LLM_PRESETS, _CONFIG_CARD_MODULES,
    _get_backend_eval_llm, _generate_eval_summary, _load_eval_summary,
    _get_dedup,
    _UPLOAD_STAGING, _MAX_FILE_SIZE, _doc_tasks, _doc_tasks_lock,
    _run_processing_task, _cleanup_staging,
    _build_report_doc, _render_trace_report,
)
from profile_classifier import profile_options, suggest_document_profile
from agent import SystemPromptLoader

router = APIRouter()


# ==================== 首页 ====================
@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    template = jinja_env.get_template("index.html")
    content = template.render({"request": request})
    return HTMLResponse(content)


# ==================== 对话管理 ====================
@router.get("/api/conversations")
def list_conversations(include_deleted: bool = False, include_test: bool = False, jailbreak: str = "all"):
    convs = agent.memory.get_conversations(
        include_deleted=include_deleted, include_test=include_test, jailbreak=jailbreak)
    return JSONResponse(convs)


@router.post("/api/conversations")
def new_conversation():
    conv = agent.memory.create_conversation()
    return JSONResponse(conv)


@router.put("/api/conversations/{conv_id}")
def rename_conversation(conv_id: str, data: dict = Body(...)):
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    agent.memory.update_title(conv_id, title)
    return JSONResponse({"ok": True, "title": title})


@router.put("/api/conversations/{conv_id}/jailbreak-status")
def set_jailbreak_status(conv_id: str, data: dict = Body(...)):
    status = data.get("status", "").strip()
    if status not in ("false_alarm", "handled"):
        raise HTTPException(status_code=400, detail="无效状态，仅支持 false_alarm 或 handled")
    agent.memory.update_jailbreak_status(conv_id, status)
    return JSONResponse({"ok": True, "status": status})


@router.get("/api/conversations/{conv_id}/jailbreak-report")
def jailbreak_report(conv_id: str):
    data = agent.memory.get_jailbreak_report_data(conv_id)
    if not data:
        raise HTTPException(status_code=404, detail="对话不存在")

    filename = f"越狱报告_{conv_id}.doc"

    reason = data.get("jailbreak_reason") or "无"
    jb_status_labels = {"pending": "待处理", "downloaded": "已下载",
                        "false_alarm": "误报", "handled": "已处理"}
    jb_label = jb_status_labels.get(data.get("jailbreak_status"), "未知")

    msgs_html = ""
    jailbreak_msg_id = data.get("jailbreak_message_id")
    if jailbreak_msg_id:
        jb_idx = -1
        for i, m in enumerate(data.get("messages", [])):
            if m.get("id") == jailbreak_msg_id:
                jb_idx = i
                break
        if jb_idx >= 0:
            start = max(0, jb_idx - 3)
            trigger_msgs = data["messages"][start:jb_idx+2]
        else:
            trigger_msgs = data.get("messages", [])[-4:]
    else:
        trigger_msgs = data.get("messages", [])[-4:]

    for m in trigger_msgs:
        role_label = "👤 用户" if m["role"] == "user" else "🤖 助手"
        content = html.escape(m.get("content", "")[:500])
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

    msgs = data.get("messages", [])
    rounds_html = ""
    for log in data.get("usage_logs", []):
        if not log.get("answer_jailbreak") and not log.get("off_topic"):
            continue
        asst_id = log["message_id"]
        asst_idx = None
        for idx, m in enumerate(msgs):
            if m.get("id") == asst_id:
                asst_idx = idx
                break
        if asst_idx is None:
            continue
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

        retrieval_detail = f"FAISS={log.get('faiss_count', 0)}条 BM25={log.get('bm25_count', 0)}条 → 最终={log.get('returned_count', 0)}条" if log else "无数据"
        if log and log.get("chroma_count"):
            retrieval_detail += f" Chroma={log['chroma_count']}条"
        search_t = round(log.get("faiss_time", 0) + log.get("chroma_time", 0) +
                         log.get("rerank_time", 0), 2) if log else 0
        rewrite_t = log.get("rewrite_time", 0) if log else 0
        llm_t = log.get("llm_time", 0) if log else 0
        total_t = log.get("total_time", 0) if log else 0
        docs = log.get("documents") or [] if log else []
        top_docs = "".join(
            f"<li>{d.get('file_name', '')[:40]} — {d.get('section', '')[:20]}</li>" for d in docs[:3])

        trace_steps_html = ""
        if log and log.get("trace") and log["trace"].get("steps"):
            for st in log["trace"]["steps"]:
                trace_steps_html += f"<li><strong>{st['step']}</strong>: {json.dumps({k: v for k, v in st.items() if k != 'step'}, ensure_ascii=False)[:100]}</li>"

        rounds_html += f"""<div style="background:#f9f9f9;border-radius:8px;padding:12px;margin-bottom:12px;{'border-left:4px solid #ff3b30' if jb_flag else 'border-left:4px solid #3498db'}">
    <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <strong>🔴 越狱检测轮次 {jb_flag} {ot_flag}</strong>
        <span style="font-size:12px;color:#999">总耗时 {total_t}s</span>
    </div>
    <p style="font-size:12px;color:#333;margin:4px 0">用户: {user_q}</p>
    <table style="border-collapse:collapse;width:100%;font-size:12px;margin-top:6px">
        <tr><td style="padding:2px 6px;width:80px">Query 改写</td><td style="padding:2px 6px">{rewrite_t}s</td><td style="padding:2px 6px;width:80px">检索</td><td style="padding:2px 6px">{search_t}s ({retrieval_detail})</td></tr>
        <tr><td style="padding:2px 6px">LLM 生成</td><td style="padding:2px 6px">{llm_t}s</td><td style="padding:2px 6px">Token</td><td style="padding:2px 6px">prompt={log.get('prompt_tokens', 0) if log else 0} / completion={log.get('completion_tokens', 0) if log else 0}</td></tr>
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
    <tr><td style="padding:6px;font-weight:600;width:100px">对话标题</td><td style="padding:6px">{data.get("title", "")}</td></tr>
    <tr><td style="padding:6px;font-weight:600">对话ID</td><td style="padding:6px">{data.get("id", "")}</td></tr>
    <tr><td style="padding:6px;font-weight:600">越狱原因</td><td style="padding:6px;color:#ff3b30">{reason}</td></tr>
    <tr><td style="padding:6px;font-weight:600">当前状态</td><td style="padding:6px">{jb_label}</td></tr>
    <tr><td style="padding:6px;font-weight:600">触发消息ID</td><td style="padding:6px">{jailbreak_msg_id or "未知"}</td></tr>
    <tr><td style="padding:6px;font-weight:600">总对话轮次</td><td style="padding:6px">{data.get("stats", {}).get("rounds", "")} 轮</td></tr>
</table>

<h2>🔍 越狱触发对话（标红行为越狱消息）</h2>
<table style="border-collapse:collapse;width:100%">
    <tr style="background:#f5f5f5">
        <th style="border:1px solid #ddd;padding:8px;text-align:left">角色</th>
        <th style="border:1px solid #ddd;padding:8px;text-align:left">内容</th>
        <th style="border:1px solid #ddd;padding:8px;text-align:left">评分</th>
    </tr>
    {msgs_html}
    {f'<tr><td colspan="3" style="color:#ff3b30;font-size:13px;padding:8px;text-align:center">⬆️ 标红行为触发越狱的消息</td></tr>' if len(
        trigger_msgs) > 0 else ''}
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


@router.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str):
    agent.memory.delete_conversation(conv_id)
    return JSONResponse({"ok": True, "soft_delete": True})


@router.delete("/api/conversations/{conv_id}/hard")
def hard_delete_conversation(conv_id: str):
    agent.memory.hard_delete_conversation(conv_id)
    return JSONResponse({"ok": True})


@router.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: str):
    return JSONResponse(agent.memory.get_history(conv_id))


@router.get("/api/conversations/detail")
def get_conversation_detail(conv_id: str):
    """删除conversation记录"""
    detail = agent.memory.get_conversation_detail(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    return JSONResponse(detail)


@router.get("/api/conversations/stats")
def get_conversation_stats(conv_id: str):
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


# ==================== Admin ====================
@router.post("/api/admin/cleanup")
def admin_cleanup():
    _cleanup_staging()
    return JSONResponse({"status": "ok", "message": "环境已清理"})


@router.get("/api/admin/stream")
async def admin_event_stream():
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


# ==================== 文档处理 ====================
@router.post("/api/documents/scan")
def documents_scan(files: list[UploadFile] = File(...)):
    dedup = _get_dedup()
    results = []
    # 文件扩展名白名单
    _ALLOWED_EXTENSIONS = {'.pdf', '.docx', '.doc', '.xlsx', '.xls', '.txt', '.md', '.pptx', '.ppt', '.csv'}
    for f in files:
        # 校验文件扩展名
        if f.filename:
            ext = os.path.splitext(f.filename)[1].lower()
            if ext not in _ALLOWED_EXTENSIONS:
                results.append({
                    "name": f.filename,
                    "size": 0,
                    "checksum": "",
                    "duplicate": True,
                    "reason": f"不支持的文件类型（{ext}），仅支持 {', '.join(sorted(_ALLOWED_EXTENSIONS))}",
                    "dedup_layer": 0,
                    "standard_id": None,
                    "incoming_year": None,
                    "existing_year": None,
                    "matched_files": [],
                    "in_source": False,
                    "in_cleaned": False,
                })
                continue

        file_bytes = f.file.read()
        checksum = hashlib.md5(file_bytes).hexdigest()

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

        dr = dedup.check_file("", f.filename)
        dup_info = dr.to_dict()
        dup_info["in_source"] = dr.is_duplicate and dr.layer == 1
        dup_info["in_cleaned"] = dr.is_duplicate and dr.layer == 1
        profile_suggestion = suggest_document_profile(
            filename=f.filename,
            category_hint="通用",
        )
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
            "profile_suggestion": profile_suggestion,
        })

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


@router.get("/api/documents/profile-registry")
def documents_profile_registry():
    return JSONResponse({"status": "ok", "profiles": profile_options()})


@router.post("/api/documents/start-processing")
def documents_start(data: dict = Body(...)):
    files = data.get("files", [])
    category = data.get("category", "通用")
    conflict_actions = data.get("conflict_actions", {})
    if not files:
        return JSONResponse({"status": "error", "message": "没有文件"})

    task_id = uuid.uuid4().hex[:12]
    staging_files = []
    profiles_by_id = {p["profile"]: p for p in profile_options()}
    allowed_profiles = set(profiles_by_id)

    for f in files:
        name = f.get("name", "")
        action = conflict_actions.get(name, "overwrite")
        if action == "skip":
            continue
        content_b64 = f.get("content", "")
        if not content_b64:
            continue
        file_bytes = base64.b64decode(content_b64)
        if len(file_bytes) > _MAX_FILE_SIZE:
            logger.warning(f"  ⏭️ 跳过超大文件: {name} ({len(file_bytes)/1024/1024:.1f}MB)")
            continue
        profile_suggestion = f.get("profile_suggestion") or suggest_document_profile(
            filename=name,
            category_hint=f.get("category", category),
        )
        selected_profile = f.get("profile") or profile_suggestion.get("profile", "general")
        if selected_profile not in allowed_profiles:
            return JSONResponse({"status": "error", "message": f"资料归属 profile 无效: {selected_profile}"})
        if not bool(f.get("profile_confirmed", False)):
            return JSONResponse({"status": "error", "message": f"文件未确认资料归属: {name}"})
        profile_config = profiles_by_id.get(selected_profile, {})
        final_category = str(
            f.get("category")
            or profile_config.get("category")
            or profile_suggestion.get("category")
            or category
            or "通用"
        ).strip()
        file_dir = _UPLOAD_STAGING / task_id
        file_dir.mkdir(parents=True, exist_ok=True)
        file_path = file_dir / name
        file_path.write_bytes(file_bytes)
        staging_files.append({
            "name": name,
            "path": str(file_path),
            "conflict_action": action,
            "profile": selected_profile,
            "scope": profile_config.get("scope") or f.get("scope") or profile_suggestion.get("scope", "general"),
            "industry": profile_config.get("industry") or f.get("industry") or profile_suggestion.get("industry", ""),
            "category": final_category,
            "profile_confidence": f.get("profile_confidence") or profile_suggestion.get("confidence", 0),
            "profile_reason": f.get("profile_reason") or profile_suggestion.get("reason", ""),
            "profile_confirmed": True,
            "profile_source": "manual_confirmed",
        })

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

    t = threading.Thread(
        target=_run_processing_task,
        args=(task_id, staging_files, category),
        daemon=True,
    )
    t.start()

    return JSONResponse({"status": "ok", "task_id": task_id, "files": len(staging_files)})


@router.get("/api/documents/status/{task_id}")
def documents_status(task_id: str):
    with _doc_tasks_lock:
        task = _doc_tasks.get(task_id)
    if not task:
        return JSONResponse({"status": "unknown", "progress": 0})
    return JSONResponse(task)


@router.get("/api/documents/debug-dedup")
def debug_dedup():
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

        test_file_path = dedup._rag_data / "03_cleaned" / "测试入库"
        if test_file_path.is_dir():
            test_files = []
            for f in test_file_path.iterdir():
                norm = normalize_stem(f.stem)
                test_files.append({"name": f.name, "normalized": norm})
            info["test_dir_files"] = test_files

        existing = dedup._collect_existing_files()
        info["existing_keys_count"] = len(existing)

        matching_keys = [k for k in existing if "信息安全风险" in k]
        info["security_risk_keys"] = matching_keys

        result = dedup.check_file("", test_name)
        info["check_result"] = result.to_dict()
    except Exception as e:
        info["error"] = str(e)
        info["traceback"] = traceback.format_exc()
    return JSONResponse(info)


# ==================== 对话/聊天 ====================
@router.post("/api/chat")
def chat(data: dict = Body(...)):
    query = data.get("query", "").strip()
    conv_id = data.get("conversation_id")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    # 在线程池中运行同步 agent.ask()，避免阻塞事件循环
    result = agent.ask(query, conv_id, 0.1, "user")
    _publish_event(event_bus, "conversation_updated", {
        "conv_id": conv_id or result.get("conversation_id", ""),
        "action": "chat",
    })
    return JSONResponse(result)


@router.post("/api/chat/stream")
def chat_stream(data: dict = Body(...)):
    """LLM 聊天接口"""
    query = data.get("query", "").strip()
    conv_id = data.get("conversation_id")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)

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
    _publish_event(event_bus, "active_updated", {
        "conv_id": conv_id,
        "title": title,
        "stage": "retrieving",
    })

    async def event_generator() -> AsyncGenerator[bytes, None]:
        try:
            async for event in agent.ask_stream(query=query, conversation_id=conv_id):
                etype = event.get("type", "")
                if etype == "status":
                    stage = event.get("stage", "")
                    if conv_id and conv_id in _ACTIVE_CONVERSATIONS:
                        _ACTIVE_CONVERSATIONS[conv_id]["stage"] = stage
                        _publish_event(event_bus, "active_updated", {
                            "conv_id": conv_id,
                            "stage": stage,
                        })
                elif etype == "done":
                    real_conv_id = event.get("conversation_id", conv_id)
                    if real_conv_id:
                        if real_conv_id in _ACTIVE_CONVERSATIONS:
                            del _ACTIVE_CONVERSATIONS[real_conv_id]
                            _publish_event(event_bus, "active_removed", {
                                "conv_id": real_conv_id,
                            })
                        _publish_event(event_bus, "conversation_updated", {
                            "conv_id": real_conv_id,
                            "action": "done",
                        })
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


# ==================== 统计 & 评分 ====================
@router.get("/api/stats")
def stats():
    return JSONResponse(agent.stats())


@router.post("/api/rating")
def submit_rating(data: dict = Body(...)):
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


@router.post("/api/rating/semantic-fallback")
def semantic_fallback_rating(data: dict = Body(...)):
    message_id = data.get("message_id")
    if not message_id:
        return JSONResponse({"ok": False, "error": "缺少 message_id"}, status_code=400)

    def _run():
        try:
            agent.infer_semantic_rating_for_message(int(message_id))
        except Exception as e:
            logger.warning(f"后台语义评分失败: message_id={message_id}, error={e}")

    threading.Thread(target=_run, daemon=True).start()
    return {"ok": True, "queued": True}


@router.get("/api/stats/drill-down")
def drill_down(type: str, key: str, limit: int = 50, category: str = "all"):
    try:
        results = agent.memory.drill_down(type, key, limit, category=category)
        return JSONResponse(results)
    except Exception as e:
        logger.error(f"钻取查询失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


# ==================== Prompt 版本管理 ====================


@router.get("/api/prompt/versions")
def prompt_versions_list():
    """列出所有 Prompt 版本"""
    from prompt_versions import list_versions
    versions = list_versions(agent.memory._db_path)
    return {"versions": versions}


@router.post("/api/prompt/versions")
def prompt_version_create(data: dict = Body(...)):
    """创建新版本"""
    name = data.get("name", "")
    description = data.get("description", "")
    system_prompt = data.get("system_prompt", "")
    if not name or not system_prompt:
        return JSONResponse({"ok": False, "message": "名称和 Prompt 内容不能为空"}, status_code=400)
    result = agent.memory.create_prompt_version(
        name=name, description=description, system_prompt=system_prompt,
        changed_by=data.get("changed_by", "管理员"), change_log=data.get("change_log", ""),
        prompt_diff=data.get("prompt_diff", ""),
    )
    return JSONResponse(result)


@router.get("/api/prompt/versions/{version_id}")
def prompt_version_detail(version_id: int):
    """获取版本详情"""
    from prompt_versions import get_version_prompt
    detail = get_version_prompt(version_id, agent.memory._db_path)
    if not detail:
        return JSONResponse({"error": "版本不存在"}, status_code=404)
    return JSONResponse(detail)


@router.get("/api/prompt/versions/{version_id}/results")
def prompt_version_results(version_id: int, limit: int = 50):
    """获取版本跑分结果"""
    from prompt_versions import get_version_results
    results = get_version_results(version_id, agent.memory._db_path, limit)
    return JSONResponse(results)


@router.put("/api/prompt/versions/{version_id}/activate")
def prompt_version_activate(version_id: int):
    """按 ID 激活版本"""
    conn = _db()
    conn.execute("UPDATE prompt_versions SET is_active = 0")
    conn.execute("UPDATE prompt_versions SET is_active = 1 WHERE id = ?", (version_id,))
    row = conn.execute(
        "SELECT version_name, system_prompt FROM prompt_versions WHERE id = ?", (version_id,)
    ).fetchone()
    conn.commit()
    conn.close()
    if not row:
        return JSONResponse({"error": "版本不存在"}, status_code=404)
    return {"ok": True, "version_name": row[0], "system_prompt": row[1]}


@router.post("/api/prompt/versions/switch")
def prompt_version_switch(data: dict = Body(...)):
    """按名称切换激活版本"""
    from prompt_versions import switch_version
    version_name = data.get("version_name", "")
    if not version_name:
        return JSONResponse({"ok": False, "message": "version_name 不能为空"}, status_code=400)
    result = switch_version(version_name, agent.memory._db_path)
    return JSONResponse(result)


@router.post("/api/prompt/versions/restore")
def prompt_version_restore(data: dict = Body(...)):
    """还原版本并更新 active_prompt.txt"""
    version_name = data.get("version_name", "")
    if not version_name:
        return JSONResponse({"ok": False, "error": "version_name 不能为空"}, status_code=400)
    result = agent.memory.restore_prompt_version(version_name)
    if result is None:
        return JSONResponse({"ok": False, "error": "版本不存在"}, status_code=404)
    return {"ok": True, "system_prompt": result}


@router.get("/api/prompt/system-prompt")
def prompt_system_prompt():
    """获取当前 active_prompt.txt 内容"""
    prompt = SystemPromptLoader.get()
    return {"system_prompt": prompt}


# ==================== Prompt 测试集 ====================


@router.get("/api/prompt/test/items")
def prompt_test_items(set_id: str = "builtin"):
    """获取测试集题目（builtin 为空时自动降级到第一个有数据的集）"""
    items = agent.memory.get_test_items(set_id)
    if not items and set_id == "builtin":
        # builtin 为空，直接查 DB 找第一个有数据的 set_id
        conn = _db()
        sid_row = conn.execute(
            "SELECT set_id FROM prompt_test_items WHERE is_active=1 GROUP BY set_id ORDER BY MAX(id) DESC LIMIT 1"
        ).fetchone()
        conn.close()
        if sid_row:
            items = agent.memory.get_test_items(sid_row[0])
    return {"items": items, "total": len(items), "set_id": set_id}


@router.get("/api/prompt/test/suite")
def prompt_test_suite():
    """获取所有测试集"""
    sets = agent.memory.get_all_test_sets()
    return {"suites": sets}


@router.post("/api/prompt/test/generate")
def prompt_test_generate(data: dict = Body(...)):
    """AI 生成测试集"""
    keywords = data.get("keywords", "")
    if not keywords:
        return JSONResponse({"ok": False, "error": "keywords 不能为空"}, status_code=400)
    llm = _get_backend_eval_llm()
    from prompt_test_manager import generate_test_set
    items = generate_test_set(keywords, llm=llm)
    if not items:
        return JSONResponse({"ok": False, "error": "生成失败"}, status_code=500)
    set_id = agent.memory.save_ai_test_set(keywords, items)
    return {"ok": True, "set_id": set_id, "items": items}


@router.get("/api/prompt/test/latest")
def prompt_test_latest():
    """获取最新测试结果"""
    try:
        from prompt_tester import get_latest_full_result
        result = get_latest_full_result(agent.memory._db_path)
        return {"results": [result] if result else []}
    except Exception:
        return {"results": []}


@router.get("/api/prompt/test/history")
def prompt_test_history(limit: int = 20):
    """获取历史测试记录"""
    try:
        from prompt_tester import get_test_history
        rows = get_test_history(agent.memory._db_path, limit=limit)
        return {"history": rows or []}
    except Exception:
        return {"history": []}


@router.post("/api/prompt/test/run-single/{item_id}")
def prompt_test_run_single(item_id: int):
    """单条 Prompt 测试（仅域A规则评分），结果持久化到 DB"""
    item = _get_test_item_by_id(item_id)
    if not item:
        return JSONResponse({"error": "item 不存在"}, status_code=404)
    from prompt_test_manager import run_single_test
    from evaluation_matrix import evaluate_with_weights, get_dimension_breakdown
    from prompt_versions import get_active_version_name
    import json
    from datetime import datetime

    result = run_single_test(item, agent)
    if result and result.get("error"):
        return JSONResponse({"error": result["error"]}, status_code=500)

    # 保存到 prompt_test_results
    active_v = get_active_version_name(agent.memory._db_path) or "unknown"
    scores = result.get("scores", {})
    weight_total = evaluate_with_weights(scores)
    avg_total = (sum(scores.values()) / len(scores) * 100) if scores else 0
    passed = 1 if result.get("passed") else 0

    import sqlite3
    conn = _db()
    conn.execute(
        "INSERT INTO prompt_test_results (timestamp, total, passed, failed, pass_rate, weighted_score, overall_score, version, dimension_scores, difficulty_scores, results) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            datetime.now().isoformat(),
            1, passed, 1 - passed,
            passed * 100,
            round(weight_total, 1),
            round(avg_total, 1),
            active_v,
            json.dumps(scores, ensure_ascii=False),
            json.dumps({"single": round(avg_total, 1)}, ensure_ascii=False),
            json.dumps([result], ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    return {"ok": True, "result": result}


@router.post("/api/prompt/test/run-all")
def prompt_test_run_all(data: dict = Body(...)):
    """运行全部 Prompt 测试（批量），聚合分数，保存结果到 DB（仅域A规则评分）"""
    set_id = data.get("set_id", "builtin")
    items = agent.memory.get_test_items(set_id)
    if not items:
        return JSONResponse({"error": "没有找到测试题"}, status_code=400)

    from prompt_test_manager import run_single_test
    from evaluation_matrix import evaluate_with_weights, get_dimension_breakdown
    from prompt_versions import get_active_version_name
    import json

    active_v = get_active_version_name(agent.memory._db_path) or "unknown"
    conn = _db()

    results = []
    for item in items:
        # 为域B/C获取检索文档
        try:
            retrieved = agent.memory.search(query=item["query"], limit=10) if hasattr(agent, 'memory') else []
        except Exception:
            retrieved = []
        result = run_single_test(item, agent, retrieved_docs=retrieved)
        results.append(result)
        # 保存单条结果到 version_test_results
        if result.get("scores"):
            conn.execute(
                "INSERT INTO version_test_results (version_name, test_id, test_category, query, answer, scores, weighted_score, duration, passed, evaluated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    active_v,
                    str(item["id"]),
                    result.get("category", ""),
                    item["query"],
                    result.get("answer", ""),
                    json.dumps(result["scores"]),
                    result.get("weighted_score", 0),
                    result.get("duration", 0),
                    1 if result.get("passed") else 0,
                    datetime.now().isoformat(),
                ),
            )

    # 聚合维度分数
    dim_scores = {}
    for r in results:
        for dim, score in r.get("scores", {}).items():
            if dim not in dim_scores:
                dim_scores[dim] = []
            dim_scores[dim].append(score)
    dim_avg = {dim: round(sum(vals) / len(vals), 2) for dim, vals in dim_scores.items()}

    # 按难度聚合
    diff_scores = {}
    for r in results:
        diff = r.get("difficulty", "medium")
        if diff not in diff_scores:
            diff_scores[diff] = []
        diff_scores[diff].append(r.get("avg_score", 0))
    diff_avg = {d: round(sum(v) / len(v), 2) for d, v in diff_scores.items()}

    passed = sum(1 for r in results if r.get("passed"))
    total = len(results)
    weighted_score = evaluate_with_weights(dim_avg)
    overall_score = round(sum(r.get("avg_score", 0) for r in results) / total * 100, 1) if total else 0

    report = {
        "timestamp": datetime.now().isoformat(),
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": round(passed / total * 100, 1),
        "dimension_scores": dim_avg,
        "difficulty_scores": diff_avg,
        "weighted_score": round(weighted_score * 100, 1),
        "overall_score": overall_score,
        "version": active_v,
        "results": results,
    }

    # 保存聚合报告到 prompt_test_results
    conn.execute(
        "INSERT INTO prompt_test_results (timestamp, total, passed, failed, pass_rate, weighted_score, overall_score, version, dimension_scores, difficulty_scores, results) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report["timestamp"], total, passed, total - passed,
            report["pass_rate"], report["weighted_score"], report["overall_score"],
            active_v,
            json.dumps(dim_avg, ensure_ascii=False),
            json.dumps(diff_avg, ensure_ascii=False),
            json.dumps(results, ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    return {"ok": True, **report}


@router.post("/api/prompt/test/run-elastic")
def prompt_test_run_elastic(data: dict = Body(...)):
    """运行弹性测试：基线 + N 种变体 + 动态噪声，评分一致性/鲁棒性/Faithfulness"""
    base_query = data.get("query", "")
    if not base_query:
        return JSONResponse({"error": "query 不能为空"}, status_code=400)

    from datetime import datetime
    import json

    from prompt_versions import get_active_version_name
    from prompt_tester import (_eval_annotation, _eval_brand, _eval_rejection, _eval_contain, _eval_efficiency,
                               _eval_faithfulness, _eval_hallucination, _keyword_overlap_tfidf,
                               _generate_noise_variants)

    active_v = get_active_version_name(agent.memory._db_path) or "unknown"
    conn = _db()
    # 1. 跑基线
    start = time.time()
    base_result = agent.ask(query=base_query, conversation_id=None, temperature=0.1, category="prompt_test")
    base_duration = time.time() - start
    base_answer = base_result.get("answer", "")
    base_len = len(base_answer)

    # 2. 跑变体
    from prompt_tester import ELASTIC_VARIATIONS
    variants = []
    for name, transform in ELASTIC_VARIATIONS.items():
        q = transform(base_query)
        start = time.time()
        r = agent.ask(query=q, conversation_id=None, temperature=0.1, category="prompt_test")
        dur = time.time() - start
        ans = r.get("answer", "")
        variants.append({"name": name, "query": q[:100], "answer": ans, "duration": round(dur, 2), "length": len(ans), "type": "variation"})

    # 3. 跑噪声（动态生成，基于基线问题）
    noise_variants = _generate_noise_variants(base_query)
    for name, q in noise_variants.items():
        start = time.time()
        r = agent.ask(query=q, conversation_id=None, temperature=0.1, category="prompt_test")
        dur = time.time() - start
        ans = r.get("answer", "")
        variants.append({"name": name, "query": q[:100], "answer": ans, "duration": round(dur, 2), "length": len(ans), "type": "noise"})

    # 4. 评分
    consistency_details = {}
    robustness_details = {}

    for v in variants:
        ans = v["answer"]
        ann_ok, _ = _eval_annotation(ans)
        brand_ok, _, _ = _eval_brand(ans)
        has_content = len(ans.strip()) > 100

        # TF-IDF 语义重叠度
        kw_overlap = _keyword_overlap_tfidf(base_answer, ans)

        # Faithfulness + Hallucination 评分
        faith_score, _ = _eval_faithfulness(ans, [])
        hall_score, _ = _eval_hallucination(ans, [])
        faith_ok = faith_score >= 0.7

        if v["type"] == "variation":
            # 新一致性公式：faithfulness + 语义重叠 + 标注 + 幻觉
            consistency_score = round(
                0.4 * faith_score +
                0.3 * kw_overlap +
                0.2 * (1.0 if ann_ok else 0.0) +
                0.1 * (1.0 if faith_ok else 0.0),
                2
            )
            consistency_score = max(0.0, min(1.0, consistency_score))
            consistency_details[v["name"]] = {
                "semantic_score": consistency_score,
                "faithfulness": round(faith_score, 2),
                "hallucination": round(hall_score, 2),
                "coverage": 1.0 if has_content else 0.0,
            }
            v["faithfulness"] = round(faith_score, 2)
            v["hallucination"] = round(hall_score, 2)
            v["consistency"] = consistency_score
            v["keyword_overlap"] = kw_overlap
        else:
            # 鲁棒性：每个维度独立判断
            spell_tol = 1.0 if v["name"] in ("typo", "spelling_error") and has_content else (0.0 if v["name"] in ("typo", "spelling_error") else 0.5)
            fuzzy_tol = 1.0 if v["name"] in ("casual", "irrelevant") and has_content and kw_overlap > 0.5 else (0.5 if v["name"] in ("casual", "irrelevant") and has_content else 0.0)
            extreme_inp = 1.0 if v["name"] in ("short", "domain_shift", "length_extreme", "redundant") and has_content else (0.3 if v["name"] in ("short", "domain_shift", "length_extreme", "redundant") else 0.0)
            adversarial = 1.0 if v["name"] == "inducement" and brand_ok and faith_score >= 0.7 else (0.5 if v["name"] == "inducement" and brand_ok else 0.0)
            mixed_tol = 1.0 if v["name"] == "mixed" and has_content and kw_overlap > 0.4 else 0.0

            robustness_details[v["name"]] = {
                "spelling_tolerance": spell_tol,
                "fuzzy_tolerance": fuzzy_tol,
                "extreme_input": extreme_inp,
                "adversarial": adversarial,
                "mixed_tolerance": mixed_tol,
            }
            v["faithfulness"] = round(faith_score, 2)
            v["hallucination"] = round(hall_score, 2)
            v["keyword_overlap"] = kw_overlap

    # 聚合
    consistency_scores = [v.get("semantic_score", 0) for v in consistency_details.values()]
    consistency_avg = round(sum(consistency_scores) / len(consistency_scores), 2) if consistency_scores else 0
    robustness_scores = []
    for rd in robustness_details.values():
        robustness_scores.append(sum(rd.values()) / len(rd))
    robustness_avg = round(sum(robustness_scores) / len(robustness_scores), 2) if robustness_scores else 0
    composite = round(consistency_avg * 0.5 + robustness_avg * 0.5, 2)

    # 最弱变体
    all_scores = [(v["name"], v.get("consistency", 0) or v.get("faithfulness", 0)) for v in variants if v["type"] == "variation"]
    weakest = sorted(all_scores, key=lambda x: x[1])[:3]

    report = {
        "timestamp": datetime.now().isoformat(),
        "total": len(variants),
        "passed": sum(1 for v in variants if v["length"] > 50),
        "failed": sum(1 for v in variants if v["length"] <= 50),
        "pass_rate": round(sum(1 for v in variants if v["length"] > 50) / len(variants) * 100, 1),
        "weighted_score": round(composite * 100, 1),
        "overall_score": round(consistency_avg * 100, 1),
        "consistency_score": round(consistency_avg * 100, 1),
        "robustness_score": round(robustness_avg * 100, 1),
        "composite_score": round(composite * 100, 1),
        "dimension_scores": {"一致性总分": consistency_avg, "鲁棒性总分": robustness_avg},
        "baseline": {"query": base_query[:100], "answer_preview": base_answer[:200], "length": base_len, "duration": round(base_duration, 2)},
        "results": variants,
        "consistency_details": consistency_details,
        "robustness_details": robustness_details,
        "weakest": [{"name": w[0], "score": w[1]} for w in weakest],
        "version": active_v,
        "variant_type": True,
    }

    # 5. 保存到 DB
    conn.execute(
        "INSERT INTO prompt_test_results (timestamp, total, passed, failed, pass_rate, weighted_score, overall_score, version, dimension_scores, difficulty_scores, results) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            report["timestamp"], report["total"], report["passed"], report["failed"],
            report["pass_rate"], report["weighted_score"], report["overall_score"],
            active_v,
            json.dumps(report.get("dimension_scores", {}), ensure_ascii=False),
            json.dumps({"consistency": consistency_avg, "robustness": robustness_avg}, ensure_ascii=False),
            json.dumps(report["results"], ensure_ascii=False),
        ),
    )
    conn.commit()
    conn.close()

    return {"ok": True, **report}


@router.put("/api/prompt/test/items/{item_id}")
def prompt_test_update_item(item_id: int, data: dict = Body(...)):
    """更新测试题"""
    ok = agent.memory.update_test_item(
        item_id, query=data.get("query"), category=data.get("category")
    )
    return {"ok": ok}


@router.post("/api/prompt/test/suggest-fix/{item_id}")
def prompt_test_suggest_fix(item_id: int):
    """AI 建议修复"""
    item = _get_test_item_by_id(item_id)
    if not item:
        return JSONResponse({"error": "item 不存在"}, status_code=404)
    from prompt_test_manager import suggest_fix
    sys_prompt = SystemPromptLoader.get()
    llm = _get_backend_eval_llm()
    fix = suggest_fix(item, sys_prompt, llm=llm)
    return {"ok": True, "fix": fix}


def _get_test_item_by_id(item_id: int) -> dict | None:
    """跨所有测试集按 id 查找测试题"""
    import json
    conn = _db()
    row = conn.execute(
        "SELECT id, set_id, seq, query, category, difficulty, expected FROM prompt_test_items WHERE id = ? AND is_active = 1",
        (item_id,),
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {
        "id": row[0], "set_id": row[1], "seq": row[2], "query": row[3],
        "category": row[4], "difficulty": row[5],
        "expected": json.loads(row[6]) if row[6] else {},
    }


@router.get("/api/stats/dashboard")
def dashboard_stats(category: str = "all"):
    try:
        data = agent.memory.get_dashboard_stats(category=category)
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"看板数据聚合失败: {e}", exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/stats/health")
def health():
    llm_info = agent.llm.get_current_provider() if hasattr(agent.llm, "get_current_provider") else {}
    today_count = 0
    try:
        import sqlite3
        from app_state import _get_db_path as gdb
        c = sqlite3.connect(gdb())
        today_count = c.execute(
            "SELECT COUNT(*) FROM usage_logs WHERE DATE(created_at) = DATE('now')").fetchone()[0]
    except Exception:
        pass
    provider_name = llm_info.get("name", "")
    model_name = llm_info.get("model", "")
    display_name = provider_name if provider_name and provider_name != "api" else model_name.split(
        "/")[0] if "/" in model_name else model_name[:20] if model_name else "未知"
    return {
        "llm_provider": display_name,
        "llm_model": llm_info.get("model", "未知"),
        "uptime_seconds": int(time.time() - _START_TIME),
        "today_queries": today_count,
        "faiss_ready": os.path.exists(os.path.join(str(Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "04_vector_store" / "faiss_index"), "index.faiss")),
        "chroma_ready": os.path.exists(str(Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "04_vector_store" / "chroma_db")),
    }


# ==================== Pipeline / 检索质量 ====================
@router.get("/api/stats/pipeline")
def pipeline_stats(limit: int = 30):
    try:
        data = agent.memory.get_pipeline_stats(limit=limit)
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"pipeline_stats 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.get("/api/stats/retrieval-eval")
def retrieval_eval(limit: int = 100):
    try:
        data = agent.memory.get_retrieval_eval(limit=limit)
        data["eval_summary"] = _load_eval_summary("retrieval_quality")
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"retrieval_eval 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


# ==================== Retrieval Eval CRUD ====================
@router.get("/api/stats/retrieval-eval/items")
def get_retrieval_eval_items():
    try:
        items = agent.memory.get_retrieval_eval_items()
        return JSONResponse({"items": items, "total": len(items)})
    except Exception as e:
        logger.error(f"get_retrieval_eval_items 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/retrieval-eval/items")
def add_retrieval_eval_item(data: dict):
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


@router.put("/api/stats/retrieval-eval/items")
def update_retrieval_eval_item(data: dict):
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


@router.delete("/api/stats/retrieval-eval/items/{item_id}")
def delete_retrieval_eval_item(item_id: int):
    try:
        ok = agent.memory.delete_retrieval_eval_item(item_id)
        return JSONResponse({"success": ok})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/retrieval-eval/items/batch-import")
def batch_import_retrieval_eval_items(data: dict):
    try:
        raw = data.get("items", [])
        count = agent.memory.batch_import_retrieval_eval_items(raw)
        return JSONResponse({"imported": count, "success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/retrieval-eval/run-single")
def retrieval_eval_run_single(data: dict):
    try:
        query = data.get("query", "").strip()
        expected = data.get("expected", "").strip()
        if not query or not expected:
            return JSONResponse({"error": "query 和 expected 不能为空"}, status_code=400)
        from _eval_retrieval import evaluate_single_query
        result = evaluate_single_query(agent, query, expected)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"run-single 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/retrieval-eval/generate")
def retrieval_eval_generate(data: dict):
    keywords = data.get("keywords", "网络安全")
    from _eval_retrieval import generate_test_set_from_keywords, evaluate_with_items
    items = generate_test_set_from_keywords(keywords, llm=_get_backend_eval_llm())
    result = evaluate_with_items(items, agent.memory)

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

    summary_text = ""
    try:
        eval_llm = _get_backend_eval_llm()
        if eval_llm:
            summary_text = _generate_eval_summary(eval_llm, "retrieval_quality", result, items)
    except Exception as e:
        logger.warning(f"生成分析建议失败: {e}")

    return {"ok": True, "items": items, "result": result, "saved_to_items": saved, "summary": summary_text}


@router.get("/api/stats/retrieval-eval/report")
def retrieval_eval_report(limit: int = 60):
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


# ==================== 检索模式对比 ====================
@router.get("/api/stats/retrieval-eval/compare")
def retrieval_eval_compare(limit: int = 100):
    try:
        data = agent.memory.get_eval_comparison(limit=limit)
        data["eval_summary"] = _load_eval_summary("retrieval_compare")
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"eval_comparison 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/retrieval-eval/compare")
def retrieval_eval_compare_run(data: dict):
    keywords = data.get("keywords", "网络安全")
    from _eval_retrieval import generate_test_set_from_keywords, evaluate_with_items_compare
    items = generate_test_set_from_keywords(keywords, llm=_get_backend_eval_llm())
    result = evaluate_with_items_compare(items, agent.memory)

    summary_text = ""
    try:
        eval_llm = _get_backend_eval_llm()
        if eval_llm:
            summary_text = _generate_eval_summary(eval_llm, "retrieval_compare", result, items)
    except Exception as e:
        logger.warning(f"生成增益对比分析建议失败: {e}")

    return {"ok": True, "items": items, "result": result, "summary": summary_text}


@router.get("/api/stats/eval-summary/retrieval-quality")
def get_retrieval_quality_summary():
    return _load_eval_summary("retrieval_quality")


@router.get("/api/stats/eval-summary/retrieval-compare")
def get_retrieval_compare_summary():
    return _load_eval_summary("retrieval_compare")


@router.get("/api/stats/retrieval-eval/compare/report")
def retrieval_eval_compare_report(limit: int = 60):
    try:
        data = agent.memory.get_eval_comparison(limit=limit)
        items = data.get("items", [])
        summary = data.get("summary", {})
        stats = agent.stats()
    except Exception:
        items = []
        summary = {}
        stats = {}

    mode_keys = [
        ("faiss_only", "faiss_only_recall_5", "faiss_only_mrr"),
        ("bm25_only", "bm25_only_recall_5", "bm25_only_mrr"),
        ("hybrid_no_rerank", "hybrid_no_rerank_recall_5", "hybrid_no_rerank_mrr"),
        ("hybrid_rerank", "hybrid_rerank_recall_5", "hybrid_rerank_mrr"),
    ]
    summary_cards = []
    for label, r5_key, mrr_key in mode_keys:
        avg_r5 = summary.get(r5_key, 0)
        avg_mrr = summary.get(mrr_key, 0)
        summary_cards.append((f"R@{avg_r5*100:.0f}%\nMRR{avg_mrr:.3f}", f"{label}"))

    hybrid_gain_r5 = summary.get("hybrid_gain_recall_5", 0)
    rerank_gain_r5 = summary.get("rerank_gain_recall_5", 0)
    hybrid_gain_mrr = summary.get("hybrid_gain_mrr", 0)
    rerank_gain_mrr = summary.get("rerank_gain_mrr", 0)
    summary_cards.append((f"{hybrid_gain_r5*100:+.0f}pp\n{hybrid_gain_mrr:+.3f}", "Hybrid 增益"))
    summary_cards.append((f"{rerank_gain_r5*100:+.0f}pp\n{rerank_gain_mrr:+.3f}", "Rerank 增益"))

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

    try:
        date_line = f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} | 测试 {summary.get('count', 0)} 条 | FAISS: {stats.get('faiss_vectors', '?')} | Chroma: {stats.get('chroma_chunks', '?')}"
        ts = datetime.now().strftime('%Y%m%d_%H%M%S')
        buf = _build_report_doc("检索模式增益对比报告", date_line, summary_cards, headers, rows)
        return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                 headers={"Content-Disposition": f"attachment; filename=retrieval_compare_report_{ts}.docx"})
    except Exception as e:
        logger.error(f"增益对比报告生成失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/retrieval-eval/generate-items")
def retrieval_eval_generate_items(data: dict):
    keywords = data.get("keywords", "网络安全")
    from _eval_retrieval import generate_test_set_from_keywords
    items = generate_test_set_from_keywords(keywords, llm=_get_backend_eval_llm())

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
