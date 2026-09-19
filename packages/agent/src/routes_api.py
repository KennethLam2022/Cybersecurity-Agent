import os
import json
import time
import hashlib
import base64
import urllib.parse
import uuid
import threading
from datetime import datetime
from pathlib import Path, PureWindowsPath
from typing import AsyncGenerator, Optional
from fastapi import APIRouter, Request, Body, HTTPException, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, FileResponse, Response, RedirectResponse
import asyncio
import sqlite3
import html
import csv
import io
import secrets

# ---- WAL 连接辅助 (防并发锁) ----
def _db():
    c = sqlite3.connect(agent.memory._db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


def _publish_event(bus, event_type: str, data: dict) -> None:
    """在线程池/同步上下文中安全发布事件"""
    publish_system_event(agent.memory, bus, event_type, data)


def _authorized_profiles(tenant_id: str, requested: object) -> set[str] | None:
    """Resolve the tenant's retrieval scope and validate explicit narrowing."""
    allowed = _governance_store_for_current_memory().enabled_profiles(tenant_id)
    if requested is None:
        return allowed
    if not isinstance(requested, (list, tuple, set)):
        raise HTTPException(status_code=400, detail="profiles 必须是字符串列表")
    values = {str(item).strip() for item in requested if str(item).strip()}
    if not values:
        return allowed
    denied = sorted(values - allowed)
    if denied:
        raise HTTPException(status_code=403, detail="Profile 未启用或不属于当前工作区: " + ", ".join(denied))
    return values

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
    _PROJECT_ROOT,
)
from profile_classifier import profile_options, suggest_document_profile
from profile_extensions import confirm_profile_extension, propose_profile_extension
from profile_migration import (
    apply_profile_migration,
    confirm_profile_migration,
    profile_assignment_history,
    scan_profile_migration,
)
from access_migration import (
    access_assignment_history,
    confirm_access_migration,
    rollback_access_migration,
    scan_access_migration,
)
from agent import SystemPromptLoader
from llm_provider import LLMProvider
from reflection_engine import reflect_answer
from reflection_engine import PROMPT_ASSET_DEFAULTS
from prompt_asset_tester import (compare_reflection_asset_versions, evaluate_slot_result,
                                 compare_slot_contract_versions, get_slot_golden_cases,
                                 run_reflection_golden_suite, run_structured_prompt_golden_suite,
                                 compare_structured_prompt_versions, EXECUTABLE_STRUCTURED_SLOTS)
from identity import (ADMIN_CSRF_COOKIE, ADMIN_SESSION_COOKIE, FRONT_SESSION_COOKIE,
                      principal_from_request, require_permission, require_platform_permission,
                      session_token_from_request, uses_admin_session)
from capability_router import build_outline, route_capability
from capability_executor import execute_capability, CapabilityExecutionError
from generation_manager import artifact_path, render_artifact
from workflow_engine import execute_workflow
from knowledge_graph import (extract_document_graph, extract_semantic_graph_candidates,
                             graph_impact, is_semantic_graph_relation, scan_graph_conflicts)
from neo4j_graph_store import get_neo4j_graph_store
from monitoring import build_issue_report, generate_issue_diagnosis, record_event, scan_cost_spike
from monitoring_adapters import run_approved_change_action
from memory_governance import propose_memory_conflict, propose_user_profile
from trace_observability import build_runtime_context
from generation_evidence import (
    append_mcp_fetch_evidence,
    append_mcp_search_evidence,
    build_generation_evidence_plan,
    collect_generation_references,
    generate_outline_from_evidence,
)
from rag_document_sync import scan_existing_rag_documents, sync_existing_rag_documents
from data_source_security import validate_data_source_config
from data_source_reader import read_url
from backup_manager import create_backup, list_backups, restore_backup, verify_backup
from governance_store import GovernanceStore

from webhook_delivery import WEBHOOK_EVENTS, dispatch_webhook_event
from notification_delivery import publish_system_event
from sso_provider import (
    authenticate_ldap,
    build_oidc_authorize_url,
    discover_oidc_provider,
    exchange_oidc_code,
    extract_oidc_identity,
    get_oidc_presets,
    test_ldap_connection,
)

router = APIRouter()

_ALLOWED_UPLOAD_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".xlsx", ".xls", ".txt", ".md", ".pptx", ".ppt", ".csv",
}
_MAX_BATCH_UPLOAD_SIZE = _MAX_FILE_SIZE * 10


def _safe_upload_filename(name: str) -> str:
    """Return a basename safe to write below the upload staging directory."""
    raw = str(name or "").strip()
    windows_path = PureWindowsPath(raw)
    if (
        not raw
        or Path(raw).name != raw
        or windows_path.name != raw
        or Path(raw).is_absolute()
        or windows_path.is_absolute()
        or raw in {".", ".."}
    ):
        raise HTTPException(status_code=400, detail="文件名不合法")
    if Path(raw).suffix.lower() not in _ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(status_code=400, detail="不支持的文件类型")
    return raw


def _validate_upload_batch_size(current_size: int, next_size: int) -> int:
    total = int(current_size or 0) + int(next_size or 0)
    if total > _MAX_BATCH_UPLOAD_SIZE:
        raise HTTPException(status_code=413, detail="本批次文件总大小超过限制")
    return total

@router.get("/api/admin/rag-documents/sync/preview")
def admin_preview_existing_rag_documents(request: Request, limit: int | None = None):
    require_platform_permission(request, agent.memory)
    return JSONResponse(scan_existing_rag_documents(limit=max(1, min(limit, 10000)) if limit else None))

@router.post("/api/admin/rag-documents/sync")
def admin_sync_existing_rag_documents(request: Request, data: dict = Body(default={} )):
    principal = require_platform_permission(request, agent.memory)
    raw_limit = data.get("limit")
    limit = max(1, min(int(raw_limit), 10000)) if raw_limit is not None else None
    result = sync_existing_rag_documents(agent.memory, limit=limit, changed_by=principal.user_id or "admin")
    agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id, "document.rag_reconcile.summary", "document", "", {k: result[k] for k in ("scanned", "created", "updated", "skipped", "needs_review")})
    return JSONResponse(result)
def _governance_store_for_current_memory() -> GovernanceStore:
    """Keep governance metadata in the same database as the active Agent."""
    return GovernanceStore(str(agent.memory._db_path))


def _backup_root() -> str:
    return str(Path(agent.memory._db_path).resolve().parent / "backups")


@router.get("/api/admin/backups")
def admin_list_backups(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": list_backups(_backup_root())})


@router.post("/api/admin/backups")
def admin_create_backup(request: Request):
    principal = require_platform_permission(request, agent.memory)
    result = create_backup(agent.memory._db_path, str(_PROJECT_ROOT), _backup_root())
    agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                           "backup.create", "backup", result["id"], {"file_count": result["file_count"]})
    return JSONResponse(result)


@router.post("/api/admin/backups/verify")
def admin_verify_backup(request: Request, data: dict = Body(...)):
    require_platform_permission(request, agent.memory)
    backup_id = str(data.get("backup_id") or "")
    root = Path(_backup_root()).resolve()
    target = (root / backup_id).resolve()
    if not backup_id or target.parent != root:
        raise HTTPException(status_code=400, detail="备份标识不合法")
    return JSONResponse(verify_backup(str(target)))


@router.post("/api/admin/backups/restore")
def admin_restore_backup(request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    if data.get("confirm") is not True:
        raise HTTPException(status_code=400, detail="恢复操作需要明确 confirm=true")
    backup_id = str(data.get("backup_id") or "")
    root = Path(_backup_root()).resolve()
    target = (root / backup_id).resolve()
    if not backup_id or target.parent != root:
        raise HTTPException(status_code=400, detail="备份标识不合法")
    try:
        result = restore_backup(str(target), agent.memory._db_path, str(_PROJECT_ROOT))
    except (OSError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                           "backup.restore", "backup", backup_id,
                           {"protection_backup": result.get("protection_backup", "")})
    return JSONResponse(result)


def _attach_browser_session(response: Response, token: str, max_age: int = 7 * 24 * 3600,
                            admin: bool = False) -> Response:
    """Issue an HttpOnly session cookie plus a readable CSRF cookie for browser clients."""
    secure = os.environ.get("APP_ENV", "development").strip().lower() in {"production", "prod"}
    session_cookie = ADMIN_SESSION_COOKIE if admin else FRONT_SESSION_COOKIE
    csrf_cookie = ADMIN_CSRF_COOKIE if admin else "securenexus_csrf"
    response.set_cookie(session_cookie, token, max_age=max_age, httponly=True,
                       secure=secure, samesite="lax", path="/")
    response.set_cookie(csrf_cookie, secrets.token_urlsafe(24), max_age=max_age,
                       httponly=False, secure=secure, samesite="lax", path="/")
    return response


def _request_session_token(request: Request) -> str:
    authorization = session_token_from_request(request)
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() == "bearer" and token:
        return token
    return authorization


def _tenant_admin_scope(request: Request, tenant_id: str = ""):
    """Resolve a tenant administration scope from the authenticated session."""
    principal = require_permission(request, agent.memory, "tenant.manage", tenant_id)
    return principal, (str(tenant_id or principal.tenant_id) if principal.role == "platform_admin" else principal.tenant_id)


def _require_ingestion_job_scope(request: Request, job: dict):
    principal = principal_from_request(request, agent.memory)
    if principal.role != "platform_admin" and principal.tenant_id != str(job.get("tenant_id") or ""):
        raise HTTPException(status_code=403, detail="无权访问其他工作区的入库任务")
    return principal


def _require_access_migration_permission(request: Request):
    return require_platform_permission(request, agent.memory)


def _require_resource_tenant(principal, resource_tenant_id: str) -> None:
    if principal.role != "platform_admin" and resource_tenant_id != principal.tenant_id:
        raise HTTPException(status_code=403, detail="无权访问其他租户的资源")


def _admin_conversation_scope(request: Request, conversation_id: str, tenant_id: str = ""):
    """Resolve an admin's tenant scope and verify the conversation belongs to it."""
    principal, resolved_tenant = _tenant_admin_scope(request, tenant_id)
    owner = agent.memory.get_conversation_owner(conversation_id)
    if not owner or owner.get("tenant_id") != resolved_tenant:
        raise HTTPException(status_code=404, detail="对话不存在或不属于当前工作区")
    return principal, resolved_tenant


def _audit_sensitive_access(principal, resource_type: str, resource_id: str,
                            reason: str, detail: dict | None = None) -> None:
    """Record a sensitive read without copying the protected payload into audit logs."""
    agent.memory.log_audit(
        principal.tenant_id, principal.user_id, principal.agent_id,
        "sensitive.read", resource_type, resource_id,
        {"reason": str(reason or "管理端查看")[:300], **(detail or {})},
    )


def _record_auxiliary_llm_usage(module: str, llm_result: dict | None, model: str = "",
                                tenant_id: str = "local-default") -> None:
    usage = (llm_result or {}).get("usage") or {}
    agent.memory.record_llm_usage_event(
        tenant_id=tenant_id, module=module, provider="", model=(llm_result or {}).get("model") or model,
        prompt_tokens=usage.get("prompt_tokens", 0), completion_tokens=usage.get("completion_tokens", 0),
    )


def _reflect_generation_outline(principal, mode: str, query: str, outline: dict,
                                references: list[dict]) -> dict:
    """Review generation scope before an artifact is persisted or rendered."""
    config = _get_llm_config_card("reflection")
    llm = None
    if config.get("model") and config.get("base_url"):
        try:
            llm = LLMProvider(base_url=config["base_url"], api_key=config.get("api_key", ""),
                              model=config["model"], use_ollama_fallback=False)
        except Exception as exc:
            logger.warning("生成反思模型初始化失败: %s", exc)
    candidate = json.dumps(outline, ensure_ascii=False)
    review = reflect_answer(
        agent.memory, llm, query, candidate, references, mode, structured_answer=True,
        usage_sink=lambda result, model: _record_auxiliary_llm_usage(
            "reflection", result, model, tenant_id=principal.tenant_id,
        ),
    )
    review["model"] = config.get("model", "")
    if review["decision"] == "revise":
        try:
            revised_outline = json.loads(review["answer"])
            if isinstance(revised_outline, dict):
                review["outline"] = revised_outline
            else:
                review["outline"] = outline
                review["decision"] = "degraded"
                review["reason"] = "revision_not_structured"
        except (TypeError, ValueError):
            # Outline changes must remain structured. Do not inject free text into the renderer.
            review["outline"] = outline
            review["decision"] = "degraded"
            review["reason"] = "revision_not_structured"
    else:
        review["outline"] = outline
    return review


def _run_data_source_sync(run_id: str) -> None:
    """Read a configured source into review staging; never index automatically."""
    run = agent.memory.get_data_source_sync_run(run_id)
    if not run:
        return
    source = agent.memory.get_data_source(run["data_source_id"], run.get("tenant_id"))
    if not source:
        agent.memory.update_data_source_sync_run(run_id, "failed", "数据源不存在")
        return
    agent.memory.update_data_source_sync_run(run_id, "running")
    try:
        if source["source_type"] != "url":
            raise ValueError("当前仅支持 URL 数据源自动读取，其他类型请使用对应导入流程")
        result = read_url(source["endpoint"], source.get("config") or {})
        kb = agent.memory.get_knowledge_base(source.get("knowledge_base_id", ""), source["tenant_id"])
        visibility = (kb or {}).get("visibility") or "tenant"
        owner_user_id = source.get("owner_user_id", "") if visibility == "private" else ""
        profile = str((kb or {}).get("profile") or (source.get("config") or {}).get("profile") or "general")
        category = str((source.get("config") or {}).get("category") or "通用")
        document_id = "doc-" + uuid.uuid4().hex
        staging_dir = _UPLOAD_STAGING / f"data-source-{run_id}"
        staging_dir.mkdir(parents=True, exist_ok=True)
        safe_name = Path(result["source_name"]).name or "source.txt"
        if not safe_name.lower().endswith((".txt", ".md", ".html", ".htm")):
            safe_name += ".txt"
        staged_path = staging_dir / f"{document_id}__{safe_name}"
        staged_path.write_text(result["content"], encoding="utf-8")
        metadata = {
            "source_id": source["id"], "source_url": result["endpoint"],
            "source_type": source["source_type"], "content_hash": result["content_hash"],
            "content_type": result["content_type"], "bytes": result["bytes"],
            "sync_run_id": run_id, "profile": profile, "category": category,
        }
        agent.memory.create_staged_document_from_source(
            document_id=document_id, source_name=result["source_name"],
            staged_path=str(staged_path), metadata=metadata, category=category,
            profile=profile, visibility=visibility, tenant_id=source["tenant_id"],
            owner_user_id=owner_user_id, knowledge_base_id=source.get("knowledge_base_id", ""),
        )
        staged_path.with_suffix(staged_path.suffix + ".meta.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8",
        )
        agent.memory.update_data_source_sync_run(
            run_id, "succeeded", content_hash=result["content_hash"],
            documents_found=1, documents_ingested=0,
        )
        agent.memory.mark_data_source_synced(source["id"], result["content_hash"], source["tenant_id"])
    except Exception as exc:
        logger.warning("数据源同步失败 %s: %s", run_id, exc)
        agent.memory.update_data_source_sync_run(run_id, "failed", str(exc))
        agent.memory.update_data_source_status(source["id"], "error", source["tenant_id"], str(exc))


def _run_staged_document_processing(task_id: str, document_id: str, tenant_id: str,
                                    files: list[dict], category: str) -> None:
    """Publish a reviewed staged document through the normal parse/index pipeline."""
    try:
        _run_processing_task(task_id, files, category)
        document = agent.memory.get_document(document_id, tenant_id)
        if not document or document.get("status") != "indexed":
            agent.memory.update_document_lifecycle(
                document_id, "review", tenant_id, "system", "发布后的解析或索引失败，自动退回待审核",
            )
    except Exception as exc:
        logger.warning("待审核文档发布处理失败 %s: %s", document_id, exc)
        agent.memory.update_document_lifecycle(
            document_id, "review", tenant_id, "system", f"发布处理异常：{exc}",
        )


def _queue_staged_document_processing(document: dict, tenant_id: str) -> str:
    staged_path = str(document.get("cleaned_path") or "")
    if not staged_path or not Path(staged_path).is_file():
        raise ValueError("待处理文件不存在")
    document_id = document["id"]
    metadata = document.get("metadata") or {}
    task_id = "ingest-source-" + uuid.uuid4().hex[:12]
    file_meta = {
        "source_name": document.get("source_name", ""), "category": document.get("category", "通用"),
        "profile": document.get("profile", "general"), "document_id": document_id,
        "visibility": document.get("visibility", "tenant"), "tenant_id": document.get("tenant_id", ""),
        "owner_user_id": document.get("owner_user_id", ""), "agent_id": document.get("agent_id", ""),
        "knowledge_base_id": document.get("knowledge_base_id", ""),
        "profile_confirmed": True, "profile_source": "source_metadata", "source_metadata": metadata,
    }
    files = [{"name": document.get("source_name", "source.txt"), "path": staged_path,
              "category": document.get("category", "通用"), "profile": document.get("profile", "general"),
              "document_id": document_id, "visibility": document.get("visibility", "tenant"),
              "tenant_id": document.get("tenant_id", ""), "owner_user_id": document.get("owner_user_id", ""),
              "agent_id": document.get("agent_id", ""), "conflict_action": "rename", **file_meta}]
    agent.memory.create_ingestion_job(
        task_id, document.get("tenant_id", ""), "system", document.get("agent_id", ""),
        [{"document_id": document_id, "source_name": document.get("source_name", "")}],
    )
    with _doc_tasks_lock:
        _doc_tasks[task_id] = {
            "status": "processing", "progress": 0, "stage": "starting",
            "current_file": document.get("source_name", ""), "summary": None, "error": None,
            "document_ids": [document_id], "document_map": {document.get("source_name", ""): document_id},
        }
    threading.Thread(
        target=_run_staged_document_processing,
        args=(task_id, document_id, tenant_id or document.get("tenant_id", ""), files,
              document.get("category", "通用")),
        daemon=True, name=f"ingest-{document_id}",
    ).start()
    return task_id


# ==================== 首页 ====================
@router.get("/", response_class=HTMLResponse)
def index(request: Request):
    if agent.memory.platform_admin_setup_required():
        return RedirectResponse("/setup", status_code=303)
    try:
        principal = principal_from_request(request, agent.memory)
        if principal.authenticated:
            return RedirectResponse("/chat", status_code=303)
    except HTTPException:
        pass
    return RedirectResponse("/login?next=/chat", status_code=303)


@router.get("/chat", response_class=HTMLResponse)
def chat_page(request: Request):
    """Render chat only for a real browser session, never the legacy local principal."""
    if agent.memory.platform_admin_setup_required():
        return RedirectResponse("/setup", status_code=303)
    try:
        principal = principal_from_request(request, agent.memory)
        if not principal.authenticated:
            return RedirectResponse("/login?next=/chat", status_code=303)
    except HTTPException:
        return RedirectResponse("/login?next=/chat", status_code=303)
    template = jinja_env.get_template("index.html")
    content = template.render({"request": request})
    return HTMLResponse(content)


# ==================== 对外 API 网关 ====================
def _external_app_principal(request: Request, scope: str = "chat") -> dict:
    app_key = request.headers.get("X-App-Key", "").strip()
    app_secret = request.headers.get("X-App-Secret", "").strip()
    if not app_key or not app_secret:
        raise HTTPException(status_code=401, detail="需要 X-App-Key 和 X-App-Secret")
    app = agent.memory.authenticate_external_app(app_key, app_secret)
    if not app or scope not in app.get("scopes", []):
        raise HTTPException(status_code=403, detail="应用凭证无效或未授权该作用域")
    if not agent.memory.external_app_rate_allowed(app["id"]):
        raise HTTPException(status_code=429, detail="应用调用频率已达上限")
    return app


@router.post("/api/external/chat")
def external_chat(request: Request, data: dict = Body(...)):
    app = _external_app_principal(request, "chat")
    query = str(data.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")
    request_id = "req-" + uuid.uuid4().hex[:16]
    started = time.time()
    user_ref = str(data.get("user_ref") or "anonymous")[:160]
    try:
        result = agent.ask(
            query=query,
            category="external_api",
            tenant_id=app["tenant_id"],
            user_id=f"external:{app['id']}:{user_ref}",
            agent_id=app["agent_id"],
            knowledge_base_id=str(data.get("knowledge_base_id") or ""),
            profiles=_authorized_profiles(app["tenant_id"], data.get("profiles")),
        )
        stats = result.get("stats") or {}
        llm_context = (((stats.get("trace") or {}).get("context") or {}).get("llm") or {}).get("chat") or {}
        agent.memory.record_llm_usage_event(
            app["tenant_id"], f"external:{app['id']}:{user_ref}", app["agent_id"],
            result.get("conversation_id", ""), None, "external_api", llm_context.get("provider", ""),
            stats.get("model") or llm_context.get("model", ""), stats.get("prompt_tokens", 0), stats.get("completion_tokens", 0),
        )
        usage = agent.memory.record_external_app_usage(
            app["id"], app["tenant_id"], user_ref, "/api/external/chat", "succeeded",
            duration_ms=round((time.time() - started) * 1000),
            prompt_tokens=stats.get("prompt_tokens", 0),
            completion_tokens=stats.get("completion_tokens", 0), request_id=request_id,
        )
        return JSONResponse({"ok": True, "request_id": request_id, "answer": result.get("answer", ""),
                             "sources": result.get("sources", []), "conversation_id": result.get("conversation_id", ""),
                             "stats": stats, "usage_id": usage})
    except HTTPException:
        raise
    except Exception as exc:
        agent.memory.record_external_app_usage(
            app["id"], app["tenant_id"], user_ref, "/api/external/chat", "failed",
            duration_ms=round((time.time() - started) * 1000), error=str(exc), request_id=request_id,
        )
        logger.error("对外 API 调用失败: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Agent 调用失败") from exc


@router.get("/api/external/usage")
def external_usage(request: Request, limit: int = 100):
    app = _external_app_principal(request, "usage")
    return JSONResponse({"items": [item for item in agent.memory.list_external_app_usage(app["tenant_id"], limit)
                                     if item["app_id"] == app["id"]]})


@router.get("/api/admin/external-apps")
def admin_list_external_apps(request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_external_apps(tenant_id)})


@router.post("/api/admin/external-apps")
def admin_create_external_app(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        app = agent.memory.create_external_app(
            tenant_id, str(data.get("name") or ""),
            data.get("scopes") if isinstance(data.get("scopes"), list) else ["chat"],
            int(data.get("rate_limit_per_minute", 60)), principal.user_id,
            str(data.get("agent_id") or principal.agent_id),
        )
        agent.memory.log_audit(app["tenant_id"], principal.user_id, "",
                               "external_app.create", "external_app", app["id"],
                               {"scopes": app["scopes"], "app_key": app["app_key"]})
        return JSONResponse({"ok": True, "app": app})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/external-apps/{app_id}/status")
def admin_update_external_app_status(app_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    if not agent.memory.update_external_app_status(app_id, bool(data.get("enabled")), tenant_id):
        raise HTTPException(status_code=404, detail="外部应用不存在")
    return {"ok": True}


@router.get("/api/admin/external-apps/usage")
def admin_external_app_usage(request: Request, tenant_id: str = "", limit: int = 100):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_external_app_usage(tenant_id, limit)})


@router.post("/api/admin/external-apps/{app_id}/embed-token")
def admin_create_embed_token(app_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, _ = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        app = agent.memory.get_external_app(app_id)
        if not app:
            raise HTTPException(status_code=404, detail="外部应用不存在")
        _require_resource_tenant(principal, app["tenant_id"])
        token = agent.memory.create_embed_token(
            app_id, str(data.get("origin") or ""), int(data.get("ttl_hours", 24)),
            principal.user_id,
        )
        agent.memory.log_audit(token["tenant_id"], principal.user_id, "",
                               "embed_token.create", "external_app", app_id,
                               {"token_id": token["id"], "origin": token["origin"],
                                "expires_in_hours": token["expires_in_hours"]})
        return JSONResponse({"ok": True, "embed_token": token})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/external-apps/{app_id}/embed-tokens")
def admin_list_embed_tokens(app_id: str, request: Request):
    principal, _ = _tenant_admin_scope(request)
    app = agent.memory.get_external_app(app_id)
    if not app:
        raise HTTPException(status_code=404, detail="外部应用不存在")
    _require_resource_tenant(principal, app["tenant_id"])
    return JSONResponse({"items": agent.memory.list_embed_tokens(app_id)})


@router.put("/api/admin/embed-tokens/{token_id}/status")
def admin_update_embed_token_status(token_id: str, request: Request, data: dict = Body(...)):
    principal, _ = _tenant_admin_scope(request)
    token = agent.memory.get_embed_token(token_id)
    if not token:
        raise HTTPException(status_code=404, detail="Embed Token 不存在")
    _require_resource_tenant(principal, token["tenant_id"])
    if not agent.memory.update_embed_token_status(token_id, bool(data.get("enabled")), token["tenant_id"]):
        raise HTTPException(status_code=404, detail="Embed Token 不存在")
    return {"ok": True}


@router.put("/api/admin/external-apps/{app_id}/agent")
def admin_update_external_app_agent(app_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    app = agent.memory.get_external_app(app_id, tenant_id)
    if not app:
        raise HTTPException(status_code=404, detail="外部应用不存在")
    agent_id = str(data.get("agent_id") or "")
    if not agent.memory.update_external_app_agent(app_id, tenant_id, agent_id):
        raise HTTPException(status_code=400, detail="目标 Agent 不存在、未启用或不属于当前工作区")
    agent.memory.log_audit(tenant_id, principal.user_id, agent_id, "external_app.agent.update", "external_app", app_id)
    return {"ok": True, "agent_id": agent_id}


@router.post("/api/embed/chat")
def embed_chat(request: Request, data: dict = Body(...)):
    token = request.headers.get("X-Embed-Token", "").strip()
    if not token:
        raise HTTPException(status_code=401, detail="需要 X-Embed-Token")
    principal = agent.memory.authenticate_embed_token(token, request.headers.get("Origin", ""))
    if not principal:
        raise HTTPException(status_code=403, detail="Embed Token 无效、过期、停用或来源不匹配")
    query = str(data.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="query 不能为空")
    user_ref = str(data.get("user_ref") or "anonymous")[:160]
    request_id = "embed-" + uuid.uuid4().hex[:16]
    started = time.time()
    try:
        result = agent.ask(query=query, category="embed", tenant_id=principal["tenant_id"],
                           user_id=f"embed:{principal['app_id']}:{user_ref}",
                           agent_id=principal["agent_id"],
                           knowledge_base_id=str(data.get("knowledge_base_id") or ""),
                           profiles=_authorized_profiles(principal["tenant_id"], data.get("profiles")),
                           response_language=str(data.get("language") or "zh-CN"))
        stats = result.get("stats") or {}
        llm_context = (((stats.get("trace") or {}).get("context") or {}).get("llm") or {}).get("chat") or {}
        agent.memory.record_llm_usage_event(
            principal["tenant_id"], f"embed:{principal['app_id']}:{user_ref}", principal["agent_id"],
            result.get("conversation_id", ""), None, "external_api", llm_context.get("provider", ""),
            stats.get("model") or llm_context.get("model", ""), stats.get("prompt_tokens", 0), stats.get("completion_tokens", 0),
        )
        usage_id = agent.memory.record_external_app_usage(
            principal["app_id"], principal["tenant_id"], user_ref, "/api/embed/chat", "succeeded",
            duration_ms=round((time.time() - started) * 1000),
            prompt_tokens=stats.get("prompt_tokens", 0), completion_tokens=stats.get("completion_tokens", 0),
            request_id=request_id,
        )
        return JSONResponse({"ok": True, "request_id": request_id, "answer": result.get("answer", ""),
                             "sources": result.get("sources", []), "stats": stats, "usage_id": usage_id})
    except HTTPException:
        raise
    except Exception as exc:
        agent.memory.record_external_app_usage(
            principal["app_id"], principal["tenant_id"], user_ref, "/api/embed/chat", "failed",
            duration_ms=round((time.time() - started) * 1000), error=str(exc), request_id=request_id,
        )
        raise HTTPException(status_code=500, detail="Agent 调用失败") from exc


# ==================== Webhook 通知 ====================
@router.get("/api/admin/webhooks/events")
def admin_webhook_events(request: Request):
    _tenant_admin_scope(request)
    return JSONResponse({"items": sorted(WEBHOOK_EVENTS)})


@router.get("/api/admin/webhooks")
def admin_list_webhooks(request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_webhook_subscriptions(tenant_id)})


@router.post("/api/admin/webhooks")
def admin_create_webhook(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        item = agent.memory.create_webhook_subscription(
            tenant_id, data.get("name", ""), data.get("url", ""),
            data.get("event_types") if isinstance(data.get("event_types"), list) else [],
            int(data.get("max_attempts", 3)), int(data.get("timeout_seconds", 10)),
            principal.user_id,
        )
        agent.memory.log_audit(item["tenant_id"], principal.user_id, "",
                               "webhook.create", "webhook", item["id"],
                               {"event_types": item["event_types"], "url": item["url"]})
        return JSONResponse({"ok": True, "webhook": item})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/webhooks/{subscription_id}/status")
def admin_update_webhook_status(subscription_id: str, request: Request, data: dict = Body(...)):
    principal, _ = _tenant_admin_scope(request)
    subscription = agent.memory.get_webhook_subscription(subscription_id)
    if not subscription:
        raise HTTPException(status_code=404, detail="Webhook 订阅不存在")
    _require_resource_tenant(principal, subscription["tenant_id"])
    if not agent.memory.update_webhook_subscription_status(subscription_id, bool(data.get("enabled")), subscription["tenant_id"]):
        raise HTTPException(status_code=404, detail="Webhook 订阅不存在")
    return {"ok": True}


@router.get("/api/admin/webhooks/deliveries")
def admin_webhook_deliveries(request: Request, tenant_id: str = "", limit: int = 100):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_webhook_deliveries(tenant_id, limit)})


@router.post("/api/admin/webhooks/test")
def admin_test_webhook(request: Request, data: dict = Body(...)):
    event_type = str(data.get("event_type") or "eval.completed")
    if event_type not in WEBHOOK_EVENTS:
        raise HTTPException(status_code=400, detail="不支持的 Webhook 事件")
    _, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    return JSONResponse({"ok": True, **dispatch_webhook_event(
        agent.memory, event_type, {"test": True, "message": "SecureNexus webhook test"},
        tenant_id,
    )})


@router.get("/api/admin/email-notifications/config")
def admin_email_notification_config(request: Request):
    require_platform_permission(request, agent.memory)
    config = agent.memory.get_email_notification_config()
    return JSONResponse({"config": config})


@router.put("/api/admin/email-notifications/config")
def admin_save_email_notification_config(request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    try:
        agent.memory.save_email_notification_config(data or {})
        config = agent.memory.get_email_notification_config()
        agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                               "email.config.update", "email_config", "1",
                               {"enabled": config["enabled"], "events": config["events"]})
        return JSONResponse({"ok": True, "config": config})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/email-notifications/test")
def admin_test_email_notification(request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    from email_delivery import send_email
    config = agent.memory.get_email_notification_config(include_secret=True)
    if not config.get("enabled"):
        raise HTTPException(status_code=400, detail="邮件通知未启用")
    try:
        result = send_email(config, "SecureNexus SMTP test", "这是安枢 SecureNexus 的 SMTP 测试邮件。")
        agent.memory.record_email_delivery(principal.tenant_id, "admin.test", principal.user_id, 1, bool(result.get("sent")), int(result.get("recipient_count", 0)), str(result.get("reason", "")))
        return JSONResponse({"ok": True, **result})
    except Exception as exc:
        agent.memory.record_email_delivery(principal.tenant_id, "admin.test", principal.user_id, 1, False, 0, str(exc))
        raise HTTPException(status_code=502, detail=f"SMTP 测试失败: {exc}") from exc


@router.get("/api/admin/email-notifications/deliveries")
def admin_email_notification_deliveries(request: Request, tenant_id: str = "local-default", limit: int = 100):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_email_deliveries(tenant_id or None, limit)})


# ==================== P4 身份与工作区 ====================
@router.get("/api/auth/bootstrap-status")
def bootstrap_status():
    """Expose only whether the one-time first-run initialization is needed."""
    return JSONResponse({"setup_required": agent.memory.platform_admin_setup_required()})


@router.post("/api/auth/bootstrap")
def bootstrap_platform_admin(request: Request, data: dict = Body(...)):
    """One-time platform administrator activation for a newly installed instance."""
    if not agent.memory.platform_admin_setup_required():
        raise HTTPException(status_code=409, detail="平台管理员已初始化")
    try:
        user = agent.memory.bootstrap_platform_admin(
            data.get("email", ""), data.get("password", ""), data.get("display_name", ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    token = agent.memory.create_auth_session(
        user, ip_address=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    agent.memory.record_login_event(user["email"], True, "platform_bootstrap")
    return _attach_browser_session(JSONResponse({"ok": True, "user": user}), token, admin=True)


@router.post("/api/auth/register")
def register_user(request: Request, data: dict = Body(...)):
    try:
        if agent.memory.platform_admin_setup_required():
            raise HTTPException(status_code=503, detail="请先完成首次平台管理员初始化")
        invite_token = str(data.get("invite_token") or "")
        require_invite = os.environ.get("REQUIRE_INVITATION_REGISTRATION", "0").strip().lower() in {"1", "true", "yes", "on"}
        if invite_token:
            user = agent.memory.accept_workspace_invitation(
                invite_token, data.get("email", ""), data.get("password", ""), data.get("display_name", ""),
            )
        elif require_invite:
            raise HTTPException(status_code=403, detail="当前环境仅允许使用管理员邀请码注册")
        else:
            user = agent.memory.submit_user_registration(
                data.get("email", ""), data.get("password", ""), data.get("display_name", ""),
            )
        if user.get("status") == "pending_approval":
            return JSONResponse({"ok": True, "pending_approval": True, "user": user,
                                 "message": "注册信息已提交，等待工作区管理员审批。"})
        token = agent.memory.create_auth_session(
            user, ip_address=request.client.host if request.client else "",
            user_agent=request.headers.get("user-agent", ""),
        )
        agent.memory.record_login_event(user.get("email", ""), True, "register")
        return _attach_browser_session(JSONResponse({"ok": True, "token": token, "user": user}), token)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/auth/login")
def login_user(request: Request, data: dict = Body(...)):
    user = agent.memory.authenticate_user(data.get("email", ""), data.get("password", ""))
    if not user:
        agent.memory.record_login_event(data.get("email", ""), False, "invalid_credentials_or_locked")
        raise HTTPException(status_code=401, detail="邮箱或密码错误")
    token = agent.memory.create_auth_session(
        user, ip_address=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    agent.memory.record_login_event(user.get("email", ""), True, "password")
    return _attach_browser_session(JSONResponse({"ok": True, "token": token, "user": user}), token)


@router.post("/api/auth/admin-login")
def admin_login_user(request: Request, data: dict = Body(...)):
    """Authenticate only platform and organization administrators."""
    user = agent.memory.authenticate_user(data.get("email", ""), data.get("password", ""))
    if not user or user.get("role") not in {"platform_admin", "org_admin"}:
        agent.memory.record_login_event(data.get("email", ""), False, "invalid_admin_credentials")
        raise HTTPException(status_code=401, detail="管理员邮箱或密码错误")
    token = agent.memory.create_auth_session(
        user, ip_address=request.client.host if request.client else "",
        user_agent=request.headers.get("user-agent", ""),
    )
    agent.memory.record_login_event(user.get("email", ""), True, "admin_password")
    return _attach_browser_session(JSONResponse({"ok": True, "token": token, "user": user}), token, admin=True)


@router.post("/api/auth/logout")
def logout_user(request: Request):
    token = _request_session_token(request)
    if token:
        session = agent.memory.get_auth_session(token)
        agent.memory.revoke_auth_session(token)
        if session:
            agent.memory.log_audit(session["tenant_id"], session["user_id"], session["agent_id"],
                                   "auth.logout", "session", session["session_id"])
    response = JSONResponse({"ok": True})
    admin_context = uses_admin_session(request)
    response.delete_cookie(ADMIN_SESSION_COOKIE if admin_context else FRONT_SESSION_COOKIE, path="/")
    response.delete_cookie(ADMIN_CSRF_COOKIE if admin_context else "securenexus_csrf", path="/")
    return response


@router.post("/api/auth/password")
def change_current_password(request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        if not agent.memory.change_password(principal.user_id, data.get("current_password", ""), data.get("new_password", "")):
            raise HTTPException(status_code=400, detail="当前密码错误")
        agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                               "auth.password.change", "user", principal.user_id)
        return JSONResponse({"ok": True, "message": "密码已修改，请重新登录"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/auth/export")
def export_current_user_data(request: Request):
    principal = principal_from_request(request, agent.memory)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="需要登录")
    agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                           "user.data.export", "user", principal.user_id)
    return JSONResponse(agent.memory.export_user_data(principal.tenant_id, principal.user_id))


@router.post("/api/auth/deletion-request")
def request_current_account_deletion(request: Request, data: dict = Body(default={})):
    principal = principal_from_request(request, agent.memory)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        agent.memory.request_account_deletion(principal.user_id, data.get("reason", ""))
        return JSONResponse({"ok": True, "message": "注销申请已提交，账号已停用，数据将在保留期结束后清理。"})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/auth/me")
def current_user(request: Request):
    principal = principal_from_request(request, agent.memory)
    token = _request_session_token(request)
    session = agent.memory.get_auth_session(token) if token else None
    return JSONResponse({
        "tenant_id": principal.tenant_id, "user_id": principal.user_id,
        "agent_id": principal.agent_id, "role": principal.role,
        "authenticated": principal.authenticated,
        "email": (session or {}).get("email", ""),
        "display_name": (session or {}).get("display_name", ""),
        "language": agent.memory.get_user_language(principal.tenant_id, principal.user_id, principal.agent_id),
        "workspaces": agent.memory.list_user_workspaces(principal.user_id) if principal.authenticated else [],
        "agents": agent.memory.list_user_agents(principal.tenant_id, principal.user_id),
    })


@router.put("/api/auth/language")
def update_current_language(request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="需要登录")
    try:
        language = agent.memory.set_user_language(
            principal.tenant_id, principal.user_id, principal.agent_id, data.get("language", "zh-CN"),
        )
        return JSONResponse({"ok": True, "language": language})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/user-registrations")
def admin_list_user_registrations(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_pending_user_registrations()})


@router.get("/api/admin/security/login-locks")
def admin_list_login_locks(request: Request, include_expired: bool = False):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_login_locks(include_expired)})


@router.post("/api/admin/security/login-locks/unlock")
def admin_unlock_login(request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    email = str(data.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="请输入账号邮箱")
    if not agent.memory.unlock_login(email, principal.user_id):
        raise HTTPException(status_code=404, detail="该账号当前没有登录锁定记录")
    return JSONResponse({"ok": True})


@router.post("/api/admin/organizations")
def admin_create_organization(request: Request, data: dict = Body(...)):
    try:
        principal = require_platform_permission(request, agent.memory)
        return JSONResponse({"ok": True, "organization": agent.memory.create_organization(
            data.get("name", ""), principal.user_id,
        )})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/user-registrations/{user_id}/{action}")
def admin_review_user_registration(user_id: str, action: str, request: Request):
    principal = require_platform_permission(request, agent.memory)
    if action == "approve":
        result = agent.memory.approve_user_registration(user_id, principal.user_id)
        if not result:
            raise HTTPException(status_code=404, detail="待审核注册申请不存在")
        return JSONResponse({"ok": True, "user": result})
    if action == "reject":
        if not agent.memory.reject_user_registration(user_id, principal.user_id):
            raise HTTPException(status_code=404, detail="待审核注册申请不存在")
        return JSONResponse({"ok": True})
    raise HTTPException(status_code=400, detail="操作仅支持 approve 或 reject")

# ==================== P6-D2 站内通知中心 ====================
@router.get("/api/notifications")
def current_user_notifications(request: Request, status: str = "", limit: int = 50):
    principal = principal_from_request(request, agent.memory)
    return JSONResponse({
        "items": agent.memory.list_notifications(
            principal.tenant_id, principal.user_id, principal.agent_id, status=status, limit=limit,
        ),
        "summary": agent.memory.notification_summary(
            principal.tenant_id, principal.user_id, principal.agent_id,
        ),
    })


@router.put("/api/notifications/status")
def update_current_notifications(request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    try:
        updated = agent.memory.update_notifications_status(
            principal.tenant_id, data.get("ids") or [], data.get("status") or "",
            principal.user_id, admin=False,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    agent.memory.log_audit(
        principal.tenant_id, principal.user_id, principal.agent_id,
        "notification.status.update", "notification", "",
        {"status": data.get("status"), "count": updated},
    )
    return JSONResponse({"ok": True, "updated": updated})


@router.get("/api/admin/notifications")
def admin_notifications(request: Request, tenant_id: str = "", status: str = "", limit: int = 100):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({
        "items": agent.memory.list_notifications(
            tenant_id, status=status, limit=limit,
        ),
        "summary": agent.memory.notification_summary(tenant_id),
    })


@router.put("/api/admin/notifications/status")
def admin_update_notifications(request: Request, data: dict = Body(...)):
    principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    try:
        updated = agent.memory.update_notifications_status(
            tenant_id, data.get("ids") or [], data.get("status") or "", admin=True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    agent.memory.log_audit(
        tenant_id, principal.user_id, principal.agent_id,
        "notification.status.update", "notification", "",
        {"status": data.get("status"), "count": updated},
    )
    return JSONResponse({"ok": True, "updated": updated})

# ==================== P6-D1 SSO / 企业身份集成 ====================
def _sso_callback_html(result: dict) -> str:
    """Render a tiny callback page; the server sets the session cookie."""
    ok = bool(result.get("ok"))
    token = str(result.get("token") or "")
    error = str(result.get("error") or "").replace("<", "&lt;").replace(">", "&gt;")
    if ok and token:
        script = "window.location.href = '/';"
    else:
        message = error or "SSO 登录失败"
        script = (
            "alert(" + json.dumps(message) + "); window.location.href = '/';"
        )
    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        "<title>SSO 登录结果</title></head><body>"
        "<p>正在返回安枢 SecureNexus...</p><script>" + script + "</script>"
        "</body></html>"
    )


@router.get("/api/admin/sso/providers")
def admin_list_sso_providers(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_sso_providers(include_secrets=False)})


@router.get("/api/admin/sso/oidc-presets")
def admin_oidc_presets(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": get_oidc_presets()})


@router.post("/api/admin/sso/providers")
def admin_save_sso_provider(request: Request, data: dict = Body(...)):
    try:
        require_platform_permission(request, agent.memory)
        provider = agent.memory.save_sso_provider(data)
        return JSONResponse({"ok": True, "provider": provider})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/sso/providers/{provider_id}")
def admin_update_sso_provider(provider_id: str, request: Request, data: dict = Body(...)):
    require_platform_permission(request, agent.memory)
    if not agent.memory.get_sso_provider(provider_id):
        raise HTTPException(status_code=404, detail="SSO 配置不存在")
    try:
        provider = agent.memory.save_sso_provider(data, provider_id=provider_id)
        return JSONResponse({"ok": True, "provider": provider})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/admin/sso/providers/{provider_id}")
def admin_delete_sso_provider(provider_id: str, request: Request):
    require_platform_permission(request, agent.memory)
    if not agent.memory.delete_sso_provider(provider_id):
        raise HTTPException(status_code=404, detail="SSO 配置不存在")
    return JSONResponse({"ok": True})


@router.put("/api/admin/sso/providers/{provider_id}/status")
def admin_update_sso_provider_status(provider_id: str, request: Request, data: dict = Body(...)):
    require_platform_permission(request, agent.memory)
    if not agent.memory.update_sso_provider_status(provider_id, bool(data.get("enabled"))):
        raise HTTPException(status_code=404, detail="SSO 配置不存在")
    return JSONResponse({"ok": True})


@router.post("/api/admin/sso/providers/{provider_id}/test")
def admin_test_sso_provider(provider_id: str, request: Request):
    require_platform_permission(request, agent.memory)
    provider = agent.memory.get_sso_provider(provider_id, include_secrets=True)
    if not provider:
        raise HTTPException(status_code=404, detail="SSO 配置不存在")
    try:
        if provider["provider_type"] == "ldap":
            result = test_ldap_connection(provider["config"], provider.get("secrets") or {})
            return JSONResponse({"ok": True, "result": result})
        discovery = discover_oidc_provider(
            provider["config"]["issuer_url"],
            provider["config"].get("connect_timeout", 8),
        )
        return JSONResponse({"ok": True, "result": {
            "issuer": discovery.get("issuer", ""),
            "authorization_endpoint": bool(discovery.get("authorization_endpoint")),
            "token_endpoint": bool(discovery.get("token_endpoint")),
            "userinfo_endpoint": bool(discovery.get("userinfo_endpoint")),
        }})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/sso/approvals")
def admin_list_sso_approvals(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_sso_pending_approvals()})


@router.post("/api/admin/sso/approvals/{link_id}")
def admin_handle_sso_approval(link_id: str, request: Request, data: dict = Body(...)):
    require_platform_permission(request, agent.memory)
    action = str(data.get("action") or "")
    if action == "approve":
        identity = agent.memory.approve_sso_identity_link(
            link_id,
            str(data.get("default_role") or ""),
            str(data.get("default_tenant_id") or ""),
        )
        if not identity:
            raise HTTPException(status_code=404, detail="待审批身份不存在")
        return JSONResponse({"ok": True, "identity": identity})
    if action == "reject":
        if not agent.memory.reject_sso_identity_link(link_id):
            raise HTTPException(status_code=404, detail="待审批身份不存在")
        return JSONResponse({"ok": True})
    raise HTTPException(status_code=400, detail="action 仅支持 approve 或 reject")


@router.get("/api/auth/sso/providers")
def public_sso_providers():
    return JSONResponse({"items": agent.memory.list_sso_providers(enabled_only=True, include_secrets=False)})


@router.post("/api/auth/sso/oidc/authorize")
def sso_oidc_authorize(data: dict = Body(...)):
    provider_id = str(data.get("provider_id") or "")
    provider = agent.memory.get_sso_provider(provider_id, include_secrets=True)
    if not provider or not provider["enabled"] or provider["provider_type"] != "oidc":
        raise HTTPException(status_code=404, detail="OIDC 登录方式未启用")
    redirect_uri = str(data.get("redirect_uri") or "")
    configured_uri = provider["config"].get("redirect_uri", "")
    if redirect_uri and configured_uri and redirect_uri != configured_uri:
        raise HTTPException(status_code=400, detail="redirect_uri 与配置不一致")
    state = agent.memory.create_oauth_state(provider_id, redirect_uri or configured_uri)
    try:
        discovery = discover_oidc_provider(
            provider["config"]["issuer_url"],
            provider["config"].get("connect_timeout", 8),
        )
        provider["discovery"] = discovery
        authorize_url = build_oidc_authorize_url(provider, state)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse({"ok": True, "authorize_url": authorize_url, "state": state})


@router.get("/api/auth/sso/oidc/callback")
def sso_oidc_callback(code: str = "", state: str = "", error: str = "",
                      error_description: str = ""):
    if error:
        return HTMLResponse(
            _sso_callback_html({"ok": False, "error": f"{error} {error_description}".strip()}),
            status_code=400,
        )
    oauth_state = agent.memory.consume_oauth_state(state) if state else None
    if not oauth_state:
        return HTMLResponse(_sso_callback_html({"ok": False, "error": "OAuth state 无效或已过期"}), status_code=400)
    provider = agent.memory.get_sso_provider(oauth_state["provider_id"], include_secrets=True)
    if not provider or not provider["enabled"] or provider["provider_type"] != "oidc":
        return HTMLResponse(_sso_callback_html({"ok": False, "error": "OIDC 登录方式已停用"}), status_code=400)
    try:
        discovery = discover_oidc_provider(
            provider["config"]["issuer_url"],
            provider["config"].get("connect_timeout", 8),
        )
        provider["discovery"] = discovery
        userinfo = exchange_oidc_code(provider, provider.get("secrets") or {}, code)
        identity_fields = extract_oidc_identity(provider, userinfo)
        linked = agent.memory.link_sso_identity(
            provider["id"], identity_fields["subject"], identity_fields["email"],
            identity_fields["display_name"], bool(provider["auto_provision"]),
            provider["default_role"], provider["default_tenant_id"],
        )
    except Exception as exc:
        return HTMLResponse(_sso_callback_html({"ok": False, "error": str(exc)}), status_code=400)
    if linked.get("result") == "pending":
        _publish_event(event_bus, "approval.pending", {
            "tenant_id": provider.get("default_tenant_id") or "local-default",
            "notification_body": f"{identity_fields['email']} 正在等待 SSO 登录审批。",
            "provider_id": provider["id"], "email": identity_fields["email"],
        })
        return HTMLResponse(
            _sso_callback_html({"ok": False, "error": "账号待管理员审批，审批通过后即可使用 SSO 登录。"}),
            status_code=403,
        )
    try:
        token = agent.memory.create_sso_session(linked)
    except Exception as exc:
        return HTMLResponse(_sso_callback_html({"ok": False, "error": str(exc)}), status_code=500)
    return _attach_browser_session(HTMLResponse(_sso_callback_html({"ok": True, "user": linked})), token)


@router.post("/api/auth/sso/ldap/login")
def sso_ldap_login(data: dict = Body(...)):
    provider_id = str(data.get("provider_id") or "")
    provider = agent.memory.get_sso_provider(provider_id, include_secrets=True)
    if not provider or not provider["enabled"] or provider["provider_type"] != "ldap":
        raise HTTPException(status_code=404, detail="LDAP 登录方式未启用")
    try:
        identity_fields = authenticate_ldap(
            provider["config"], provider.get("secrets") or {},
            data.get("username", ""), data.get("password", ""),
        )
        linked = agent.memory.link_sso_identity(
            provider["id"], identity_fields["subject"], identity_fields["email"],
            identity_fields["display_name"], bool(provider["auto_provision"]),
            provider["default_role"], provider["default_tenant_id"],
        )
    except ValueError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if linked.get("result") == "pending":
        _publish_event(event_bus, "approval.pending", {
            "tenant_id": provider.get("default_tenant_id") or "local-default",
            "notification_body": f"{identity_fields['email']} 正在等待 SSO 登录审批。",
            "provider_id": provider["id"], "email": identity_fields["email"],
        })
        raise HTTPException(status_code=403, detail="账号待管理员审批，审批通过后即可使用 SSO 登录。")
    try:
        token = agent.memory.create_sso_session(linked)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return _attach_browser_session(JSONResponse({"ok": True, "token": token, "user": linked}), token)



@router.get("/api/auth/agents")
def current_user_agents(request: Request):
    principal = principal_from_request(request, agent.memory)
    if not principal.authenticated:
        return JSONResponse({"items": []})
    return JSONResponse({"items": agent.memory.list_user_agents(
        principal.tenant_id, principal.user_id,
    )})


@router.post("/api/auth/switch-agent")
def switch_current_agent(request: Request, data: dict = Body(...)):
    token = _request_session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="需要登录后才能切换 Agent")
    selected = agent.memory.switch_auth_session_agent(token, str(data.get("agent_id") or ""))
    if not selected:
        raise HTTPException(status_code=404, detail="Agent 不存在、已停用或未授权")
    return JSONResponse({"ok": True, "agent": selected})


@router.post("/api/auth/switch-workspace")
def switch_current_workspace(request: Request, data: dict = Body(...)):
    token = _request_session_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="需要登录后才能切换工作区")
    selected = agent.memory.switch_auth_session_workspace(token, str(data.get("tenant_id") or ""))
    if not selected:
        raise HTTPException(status_code=404, detail="工作区不存在、未授权或没有可用 Agent")
    return JSONResponse({"ok": True, "workspace": selected})


@router.get("/api/memories")
def current_user_memories(request: Request):
    principal = principal_from_request(request, agent.memory)
    return JSONResponse({
        "enabled": agent.memory.long_term_memory_enabled(
            principal.tenant_id, principal.user_id, principal.agent_id,
        ),
        "items": agent.memory.list_long_term_memories(
            principal.tenant_id, principal.user_id, principal.agent_id,
        ),
    })


@router.put("/api/memories/preferences")
def update_current_memory_preference(request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    agent.memory.set_long_term_memory_enabled(
        principal.tenant_id, principal.user_id, principal.agent_id, bool(data.get("enabled")),
    )
    return JSONResponse({"ok": True, "enabled": bool(data.get("enabled"))})


@router.put("/api/memories/{memory_id}")
def update_current_memory(request: Request, memory_id: int, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    try:
        if "content" in data:
            ok = agent.memory.update_long_term_memory_content(
                principal.tenant_id, principal.user_id, memory_id, data.get("content", ""),
            )
        else:
            ok = agent.memory.update_long_term_memory_status(
                principal.tenant_id, principal.user_id, memory_id, data.get("status", ""),
            )
        if not ok:
            raise HTTPException(status_code=404, detail="记忆不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/memories/{memory_id}")
def delete_current_memory(request: Request, memory_id: int):
    principal = principal_from_request(request, agent.memory)
    if not agent.memory.delete_long_term_memory(principal.tenant_id, principal.user_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return JSONResponse({"ok": True})


@router.get("/api/admin/workspaces")
def admin_list_workspaces(request: Request):
    principal, tenant_id = _tenant_admin_scope(request)
    workspaces = agent.memory.list_workspaces()
    if principal.role != "platform_admin":
        workspaces = [item for item in workspaces if item["id"] == tenant_id]
    return JSONResponse({"items": workspaces})


@router.get("/api/admin/audit-logs")
def admin_list_audit_logs(request: Request, tenant_id: str = "", action: str = "",
                          resource_type: str = "", limit: int = 200, reason: str = ""):
    principal = require_permission(request, agent.memory, "audit.read", tenant_id)
    resolved_tenant = principal.tenant_id if principal.role != "platform_admin" else str(tenant_id or principal.tenant_id)
    _audit_sensitive_access(principal, "audit_log", resolved_tenant, reason,
                            {"access": "list", "action_filter": action or "",
                             "resource_type_filter": resource_type or ""})
    return JSONResponse({"items": agent.memory.list_audit_logs(
        resolved_tenant, action, resource_type, limit,
    )})


@router.post("/api/admin/workspaces/{tenant_id}/agents")
def admin_create_agent(tenant_id: str, request: Request, data: dict = Body(...)):
    try:
        _, tenant_id = _tenant_admin_scope(request, tenant_id)
        return JSONResponse({"ok": True, "agent": agent.memory.create_agent(tenant_id, data.get("name", ""))})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/workspaces/{tenant_id}/members")
def admin_add_workspace_member(tenant_id: str, request: Request, data: dict = Body(...)):
    try:
        _, tenant_id = _tenant_admin_scope(request, tenant_id)
        member = agent.memory.add_workspace_member(
            tenant_id, data.get("email", ""), data.get("role", "user"),
        )
        return JSONResponse({"ok": True, "member": member})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/workspaces/{tenant_id}/members/{user_id}")
def admin_update_workspace_member(tenant_id: str, user_id: str, request: Request, data: dict = Body(...)):
    try:
        _, tenant_id = _tenant_admin_scope(request, tenant_id)
        if not agent.memory.update_membership(
            tenant_id, user_id, data.get("role", "user"), data.get("status", "active"),
        ):
            raise HTTPException(status_code=404, detail="组织成员不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/workspaces/{tenant_id}/agents/{agent_id}/members/{user_id}")
def admin_update_agent_member(tenant_id: str, agent_id: str, user_id: str, request: Request, data: dict = Body(...)):
    try:
        _, tenant_id = _tenant_admin_scope(request, tenant_id)
        if not agent.memory.update_agent_membership(
            tenant_id, agent_id, user_id, data.get("status", "active"), data.get("role", "user"),
        ):
            raise HTTPException(status_code=404, detail="Agent 授权关系不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/workspaces/{tenant_id}/agents/{agent_id}")
def admin_update_agent(tenant_id: str, agent_id: str, request: Request, data: dict = Body(...)):
    try:
        _, tenant_id = _tenant_admin_scope(request, tenant_id)
        if not agent.memory.update_agent_status(tenant_id, agent_id, data.get("status", "")):
            raise HTTPException(status_code=404, detail="Agent 不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/workspaces/{tenant_id}/users/{user_id}")
def admin_update_user(tenant_id: str, user_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, tenant_id)
        requested = data.get("status", "")
        membership_status = "active" if requested == "active" else "disabled"
        revoked = agent.memory.update_workspace_member_status(
            tenant_id, user_id, membership_status, return_revoked_count=True,
        )
        if revoked is None:
            raise HTTPException(status_code=404, detail="工作区成员不存在")
        agent.memory.log_audit(
            tenant_id, principal.user_id, principal.agent_id, "workspace.user.status.update", "user", user_id,
            {"status": membership_status, "revoked_sessions": revoked},
        )
        return JSONResponse({"ok": True, "revoked_sessions": revoked})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/workspaces/{tenant_id}/users/{user_id}/password-reset")
def admin_reset_user_password(tenant_id: str, user_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, tenant_id)
        new_password = str(data.get("new_password") or "")
        if not new_password:
            new_password = "Secure-" + secrets.token_urlsafe(8) + "9A"
        if not agent.memory.reset_user_password(tenant_id, user_id, new_password, principal.user_id):
            raise HTTPException(status_code=404, detail="用户不存在")
        return JSONResponse({"ok": True, "temporary_password": new_password})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/workspaces/{tenant_id}/users")
def admin_create_workspace_user(tenant_id: str, request: Request, data: dict = Body(...)):
    """Provision a new credentialed workspace user from the admin console."""
    try:
        principal, tenant_id = _tenant_admin_scope(request, tenant_id)
        user = agent.memory.create_workspace_user(
            tenant_id, data.get("email", ""), data.get("password", ""),
            data.get("display_name", ""), data.get("role", "user"),
            data.get("agent_id", ""), principal.user_id,
        )
        return JSONResponse({"ok": True, "user": user})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/workspaces/{tenant_id}/invitations")
def admin_create_workspace_invitation(tenant_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, tenant_id)
        invitation = agent.memory.create_workspace_invitation(
            tenant_id, data.get("email", ""), data.get("role", "user"), principal.user_id,
            data.get("agent_id", ""), data.get("expires_hours", 72),
        )
        return JSONResponse({"ok": True, "invitation": invitation})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/workspaces/{tenant_id}/invitations")
def admin_list_workspace_invitations(tenant_id: str, request: Request, status: str = "pending"):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_workspace_invitations(tenant_id, status)})


@router.put("/api/admin/workspaces/{tenant_id}/invitations/{invitation_id}/approve")
def admin_approve_workspace_invitation(tenant_id: str, invitation_id: str, request: Request):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    result = agent.memory.approve_workspace_invitation(tenant_id, invitation_id, principal.user_id)
    if not result:
        raise HTTPException(status_code=404, detail="待审批邀请不存在或已处理")
    return JSONResponse({"ok": True, "user": result})


@router.put("/api/admin/workspaces/{tenant_id}/invitations/{invitation_id}/revoke")
def admin_revoke_workspace_invitation(tenant_id: str, invitation_id: str, request: Request):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    if not agent.memory.revoke_workspace_invitation(tenant_id, invitation_id, principal.user_id):
        raise HTTPException(status_code=404, detail="可撤销邀请不存在或已处理")
    return JSONResponse({"ok": True})


@router.post("/api/admin/workspaces/{tenant_id}/invitations/{invitation_id}/resend")
def admin_resend_workspace_invitation(tenant_id: str, invitation_id: str, request: Request,
                                       data: dict = Body(default={} )):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    try:
        result = agent.memory.resend_workspace_invitation(
            tenant_id, invitation_id, principal.user_id, data.get("expires_hours", 72),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not result:
        raise HTTPException(status_code=404, detail="可重发邀请不存在或已被接受")
    return JSONResponse({"ok": True, "invitation": result})


@router.post("/api/admin/workspaces/{tenant_id}/users/{user_id}/sessions/revoke")
def admin_revoke_user_sessions(tenant_id: str, user_id: str, request: Request):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    revoked = agent.memory.revoke_user_auth_sessions(tenant_id, user_id)
    agent.memory.log_audit(
        tenant_id, principal.user_id, principal.agent_id, "workspace.user.sessions.revoke", "user", user_id,
        {"revoked_sessions": revoked},
    )
    return JSONResponse({"ok": True, "revoked_sessions": revoked})


@router.get("/api/admin/workspaces/{tenant_id}/deletion-requests")
def admin_list_account_deletion_requests(tenant_id: str, request: Request, status: str = "pending"):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_account_deletion_requests(tenant_id, status)})


@router.put("/api/admin/workspaces/{tenant_id}/deletion-requests/{user_id}/cancel")
def admin_cancel_account_deletion(tenant_id: str, user_id: str, request: Request):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    if not agent.memory.cancel_account_deletion(tenant_id, user_id, principal.user_id):
        raise HTTPException(status_code=404, detail="待处理的注销申请不存在")
    return JSONResponse({"ok": True})


@router.get("/api/admin/workspaces/{tenant_id}/users/{user_id}/memories")
def admin_list_user_memories(tenant_id: str, user_id: str, request: Request, agent_id: str = "", reason: str = ""):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    _audit_sensitive_access(principal, "long_term_memory", user_id, reason,
                            {"access": "list", "target_agent_id": agent_id or ""})
    return JSONResponse({"items": agent.memory.list_long_term_memories(
        tenant_id, user_id, agent_id or None,
    )})


@router.put("/api/admin/workspaces/{tenant_id}/users/{user_id}/memories/{memory_id}")
def admin_update_user_memory(tenant_id: str, user_id: str, memory_id: int, request: Request, data: dict = Body(...)):
    try:
        _, tenant_id = _tenant_admin_scope(request, tenant_id)
        if not agent.memory.update_long_term_memory_status(
            tenant_id, user_id, memory_id, data.get("status", ""),
        ):
            raise HTTPException(status_code=404, detail="记忆不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/admin/workspaces/{tenant_id}/users/{user_id}/memories/{memory_id}")
def admin_delete_user_memory(tenant_id: str, user_id: str, memory_id: int, request: Request):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    if not agent.memory.delete_long_term_memory(tenant_id, user_id, memory_id):
        raise HTTPException(status_code=404, detail="记忆不存在")
    return JSONResponse({"ok": True})


# ==================== 联网检索与威胁情报源 ====================
@router.get("/api/admin/external-retrieval/config")
def admin_get_external_retrieval_config(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse(agent.memory.get_external_retrieval_config())


@router.put("/api/admin/external-retrieval/config")
def admin_update_external_retrieval_config(request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    try:
        config = agent.memory.save_external_retrieval_config(
            data, principal.user_id,
        )
        agent.memory.log_audit(
            principal.tenant_id, principal.user_id, principal.agent_id,
            "external_retrieval.config.update", "external_retrieval", "config", config,
        )
        return JSONResponse({"ok": True, **config})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/external-retrieval/sources")
def admin_list_external_retrieval_sources(request: Request, include_disabled: bool = True):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_external_retrieval_sources(include_disabled)})


@router.post("/api/admin/external-retrieval/sources")
def admin_upsert_external_retrieval_source(request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    try:
        item = agent.memory.upsert_external_retrieval_source(
            data, principal.user_id,
        )
        return JSONResponse({"ok": True, "source": item})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/external-retrieval/sources/{source_id}/status")
def admin_update_external_retrieval_source_status(source_id: str, request: Request, data: dict = Body(...)):
    require_platform_permission(request, agent.memory)
    if not agent.memory.update_external_retrieval_source_status(
        source_id, data.get("enabled"), data.get("approved"),
    ):
        raise HTTPException(status_code=404, detail="外部来源不存在或没有状态变更")
    return {"ok": True}


@router.get("/api/admin/external-retrieval/events")
def admin_list_external_retrieval_events(request: Request, limit: int = 100):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_external_retrieval_events(limit)})


# ==================== 新需求：知识库治理基础 ====================
@router.get("/api/admin/knowledge-bases")
def admin_list_knowledge_bases(request: Request, tenant_id: str = "", include_archived: bool = False):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_knowledge_bases(
        tenant_id, include_archived=include_archived,
    )})


@router.post("/api/admin/knowledge-bases")
def admin_create_knowledge_base(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        visibility = str(data.get("visibility") or "tenant")
        if visibility == "public" and principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="只有平台管理员可以创建公共知识库")
        owner_user_id = str(data.get("owner_user_id") or "")
        if visibility == "private" and principal.role != "platform_admin":
            owner_user_id = principal.user_id
        item = agent.memory.create_knowledge_base(
            tenant_id=tenant_id,
            name=data.get("name", ""), description=data.get("description", ""),
            profile=data.get("profile", "general"), visibility=visibility,
            owner_user_id=owner_user_id,
        )
        return JSONResponse({"ok": True, "knowledge_base": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/knowledge-bases/{knowledge_base_id}/status")
def admin_update_knowledge_base_status(knowledge_base_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        kb = agent.memory.get_knowledge_base(knowledge_base_id, tenant_id)
        if not kb or (kb.get("visibility") == "public" and principal.role != "platform_admin"):
            raise HTTPException(status_code=404, detail="知识库不存在")
        if not agent.memory.update_knowledge_base_status(
            knowledge_base_id, data.get("status", ""), tenant_id,
        ):
            raise HTTPException(status_code=404, detail="知识库不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/knowledge-bases/{knowledge_base_id}/retrieval-config")
def admin_get_knowledge_base_retrieval_config(knowledge_base_id: str, request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    config = agent.memory.get_knowledge_base_retrieval_config(knowledge_base_id, tenant_id)
    if not config:
        raise HTTPException(status_code=404, detail="知识库不存在")
    return JSONResponse(config)


@router.put("/api/admin/knowledge-bases/{knowledge_base_id}/retrieval-config")
def admin_update_knowledge_base_retrieval_config(knowledge_base_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        kb = agent.memory.get_knowledge_base(knowledge_base_id, tenant_id)
        if not kb or (kb.get("visibility") == "public" and principal.role != "platform_admin"):
            raise HTTPException(status_code=404, detail="知识库不存在")
        config = agent.memory.update_knowledge_base_retrieval_config(
            knowledge_base_id, data.get("config") if isinstance(data.get("config"), dict) else data,
            tenant_id, principal.user_id,
            str(data.get("change_reason") or "后台调整检索配置"),
        )
        # 当前对话入口默认检索公共主干；公共知识库配置可即时作用于运行中的 Agent。
        # 工作区/私有知识库先保存版本，待会话携带 knowledge_base_id 后按库应用。
        if kb and kb.get("visibility") == "public":
            agent.top_k = config["config"]["top_k"]
            agent.use_rerank = config["config"]["use_rerank"]
            config["effective_runtime"] = True
        else:
            config["effective_runtime"] = False
        return JSONResponse({"ok": True, **config})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/knowledge-bases/{knowledge_base_id}/retrieval-config/versions")
def admin_list_knowledge_base_retrieval_config_versions(knowledge_base_id: str, request: Request, tenant_id: str = "", limit: int = 20):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_knowledge_base_retrieval_config_versions(
        knowledge_base_id, tenant_id, limit,
    )})


@router.get("/api/admin/knowledge-bases/{knowledge_base_id}/grants")
def admin_list_knowledge_base_grants(knowledge_base_id: str, request: Request,
                                     tenant_id: str = "", include_revoked: bool = False):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    if not agent.memory.get_knowledge_base(knowledge_base_id, tenant_id):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return JSONResponse({"items": agent.memory.list_knowledge_base_grants(
        knowledge_base_id, tenant_id, include_revoked,
    )})


@router.post("/api/admin/knowledge-bases/{knowledge_base_id}/grants")
def admin_grant_knowledge_base_access(knowledge_base_id: str, request: Request,
                                      data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        ok = agent.memory.grant_knowledge_base_access(
            knowledge_base_id, tenant_id, str(data.get("user_id") or ""),
            str(data.get("agent_id") or ""), str(data.get("role") or "viewer"), principal.user_id,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="知识库不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/admin/knowledge-bases/{knowledge_base_id}/grants")
def admin_revoke_knowledge_base_access(knowledge_base_id: str, request: Request,
                                       data: dict = Body(default={} )):
    principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    return JSONResponse({"ok": agent.memory.revoke_knowledge_base_access(
        knowledge_base_id, tenant_id, str(data.get("user_id") or ""),
        str(data.get("agent_id") or ""), principal.user_id,
    )})


@router.get("/api/admin/knowledge-bases/{knowledge_base_id}/documents")
def admin_list_knowledge_base_documents(knowledge_base_id: str, request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    if not agent.memory.get_knowledge_base(knowledge_base_id, tenant_id):
        raise HTTPException(status_code=404, detail="知识库不存在")
    return JSONResponse({"items": agent.memory.list_knowledge_base_documents(
        knowledge_base_id, tenant_id,
    )})


@router.put("/api/admin/documents/{document_id}/knowledge-base")
def admin_assign_document_knowledge_base(document_id: str, request: Request, data: dict = Body(...)):
    knowledge_base_id = str(data.get("knowledge_base_id") or "")
    if not knowledge_base_id:
        raise HTTPException(status_code=400, detail="缺少知识库 ID")
    _, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    if not agent.memory.assign_document_knowledge_base(
        document_id, knowledge_base_id, tenant_id,
    ):
        raise HTTPException(status_code=404, detail="文档或知识库不存在")
    return JSONResponse({"ok": True, "knowledge_base_id": knowledge_base_id})


@router.get("/api/admin/documents/{document_id}")
def admin_get_document(document_id: str, request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    document = agent.memory.get_document(document_id, tenant_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    return JSONResponse({"document": document})


@router.get("/api/admin/documents/{document_id}/versions")
def admin_list_document_versions(document_id: str, request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    if not agent.memory.get_document(document_id, tenant_id):
        raise HTTPException(status_code=404, detail="文档不存在")
    return JSONResponse({"items": agent.memory.list_document_versions(
        document_id, tenant_id,
    )})


@router.post("/api/admin/documents/{document_id}/versions/{version}/rollback")
def admin_rollback_document_version(document_id: str, version: int, request: Request,
                                     data: dict = Body(default={} )):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        document = agent.memory.rollback_document_version(
            document_id, version, tenant_id, principal.user_id,
            str(data.get("change_reason") or f"管理员回滚到 v{version}"),
        )
        if not document:
            raise HTTPException(status_code=404, detail="文档或版本不存在")
        return JSONResponse({"ok": True, "document": document, "rebuild_required": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/documents/expire")
def admin_expire_documents(request: Request, data: dict = Body(default={} )):
    principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    count = agent.memory.expire_documents(tenant_id, principal.user_id)
    return JSONResponse({"ok": True, "expired_count": count})


@router.put("/api/admin/documents/{document_id}/lifecycle")
def admin_update_document_lifecycle(document_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        document = agent.memory.get_document(document_id, tenant_id)
        if not document:
            raise HTTPException(status_code=404, detail="文档不存在")
        requested_status = data.get("lifecycle_status", "")
        if not agent.memory.update_document_lifecycle(
            document_id, requested_status, tenant_id,
            str(data.get("changed_by") or "admin"), str(data.get("change_reason") or ""),
        ):
            raise HTTPException(status_code=404, detail="文档不存在")
        if requested_status == "published" and document.get("status") == "staged":
            try:
                task_id = _queue_staged_document_processing(document, tenant_id or document.get("tenant_id", ""))
            except ValueError:
                agent.memory.update_document_lifecycle(
                    document_id, "review", tenant_id, "system", "待审核文件不存在，无法发布",
                )
                raise HTTPException(status_code=400, detail="待审核文件不存在，无法发布")
            return JSONResponse({"ok": True, "lifecycle_status": "published", "processing_task_id": task_id})
        return JSONResponse({"ok": True, "lifecycle_status": requested_status})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/data-sources")
def admin_list_data_sources(request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_data_sources(tenant_id)})


@router.post("/api/admin/data-sources")
def admin_create_data_source(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        owner_user_id = str(data.get("owner_user_id") or "")
        if owner_user_id and principal.role != "platform_admin":
            owner_user_id = principal.user_id
        source = agent.memory.create_data_source(
            tenant_id=tenant_id,
            name=data.get("name", ""), source_type=data.get("source_type", ""),
            endpoint=data.get("endpoint", ""), config=data.get("config") if isinstance(data.get("config"), dict) else {},
            knowledge_base_id=data.get("knowledge_base_id", ""),
            owner_user_id=owner_user_id, sync_mode=data.get("sync_mode", "manual"),
            schedule=data.get("schedule", ""),
        )
        return JSONResponse({"ok": True, "data_source": source})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/data-sources/{source_id}/status")
def admin_update_data_source_status(source_id: str, request: Request, data: dict = Body(...)):
    try:
        _, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        if not agent.memory.update_data_source_status(
            source_id, data.get("status", ""), tenant_id, data.get("error", ""),
        ):
            raise HTTPException(status_code=404, detail="数据源不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/data-sources/{source_id}/sync")
def admin_start_data_source_sync(source_id: str, request: Request, data: dict = Body(default={} )):
    _, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    source = agent.memory.get_data_source(source_id, tenant_id)
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    if source["source_type"] != "url":
        raise HTTPException(status_code=400, detail="当前仅支持 URL 数据源自动同步，其他类型请使用对应导入流程")
    validation = validate_data_source_config(
        source["source_type"], source["endpoint"], source["config"],
    )
    if not validation["valid"]:
        agent.memory.update_data_source_status(
            source_id, "error", tenant_id, "；".join(validation["errors"]),
        )
        raise HTTPException(status_code=400, detail={"message": "数据源安全校验失败", "errors": validation["errors"]})
    try:
        run = agent.memory.create_data_source_sync_run(
            source_id, tenant_id, data.get("trigger", "manual"),
        )
        threading.Thread(target=_run_data_source_sync, args=(run["id"],), daemon=True).start()
        return JSONResponse({"ok": True, "sync_run": run})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/data-sources/{source_id}/sync-runs")
def admin_list_data_source_sync_runs(source_id: str, request: Request, tenant_id: str = "", limit: int = 20):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    if not agent.memory.get_data_source(source_id, tenant_id):
        raise HTTPException(status_code=404, detail="数据源不存在")
    return JSONResponse({"items": agent.memory.list_data_source_sync_runs(
        source_id, tenant_id, limit,
    )})


@router.post("/api/admin/data-sources/sync-runs/{run_id}/retry")
def admin_retry_data_source_sync(run_id: str, request: Request, data: dict = Body(default={} )):
    try:
        _, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        run = agent.memory.retry_data_source_sync_run(run_id, tenant_id)
        if not run:
            raise HTTPException(status_code=404, detail="同步任务不存在")
        source = agent.memory.get_data_source(run["data_source_id"], tenant_id)
        if not source or source["source_type"] != "url":
            raise HTTPException(status_code=400, detail="当前仅支持 URL 数据源自动同步，其他类型请使用对应导入流程")
        threading.Thread(target=_run_data_source_sync, args=(run["id"],), daemon=True).start()
        return JSONResponse({"ok": True, "sync_run": run})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/data-sources/{source_id}/validate")
def admin_validate_data_source(source_id: str, request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    source = agent.memory.get_data_source(source_id, tenant_id)
    if not source:
        raise HTTPException(status_code=404, detail="数据源不存在")
    result = validate_data_source_config(source["source_type"], source["endpoint"], source["config"])
    return JSONResponse({"source_id": source_id, **result})


# ==================== P5 扩展中心 ====================
# 扩展必须先登记、审核并按工作区/Agent 授权；执行统一进入受控执行器，
# 禁止安装任意包、拼接 shell，MCP 凭证只能由服务端受控密钥引用提供。
@router.get("/api/admin/extensions")
def admin_list_extensions(request: Request, kind: str = ""):
    try:
        principal, _ = _tenant_admin_scope(request)
        agent.memory.ensure_builtin_capability_extensions()
        items = agent.memory.list_capability_extensions(kind)
        if principal.role != "platform_admin":
            # Organization administrators can only discover approved extensions.
            items = [item for item in items if item.get("status") == "approved"]
        return JSONResponse({"items": items})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/extensions")
def admin_register_extension(request: Request, data: dict = Body(...)):
    try:
        require_platform_permission(request, agent.memory)
        item = agent.memory.create_capability_extension(
            data.get("kind", ""), data.get("name", ""), data.get("version", ""),
            data.get("source", ""), data.get("description", ""),
            data.get("manifest") if isinstance(data.get("manifest"), dict) else {},
            data.get("permissions") if isinstance(data.get("permissions"), list) else [],
            data.get("network_scope", ""),
        )
        return JSONResponse({"ok": True, "extension": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/extensions/{extension_id}/review")
def admin_review_extension(extension_id: str, request: Request, data: dict = Body(...)):
    try:
        principal = require_platform_permission(request, agent.memory)
        if not agent.memory.review_capability_extension(
            extension_id, data.get("status", ""), principal.user_id,
        ):
            raise HTTPException(status_code=404, detail="扩展不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/extensions/{extension_id}/grants/{tenant_id}/{agent_id}")
def admin_update_extension_grant(extension_id: str, tenant_id: str, agent_id: str,
                                 request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, tenant_id)
        if not agent.memory.set_capability_extension_grant(
            extension_id, tenant_id, agent_id, bool(data.get("enabled")),
            principal.user_id,
        ):
            raise HTTPException(status_code=404, detail="扩展、工作区或 Agent 不存在或不可用")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/extensions/{extension_id}/usage")
def admin_extension_usage(extension_id: str, request: Request):
    principal, tenant_id = _tenant_admin_scope(request)
    usage = agent.memory.capability_extension_usage(
        extension_id, None if principal.role == "platform_admin" else tenant_id,
    )
    if usage is None:
        raise HTTPException(status_code=404, detail="扩展不存在")
    return JSONResponse({"ok": True, "usage": usage})


@router.post("/api/admin/extensions/{extension_id}/health")
def admin_extension_health(extension_id: str, request: Request):
    require_platform_permission(request, agent.memory)
    result = agent.memory.capability_extension_health(extension_id)
    if result is None:
        raise HTTPException(status_code=404, detail="扩展不存在")
    return JSONResponse({"ok": True, "health": result})


@router.post("/api/admin/extensions/{extension_id}/execute")
def admin_execute_extension(extension_id: str, request: Request, data: dict = Body(default={})):
    """Run an approved/granted extension through the controlled execution boundary."""
    try:
        principal = require_permission(
            request, agent.memory, "extension.execute",
            str(data.get("tenant_id") or ""),
        )
        tenant_id = principal.tenant_id
        agent_id = principal.agent_id
        if principal.role == "platform_admin":
            tenant_id = str(data.get("tenant_id") or tenant_id)
            agent_id = str(data.get("agent_id") or agent_id)
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        result = execute_capability(
            agent.memory, extension_id, tenant_id, agent_id,
            principal.user_id, principal.role, payload,
        )
        return JSONResponse({"ok": True, "result": result})
    except CapabilityExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/extensions/{extension_id}/calls")
def admin_record_extension_call(extension_id: str, request: Request, data: dict = Body(...)):
    """受控执行器的调用记录入口，不会触发任何扩展本身。"""
    try:
        require_platform_permission(request, agent.memory)
        recorded = agent.memory.record_capability_extension_call(
            extension_id, str(data.get("tenant_id", "")), str(data.get("agent_id", "")),
            data.get("status", "skipped"), data.get("duration_ms", 0), data.get("error", ""),
        )
        if not recorded:
            raise HTTPException(status_code=403, detail="扩展未审核通过或未获得该 Agent 授权")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/extensions/{extension_id}/upgrade")
def admin_upgrade_extension(extension_id: str, request: Request, data: dict = Body(...)):
    try:
        principal = require_platform_permission(request, agent.memory)
        item = agent.memory.update_capability_extension(
            extension_id, data.get("version", ""), data.get("source", ""), data.get("description", ""),
            data.get("manifest") if isinstance(data.get("manifest"), dict) else {},
            data.get("permissions") if isinstance(data.get("permissions"), list) else [],
            data.get("network_scope", ""), principal.user_id, data.get("reason", ""),
        )
        if not item:
            raise HTTPException(status_code=404, detail="扩展不存在")
        return JSONResponse({"ok": True, "extension": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/admin/extensions/{extension_id}")
def admin_uninstall_extension(extension_id: str, request: Request):
    principal = require_platform_permission(request, agent.memory)
    if not agent.memory.uninstall_capability_extension(extension_id, principal.user_id):
        raise HTTPException(status_code=404, detail="扩展不存在")
    return JSONResponse({"ok": True})


# ==================== P6-E2 知识图谱（候选抽取与人工审核） ====================
@router.post("/api/admin/knowledge-graph/extract")
def admin_extract_knowledge_graph(request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "graph.extract", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    document_id = str(data.get("document_id") or "")
    document = agent.memory.get_document(document_id, tenant_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在或不属于当前工作区")
    if document.get("lifecycle_status") not in {"published", "review"}:
        raise HTTPException(status_code=400, detail="仅可对待审核或已发布文档抽取图谱候选")
    try:
        run = extract_document_graph(agent.memory, document, principal.user_id)
        agent.memory.log_audit(tenant_id, principal.user_id, document.get("agent_id", ""),
                               "graph.extract", "document", document_id, {"run_id": run["id"]})
        return JSONResponse({"ok": True, "run": run})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/knowledge-graph")
def admin_list_knowledge_graph(request: Request, tenant_id: str = "local-default", knowledge_base_id: str = "",
                               status: str = "", limit: int = 500):
    principal = require_permission(request, agent.memory, "graph.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    allowed_statuses = {"", "pending_review", "approved", "rejected", "archived"}
    if status not in allowed_statuses:
        raise HTTPException(status_code=400, detail="图谱审核状态不合法")
    entities = agent.memory.list_graph_entities(tenant_id, knowledge_base_id, status, limit=limit)
    relations = agent.memory.list_graph_relations(tenant_id, knowledge_base_id, status, limit=limit)
    return JSONResponse({"entities": entities, "relations": relations,
                         "summary": {"entity_count": len(entities), "relation_count": len(relations),
                                     "status": status or "all"}})


@router.get("/api/admin/knowledge-graph/network")
def admin_knowledge_graph_network(request: Request, tenant_id: str = "local-default",
                                  knowledge_base_id: str = "", status: str = "approved",
                                  document_id: str = "", limit: int = 120,
                                  semantic_only: bool = True):
    principal = require_permission(request, agent.memory, "graph.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    if status not in {"", "pending_review", "approved", "rejected", "archived"}:
        raise HTTPException(status_code=400, detail="图谱状态不合法")
    neo4j_result = get_neo4j_graph_store().network(
        tenant_id, knowledge_base_id, status, limit, document_id, semantic_only,
    )
    if neo4j_result is not None:
        return JSONResponse(neo4j_result)
    relations = agent.memory.list_graph_relations(tenant_id, knowledge_base_id, status, limit=5000)
    if document_id:
        relations = [r for r in relations if str(r.get("source_document_id") or "") == document_id]
    if semantic_only:
        relations = [r for r in relations if is_semantic_graph_relation(r)]
    relations = relations[:max(1, min(int(limit), 500))]
    entity_ids = {str(r["subject_id"]) for r in relations} | {str(r["object_id"]) for r in relations}
    entities = [e for e in agent.memory.list_graph_entities(tenant_id, knowledge_base_id, "", limit=2000) if e["id"] in entity_ids]
    return JSONResponse({"nodes": [{"id": e["id"], "label": e["name"], "type": e["entity_type"], "status": e["status"]} for e in entities],
                         "edges": [{"id": r["id"], "source": r["subject_id"], "target": r["object_id"], "label": r["predicate"], "confidence": r["confidence"], "status": r["status"]} for r in relations],
                         "summary": {"node_count": len(entities), "edge_count": len(relations), "status": status or "all", "document_id": document_id}})


@router.get("/api/admin/knowledge-graph/neo4j/health")
def admin_knowledge_graph_neo4j_health(request: Request):
    require_permission(request, agent.memory, "graph.read", "")
    return JSONResponse(get_neo4j_graph_store().health())


@router.post("/api/admin/knowledge-graph/neo4j/sync")
def admin_knowledge_graph_neo4j_sync(request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "graph.review", str(data.get("tenant_id") or ""))
    tenant_id = str(data.get("tenant_id") or principal.tenant_id)
    if principal.role != "platform_admin":
        tenant_id = principal.tenant_id
    knowledge_base_id = str(data.get("knowledge_base_id") or "")
    if not knowledge_base_id:
        raise HTTPException(status_code=400, detail="缺少知识库 ID")
    try:
        return JSONResponse(get_neo4j_graph_store().sync_from_memory(
            agent.memory, tenant_id, knowledge_base_id, str(data.get("status") or "")))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Neo4j 同步失败: {str(exc)[:300]}") from exc


@router.get("/api/admin/knowledge-graph/review-assistant")
def admin_graph_review_assistant(request: Request, tenant_id: str = "local-default", knowledge_base_id: str = "", limit: int = 200):
    principal = require_permission(request, agent.memory, "graph.review", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    return JSONResponse(agent.memory.graph_review_recommendations(tenant_id, knowledge_base_id, limit))


@router.post("/api/admin/knowledge-graph/batch-review")
def admin_graph_batch_review(request: Request, data: dict = Body(...)):
    tenant_id_requested = str(data.get("tenant_id") or "")
    principal = require_permission(request, agent.memory, "graph.review", tenant_id_requested)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id_requested
    kind = str(data.get("kind") or "")
    status = str(data.get("status") or "")
    ids = data.get("ids") if isinstance(data.get("ids"), list) else []
    if not bool(data.get("confirm")):
        raise HTTPException(status_code=400, detail="批量审核必须明确 confirm=true")
    try:
        result = agent.memory.batch_update_graph_status(tenant_id, str(data.get("knowledge_base_id") or ""), kind, ids, status, principal.user_id)
        agent.memory.log_audit(tenant_id, principal.user_id, principal.agent_id, "graph.batch_review", f"graph_{kind}", "", result)
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/knowledge-graph/semantic-extract")
def admin_extract_semantic_knowledge_graph(request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "graph.extract", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    document_id = str(data.get("document_id") or "")
    document = agent.memory.get_document(document_id, tenant_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在或不属于当前工作区")
    try:
        result = extract_semantic_graph_candidates(agent.memory, document, getattr(agent, "llm", None), principal.user_id)
        return JSONResponse({"ok": True, "result": result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/knowledge-graph/entities/{entity_id}/status")
def admin_update_graph_entity(request: Request, entity_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "graph.review", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        if not agent.memory.update_graph_entity_status(entity_id, tenant_id, str(data.get("status") or ""), principal.user_id):
            raise HTTPException(status_code=404, detail="图谱实体不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/knowledge-graph/relations/{relation_id}/status")
def admin_update_graph_relation(request: Request, relation_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "graph.review", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        if not agent.memory.update_graph_relation_status(relation_id, tenant_id, str(data.get("status") or ""), principal.user_id):
            raise HTTPException(status_code=404, detail="图谱关系不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/knowledge-graph/entities/{entity_id}/impact")
def admin_graph_entity_impact(request: Request, entity_id: str, tenant_id: str = "local-default", knowledge_base_id: str = ""):
    principal = require_permission(request, agent.memory, "graph.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    return JSONResponse(graph_impact(agent.memory, tenant_id, entity_id, knowledge_base_id))


@router.get("/api/admin/knowledge-graph/conflicts")
def admin_scan_knowledge_graph_conflicts(request: Request, tenant_id: str = "local-default", knowledge_base_id: str = "", limit: int = 100):
    principal = require_permission(request, agent.memory, "graph.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    return JSONResponse(scan_graph_conflicts(agent.memory, tenant_id, knowledge_base_id, limit))


# ==================== P6-F 系统监控与问题报告 ====================
@router.get("/api/admin/operations/overview")
def admin_operations_overview(request: Request, tenant_id: str = "local-default"):
    principal = require_platform_permission(request, agent.memory)
    return JSONResponse(agent.memory.get_operations_overview(principal.tenant_id or tenant_id))


@router.get("/api/admin/search")
def admin_global_search(request: Request, q: str = "", tenant_id: str = ""):
    principal = require_permission(request, agent.memory, "tenant.manage", tenant_id)
    governance_store = _governance_store_for_current_memory()
    queries = governance_store.expand_search_terms(principal.tenant_id, q)
    items, seen = [], set()
    for query in queries:
        for item in agent.memory.global_admin_search(
            query, principal.tenant_id, include_platform_assets=principal.role == "platform_admin",
        ):
            key = (item.get("type"), item.get("id"))
            if key not in seen:
                items.append(item); seen.add(key)
    ranked = governance_store.rank_search_results(principal.tenant_id, items)[:100]
    governance_store.record_search_execution(principal.tenant_id, principal.user_id, q, len(queries), len(ranked))
    return JSONResponse({"query": q, "expanded_queries": queries, "items": ranked})


@router.get("/api/admin/integrations/status")
def admin_integrations_status(request: Request):
    """Show masked SSO/Langfuse readiness and approved data boundaries."""
    require_platform_permission(request, agent.memory)
    langfuse = agent.memory.get_langfuse_config()
    providers = agent.memory.list_sso_providers(include_secrets=False)
    return JSONResponse({
        "langfuse": {
            "enabled": bool(langfuse.get("enabled")),
            "host": langfuse.get("host", ""),
            "export_content": bool(langfuse.get("export_content")),
            "annotation_queue": langfuse.get("annotation_queue", ""),
            "data_boundary": "仅允许已审批的评测元数据；正文导出必须显式开启。",
        },
        "sso": [{"id": item.get("id"), "name": item.get("name"),
                 "provider_type": item.get("provider_type"), "enabled": bool(item.get("enabled")),
                 "has_secrets": bool(item.get("has_secrets")),
                 "data_boundary": "仅用于身份认证与工作区/角色映射，不导入对话或知识库正文。"}
                for item in providers],
    })


@router.get("/api/admin/monitoring/events")
def admin_monitoring_events(request: Request, tenant_id: str = "local-default", severity: str = "", limit: int = 100):
    principal = require_permission(request, agent.memory, "monitoring.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    if severity and severity not in {"P0", "P1", "P2", "P3"}:
        raise HTTPException(status_code=400, detail="监控事件级别不合法")
    return JSONResponse({"items": agent.memory.list_monitoring_events(tenant_id, severity, limit)})


@router.get("/api/admin/monitoring/summary")
def admin_monitoring_summary(request: Request, tenant_id: str = "local-default", limit: int = 1000):
    principal = require_permission(request, agent.memory, "monitoring.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    return JSONResponse(agent.memory.summarize_monitoring_events(tenant_id, limit))


@router.post("/api/admin/monitoring/cost-scan")
def admin_monitoring_cost_scan(request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.plan.edit", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        result = scan_cost_spike(
            agent.memory, tenant_id,
            data.get("multiplier"), data.get("minimum_cost"),
        )
        return JSONResponse({"ok": True, **result})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/monitoring/events")
def admin_create_monitoring_event(request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.plan.edit", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        event = record_event(
            agent.memory, tenant_id,
            str(data.get("event_type") or "manual_test"), str(data.get("severity") or "P2"),
            str(data.get("module") or "admin"), data.get("metrics") if isinstance(data.get("metrics"), dict) else {},
            str(data.get("trace_id") or ""), str(data.get("detail") or ""),
        )
        return JSONResponse({"ok": True, "event": event})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/monitoring/events/{event_id}/report")
def admin_create_monitoring_report(request: Request, event_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.plan.edit", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    event = agent.memory.get_monitoring_event(event_id, tenant_id)
    if not event:
        raise HTTPException(status_code=404, detail="监控事件不存在")
    report = build_issue_report(event, principal.user_id)
    return JSONResponse({"ok": True, "report": agent.memory.create_monitoring_issue_report(
        tenant_id, event_id, report, principal.user_id,
    )})


@router.post("/api/admin/monitoring/reports/{report_id}/diagnosis")
def admin_generate_monitoring_diagnosis(request: Request, report_id: str, data: dict = Body(default={} )):
    principal = require_permission(request, agent.memory, "monitoring.plan.edit",
                                   str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    report = agent.memory.get_monitoring_issue_report(report_id, tenant_id)
    if not report:
        raise HTTPException(status_code=404, detail="问题报告不存在")
    event = agent.memory.get_monitoring_event(report["event_id"], tenant_id)
    if not event:
        raise HTTPException(status_code=404, detail="关联监控事件不存在")
    llm = _get_backend_eval_llm()
    diagnosis = generate_issue_diagnosis(
        llm, event, report,
        usage_sink=lambda result, model: _record_auxiliary_llm_usage("monitoring_diagnosis", result, model, tenant_id),
    )
    updated = agent.memory.update_monitoring_issue_report_content(
        report_id, tenant_id, {"diagnosis": diagnosis,
                               "root_cause_hypothesis": diagnosis["root_cause_hypothesis"],
                               "recommendation": diagnosis["recommendation"]}, principal.user_id,
    )
    return JSONResponse({"ok": True, "diagnosis": diagnosis, "report": updated})


@router.get("/api/admin/monitoring/reports")
def admin_monitoring_reports(request: Request, tenant_id: str = "local-default", state: str = "", limit: int = 100):
    principal = require_permission(request, agent.memory, "monitoring.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    if state and state not in {"pending_review", "review_plan", "pending_approval", "approved", "rejected"}:
        raise HTTPException(status_code=400, detail="问题报告状态不合法")
    return JSONResponse({"items": agent.memory.list_monitoring_issue_reports(tenant_id, state, limit)})


@router.put("/api/admin/monitoring/reports/{report_id}/state")
def admin_update_monitoring_report(request: Request, report_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.review", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        report = agent.memory.update_monitoring_issue_report(
            report_id, tenant_id, str(data.get("state") or ""),
            principal.user_id, str(data.get("review_note") or ""),
        )
        return JSONResponse({"ok": True, "report": report})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/monitoring/reports/{report_id}")
def admin_update_monitoring_report_content(request: Request, report_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.plan.edit", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        report = agent.memory.update_monitoring_issue_report_content(
            report_id, tenant_id,
            data.get("report") if isinstance(data.get("report"), dict) else {},
            principal.user_id,
        )
        return JSONResponse({"ok": True, "report": report})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/monitoring/reports/{report_id}/verifications")
def admin_list_monitoring_verifications(request: Request, report_id: str,
                                        tenant_id: str = "local-default", limit: int = 50):
    principal = require_permission(request, agent.memory, "monitoring.read", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    if not agent.memory.get_monitoring_issue_report(report_id, tenant_id):
        raise HTTPException(status_code=404, detail="问题报告不存在")
    return JSONResponse({"items": agent.memory.list_monitoring_report_verifications(report_id, tenant_id, limit)})


@router.post("/api/admin/monitoring/reports/{report_id}/verifications")
def admin_create_monitoring_verification(request: Request, report_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.validation.run",
                                   str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        verification = agent.memory.create_monitoring_report_verification(
            report_id, tenant_id, str(data.get("result") or ""),
            data.get("checks") if isinstance(data.get("checks"), list) else [],
            str(data.get("note") or ""), principal.user_id,
        )
        return JSONResponse({"ok": True, "verification": verification})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/monitoring/reports/{report_id}/execute")
def admin_execute_monitoring_change(request: Request, report_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.change.execute",
                                   str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    report = agent.memory.get_monitoring_issue_report(report_id, tenant_id)
    if not report:
        raise HTTPException(status_code=404, detail="问题报告不存在")
    try:
        result = run_approved_change_action(report, "execute", {"tenant_id": tenant_id, "actor_id": principal.user_id, "report_id": report_id})
        agent.memory.log_audit(tenant_id, principal.user_id, "", "monitoring.change.execute",
                               "issue_report", report_id, {"adapter": result.get("adapter"), "status": result.get("status"), "side_effects": result.get("side_effects", False)})
        return JSONResponse({"ok": True, "result": result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/monitoring/reports/{report_id}/adapter/{action}")
def admin_run_monitoring_adapter_action(request: Request, report_id: str, action: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "monitoring.change.execute", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    report = agent.memory.get_monitoring_issue_report(report_id, tenant_id)
    if not report:
        raise HTTPException(status_code=404, detail="问题报告不存在")
    try:
        result = run_approved_change_action(report, action, {"tenant_id": tenant_id, "actor_id": principal.user_id, "report_id": report_id})
        agent.memory.log_audit(tenant_id, principal.user_id, "", f"monitoring.change.{action}", "issue_report", report_id,
                               {"adapter": result.get("adapter"), "status": result.get("status"), "side_effects": result.get("side_effects", False)})
        return JSONResponse({"ok": True, "result": result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


# ==================== P6-E1 可视化工作流 ====================
# Definitions are scoped to a tenant and Agent. A workflow cannot grant itself
# permission to execute an extension or access another workspace's data.
def _workflow_scope(request: Request, permission: str, tenant_id: str = "", agent_id: str = ""):
    """Resolve a workflow scope from the authenticated principal, never request data alone."""
    principal = require_permission(request, agent.memory, permission, tenant_id)
    scoped_tenant_id = str(tenant_id or principal.tenant_id) if principal.role == "platform_admin" else principal.tenant_id
    requested_agent_id = str(agent_id or "")
    if principal.role != "platform_admin":
        if requested_agent_id and requested_agent_id != principal.agent_id:
            raise HTTPException(status_code=403, detail="无权访问其他 Agent 的工作流")
        requested_agent_id = principal.agent_id
    return principal, scoped_tenant_id, requested_agent_id


@router.get("/api/admin/workflows/templates")
def admin_workflow_templates(request: Request, tenant_id: str = ""):
    principal, tenant_id, _ = _workflow_scope(request, "workflow.read", tenant_id, "")
    return JSONResponse({"items": agent.memory.list_workflow_templates(
        tenant_id, include_platform=principal.role == "platform_admin" or bool(tenant_id),
        viewer_id=principal.user_id,
    )})


@router.post("/api/admin/workflows/templates")
def admin_import_workflow_template(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id, _ = _workflow_scope(
            request, "workflow.write", str(data.get("tenant_id") or ""), "",
        )
        item = agent.memory.create_workflow_template(
            tenant_id, str(data.get("template_key") or "custom"),
            str(data.get("name") or ""), str(data.get("description") or ""),
            data.get("nodes") if isinstance(data.get("nodes"), list) else [],
            data.get("edges") if isinstance(data.get("edges"), list) else [],
            principal.user_id, str(data.get("visibility") or "private"),
            str(data.get("source_template_key") or ""),
        )
        return JSONResponse({"ok": True, "template": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/workflows/templates/{template_id}/copy")
def admin_copy_workflow_template(template_id: str, request: Request, data: dict = Body(default={} )):
    try:
        principal, tenant_id, agent_id = _workflow_scope(
            request, "workflow.write", str(data.get("tenant_id") or ""), str(data.get("agent_id") or ""),
        )
        if not agent_id:
            raise HTTPException(status_code=400, detail="复制模板必须指定 Agent")
        item = agent.memory.copy_workflow_template(
            template_id, tenant_id, agent_id, principal.user_id,
            str(data.get("name") or ""), str(data.get("description") or ""),
        )
        return JSONResponse({"ok": True, "workflow": item})
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/workflows/templates/{template_id}/share")
def admin_share_workflow_template(template_id: str, request: Request, data: dict = Body(default={} )):
    try:
        principal, tenant_id, _ = _workflow_scope(
            request, "workflow.write", str(data.get("tenant_id") or ""), "",
        )
        item = agent.memory.share_workflow_template(template_id, tenant_id, principal.user_id)
        return JSONResponse({"ok": True, "template": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/workflows")
def admin_list_workflows(request: Request, tenant_id: str = "", agent_id: str = ""):
    _, tenant_id, agent_id = _workflow_scope(request, "workflow.read", tenant_id, agent_id)
    return JSONResponse({"items": agent.memory.list_workflow_definitions(tenant_id, agent_id)})


@router.post("/api/admin/workflows")
def admin_create_workflow(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id, agent_id = _workflow_scope(
            request, "workflow.write", str(data.get("tenant_id") or ""), str(data.get("agent_id") or ""),
        )
        if not agent_id:
            raise HTTPException(status_code=400, detail="创建工作流必须指定 Agent")
        item = agent.memory.create_workflow_definition(
            tenant_id, agent_id,
            str(data.get("name") or ""), str(data.get("description") or ""),
            data.get("nodes") if isinstance(data.get("nodes"), list) else [],
            data.get("edges") if isinstance(data.get("edges"), list) else [],
            principal.user_id, str(data.get("change_note") or "从模板创建"),
        )
        return JSONResponse({"ok": True, "workflow": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/workflows/{workflow_id}")
def admin_get_workflow(workflow_id: str, request: Request, tenant_id: str = "", agent_id: str = ""):
    _, tenant_id, agent_id = _workflow_scope(request, "workflow.read", tenant_id, agent_id)
    item = agent.memory.get_workflow_definition(workflow_id, tenant_id, agent_id)
    if not item:
        raise HTTPException(status_code=404, detail="工作流不存在")
    return JSONResponse({"workflow": item, "versions": agent.memory.list_workflow_versions(workflow_id, tenant_id, agent_id)})


@router.post("/api/admin/workflows/{workflow_id}/versions")
def admin_save_workflow_version(workflow_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id, agent_id = _workflow_scope(
            request, "workflow.write", str(data.get("tenant_id") or ""), str(data.get("agent_id") or ""),
        )
        item = agent.memory.save_workflow_version(
            workflow_id, tenant_id, agent_id,
            data.get("nodes") if isinstance(data.get("nodes"), list) else [],
            data.get("edges") if isinstance(data.get("edges"), list) else [],
            principal.user_id, str(data.get("change_note") or ""),
        )
        return JSONResponse({"ok": True, "workflow": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/workflows/{workflow_id}/publish")
def admin_publish_workflow(workflow_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id, agent_id = _workflow_scope(
            request, "workflow.write", str(data.get("tenant_id") or ""), str(data.get("agent_id") or ""),
        )
        ok = agent.memory.publish_workflow_version(
            workflow_id, tenant_id, agent_id,
            int(data.get("version") or 0), principal.user_id,
        )
        if not ok:
            raise HTTPException(status_code=404, detail="工作流或版本不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _execute_admin_workflow(workflow_id: str, data: dict, principal, tenant_id: str, agent_id: str,
                            resume_run_id: str = "") -> dict:
    if resume_run_id:
        run = agent.memory.get_workflow_run(resume_run_id, tenant_id, agent_id)
        if not run:
            raise HTTPException(status_code=404, detail="工作流运行不存在")
        if run["workflow_id"] != workflow_id or run["status"] != "awaiting_approval":
            raise HTTPException(status_code=400, detail="该运行当前不可审批继续")
        completed = execute_workflow(
            agent.memory, run, approval=True, agent=agent,
            notifier=lambda event_type, payload: publish_system_event(agent.memory, event_bus, event_type, payload),
        )
        agent.memory.log_audit(tenant_id, principal.user_id, agent_id,
                               "workflow.approve", "workflow_run", resume_run_id)
    else:
        workflow = agent.memory.get_workflow_definition(workflow_id, tenant_id, agent_id)
        if not workflow or not workflow.get("published_version"):
            raise HTTPException(status_code=400, detail="请先发布工作流版本后再运行")
        run = agent.memory.create_workflow_run(
            workflow_id, tenant_id, agent_id, int(workflow["published_version"]),
            {"query": str(data.get("query") or ""), "fields": data.get("fields") or {},
             "user_id": str(data.get("user_id") or "") if principal.role == "platform_admin" else principal.user_id},
            principal.user_id,
        )
        completed = execute_workflow(
            agent.memory, run, agent=agent,
            notifier=lambda event_type, payload: publish_system_event(agent.memory, event_bus, event_type, payload),
        )
        agent.memory.log_audit(tenant_id, principal.user_id, agent_id,
                               "workflow.run", "workflow_run", run["id"], {"version": run["version"]})
    if completed["status"] == "completed":
        _publish_event(event_bus, "workflow.completed", {
            "tenant_id": tenant_id, "agent_id": agent_id, "workflow_id": workflow_id,
            "run_id": completed["id"], "link_path": "/admin?tab=workflows",
            "notification_title": "工作流运行已完成",
            "notification_body": "工作流运行已完成，可查看节点 Trace 和输出结果。",
        })
    return completed


@router.post("/api/admin/workflows/{workflow_id}/runs")
def admin_run_workflow(workflow_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id, agent_id = _workflow_scope(
            request, "workflow.execute", str(data.get("tenant_id") or ""), str(data.get("agent_id") or ""),
        )
        return JSONResponse({"ok": True, "run": _execute_admin_workflow(workflow_id, data, principal, tenant_id, agent_id)})
    except HTTPException:
        raise
    except (ValueError, CapabilityExecutionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/workflows/{workflow_id}/runs/{run_id}/approve")
def admin_approve_workflow_run(workflow_id: str, run_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id, agent_id = _workflow_scope(
            request, "workflow.execute", str(data.get("tenant_id") or ""), str(data.get("agent_id") or ""),
        )
        return JSONResponse({"ok": True, "run": _execute_admin_workflow(workflow_id, data, principal, tenant_id, agent_id, run_id)})
    except HTTPException:
        raise
    except (ValueError, CapabilityExecutionError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/workflows/runs/list")
def admin_list_workflow_runs(request: Request, tenant_id: str = "", agent_id: str = "", limit: int = 30):
    _, tenant_id, agent_id = _workflow_scope(request, "workflow.read", tenant_id, agent_id)
    return JSONResponse({"items": agent.memory.list_workflow_runs(tenant_id, agent_id, limit)})


@router.get("/api/admin/workflows/runs/{run_id}/traces")
def admin_workflow_run_traces(run_id: str, request: Request, tenant_id: str = "", agent_id: str = ""):
    _, tenant_id, agent_id = _workflow_scope(request, "workflow.read", tenant_id, agent_id)
    run = agent.memory.get_workflow_run(run_id, tenant_id, agent_id)
    if not run:
        raise HTTPException(status_code=404, detail="工作流运行不存在")
    return JSONResponse({"run": run, "traces": agent.memory.list_workflow_run_traces(run_id, tenant_id, agent_id)})


# ==================== P6-E3 成本与模型路由建议 ====================
@router.get("/api/admin/model-pricing")
def admin_list_model_pricing(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_model_pricing()})


@router.get("/api/admin/llm-usage-events")
def admin_llm_usage_events(request: Request, tenant_id: str = "", module: str = "", limit: int = 1000,
                           reason: str = ""):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    _audit_sensitive_access(principal, "llm_usage", tenant_id, reason,
                            {"access": "list", "module": module or "", "limit": min(int(limit or 1000), 10000)})
    return JSONResponse({"items": agent.memory.list_llm_usage_events(tenant_id, module, limit)})


@router.post("/api/admin/model-pricing")
def admin_save_model_pricing(request: Request, data: dict = Body(...)):
    try:
        require_platform_permission(request, agent.memory)
        return JSONResponse({"ok": True, "pricing": agent.memory.upsert_model_pricing(data)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/model-pricing/{pricing_id}/status")
def admin_update_model_pricing_status(pricing_id: str, request: Request, data: dict = Body(...)):
    require_platform_permission(request, agent.memory)
    if not agent.memory.update_model_pricing_status(pricing_id, bool(data.get("enabled"))):
        raise HTTPException(status_code=404, detail="模型价格配置不存在")
    return JSONResponse({"ok": True})


@router.post("/api/memories/profile-proposals")
def propose_current_user_profile(request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "memory.profile.propose")
    try:
        proposal = propose_user_profile(
            agent.memory, _get_backend_eval_llm(), principal.tenant_id, principal.user_id,
            principal.agent_id, str(data.get("statement") or ""),
            str(data.get("source_conversation_id") or ""),
            usage_sink=lambda result, model: _record_auxiliary_llm_usage("memory_profile_proposal", result, model, principal.tenant_id),
        )
        return JSONResponse({"ok": True, "proposal": proposal})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/memories/conflict-proposals")
def propose_current_memory_conflict(request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "memory.conflict.propose")
    try:
        proposal = propose_memory_conflict(
            agent.memory, _get_backend_eval_llm(), principal.tenant_id, principal.user_id,
            principal.agent_id, int(data.get("existing_memory_id")), str(data.get("statement") or ""),
            usage_sink=lambda result, model: _record_auxiliary_llm_usage("memory_conflict", result, model, principal.tenant_id),
        )
        return JSONResponse({"ok": True, "proposal": proposal})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/memory/profile-proposals")
def admin_list_memory_profile_proposals(request: Request, tenant_id: str = "local-default",
                                        user_id: str = "", status: str = "", limit: int = 100):
    principal = require_permission(request, agent.memory, "memory.profile.review", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    return JSONResponse({"items": agent.memory.list_memory_profile_proposals(tenant_id, user_id, status, limit)})


@router.put("/api/admin/memory/profile-proposals/{proposal_id}")
def admin_review_memory_profile_proposal(request: Request, proposal_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "memory.profile.review", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        proposal = agent.memory.review_memory_profile_proposal(
            proposal_id, tenant_id, principal.user_id, str(data.get("status") or ""),
            str(data.get("review_note") or ""),
        )
        return JSONResponse({"ok": True, "proposal": proposal})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/memory/conflict-proposals")
def admin_list_memory_conflict_proposals(request: Request, tenant_id: str = "local-default",
                                         user_id: str = "", status: str = "", limit: int = 100):
    principal = require_permission(request, agent.memory, "memory.conflict.review", tenant_id)
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else tenant_id
    return JSONResponse({"items": agent.memory.list_memory_conflict_proposals(tenant_id, user_id, status, limit)})


@router.put("/api/admin/memory/conflict-proposals/{proposal_id}")
def admin_review_memory_conflict_proposal(request: Request, proposal_id: str, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "memory.conflict.review", str(data.get("tenant_id") or ""))
    tenant_id = principal.tenant_id if principal.role != "platform_admin" else str(data.get("tenant_id") or "local-default")
    try:
        proposal = agent.memory.review_memory_conflict_proposal(
            proposal_id, tenant_id, principal.user_id, str(data.get("status") or ""),
            str(data.get("review_note") or ""),
        )
        return JSONResponse({"ok": True, "proposal": proposal})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/model-usage-cost")
def admin_model_usage_cost(request: Request, tenant_id: str = "", limit: int = 10000, reason: str = ""):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    _audit_sensitive_access(principal, "usage_cost", tenant_id, reason,
                            {"access": "summary", "limit": min(int(limit or 10000), 10000)})
    return JSONResponse(agent.memory.get_usage_cost_summary(tenant_id, limit))


@router.get("/api/admin/quota-budgets")
def admin_list_quota_budgets(request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_quota_budgets(tenant_id)})


@router.get("/api/admin/model-usage-cost/analysis")
def admin_model_usage_cost_analysis(request: Request, tenant_id: str = "", limit: int = 10000):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    _audit_sensitive_access(principal, "usage_cost", tenant_id, "费用分析", {"access": "analysis"})
    return JSONResponse(agent.memory.get_usage_cost_analysis(tenant_id, limit))


@router.get("/api/usage/me")
def current_user_usage(request: Request, limit: int = 10000):
    principal = principal_from_request(request, agent.memory)
    if not principal.authenticated:
        raise HTTPException(status_code=401, detail="请先登录")
    return JSONResponse(agent.memory.get_user_usage_summary(principal.tenant_id, principal.user_id, limit))


@router.get("/api/admin/departments/costs")
def admin_department_costs(request: Request, tenant_id: str = "", limit: int = 10000):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    _audit_sensitive_access(principal, "department_cost", tenant_id, "部门费用视图", {"access": "summary"})
    return JSONResponse({"items": agent.memory.list_department_costs(tenant_id, limit)})


@router.post("/api/admin/departments")
def admin_create_department(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        department = agent.memory.create_department(tenant_id, data.get("name", ""), data.get("cost_center", ""), principal.user_id)
        return JSONResponse({"ok": True, "department": department})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/departments/{department_id}/members")
def admin_assign_department_member(department_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
    user_id = str(data.get("user_id") or "")
    if not user_id or not agent.memory.assign_user_department(tenant_id, department_id, user_id, principal.user_id):
        raise HTTPException(status_code=404, detail="部门或用户不存在")
    return JSONResponse({"ok": True})


@router.get("/api/admin/model-usage-cost/export")
def admin_model_usage_cost_export(request: Request, tenant_id: str = "", limit: int = 10000):
    principal, tenant_id = _tenant_admin_scope(request, tenant_id)
    _audit_sensitive_access(principal, "usage_cost", tenant_id, "费用导出", {"access": "export"})
    items = agent.memory.list_llm_usage_events(tenant_id, limit=limit)
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(["时间", "租户", "用户", "Agent", "模块", "提供商", "模型", "输入Token", "输出Token", "总Token", "估算费用", "计价状态"])
    for item in items:
        writer.writerow([item.get("created_at", ""), item.get("tenant_id", ""), item.get("user_id", ""),
                         item.get("agent_id", ""), item.get("module", ""), item.get("provider", ""),
                         item.get("model", ""), item.get("prompt_tokens", 0), item.get("completion_tokens", 0),
                         item.get("total_tokens", 0), item.get("estimated_cost", ""), item.get("pricing_status", "")])
    return Response(content=output.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": "attachment; filename=securenexus-usage.csv"})


@router.post("/api/admin/quota-budgets")
def admin_save_quota_budget(request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _tenant_admin_scope(request, str(data.get("tenant_id") or ""))
        scope_type = str(data.get("scope_type") or "tenant")
        if scope_type == "platform" and principal.role != "platform_admin":
            raise HTTPException(status_code=403, detail="只有平台管理员可以配置平台预算")
        scope_id = str(data.get("scope_id") or (tenant_id if scope_type == "tenant" else principal.user_id))
        budget = agent.memory.save_quota_budget(
            scope_type, scope_id, str(data.get("period") or "monthly"),
            data.get("token_limit"), data.get("cost_limit"),
            float(data.get("warn_percent", 80)), tenant_id, principal.user_id,
        )
        return JSONResponse({"ok": True, "budget": budget})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/model-routing/recommend")
def admin_recommend_model_route(request: Request, mode: str = "chat", quality_floor: float = 0.0,
                                prompt_tokens: int = 0, completion_tokens: int = 0,
                                budget_limit: float | None = None, budget_used: float = 0.0,
                                provider: str = "", model: str = ""):
    require_platform_permission(request, agent.memory)
    allowed = {"chat", "writing", "presentation", "complex_analysis"}
    if mode not in allowed:
        raise HTTPException(status_code=400, detail="路由模式不合法")
    return JSONResponse(agent.memory.recommend_model_route(
        mode, quality_floor, prompt_tokens, completion_tokens, budget_limit, budget_used, provider, model,
    ))


@router.get("/api/admin/reflection-rules")
def admin_list_reflection_rules(request: Request, status: str = ""):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_reflection_rules(status)})


@router.post("/api/admin/reflection-rules")
def admin_save_reflection_rule(request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    try:
        payload = {**data, "created_by": principal.user_id}
        return JSONResponse({"ok": True, "rule": agent.memory.save_reflection_rule(payload)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/reflection-rules/{rule_id}")
def admin_update_reflection_rule(rule_id: str, request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    try:
        payload = {**data, "created_by": principal.user_id}
        return JSONResponse({"ok": True, "rule": agent.memory.save_reflection_rule(payload, rule_id)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/reflection-rules/{rule_id}/versions")
def admin_list_reflection_rule_versions(rule_id: str, request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_reflection_rule_versions(rule_id)})


@router.post("/api/admin/reflection-rules/{rule_id}/rollback")
def admin_rollback_reflection_rule(rule_id: str, request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    try:
        version = int(data.get("version"))
        rule = agent.memory.restore_reflection_rule_version(
            rule_id, version, principal.user_id,
        )
        return JSONResponse({"ok": True, "rule": rule})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/reflection-runs")
def admin_list_reflection_runs(request: Request, tenant_id: str = "", limit: int = 100):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_reflection_runs(tenant_id, limit)})


@router.get("/api/admin/reflection-summary")
def admin_reflection_summary(request: Request, tenant_id: str = ""):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse(agent.memory.get_reflection_summary(tenant_id))


# ==================== 对话管理 ====================
@router.get("/api/conversations")
def list_conversations(request: Request, include_deleted: bool = False, include_test: bool = False, jailbreak: str = "all"):
    principal = principal_from_request(request, agent.memory)
    convs = agent.memory.get_conversations(
        include_deleted=include_deleted, include_test=include_test, jailbreak=jailbreak,
        tenant_id=principal.tenant_id, user_id=principal.user_id, agent_id=principal.agent_id,
    )
    return JSONResponse(convs)


@router.post("/api/conversations")
def new_conversation(request: Request, data: dict = Body(default={} )):
    principal = principal_from_request(request, agent.memory)
    conv = agent.memory.create_conversation(
        tenant_id=principal.tenant_id, user_id=principal.user_id, agent_id=principal.agent_id,
        knowledge_base_id=str(data.get("knowledge_base_id") or ""),
    )
    agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                           "conversation.create", "conversation", conv["id"])
    return JSONResponse(conv)


@router.put("/api/conversations/{conv_id}")
def rename_conversation(conv_id: str, request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    if not agent.memory.conversation_belongs_to(conv_id, principal.tenant_id, principal.user_id, principal.agent_id):
        raise HTTPException(status_code=404, detail="对话不存在")
    title = (data.get("title") or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="标题不能为空")
    agent.memory.update_title(conv_id, title)
    return JSONResponse({"ok": True, "title": title})


@router.put("/api/conversations/{conv_id}/jailbreak-status")
def set_jailbreak_status(conv_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _admin_conversation_scope(request, conv_id)
    status = data.get("status", "").strip()
    if status not in ("false_alarm", "handled"):
        raise HTTPException(status_code=400, detail="无效状态，仅支持 false_alarm 或 handled")
    agent.memory.update_jailbreak_status(conv_id, status)
    agent.memory.log_audit(
        tenant_id, principal.user_id, principal.agent_id,
        "conversation.jailbreak_status.update", "conversation", conv_id,
        {"status": status},
    )
    return JSONResponse({"ok": True, "status": status})


@router.get("/api/conversations/{conv_id}/jailbreak-report")
def jailbreak_report(conv_id: str, request: Request, reason: str = ""):
    principal, _ = _admin_conversation_scope(request, conv_id)
    _audit_sensitive_access(principal, "conversation", conv_id, reason,
                            {"access": "jailbreak_report"})
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

    report_html = f"""<html>
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
    {trace_html}
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
    return HTMLResponse(content=report_html, headers={"Content-Disposition": disp})


@router.delete("/api/conversations/{conv_id}")
def delete_conversation(conv_id: str, request: Request):
    principal = principal_from_request(request, agent.memory)
    if not agent.memory.conversation_belongs_to(conv_id, principal.tenant_id, principal.user_id, principal.agent_id):
        raise HTTPException(status_code=404, detail="对话不存在")
    agent.memory.delete_conversation(conv_id)
    agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                           "conversation.soft_delete", "conversation", conv_id)
    return JSONResponse({"ok": True, "soft_delete": True})


@router.delete("/api/conversations/{conv_id}/hard")
def hard_delete_conversation(conv_id: str, request: Request, reason: str = ""):
    principal, tenant_id = _admin_conversation_scope(request, conv_id)
    if not agent.memory.hard_delete_conversation(conv_id):
        raise HTTPException(status_code=404, detail="对话不存在")
    agent.memory.log_audit(tenant_id, principal.user_id, principal.agent_id,
                           "sensitive.delete", "conversation", conv_id,
                           {"reason": str(reason or "管理端删除")[:300]})
    return JSONResponse({"ok": True})


@router.get("/api/conversations/{conv_id}/messages")
def get_messages(conv_id: str, request: Request):
    principal = principal_from_request(request, agent.memory)
    if not agent.memory.conversation_belongs_to(conv_id, principal.tenant_id, principal.user_id, principal.agent_id):
        raise HTTPException(status_code=404, detail="对话不存在")
    return JSONResponse(agent.memory.get_history(conv_id))


@router.get("/api/conversations/detail")
def get_conversation_detail(conv_id: str, request: Request, reason: str = ""):
    principal, _ = _admin_conversation_scope(request, conv_id)
    detail = agent.memory.get_conversation_detail(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    _audit_sensitive_access(principal, "conversation", conv_id, reason,
                            {"access": "detail"})
    return JSONResponse(detail)


@router.get("/api/conversations/stats")
def get_conversation_stats(conv_id: str, request: Request, reason: str = ""):
    principal, _ = _admin_conversation_scope(request, conv_id)
    detail = agent.memory.get_conversation_detail(conv_id)
    if detail is None:
        raise HTTPException(status_code=404, detail="对话不存在")
    _audit_sensitive_access(principal, "conversation", conv_id, reason,
                            {"access": "stats"})

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


# ==================== P6-D3 团队协作 ====================
@router.get("/api/admin/conversations/{conv_id}/shares")
def admin_list_conversation_shares(conv_id: str, request: Request, tenant_id: str = ""):
    _, tenant_id = _admin_conversation_scope(request, conv_id, tenant_id)
    return JSONResponse({"items": agent.memory.list_conversation_shares(tenant_id, conv_id)})


@router.post("/api/admin/conversations/{conv_id}/shares")
def admin_create_conversation_share(conv_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _admin_conversation_scope(request, conv_id, str(data.get("tenant_id") or ""))
    try:
        share = agent.memory.create_conversation_share(
            tenant_id, conv_id, principal.user_id,
            int(data.get("expires_hours") or 24), str(data.get("password") or ""),
            bool(data.get("allow_copy", True)),
        )
        return JSONResponse({"ok": True, "share": share})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/conversations/{conv_id}/shares/{share_id}")
def admin_update_conversation_share(conv_id: str, share_id: str, request: Request, data: dict = Body(...)):
    _, tenant_id = _admin_conversation_scope(request, conv_id, str(data.get("tenant_id") or ""))
    try:
        ok = agent.memory.update_conversation_share_status(
            tenant_id, conv_id, share_id, str(data.get("status") or ""),
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not ok:
        raise HTTPException(status_code=404, detail="分享记录不存在")
    return JSONResponse({"ok": True})


@router.get("/api/admin/conversations/{conv_id}/notes")
def admin_list_conversation_notes(conv_id: str, request: Request, tenant_id: str = ""):
    _, tenant_id = _admin_conversation_scope(request, conv_id, tenant_id)
    return JSONResponse({"items": agent.memory.list_conversation_notes(tenant_id, conv_id)})


@router.post("/api/admin/conversations/{conv_id}/notes")
def admin_create_conversation_note(conv_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _admin_conversation_scope(request, conv_id, str(data.get("tenant_id") or ""))
    try:
        note = agent.memory.create_conversation_note(
            tenant_id, conv_id, principal.user_id,
            str(data.get("content") or ""), principal.role,
        )
        return JSONResponse({"ok": True, "note": note})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/conversations/{conv_id}/revisions")
def admin_list_answer_revisions(conv_id: str, request: Request, tenant_id: str = "",
                                message_id: int | None = None):
    _, tenant_id = _admin_conversation_scope(request, conv_id, tenant_id)
    return JSONResponse({"items": agent.memory.list_answer_revisions(tenant_id, conv_id, message_id)})


@router.get("/api/admin/conversations/{conv_id}/revision-drafts")
def admin_list_answer_revision_drafts(conv_id: str, request: Request, tenant_id: str = "",
                                      message_id: int | None = None):
    _, tenant_id = _admin_conversation_scope(request, conv_id, tenant_id)
    return JSONResponse({"items": agent.memory.list_answer_revision_drafts(tenant_id, conv_id, message_id)})


@router.post("/api/admin/conversations/{conv_id}/revision-drafts")
def admin_create_answer_revision_draft(conv_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _admin_conversation_scope(request, conv_id, str(data.get("tenant_id") or ""))
    message_id = int(data.get("message_id") or 0)
    correction_note = str(data.get("correction_note") or "")
    try:
        rows = agent.memory.get_messages(conv_id)
        target = next((item for item in rows if int(item.get("id") or 0) == message_id and item.get("role") == "assistant"), None)
        if not target:
            raise ValueError("Agent 回答不存在或不属于当前工作区")
        config = _get_llm_config_card("reflection")
        if not config.get("base_url") or not config.get("model"):
            raise ValueError("未配置可用的反思模型，无法生成修订草稿")
        prompt = (
            "你是网络安全回答修订助手。请根据原回答和管理员纠错说明，生成一份完整、谨慎、可审核的修订稿。"
            "只输出修订后的回答正文，不要解释过程，不要新增无法由原回答或纠错说明支持的事实。\n\n"
            f"原回答：\n{str(target.get('content') or '')[:18000]}\n\n"
            f"管理员纠错说明：\n{correction_note[:2000]}"
        )
        import httpx
        headers = {"Content-Type": "application/json"}
        if config.get("api_key"):
            headers["Authorization"] = f"Bearer {config['api_key']}"
        url = str(config["base_url"]).rstrip("/") + "/chat/completions"
        response = httpx.post(url, headers=headers, json={"model": config["model"], "messages": [{"role": "user", "content": prompt}], "temperature": 0.1}, timeout=30)
        if response.status_code != 200:
            raise ValueError(f"修订模型请求失败: HTTP {response.status_code}")
        response_payload = response.json()
        usage = response_payload.get("usage") or {}
        agent.memory.record_llm_usage_event(
            tenant_id=tenant_id, module="answer_revision_draft",
            model=response_payload.get("model") or config["model"],
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )
        proposed = str(response_payload["choices"][0]["message"]["content"] or "").strip()
        draft = agent.memory.create_answer_revision_draft(tenant_id, conv_id, message_id, proposed, correction_note, str(config["model"]), 0, principal.user_id)
        return JSONResponse({"ok": True, "draft": draft})
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/conversations/{conv_id}/revision-drafts/{draft_id}/decision")
def admin_decide_answer_revision_draft(conv_id: str, draft_id: str, request: Request, data: dict = Body(...)):
    try:
        principal, tenant_id = _admin_conversation_scope(request, conv_id, str(data.get("tenant_id") or ""))
        result = agent.memory.decide_answer_revision_draft(
            tenant_id, draft_id,
            str(data.get("decision") or ""), principal.user_id,
            str(data.get("review_note") or ""),
        )
        return JSONResponse({"ok": True, **result})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/conversations/{conv_id}/revisions")
def admin_create_answer_revision(conv_id: str, request: Request, data: dict = Body(...)):
    principal, tenant_id = _admin_conversation_scope(request, conv_id, str(data.get("tenant_id") or ""))
    try:
        revision = agent.memory.create_answer_revision(
            tenant_id, conv_id, int(data.get("message_id") or 0),
            str(data.get("revised_content") or ""), str(data.get("correction_note") or ""),
            principal.user_id,
        )
        return JSONResponse({"ok": True, "revision": revision})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/shared/conversations/{token}")
def resolve_shared_conversation(token: str, data: dict = Body(default={})):
    result = agent.memory.resolve_conversation_share(token, str(data.get("password") or ""))
    if not result:
        raise HTTPException(status_code=404, detail="分享链接无效、已撤销或已过期")
    if result.get("password_required"):
        raise HTTPException(status_code=401, detail="该分享链接需要密码")
    return JSONResponse(result)

# ==================== Admin ====================
@router.post("/api/admin/cleanup")
def admin_cleanup(request: Request):
    require_platform_permission(request, agent.memory)
    _cleanup_staging()
    return JSONResponse({"status": "ok", "message": "环境已清理"})


@router.get("/api/admin/stream")
async def admin_event_stream(request: Request):
    require_platform_permission(request, agent.memory)
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
def documents_profile_registry(request: Request):
    """Expose only the profiles enabled for the active workspace.

    The registry remains readable without a management session for the document
    UI, but an anonymous local deployment receives only its default general
    Profile until an administrator enables an industry extension.
    """
    principal = principal_from_request(request, agent.memory)
    allowed = _governance_store_for_current_memory().enabled_profiles(principal.tenant_id)
    return JSONResponse({
        "status": "ok",
        "profiles": [item for item in profile_options() if item["profile"] in allowed],
        "enabled_profiles": sorted(allowed),
    })


@router.post("/api/documents/profile-extensions/propose")
def documents_profile_extension_propose(request: Request, data: dict = Body(...)):
    require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        proposal = propose_profile_extension(
            industry=data.get("industry", ""), label=data.get("label", ""),
            category=data.get("category", ""), keywords=data.get("keywords") or [],
            classifier_aliases=data.get("classifier_aliases") or [],
            description=data.get("description", ""),
            source_paths=data.get("source_paths") or [],
        )
        return JSONResponse({"status": "ok", "proposal": proposal})
    except ValueError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


@router.post("/api/documents/profile-extensions/confirm")
def documents_profile_extension_confirm(request: Request, data: dict = Body(...)):
    require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        stored = confirm_profile_extension(data.get("proposal") or {})
        from profile_classifier import load_profile_registry
        load_profile_registry.cache_clear()
        return JSONResponse({"status": "ok", "profile": stored})
    except ValueError as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


@router.get("/api/documents/profile-migration/summary")
def documents_profile_migration_summary(request: Request, limit: int = 500):
    require_permission(request, agent.memory, "tenant.manage", "")
    limit_val = max(1, min(int(limit or 500), 5000))
    return JSONResponse({"status": "ok", **scan_profile_migration(limit=limit_val)})


@router.post("/api/documents/profile-migration/apply")
def documents_profile_migration_apply(request: Request, data: dict = Body(default={})):
    require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    limit = data.get("limit")
    limit_val = None
    if limit:
        limit_val = max(1, min(int(limit), 5000))
    update_vector_stores = bool(data.get("update_vector_stores", True))
    result = apply_profile_migration(limit=limit_val, update_vector_stores=update_vector_stores)
    try:
        agent.refresh_retriever()
    except Exception as e:
        logger.warning(f"profile 迁移后刷新 retriever 失败: {e}")
    return JSONResponse({"status": "ok", **result})


@router.post("/api/documents/profile-migration/confirm")
def documents_profile_migration_confirm(request: Request, data: dict = Body(...)):
    require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    paths = data.get("paths") or []
    profile = str(data.get("profile") or "").strip()
    category = data.get("category")
    change_reason = str(data.get("change_reason") or "").strip()
    changed_by = str(data.get("changed_by") or "admin").strip() or "admin"
    if not paths:
        return JSONResponse({"status": "error", "message": "没有选择待确认文档"})
    if not profile:
        return JSONResponse({"status": "error", "message": "没有选择 profile"})
    try:
        result = confirm_profile_migration(
            paths=[str(p) for p in paths],
            profile=profile,
            category=str(category).strip() if category else None,
            change_reason=change_reason,
            changed_by=changed_by,
            update_vector_stores=bool(data.get("update_vector_stores", True)),
        )
        try:
            agent.refresh_retriever()
        except Exception as e:
            logger.warning(f"profile 批量确认后刷新 retriever 失败: {e}")
        return JSONResponse({"status": "ok", **result})
    except Exception as e:
        logger.warning(f"profile 批量确认失败: {e}")
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.get("/api/documents/profile-migration/history")
def documents_profile_migration_history(request: Request, path: str, limit: int = 50):
    require_permission(request, agent.memory, "tenant.manage", "")
    try:
        return JSONResponse({
            "status": "ok",
            "path": path,
            "events": profile_assignment_history(path, limit=limit),
        })
    except Exception as e:
        return JSONResponse({"status": "error", "message": str(e)}, status_code=400)


@router.get("/api/documents/access-migration/summary")
def documents_access_migration_summary(request: Request, limit: int = 500):
    _require_access_migration_permission(request)
    limit_val = max(1, min(int(limit or 500), 5000))
    return JSONResponse({"status": "ok", **scan_access_migration(limit=limit_val)})


@router.post("/api/documents/access-migration/confirm")
def documents_access_migration_confirm(request: Request, data: dict = Body(...)):
    principal = _require_access_migration_permission(request)
    paths = [str(path) for path in (data.get("paths") or [])]
    if not paths:
        return JSONResponse({"status": "error", "message": "没有选择待迁移文档"}, status_code=400)
    access = data.get("access") or {}
    try:
        result = confirm_access_migration(
            paths=paths,
            access=access,
            db_path=agent.memory._db_path,
            changed_by=principal.user_id,
            change_reason=str(data.get("change_reason") or ""),
        )
        agent.refresh_retriever()
        return JSONResponse({"status": "ok", **result})
    except Exception as exc:
        logger.warning("文档访问权限迁移失败: %s", exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


@router.get("/api/documents/access-migration/history")
def documents_access_migration_history(request: Request, path: str, limit: int = 50):
    _require_access_migration_permission(request)
    try:
        return JSONResponse({
            "status": "ok", "path": path,
            "events": access_assignment_history(path, limit=limit),
        })
    except Exception as exc:
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


@router.post("/api/documents/access-migration/rollback")
def documents_access_migration_rollback(request: Request, data: dict = Body(...)):
    principal = _require_access_migration_permission(request)
    try:
        result = rollback_access_migration(
            path=str(data.get("path") or ""), changed_at=str(data.get("changed_at") or ""),
            db_path=agent.memory._db_path, changed_by=principal.user_id,
            change_reason=str(data.get("change_reason") or ""),
        )
        agent.refresh_retriever()
        return JSONResponse({"status": "ok", **result})
    except Exception as exc:
        logger.warning("文档访问权限回滚失败: %s", exc)
        return JSONResponse({"status": "error", "message": str(exc)}, status_code=400)


@router.post("/api/documents/start-processing")
def documents_start(request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    files = data.get("files", [])
    category = data.get("category", "通用")
    conflict_actions = data.get("conflict_actions", {})
    if not files:
        return JSONResponse({"status": "error", "message": "没有文件"})

    task_id = uuid.uuid4().hex[:12]
    staging_files = []
    batch_size = 0
    profiles_by_id = {p["profile"]: p for p in profile_options()}
    allowed_profiles = set(profiles_by_id)
    tenant_enabled_profiles = _governance_store_for_current_memory().enabled_profiles(principal.tenant_id)

    for f in files:
        name = _safe_upload_filename(f.get("name", ""))
        action = conflict_actions.get(name, "overwrite")
        if action == "skip":
            continue
        content_b64 = f.get("content", "")
        if not content_b64:
            continue
        try:
            file_bytes = base64.b64decode(content_b64, validate=True)
        except (ValueError, TypeError):
            return JSONResponse({"status": "error", "message": f"文件内容编码无效: {name}"}, status_code=400)
        if len(file_bytes) > _MAX_FILE_SIZE:
            logger.warning(f"  ⏭️ 跳过超大文件: {name} ({len(file_bytes)/1024/1024:.1f}MB)")
            continue
        batch_size = _validate_upload_batch_size(batch_size, len(file_bytes))
        profile_suggestion = f.get("profile_suggestion") or suggest_document_profile(
            filename=name,
            category_hint=f.get("category", category),
        )
        selected_profile = f.get("profile") or profile_suggestion.get("profile", "general")
        if selected_profile not in allowed_profiles:
            return JSONResponse({"status": "error", "message": f"资料归属 profile 无效: {selected_profile}"})
        if (profiles_by_id[selected_profile].get("scope") == "industry"
                and selected_profile not in tenant_enabled_profiles):
            return JSONResponse({"status": "error",
                                 "message": f"当前工作区尚未启用网络安全行业扩展: {selected_profile}"},
                                status_code=403)
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
        requested_visibility = str(
            f.get("visibility") or data.get("visibility") or "public"
        ).strip().lower()
        if requested_visibility not in {"public", "tenant", "private"}:
            return JSONResponse({"status": "error", "message": f"文件可见范围无效: {name}"})
        if not principal.authenticated and requested_visibility != "public":
            return JSONResponse({"status": "error", "message": "未登录本地模式只能将资料设为公共"}, status_code=401)
        document_id = "doc-" + uuid.uuid4().hex
        # Global filename deduplication is only safe for public corpus. Tenant/private
        # uploads must never delete a same-named document owned by another scope.
        if requested_visibility != "public" and action == "overwrite":
            action = "rename"
        owner_user_id = principal.user_id if requested_visibility == "private" else ""
        scope_tenant_id = principal.tenant_id if requested_visibility in {"tenant", "private"} else ""
        scope_agent_id = principal.agent_id if requested_visibility == "private" else ""
        file_dir = _UPLOAD_STAGING / task_id
        file_dir.mkdir(parents=True, exist_ok=True)
        file_path = file_dir / name
        file_path.write_bytes(file_bytes)
        try:
            agent.memory.register_document(
                document_id=document_id, source_name=name, category=final_category,
                profile=selected_profile, visibility=requested_visibility, tenant_id=scope_tenant_id,
                owner_user_id=owner_user_id, agent_id=scope_agent_id,
                knowledge_base_id=str(f.get("knowledge_base_id") or data.get("knowledge_base_id") or ""),
            )
            agent.memory.log_audit(
                principal.tenant_id, principal.user_id, principal.agent_id, "document.stage", "document",
                document_id, {"source_name": name, "visibility": requested_visibility, "profile": selected_profile},
            )
        except Exception as exc:
            file_path.unlink(missing_ok=True)
            logger.warning("文档登记失败: %s", exc)
            return JSONResponse({"status": "error", "message": "文档登记失败"}, status_code=500)
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
            "document_id": document_id,
            "visibility": requested_visibility,
            "tenant_id": scope_tenant_id,
            "owner_user_id": owner_user_id,
            "agent_id": scope_agent_id,
            "knowledge_base_id": str(f.get("knowledge_base_id") or data.get("knowledge_base_id") or ""),
        })

    if not staging_files:
        return JSONResponse({"status": "error", "message": "没有有效的文件"})

    try:
        agent.memory.create_ingestion_job(
            task_id, principal.tenant_id, principal.user_id, principal.agent_id,
            [{"document_id": item.get("document_id"), "source_name": item.get("name")}
             for item in staging_files],
        )
    except HTTPException:
        raise
    except Exception as exc:
        logger.warning("入库任务登记失败: %s", exc)
        return JSONResponse({"status": "error", "message": "入库任务登记失败"}, status_code=500)

    with _doc_tasks_lock:
        _doc_tasks[task_id] = {
            "status": "processing",
            "progress": 0,
            "stage": "starting",
            "current_file": "",
            "summary": None,
            "error": None,
            "tenant_id": principal.tenant_id,
            "document_ids": [item.get("document_id") for item in staging_files],
            "document_map": {item.get("name"): item.get("document_id") for item in staging_files},
        }

    t = threading.Thread(
        target=_run_processing_task,
        args=(task_id, staging_files, category),
        daemon=True,
    )
    t.start()

    return JSONResponse({"status": "ok", "task_id": task_id, "files": len(staging_files)})


@router.get("/api/documents/status/{task_id}")
def documents_status(task_id: str, request: Request):
    persistent = agent.memory.get_ingestion_job(task_id)
    with _doc_tasks_lock:
        task = _doc_tasks.get(task_id)
    scope_record = persistent or task
    if scope_record:
        _require_ingestion_job_scope(request, scope_record)
    if not task:
        if persistent:
            return JSONResponse({"status": persistent["status"], "progress": 100 if persistent["status"] in {"completed", "error"} else 0,
                                 "task_id": task_id, "persistent": persistent})
        return JSONResponse({"status": "unknown", "progress": 0})
    if persistent:
        task = {**task, "persistent": persistent}
    return JSONResponse(task)


@router.get("/api/documents/ingestion/{task_id}")
def documents_ingestion_detail(task_id: str, request: Request):
    job = agent.memory.get_ingestion_job(task_id)
    if not job:
        raise HTTPException(status_code=404, detail="入库任务不存在")
    _require_ingestion_job_scope(request, job)
    return JSONResponse(job)


@router.get("/api/admin/ingestion-jobs")
def admin_list_ingestion_jobs(request: Request, tenant_id: str = "", limit: int = 50):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_ingestion_jobs(tenant_id, limit)})


@router.get("/api/admin/ingestion-failures")
def admin_list_ingestion_failures(request: Request, tenant_id: str = "", limit: int = 50):
    _, tenant_id = _tenant_admin_scope(request, tenant_id)
    return JSONResponse({"items": agent.memory.list_ingestion_failures(tenant_id, limit)})


@router.get("/api/admin/semantic-cache/stats")
def admin_semantic_cache_stats(request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse(agent.memory.semantic_cache_stats())


@router.delete("/api/admin/semantic-cache")
def admin_clear_semantic_cache(request: Request, knowledge_base_id: str = ""):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"ok": True, "deleted": agent.memory.clear_semantic_cache(knowledge_base_id or None)})


@router.post("/api/admin/documents/{document_id}/retry-ingestion")
def admin_retry_document_ingestion(document_id: str, request: Request, data: dict = Body(default={} )):
    require_platform_permission(request, agent.memory)
    tenant_id = data.get("tenant_id")
    document = agent.memory.get_document(document_id, tenant_id)
    if not document:
        raise HTTPException(status_code=404, detail="文档不存在")
    if document.get("status") == "indexed":
        raise HTTPException(status_code=400, detail="文档已经完成索引，无需重试")
    try:
        agent.memory.update_document_lifecycle(
            document_id, "published", tenant_id, str(data.get("changed_by") or "admin"),
            str(data.get("change_reason") or "管理员重试入库"),
        )
        task_id = _queue_staged_document_processing(document, tenant_id or document.get("tenant_id", ""))
        return JSONResponse({"ok": True, "processing_task_id": task_id})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/documents/debug-dedup")
def debug_dedup(request: Request):
    _require_access_migration_permission(request)
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
@router.post("/api/generation/route")
def generation_route(request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    query = str(data.get("query") or "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="请输入生成需求")
    result = route_capability(query, data.get("fields") or {})
    agent.memory.log_audit(
        principal.tenant_id, principal.user_id, principal.agent_id, "generation.route", "capability",
        result["mode"], {"confidence": result["confidence"], "missing_fields": result["missing_fields"]},
    )
    if result["mode"] == "chat":
        return JSONResponse({"ok": True, "route": result, "outline": None})
    if result["clarification"]:
        return JSONResponse({"ok": True, "route": result, "outline": None})
    evidence = collect_generation_references(
        agent.retriever, query, result["fields"],
        principal.tenant_id, principal.user_id, principal.agent_id,
    )
    evidence_plan = build_generation_evidence_plan(
        query, result["mode"], result["fields"], evidence,
    )
    enabled_extensions = agent.memory.list_enabled_capability_extensions(
        principal.tenant_id, principal.agent_id,
    )
    fetch_extension = next(
        (
            item for item in enabled_extensions
            if item.get("kind") == "mcp" and item.get("id") == "builtin-mcp-fetch"
        ),
        None,
    )
    search_extension = next(
        (item for item in enabled_extensions
         if item.get("kind") == "mcp" and item.get("id") == "builtin-mcp-bing-search"),
        None,
    )
    if evidence_plan["next_step"] == "fetch_explicit_urls":
        if not fetch_extension:
            evidence_plan["next_step"] = "clarify_or_degrade"
            evidence_plan["reason"] = "builtin-mcp-fetch-not-authorized"
        else:
            extension_id = fetch_extension["id"]
            evidence = append_mcp_fetch_evidence(
                evidence,
                evidence_plan["explicit_urls"],
                lambda url, max_length: execute_capability(
                    agent.memory, extension_id, principal.tenant_id, principal.agent_id,
                    principal.user_id, principal.role,
                    {"tool_name": "fetch", "arguments": {
                        "url": url, "max_length": max_length,
                    }},
                ),
            )
            evidence_plan = build_generation_evidence_plan(
                query, result["mode"], result["fields"], evidence,
            )
            evidence_plan["mcp_calls"] = evidence.get("mcp_calls", [])
    elif evidence_plan["next_step"] == "search_bing":
        if not search_extension:
            evidence_plan["next_step"] = "clarify_or_degrade"
            evidence_plan["reason"] = "builtin-mcp-bing-search-not-authorized"
        else:
            extension_id = search_extension["id"]
            evidence = append_mcp_search_evidence(
                evidence, query,
                lambda search_query, count: execute_capability(
                    agent.memory, extension_id, principal.tenant_id, principal.agent_id,
                    principal.user_id, principal.role,
                    {"tool_name": "bing_search", "arguments": {
                        "query": search_query, "count": count,
                    }},
                ),
            )
            evidence_plan = build_generation_evidence_plan(
                query, result["mode"], result["fields"], evidence,
            )
            evidence_plan["mcp_calls"] = evidence.get("mcp_calls", [])

    if not evidence.get("context_blocks") or not evidence_plan.get("generation_allowed"):
        agent.memory.log_audit(
            principal.tenant_id, principal.user_id, principal.agent_id,
            "generation.evidence.insufficient", "capability", result["mode"],
            {"next_step": evidence_plan["next_step"],
             "mcp_available": bool(fetch_extension or search_extension),
             "context_count": len(evidence.get("context_blocks") or [])},
        )
        return JSONResponse({
            "ok": True, "route": result, "outline": None,
            "evidence": {
                "status": evidence.get("status"),
                "references": evidence.get("references", []),
                "plan": evidence_plan,
            },
            "message": (
                "当前授权知识库没有足够资料。请补充明确的公开网页 URL，"
                "或先让管理员授权 Fetch MCP；未获得证据前不会生成 PPT 内容。"
            ),
        })

    outline, generation_meta = generate_outline_from_evidence(
        agent.memory, getattr(agent, "llm", None), result["mode"], query,
        result["fields"], evidence,
        usage_sink=lambda llm_result, model: _record_auxiliary_llm_usage(
            "generation", llm_result, model, tenant_id=principal.tenant_id,
        ),
    )
    review = _reflect_generation_outline(
        principal, result["mode"], query, outline, evidence["references"],
    )
    if review["decision"] in {"skipped", "degraded"}:
        agent.memory.log_audit(
            principal.tenant_id, principal.user_id, principal.agent_id,
            "generation.reflection.required", "capability", result["mode"],
            {"decision": review["decision"], "reason": review.get("reason", "")},
        )
        return JSONResponse({
            "ok": True, "route": result, "outline": None,
            "evidence": {
                "status": evidence.get("status"),
                "references": evidence.get("references", []),
                "plan": evidence_plan,
            },
            "review": {
                "decision": review["decision"],
                "reason": review.get("reason") or "生成必须经过反思模型",
            },
            "message": "生成内容必须经过反思模型审查；请先由管理员配置并启用反思模型。",
        })
    if review["decision"] in {"block", "clarify"}:
        agent.memory.record_reflection_run(principal.tenant_id, principal.user_id, principal.agent_id, "", {
            **review, "mode": result["mode"], "input_summary": query[:500],
            "output_summary": str(review["answer"])[:500],
        })
        return JSONResponse({"ok": True, "route": result, "outline": None,
                             "review": {"decision": review["decision"], "reason": review.get("reason", "")}})
    outline = review["outline"]
    item = agent.memory.create_generation_request(
        principal.tenant_id, principal.user_id, principal.agent_id, result["mode"], query,
        result["fields"], outline,
    )
    trace = {
        "schema_version": "2026.08.p5.generation.v1",
        "route": {"candidate": result["mode"], "selected": result["mode"],
                  "confidence": result["confidence"], "missing_fields": []},
        "scope": {"tenant_id": principal.tenant_id, "user_id": principal.user_id,
                  "agent_id": principal.agent_id},
        "context": build_runtime_context(getattr(agent.memory, "_db_path", None)),
        "references_state": evidence["status"],
        "evidence_profile": evidence["profile"],
        "evidence_plan": evidence_plan,
        "available_extensions": enabled_extensions,
        "tool_calls": evidence.get("mcp_calls", []),
        "generation": generation_meta,
        "reflection": {key: review.get(key) for key in ("decision", "reason", "rule_version", "rounds", "duration_ms", "model")},
        "degradation": "基础本地生成能力；未调用外部 Skill/MCP",
        "outcome": "outline_ready",
    }
    agent.memory.update_generation_observability(
        item["id"], principal.tenant_id, principal.user_id, principal.agent_id,
        trace, evidence["references"],
    )
    item = agent.memory.get_generation_request(
        item["id"], principal.tenant_id, principal.user_id, principal.agent_id,
    )
    agent.memory.record_reflection_run(principal.tenant_id, principal.user_id, principal.agent_id, "", {
        **review, "mode": result["mode"], "input_summary": query[:500],
        "output_summary": json.dumps(outline, ensure_ascii=False)[:500],
    })
    return JSONResponse({"ok": True, "route": result, "outline": outline, "generation": item,
                         "review": {"decision": review["decision"], "reason": review.get("reason", "")}})


@router.get("/api/generation/requests")
def list_generation_requests(request: Request):
    principal = principal_from_request(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_generation_requests(
        principal.tenant_id, principal.user_id, principal.agent_id,
    )})


@router.put("/api/generation/requests/{request_id}/outline")
def update_generation_request_outline(request_id: str, request: Request, data: dict = Body(...)):
    principal = principal_from_request(request, agent.memory)
    try:
        if not agent.memory.update_generation_outline(
            request_id, principal.tenant_id, principal.user_id, principal.agent_id,
            data.get("outline") if isinstance(data.get("outline"), dict) else {},
        ):
            raise HTTPException(status_code=404, detail="生成请求不存在")
        item = agent.memory.get_generation_request(
            request_id, principal.tenant_id, principal.user_id, principal.agent_id,
        )
        return JSONResponse({"ok": True, "generation": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/generation/requests/{request_id}/render")
def render_generation_request(request_id: str, request: Request):
    principal = principal_from_request(request, agent.memory)
    item = agent.memory.get_generation_request(
        request_id, principal.tenant_id, principal.user_id, principal.agent_id,
    )
    if not item:
        raise HTTPException(status_code=404, detail="生成请求不存在")
    next_version = len(item.get("artifacts") or []) + 1
    path = artifact_path(
        principal.tenant_id, principal.user_id, request_id,
        item["outline"].get("title", ""), item["mode"], next_version,
    )
    try:
        render_artifact(item["mode"], item["outline"], path, next_version, item.get("references") or [])
        artifact = agent.memory.add_generation_artifact(
            request_id, principal.tenant_id, principal.user_id, principal.agent_id, str(path),
        )
        if not artifact:
            raise HTTPException(status_code=404, detail="生成请求不存在")
        trace = dict(item.get("trace") or {})
        trace["outcome"] = "generated"
        trace["artifact"] = {"id": artifact["id"], "version": artifact["version"]}
        agent.memory.update_generation_observability(
            request_id, principal.tenant_id, principal.user_id, principal.agent_id,
            trace, item.get("references") or [],
        )
        completed = agent.memory.get_generation_request(
            request_id, principal.tenant_id, principal.user_id, principal.agent_id,
        )
        _publish_event(event_bus, "generation.completed", {
            "tenant_id": principal.tenant_id, "user_id": principal.user_id,
            "agent_id": principal.agent_id, "generation_id": request_id,
            "notification_body": f"{'PPT' if item['mode'] == 'presentation' else '文档'}生成已完成，可在“我的生成物”中下载。",
        })
        return JSONResponse({"ok": True, "generation": completed})
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("P5 生成物渲染失败")
        raise HTTPException(status_code=500, detail=f"生成失败：{str(exc)[:120]}") from exc


@router.get("/api/generation/requests/{request_id}/download")
def download_generation_request(request_id: str, request: Request, artifact_id: str = "", reason: str = ""):
    principal = principal_from_request(request, agent.memory)
    item = agent.memory.get_generation_request(
        request_id, principal.tenant_id, principal.user_id, principal.agent_id,
    )
    artifact = None
    if item and artifact_id:
        artifact = agent.memory.get_generation_artifact(
            request_id, artifact_id, principal.tenant_id, principal.user_id, principal.agent_id,
        )
        if not artifact:
            raise HTTPException(status_code=404, detail="生成版本不存在")
    path = Path(str((artifact or item or {}).get("artifact_path") or ""))
    if not item or item["status"] != "generated" or not path.exists():
        raise HTTPException(status_code=404, detail="生成文件不存在")
    agent.memory.log_audit(principal.tenant_id, principal.user_id, principal.agent_id,
                           "generation.download", "generation", request_id,
                           {"reason": str(reason or "用户下载生成物")[:300],
                            "artifact_id": artifact_id or ""})
    media_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    if item["mode"] == "presentation":
        media_type = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
    return FileResponse(str(path), media_type=media_type, filename=path.name)


@router.delete("/api/generation/requests/{request_id}")
def delete_generation_request(request_id: str, request: Request):
    principal = principal_from_request(request, agent.memory)
    stored_paths = agent.memory.delete_generation_request(
        request_id, principal.tenant_id, principal.user_id, principal.agent_id,
    )
    if stored_paths is None:
        raise HTTPException(status_code=404, detail="生成请求不存在")
    try:
        paths = json.loads(stored_paths)
    except (TypeError, ValueError):
        paths = [stored_paths]
    for stored_path in paths:
        path = Path(str(stored_path or ""))
        if path.exists():
            path.unlink()
    return JSONResponse({"ok": True})


@router.post("/api/chat")
def chat(request: Request, data: dict = Body(...)):
    endpoint_started = time.perf_counter()
    principal = principal_from_request(request, agent.memory)
    query = data.get("query", "").strip()
    conv_id = data.get("conversation_id")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    quota = agent.memory.check_quota(principal.tenant_id, principal.user_id)
    if not quota["allowed"]:
        raise HTTPException(status_code=429, detail={"message": "已达到用量预算限制", "quota": quota})
    if conv_id and not agent.memory.conversation_belongs_to(
        conv_id, principal.tenant_id, principal.user_id, principal.agent_id,
    ):
        raise HTTPException(status_code=404, detail="对话不存在")
    # 在线程池中运行同步 agent.ask()，避免阻塞事件循环
    result = agent.ask(query, conv_id, 0.1, "user", tenant_id=principal.tenant_id,
                       user_id=principal.user_id, agent_id=principal.agent_id,
                       knowledge_base_id=str(data.get("knowledge_base_id") or ""),
                       profiles=_authorized_profiles(principal.tenant_id, data.get("profiles")),
                       response_language=str(data.get("language") or agent.memory.get_user_language(
                           principal.tenant_id, principal.user_id, principal.agent_id)))
    _publish_event(event_bus, "conversation_updated", {
        "conv_id": conv_id or result.get("conversation_id", ""),
        "action": "chat",
    })
    if isinstance(result.get("stats"), dict):
        result["stats"]["endpoint_time"] = round(time.perf_counter() - endpoint_started, 3)
        result["stats"]["total_time"] = result["stats"]["endpoint_time"]
    return JSONResponse(result)


@router.post("/api/chat/stream")
def chat_stream(request: Request, data: dict = Body(...)):
    """LLM 聊天接口"""
    query = data.get("query", "").strip()
    conv_id = data.get("conversation_id")
    if not query:
        return JSONResponse({"error": "query is required"}, status_code=400)
    principal = principal_from_request(request, agent.memory)
    quota = agent.memory.check_quota(principal.tenant_id, principal.user_id)
    if not quota["allowed"]:
        raise HTTPException(status_code=429, detail={"message": "已达到用量预算限制", "quota": quota})
    if conv_id and not agent.memory.conversation_belongs_to(
        conv_id, principal.tenant_id, principal.user_id, principal.agent_id,
    ):
        raise HTTPException(status_code=404, detail="对话不存在")

    conv_info = _ACTIVE_CONVERSATIONS.get(conv_id)
    title = query[:40]
    if conv_info is None and conv_id:
        convs = agent.memory.get_conversations(
            tenant_id=principal.tenant_id, user_id=principal.user_id, agent_id=principal.agent_id,
        )
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
            async for event in agent.ask_stream(
                query=query, conversation_id=conv_id, tenant_id=principal.tenant_id,
                user_id=principal.user_id, agent_id=principal.agent_id,
                knowledge_base_id=str(data.get("knowledge_base_id") or ""),
                profiles=_authorized_profiles(principal.tenant_id, data.get("profiles")),
                response_language=str(data.get("language") or agent.memory.get_user_language(
                    principal.tenant_id, principal.user_id, principal.agent_id)),
            ):
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
def submit_rating(request: Request, data: dict = Body(...)):
    message_id = data.get("message_id")
    rating = data.get("rating")
    if not message_id or not rating:
        return JSONResponse({"ok": False, "error": "缺少 message_id 或 rating"}, status_code=400)
    if not isinstance(rating, int) or rating < 1 or rating > 5:
        return JSONResponse({"ok": False, "error": "rating 需为 1-5"}, status_code=400)
    principal = principal_from_request(request, agent.memory)
    if not agent.memory.message_belongs_to(
        int(message_id), principal.tenant_id, principal.user_id, principal.agent_id,
    ):
        raise HTTPException(status_code=404, detail="消息不存在")
    try:
        agent.memory.update_rating(int(message_id), int(rating))
        agent.memory.log_audit(
            principal.tenant_id, principal.user_id, principal.agent_id,
            "message.rate", "message", str(message_id), {"rating": rating},
        )
        return {"ok": True}
    except Exception as e:
        logger.error(f"评分写入失败: {e}")
        return JSONResponse({"ok": False, "error": str(e)}, status_code=500)


@router.post("/api/rating/semantic-fallback")
def semantic_fallback_rating(request: Request, data: dict = Body(...)):
    message_id = data.get("message_id")
    if not message_id:
        return JSONResponse({"ok": False, "error": "缺少 message_id"}, status_code=400)
    principal = principal_from_request(request, agent.memory)
    if not agent.memory.message_belongs_to(
        int(message_id), principal.tenant_id, principal.user_id, principal.agent_id,
    ):
        raise HTTPException(status_code=404, detail="消息不存在")

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


# ==================== 反馈与知识缺口 ====================
@router.post("/api/feedback")
def submit_feedback(request: Request, data: dict = Body(...)):
    message_id = data.get("message_id")
    feedback_type = str(data.get("feedback_type") or "").strip()
    if not message_id or feedback_type not in ("copy", "refresh", "correction", "unhelpful", "no_source"):
        return JSONResponse({"ok": False, "error": "缺少 message_id 或有效的 feedback_type"}, status_code=400)
    principal = principal_from_request(request, agent.memory)
    if not agent.memory.message_belongs_to(
        int(message_id), principal.tenant_id, principal.user_id, principal.agent_id,
    ):
        raise HTTPException(status_code=404, detail="消息不存在")
    try:
        feedback_id = agent.memory.record_feedback(
            int(message_id), feedback_type,
            str(data.get("feedback_text") or ""), user_id=principal.user_id,
        )
        if not feedback_id:
            return JSONResponse({"ok": False, "error": "消息未找到"}, status_code=404)
        agent.memory.log_audit(
            principal.tenant_id, principal.user_id, principal.agent_id,
            "message.feedback", "message", str(message_id),
            {"feedback_type": feedback_type, "feedback_id": feedback_id},
        )
        return {"ok": True, "feedback_id": feedback_id}
    except Exception as exc:
        logger.error(f"反馈写入失败: {exc}")
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.get("/api/admin/knowledge-gaps")
def admin_list_knowledge_gaps(request: Request, status: str = "open", profile: str = "",
                              knowledge_base_id: str = "", search: str = "",
                              limit: int = 50, tenant_id: str = ""):
    principal = require_permission(request, agent.memory, "tenant.manage", tenant_id)
    return JSONResponse(agent.memory.get_knowledge_gaps(
        status, profile, knowledge_base_id, search, limit, principal.tenant_id,
    ))


@router.post("/api/admin/knowledge-gaps/rebuild")
def admin_rebuild_knowledge_gaps(request: Request, limit_candidates: int = 2000, tenant_id: str = ""):
    principal = require_permission(request, agent.memory, "tenant.manage", tenant_id)
    try:
        return JSONResponse({"ok": True, **agent.memory.rebuild_knowledge_gaps(limit_candidates, tenant_id=principal.tenant_id)})
    except Exception as exc:
        logger.error(f"知识缺口聚类失败: {exc}", exc_info=True)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=500)


@router.put("/api/admin/knowledge-gaps/{gap_id}")
def admin_update_knowledge_gap(gap_id: int, request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    status = str(data.get("status") or "").strip()
    if status not in ("open", "resolved", "dismissed"):
        raise HTTPException(status_code=400, detail="status 需为 open/resolved/dismissed")
    ok = agent.memory.update_knowledge_gap_status(
        gap_id, status, str(data.get("note") or ""), principal.tenant_id,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="知识缺口不存在")
    return {"ok": True}


@router.post("/api/admin/knowledge-gaps/{gap_id}/prompt-test")
def admin_promote_gap_to_prompt_test(gap_id: int, request: Request, data: dict = Body(default={} )):
    principal = require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    result = agent.memory.promote_knowledge_gap_to_prompt_test(
        gap_id, principal.user_id, principal.tenant_id,
    )
    if not result:
        raise HTTPException(status_code=404, detail="知识缺口不存在")
    return JSONResponse({"ok": True, **result})


@router.get("/api/admin/knowledge-gaps/export")
def admin_export_knowledge_gaps(request: Request, status: str = "all", profile: str = "",
                                knowledge_base_id: str = "", tenant_id: str = ""):
    principal = require_permission(request, agent.memory, "tenant.manage", tenant_id)
    data = agent.memory.get_knowledge_gaps(status, profile, knowledge_base_id, limit=5000, tenant_id=principal.tenant_id)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["ID", "典型问题", "Profile", "知识库", "出现次数", "低分", "刷新",
                     "纠错", "无来源", "状态", "首次出现", "最近出现", "相关反馈消息ID"])
    for item in data["items"]:
        writer.writerow([
            item["id"], item["canonical_question"], item["profile"],
            item["knowledge_base_id"], item["occurrence_count"],
            item["low_rating_count"], item["refresh_count"], item["correction_count"],
            item["no_source_count"], item["status"], item["first_seen_at"],
            item["last_seen_at"], "|".join(str(x) for x in item["related_message_ids"]),
        ])
    payload = "\ufeff" + buffer.getvalue()
    return Response(
        content=payload.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="knowledge-gaps.csv"'},
    )


@router.get("/api/admin/knowledge-gaps/feedback")
def admin_list_feedback_items(request: Request, limit: int = 50):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": agent.memory.list_feedback_items(limit)})


@router.post("/api/admin/knowledge-gaps/feedback/{feedback_id}/retrieval-eval")
def admin_promote_feedback_to_retrieval_eval(feedback_id: int, request: Request,
                                               data: dict = Body(default={} )):
    """Put a feedback sample into Retrieval Eval as a pending human-label case."""
    principal = require_platform_permission(request, agent.memory)
    result = agent.memory.promote_feedback_to_retrieval_eval(
        feedback_id,
        created_by=principal.user_id,
        expected=data.get("expected") if isinstance(data, dict) else None,
        category=str((data or {}).get("category") or "feedback"),
    )
    if not result:
        raise HTTPException(status_code=404, detail="反馈记录不存在")
    return JSONResponse({"ok": True, **result})


@router.post("/api/admin/knowledge-gaps/{gap_id}/supply-task")
def admin_create_gap_supply_task(gap_id: int, request: Request, data: dict = Body(default={} )):
    principal = require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    task = agent.memory.create_gap_supply_task(
        gap_id, str(data.get("title") or ""), str(data.get("description") or ""),
        principal.user_id, principal.tenant_id, principal.agent_id,
    )
    if not task:
        raise HTTPException(status_code=404, detail="知识缺口不存在")
    return JSONResponse({"ok": True, "task": task})


@router.get("/api/admin/knowledge-gaps/supply-tasks")
def admin_list_gap_supply_tasks(request: Request, status: str = "", limit: int = 50, tenant_id: str = ""):
    principal = require_permission(request, agent.memory, "tenant.manage", tenant_id)
    return JSONResponse({"items": agent.memory.list_gap_supply_tasks(None, status, limit, principal.tenant_id)})


@router.put("/api/admin/knowledge-gaps/supply-tasks/{task_id}")
def admin_update_gap_supply_task(task_id: str, request: Request, data: dict = Body(...)):
    principal = require_permission(request, agent.memory, "tenant.manage", str(data.get("tenant_id") or ""))
    status = str(data.get("status") or "").strip()
    if status not in ("pending", "in_progress", "done", "cancelled"):
        raise HTTPException(status_code=400, detail="status 需为 pending/in_progress/done/cancelled")
    if not agent.memory.update_gap_supply_task_status(task_id, status, principal.tenant_id):
        raise HTTPException(status_code=404, detail="待补资料任务不存在")
    return {"ok": True}


# ==================== Prompt 版本管理 ====================


def _ensure_prompt_assets() -> None:
    agent.memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)


@router.get("/api/admin/prompt-assets")
def admin_list_prompt_assets(request: Request):
    require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    return JSONResponse({"items": agent.memory.list_prompt_assets()})


@router.get("/api/admin/prompt-assets/governance-summary")
def admin_prompt_asset_governance_summary(request: Request):
    require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    return JSONResponse(agent.memory.get_prompt_asset_governance_summary())


@router.get("/api/admin/prompt-assets/{slot}/versions")
def admin_list_prompt_asset_versions(slot: str, request: Request):
    require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    return JSONResponse({"items": agent.memory.list_prompt_asset_versions(slot)})


@router.post("/api/admin/prompt-assets/{slot}/versions")
def admin_save_prompt_asset_version(slot: str, request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    try:
        item = agent.memory.save_prompt_asset_version(slot, data.get("template", ""), data.get("variables") or [],
                                                      data.get("status", "draft"), data.get("change_note", ""),
                                                      principal.user_id)
        return JSONResponse({"ok": True, "version": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/prompt-assets/{slot}/rollback")
def admin_restore_prompt_asset_version(slot: str, request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    try:
        item = agent.memory.restore_prompt_asset_version(slot, int(data.get("version")), principal.user_id)
        return JSONResponse({"ok": True, "version": item})
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/prompt-assets/{slot}/versions/{version}/publish")
def admin_publish_prompt_asset_version(slot: str, version: int, request: Request, data: dict = Body(default={})):
    principal = require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    try:
        item = agent.memory.publish_prompt_asset_version(slot, version, principal.user_id)
        return JSONResponse({"ok": True, "version": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/prompt-assets/{slot}/calibration")
def admin_record_prompt_asset_calibration(slot: str, request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    try:
        version = int(data.get("version"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="请选择待校准的 Prompt 版本") from exc
    report = data.get("report") if isinstance(data.get("report"), dict) else {}
    reviewed_count = int(report.get("reviewed_count") or 0)
    agreement_rate = report.get("agreement_rate")
    mean_absolute_error = report.get("mean_absolute_error")
    try:
        passed = reviewed_count >= 3 and float(agreement_rate) >= 0.7 and float(mean_absolute_error) <= 0.2
    except (TypeError, ValueError):
        passed = False
    normalized = {"type": "human_judge_calibration", "reviewed_count": reviewed_count,
                  "agreement_rate": agreement_rate, "mean_absolute_error": mean_absolute_error,
                  "passed": passed, "source_run_id": str(data.get("source_run_id") or "")}
    run_id = agent.memory.record_prompt_asset_test_run(slot, version, normalized,
                                                        principal.user_id, "calibration")
    return JSONResponse({"ok": True, "run_id": run_id, **normalized})


@router.get("/api/admin/prompt-assets/{slot}/golden-cases")
def admin_prompt_asset_golden_cases(slot: str, request: Request):
    require_platform_permission(request, agent.memory)
    return JSONResponse({"items": get_slot_golden_cases(slot)})


@router.post("/api/admin/prompt-assets/{slot}/test-contract")
def admin_prompt_asset_test_contract(slot: str, request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    requested_version = data.get("version")
    asset = agent.memory.get_active_prompt_asset(slot)
    if requested_version is not None:
        versions = agent.memory.list_prompt_asset_versions(slot)
        asset = next((item for item in versions if item["version"] == int(requested_version)), None) or {}
    if not asset.get("version"):
        raise HTTPException(status_code=400, detail="该槽位尚无可测试 Prompt 版本")
    cases = get_slot_golden_cases(slot)
    if data.get("execute", True) and slot in ({"reflection"} | EXECUTABLE_STRUCTURED_SLOTS):
        role = asset.get("model_role") or ("reflection" if slot == "reflection" else "promptEval")
        config = _get_llm_config_card(role)
        llm = None
        if config.get("model") and config.get("base_url"):
            try:
                llm = LLMProvider(base_url=config["base_url"], api_key=config.get("api_key", ""),
                                  model=config["model"], use_ollama_fallback=False)
            except Exception:
                llm = None
        if slot == "reflection":
            report = run_reflection_golden_suite(
                agent.memory, llm, asset.get("template", ""), asset["version"],
                usage_sink=lambda result, model: _record_auxiliary_llm_usage("reflection_golden", result, model),
            )
        else:
            report = run_structured_prompt_golden_suite(
                agent.memory, llm, slot, asset.get("template", ""), asset["version"],
                usage_sink=lambda result, model: _record_auxiliary_llm_usage(f"{slot}_golden", result, model),
            )
    else:
        supplied = data.get("results") if isinstance(data.get("results"), dict) else {}
        results = [evaluate_slot_result(slot, case, supplied.get(case["id"], {})) for case in cases]
        report = {"slot": slot, "version": asset["version"], "total": len(results),
                  "passed_count": sum(1 for item in results if item["passed"]),
                  "passed": bool(results) and all(item["passed"] for item in results), "results": results}
    run_id = agent.memory.record_prompt_asset_test_run(slot, asset["version"], report,
                                                        principal.user_id)
    return JSONResponse({"ok": True, "run_id": run_id, **report})


@router.post("/api/admin/prompt-assets/{slot}/ab")
def admin_prompt_asset_ab(slot: str, request: Request, data: dict = Body(...)):
    principal = require_platform_permission(request, agent.memory)
    _ensure_prompt_assets()
    try:
        version_a = int(data.get("version_a")); version_b = int(data.get("version_b"))
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="请选择两个版本") from exc
    versions = agent.memory.list_prompt_asset_versions(slot)
    a = next((item for item in versions if item["version"] == version_a), None)
    b = next((item for item in versions if item["version"] == version_b), None)
    if not a or not b or version_a == version_b:
        raise HTTPException(status_code=400, detail="A/B 版本不存在或相同")
    if slot in ({"reflection"} | EXECUTABLE_STRUCTURED_SLOTS):
        role = a.get("model_role") or ("reflection" if slot == "reflection" else "promptEval")
        config = _get_llm_config_card(role)
        llm = None
        if config.get("model") and config.get("base_url"):
            try:
                llm = LLMProvider(base_url=config["base_url"], api_key=config.get("api_key", ""),
                                  model=config["model"], use_ollama_fallback=False)
            except Exception:
                pass
        if slot == "reflection":
            report = compare_reflection_asset_versions(
                agent.memory, llm, a, b,
                usage_sink=lambda result, model: _record_auxiliary_llm_usage("reflection_ab", result, model),
            )
        else:
            report = compare_structured_prompt_versions(
                agent.memory, llm, slot, a, b,
                usage_sink=lambda result, model: _record_auxiliary_llm_usage(f"{slot}_ab", result, model),
            )
    else:
        try:
            report = compare_slot_contract_versions(
                slot, version_a, version_b,
                data.get("results_a"), data.get("results_b"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    run_id = agent.memory.record_prompt_asset_test_run(slot, version_b, {"type": "ab", **report},
                                                        principal.user_id)
    return JSONResponse({"ok": True, "run_id": run_id, **report})


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
    items = generate_test_set(
        keywords, llm=llm,
        usage_sink=lambda result, model: _record_auxiliary_llm_usage("prompt_test", result, model),
    )
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


@router.get("/api/prompt/test/export")
def prompt_test_export():
    """Export the latest legacy Prompt Test result as a Word report."""
    from prompt_tester import get_latest_full_result

    result = get_latest_full_result(agent.memory._db_path)
    if not result:
        return JSONResponse({"error": "暂无 Prompt Test 结果，请先运行测试"}, status_code=404)

    dimension_scores = result.get("dimension_scores") or {}
    summary_cards = [
        (result.get("total", 0), "测试总数"),
        (result.get("passed", 0), "通过"),
        (result.get("failed", 0), "失败"),
        (f"{float(result.get('pass_rate') or 0) * 100:.1f}%", "通过率"),
        (f"{float(result.get('overall_score') or 0):.3f}", "综合得分"),
    ]
    rows = []
    for item in (result.get("results") or [])[:200]:
        scores = item.get("scores") or {}
        rows.append([
            item.get("id", ""), item.get("query", ""),
            "通过" if item.get("passed") else "失败",
            f"{float(item.get('total_score') or item.get('score') or 0):.3f}",
            "; ".join(f"{key}: {value}" for key, value in scores.items()),
        ])
    if not rows:
        rows = [["-", "暂无明细", "-", "-", ""]]
    date_line = (
        f"测试时间：{result.get('timestamp', '')} | "
        f"Prompt 版本：{result.get('version', '')} | "
        f"维度：{json.dumps(dimension_scores, ensure_ascii=False)}"
    )
    buf = _build_report_doc(
        "Prompt Test 评测报告", date_line, summary_cards,
        ["用例", "查询", "结果", "得分", "维度明细"], rows,
    )
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename=prompt_test_report_{ts}.docx"},
    )


@router.get("/api/prompt/ab/runs")
def prompt_ab_runs(limit: int = 20):
    """List persisted offline Prompt A/B comparisons."""
    return {"items": agent.memory.get_prompt_ab_runs(limit)}


@router.post("/api/prompt/ab/run")
def prompt_ab_run(data: dict = Body(...)):
    """Compare two saved Prompt versions without changing the active version."""
    try:
        version_a_id = int(data.get("version_a_id"))
        version_b_id = int(data.get("version_b_id"))
    except (TypeError, ValueError):
        return JSONResponse({"error": "请选择两个 Prompt 版本"}, status_code=400)
    if version_a_id == version_b_id:
        return JSONResponse({"error": "A 与 B 必须是不同版本"}, status_code=400)
    set_id = str(data.get("set_id") or "builtin")
    limit = max(1, min(int(data.get("limit") or 30), 100))
    from prompt_versions import get_version_prompt
    from prompt_ab_test import run_prompt_ab_test

    version_a = get_version_prompt(version_a_id, agent.memory._db_path)
    version_b = get_version_prompt(version_b_id, agent.memory._db_path)
    if not version_a or not version_b:
        return JSONResponse({"error": "Prompt 版本不存在"}, status_code=404)
    items = agent.memory.get_test_items(set_id)[:limit]
    if not items:
        return JSONResponse({"error": "所选测试集没有可运行用例"}, status_code=400)
    try:
        report = run_prompt_ab_test(agent, version_a, version_b, items)
        run_id = "prompt_ab_" + uuid.uuid4().hex[:12]
        report["run_id"] = run_id
        report["set_id"] = set_id
        report["context"] = {
            "same_test_set": True,
            "same_retrieval_context": True,
            "active_prompt_changed": False,
        }
        agent.memory.save_prompt_ab_run(run_id, version_a, version_b, set_id, report)
        return {"ok": True, **report}
    except Exception as exc:
        logger.exception("Prompt A/B 测试运行失败")
        return JSONResponse({"error": str(exc)}, status_code=500)


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
        "weighted_score": round(weighted_score, 1),
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
    fix = suggest_fix(
        item, sys_prompt, llm=llm,
        usage_sink=lambda result, model: _record_auxiliary_llm_usage("prompt_test", result, model),
    )
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
        data["retrieval_degradation"] = _retrieval_degradation_summary()
        return JSONResponse(data)
    except Exception as e:
        logger.error(f"retrieval_eval 查询失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


def _retrieval_degradation_summary() -> dict:
    """Summarize recent retrieval degradation and pending-profile exclusions."""
    with sqlite3.connect(agent.memory._db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        rows = conn.execute(
            """SELECT trace_data FROM usage_logs
               WHERE json_extract(trace_data, '$.retrieval') IS NOT NULL
               ORDER BY id DESC LIMIT 500"""
        ).fetchall()
    summary = {
        "sampled": 0, "degraded": 0, "pending_profile_excluded": 0,
        "degraded_stages": {}, "pending_ratio": 0.0, "degraded_ratio": 0.0,
    }
    for (trace_json,) in rows:
        try:
            trace = json.loads(trace_json or "{}")
        except (TypeError, ValueError):
            continue
        retrieval = trace.get("retrieval") or {}
        if not isinstance(retrieval, dict) or not retrieval:
            continue
        summary["sampled"] += 1
        if retrieval.get("retrieval_degraded"):
            summary["degraded"] += 1
        for stage in (retrieval.get("degraded_stages") or []):
            summary["degraded_stages"][str(stage)] = summary["degraded_stages"].get(str(stage), 0) + 1
        summary["pending_profile_excluded"] += int(retrieval.get("pending_profile_excluded") or 0)
    if summary["sampled"]:
        summary["pending_ratio"] = round(summary["pending_profile_excluded"] / summary["sampled"], 4)
        summary["degraded_ratio"] = round(summary["degraded"] / summary["sampled"], 4)
    return summary


# ==================== Misclassification Feedback Loop ====================
@router.get("/api/admin/misclassification-candidates")
def get_misclassification_candidates(request: Request, include_resolved: bool = False):
    """Return suspected misclassified documents for admin review."""
    try:
        require_permission(request, agent.memory, "tenant.manage", "")
        items = agent.memory.get_misclassification_candidates(include_resolved=include_resolved)
        return JSONResponse({"items": items, "total": len(items)})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"get_misclassification_candidates 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/admin/misclassification-candidates/{candidate_id}/resolve")
def resolve_misclassification_candidate(candidate_id: int, request: Request, data: dict):
    """Mark a misclassification candidate as reviewed with the corrected profile."""
    try:
        require_permission(request, agent.memory, "tenant.manage", "")
        resolved_profile = str(data.get("resolved_profile") or "").strip()
        if not resolved_profile:
            return JSONResponse({"error": "resolved_profile is required"}, status_code=400)
        ok = agent.memory.resolve_misclassification(candidate_id, resolved_profile)
        return JSONResponse({"status": "ok" if ok else "not_found", "resolved": ok})
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"resolve_misclassification_candidate 失败: {e}")
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
        expected = data.get("expected", "")
        if isinstance(expected, str):
            expected = expected.strip()
        profile = data.get("profile", "general")
        category = data.get("category", "")
        difficulty = data.get("difficulty", "medium")
        if not query or not expected:
            return JSONResponse({"error": "query 和 expected 不能为空"}, status_code=400)
        item_id = agent.memory.add_retrieval_eval_item(query, expected, category, difficulty, profile=profile)
        return JSONResponse({"id": item_id, "success": True})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@router.put("/api/stats/retrieval-eval/items")
def update_retrieval_eval_item(data: dict):
    try:
        item_id = data.get("id")
        if not item_id:
            return JSONResponse({"error": "id 不能为空"}, status_code=400)
        expected = data.get("expected", "")
        if isinstance(expected, str):
            expected = expected.strip()
        ok = agent.memory.update_retrieval_eval_item(
            item_id,
            data.get("query", ""),
            expected,
            data.get("category", ""),
            data.get("difficulty", "medium"),
            data.get("is_active", 1),
            profile=data.get("profile", "general"),
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
        profile = data.get("profile", "general")
        if not query or not expected:
            return JSONResponse({"error": "query 和 expected 不能为空"}, status_code=400)
        from _eval_retrieval import evaluate_single_query
        result = evaluate_single_query(agent, query, expected, profile=profile)
        return JSONResponse(result)
    except Exception as e:
        logger.error(f"run-single 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


@router.post("/api/stats/retrieval-eval/generate")
def retrieval_eval_generate(data: dict):
    keywords = data.get("keywords", "网络安全")
    from _eval_retrieval import generate_test_set_from_keywords, evaluate_with_items
    eval_llm = _get_backend_eval_llm()
    items = generate_test_set_from_keywords(
        keywords, llm=eval_llm,
        usage_sink=lambda result, model: _record_auxiliary_llm_usage("retrieval_evaluation", result, model),
    )
    result = evaluate_with_items(items, agent.memory)

    saved = 0
    for item in items:
        try:
            agent.memory.add_retrieval_eval_item(
                query=item.get("query", ""),
                expected=item.get("expected", ""),
                category=item.get("category", ""),
                difficulty=item.get("difficulty", "medium"),
                profile=item.get("profile", "general"),
            )
            saved += 1
        except Exception:
            continue
    logger.info(f"生成并跑分: {len(items)} 条, 已存入测试集 {saved} 条")

    summary_text = ""
    try:
        eval_llm = _get_backend_eval_llm()
        if eval_llm:
            summary_text = _generate_eval_summary(
                eval_llm, "retrieval_quality", result, items,
                usage_sink=lambda response, model: _record_auxiliary_llm_usage("retrieval_evaluation", response, model),
            )
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
        profile_counts = summary.get("profile_counts", {})
        profile_summary = ", ".join(
            f"{profile}: {count}" for profile, count in profile_counts.items()
        ) or "general"
        summary_cards.append((profile_summary, "评测 Profile 分布"))

        headers = ["#", "查询", "期望来源", "Profile", "R@5", "R@10", "MRR", "评估时间"]
        rows = []
        for i, item in enumerate(items[:60], 1):
            r5 = "✓" if item.get("recall_5") else "✗"
            r10 = "✓" if item.get("recall_10") else "✗"
            rows.append([
                i, item.get('query', ''), item.get('expected_source', ''),
                item.get('profile', 'general'),
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
    profile = data.get("profile", "general")
    from _eval_retrieval import generate_test_set_from_keywords, evaluate_with_items_compare
    eval_llm = _get_backend_eval_llm()
    items = generate_test_set_from_keywords(
        keywords, llm=eval_llm,
        usage_sink=lambda result, model: _record_auxiliary_llm_usage("retrieval_evaluation", result, model),
    )
    for item in items:
        item["profile"] = profile
    result = evaluate_with_items_compare(items, agent.memory)

    summary_text = ""
    try:
        eval_llm = _get_backend_eval_llm()
        if eval_llm:
            summary_text = _generate_eval_summary(
                eval_llm, "retrieval_compare", result, items,
                usage_sink=lambda response, model: _record_auxiliary_llm_usage("retrieval_evaluation", response, model),
            )
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
    profile_counts = summary.get("profile_counts", {})
    profile_summary = ", ".join(
        f"{profile}: {count}" for profile, count in profile_counts.items()
    ) or "general"
    summary_cards.append((profile_summary, "评测 Profile 分布"))

    headers = ["#", "查询", "Profile", "模式", "R@5", "MRR", "评估时间"]
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
                idx, query, it.get('profile', 'general'), mode_label,
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
    profile = data.get("profile", "general")
    from _eval_retrieval import generate_test_set_from_keywords
    items = generate_test_set_from_keywords(keywords, llm=_get_backend_eval_llm())

    agent.memory.clear_retrieval_eval_items()
    saved = 0
    for item in items:
        try:
            item["profile"] = profile
            agent.memory.add_retrieval_eval_item(
                query=item.get("query", ""),
                expected=item.get("expected", ""),
                category=item.get("category", ""),
                difficulty=item.get("difficulty", "medium"),
                profile=profile,
            )
            saved += 1
        except Exception:
            continue
    logger.info(f"生成测试集: {len(items)} 条, 已替换 {saved} 条")
    return {"ok": True, "items": items, "saved": saved}
