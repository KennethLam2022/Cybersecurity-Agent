"""Governance APIs.

Browser callers only receive credential metadata.  Plaintext resolution is
deliberately kept inside service adapters via ``GovernanceStore.get_secret``.
"""
import json
import sqlite3

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import JSONResponse

from app_state import agent
from capability_executor import CapabilityExecutionError, execute_governed_mcp
from identity import require_permission
from governance_store import GovernanceStore
from profile_classifier import profile_options, profile_version_snapshot

router = APIRouter()


@router.get("/api/admin/roles/permissions")
def admin_role_permission_catalog(request: Request):
    _principal(request, "tenant.manage")
    return JSONResponse({"items": agent.memory.list_permission_catalog()})


@router.get("/api/admin/roles")
def admin_list_custom_roles(request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    return JSONResponse({"items": agent.memory.list_custom_roles(principal.tenant_id)})


@router.post("/api/admin/roles")
def admin_save_custom_role(request: Request, data: dict = Body(...)):
    try:
        principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
        item = agent.memory.save_custom_role(
            principal.tenant_id, str(data.get("name") or ""), str(data.get("description") or ""),
            data.get("permissions") if isinstance(data.get("permissions"), list) else [],
            principal.user_id, str(data.get("id") or ""),
        )
        return JSONResponse({"ok": True, "role": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/roles/{role_id}/status")
def admin_custom_role_status(role_id: str, request: Request, data: dict = Body(...)):
    try:
        principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
        if not agent.memory.set_custom_role_status(role_id, principal.tenant_id, str(data.get("status") or ""), principal.user_id):
            raise HTTPException(status_code=404, detail="自定义角色不存在")
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/roles/{role_id}/assign")
def admin_assign_custom_role(role_id: str, request: Request, data: dict = Body(...)):
    try:
        principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
        user_id = str(data.get("user_id") or "").strip()
        if not user_id:
            raise HTTPException(status_code=400, detail="必须指定用户")
        if not agent.memory.assign_custom_role(principal.tenant_id, user_id, role_id, principal.user_id):
            raise HTTPException(status_code=404, detail="用户不存在或不属于当前工作区")
        return JSONResponse({"ok": True})
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
def _principal(request: Request, permission: str, tenant_id: str = ""):
    return require_permission(request, agent.memory, permission, tenant_id)


def _governance_store() -> GovernanceStore:
    """Keep profile grants alongside the active Agent tenant data."""
    return GovernanceStore(str(agent.memory._db_path))


class _GovernanceStoreProxy:
    def __getattr__(self, name):
        return getattr(_governance_store(), name)


store = _GovernanceStoreProxy()


@router.get("/api/admin/governance/industry-profiles")
def list_industry_profiles(request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    store = _governance_store()
    grants = {item["profile"]: item for item in store.list_industry_profile_grants(principal.tenant_id)}
    items = []
    for profile in profile_options():
        if profile.get("scope") != "industry":
            continue
        item = {**profile, **grants.get(profile["profile"], {})}
        item["enabled"] = bool(item.get("enabled", False))
        item["profile_snapshot"] = profile_version_snapshot(profile["profile"])
        items.append(item)
    return JSONResponse({"items": items, "enabled_profiles": sorted(store.enabled_profiles(principal.tenant_id))})


@router.put("/api/admin/governance/industry-profiles/{profile}")
def set_industry_profile(profile: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    store = _governance_store()
    known = {item["profile"]: item for item in profile_options()}
    if profile not in known or known[profile].get("scope") != "industry":
        raise HTTPException(status_code=404, detail="网络安全行业扩展 Profile 不存在")
    try:
        item = store.set_industry_profile_grant(
            principal.tenant_id, profile, bool(data.get("enabled")),
            profile_version_snapshot(profile).get("profile_version", ""), principal.user_id,
            str(data.get("change_note") or ""),
        )
        return JSONResponse({"ok": True, "profile": item,
                             "enabled_profiles": sorted(store.enabled_profiles(principal.tenant_id))})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _sync_operations_tasks(tenant_id: str, actor_user_id: str) -> dict:
    """Project existing actionable records into idempotent operations tasks."""
    candidates = []
    with sqlite3.connect(agent.memory._db_path) as conn:
        for row in conn.execute("SELECT id, source_name, lifecycle_status FROM documents WHERE tenant_id=? AND lifecycle_status IN ('review','staged')", (tenant_id,)):
            candidates.append(("document_review", str(row[0]), f"审核文档：{row[1]}", f"当前状态：{row[2]}", "medium"))
        for row in conn.execute("SELECT id, source_name, metadata_json FROM documents WHERE tenant_id=?", (tenant_id,)):
            try:
                metadata = json.loads(row[2] or "{}")
            except (TypeError, ValueError):
                metadata = {}
            if metadata.get("profile_confirmed") is False:
                candidates.append(("profile_confirmation", str(row[0]), f"确认文档 Profile：{row[1]}", "文档 Profile 仍为候选状态，需要管理员人工确认。", "medium"))
        for row in conn.execute("SELECT id, canonical_question, occurrence_count FROM knowledge_gaps WHERE tenant_id=? AND status='open'", (tenant_id,)):
            candidates.append(("knowledge_gap", str(row[0]), f"处理知识缺口：{row[1][:80]}", f"出现次数：{row[2]}", "high"))
        for row in conn.execute("SELECT run_id, profile, status FROM agent_eval_runs WHERE tenant_id=? AND status='error'", (tenant_id,)):
            candidates.append(("evaluation_failure", str(row[0]), f"处理评测异常：{row[0]}", f"Profile：{row[1]}；状态：{row[2]}", "high"))
        for row in conn.execute("SELECT id, state FROM monitoring_issue_reports WHERE tenant_id=? AND state IN ('pending_review','review_plan','pending_approval')", (tenant_id,)):
            candidates.append(("monitoring_report", str(row[0]), f"审阅监控预案：{row[0]}", f"当前状态：{row[1]}", "critical"))
        for row in conn.execute("SELECT g.extension_id, e.name FROM capability_extension_grants g JOIN capability_extensions e ON e.id=g.extension_id WHERE g.tenant_id=? AND g.enabled=0 AND g.approved_by=''", (tenant_id,)):
            candidates.append(("extension_grant", str(row[0]), f"审核扩展授权：{row[1]}", "等待为当前工作区 Agent 分配扩展。", "medium"))
    created = 0
    for source_type, source_id, title, description, priority in candidates:
        _, was_created = store.ensure_operations_task(tenant_id, actor_user_id, source_type=source_type,
                                                      source_id=source_id, title=title,
                                                      description=description, priority=priority)
        created += int(was_created)
    return {"candidates": len(candidates), "created": created}


@router.get("/api/admin/governance/secrets")
def list_secrets(request: Request, tenant_id: str = "", category: str = ""):
    principal = _principal(request, "secrets.read", tenant_id)
    return JSONResponse({"items": store.list_secrets(principal.tenant_id, category or None)})


@router.post("/api/admin/governance/secrets")
def create_secret(request: Request, data: dict = Body(...)):
    principal = _principal(request, "secrets.manage", str(data.get("tenant_id") or ""))
    try:
        item = store.create_secret(
            principal.tenant_id, str(data.get("name") or ""), str(data.get("category") or ""),
            str(data.get("value") or ""), principal.user_id, principal.role,
            str(data.get("description") or ""), data.get("expires_at"),
        )
        return JSONResponse({"secret": item}, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/governance/secrets/{secret_id}/rotate")
def rotate_secret(secret_id: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "secrets.manage", str(data.get("tenant_id") or ""))
    try:
        item = store.rotate_secret(principal.tenant_id, secret_id, str(data.get("value") or ""),
                                   principal.user_id, principal.role, str(data.get("reason") or ""))
        return JSONResponse({"secret": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/governance/secrets/{secret_id}/revoke")
def revoke_secret(secret_id: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "secrets.manage", str(data.get("tenant_id") or ""))
    try:
        return JSONResponse(store.revoke_secret(principal.tenant_id, secret_id,
                                                principal.user_id, principal.role,
                                                str(data.get("reason") or "")))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/governance/secrets/{secret_id}/versions")
def list_secret_versions(secret_id: str, request: Request, tenant_id: str = ""):
    principal = _principal(request, "secrets.read", tenant_id)
    try:
        return JSONResponse({"items": store.secret_versions(principal.tenant_id, secret_id)})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/admin/governance/audit")
def list_governance_audit(request: Request, tenant_id: str = "", resource_type: str = "", limit: int = 100):
    principal = _principal(request, "audit.read", tenant_id)
    return JSONResponse({"items": store.list_audit(principal.tenant_id, resource_type or None, limit)})


@router.get("/api/admin/governance/saved-searches")
def list_saved_searches(request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    return JSONResponse({"items": store.list_saved_searches(principal.tenant_id, principal.user_id)})


@router.post("/api/admin/governance/saved-searches")
def save_search(request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        item = store.save_search(principal.tenant_id, principal.user_id, data.get("name", ""),
                                 data.get("query_text", ""), data.get("resource_types") or [])
        return JSONResponse({"item": item}, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/api/admin/governance/saved-searches/{search_id}")
def delete_saved_search(search_id: str, request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    if not store.delete_saved_search(principal.tenant_id, principal.user_id, search_id):
        raise HTTPException(status_code=404, detail="保存的搜索不存在")
    return JSONResponse({"ok": True})


@router.put("/api/admin/governance/search-synonyms")
def save_search_synonym(request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        item = store.upsert_search_synonym(principal.tenant_id, data.get("term", ""), data.get("alternatives") or [], principal.user_id)
        return JSONResponse({"item": item})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/governance/search-ranking")
def get_search_ranking(request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    return JSONResponse({"weights": store.get_search_ranking(principal.tenant_id)})


@router.put("/api/admin/governance/search-ranking")
def save_search_ranking(request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        return JSONResponse({"weights": store.save_search_ranking(principal.tenant_id, data.get("weights") or {}, principal.user_id)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/governance/tasks")
def list_operations_tasks(request: Request, tenant_id: str = "", status: str = "", assignee_user_id: str = "", sla_state: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    sync = _sync_operations_tasks(principal.tenant_id, principal.user_id)
    return JSONResponse({"items": store.list_operations_tasks(principal.tenant_id, status, assignee_user_id, sla_state), "sync": sync})


@router.post("/api/admin/governance/tasks/sync")
def sync_operations_tasks(request: Request, data: dict = Body(default={})):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    return JSONResponse({"ok": True, **_sync_operations_tasks(principal.tenant_id, principal.user_id)})


@router.post("/api/admin/governance/tasks")
def create_operations_task(request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        task = store.create_operations_task(principal.tenant_id, principal.user_id,
                                            source_type=data.get("source_type", "manual"), title=data.get("title", ""),
                                            description=data.get("description", ""), source_id=data.get("source_id", ""),
                                            priority=data.get("priority", "medium"), assignee_user_id=data.get("assignee_user_id", ""),
                                            collaborator_user_ids=data.get("collaborator_user_ids") or [], department=data.get("department", ""),
                                            due_at=data.get("due_at"), sla_hours=data.get("sla_hours"))
        return JSONResponse({"task": task}, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.patch("/api/admin/governance/tasks/{task_id}")
def update_operations_task(task_id: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        task = store.update_operations_task(principal.tenant_id, task_id, principal.user_id, data)
        if not task: raise HTTPException(status_code=404, detail="待办不存在")
        return JSONResponse({"task": task})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/governance/tasks/{task_id}/comments")
def add_operations_task_comment(task_id: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        return JSONResponse({"comment": store.add_operations_task_comment(principal.tenant_id, task_id, principal.user_id, data.get("content", ""))}, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/governance/tasks/{task_id}/comments")
def list_operations_task_comments(task_id: str, request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    return JSONResponse({"items": store.list_operations_task_comments(principal.tenant_id, task_id)})


@router.post("/api/admin/governance/tasks/{task_id}/attachments")
def add_operations_task_attachment(task_id: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        item = store.add_operations_task_attachment(principal.tenant_id, task_id, principal.user_id,
                                                    data.get("name", ""), data.get("resource_type", "reference"),
                                                    data.get("resource_id", ""))
        return JSONResponse({"attachment": item}, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/governance/tasks/{task_id}/attachments")
def list_operations_task_attachments(task_id: str, request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    return JSONResponse({"items": store.list_operations_task_attachments(principal.tenant_id, task_id)})


@router.get("/api/admin/governance/notification-policies")
def list_notification_policies(request: Request, tenant_id: str = ""):
    principal = _principal(request, "tenant.manage", tenant_id)
    return JSONResponse({"items": store.list_notification_policies(principal.tenant_id)})


@router.put("/api/admin/governance/notification-policies")
def save_notification_policy(request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    try:
        return JSONResponse({"policy": store.upsert_notification_policy(principal.tenant_id, principal.user_id, data)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/governance/notification-policies/preview")
def preview_notification_policy(request: Request, data: dict = Body(...)):
    principal = _principal(request, "tenant.manage", str(data.get("tenant_id") or ""))
    event_type = str(data.get("event_type") or "").strip()
    if not event_type:
        raise HTTPException(status_code=400, detail="event_type 不能为空")
    # Preview reads matching policies but intentionally does not create a delivery record.
    policies = [item for item in store.list_notification_policies(principal.tenant_id)
                if item.get("enabled") and event_type in item.get("event_types", [])]
    return JSONResponse({"event_type": event_type, "matching_policies": policies,
                         "note": "预览不投递通知；实际投递会应用静默期和重复抑制。"})


@router.get("/api/admin/governance/notification-policy-deliveries")
def list_notification_policy_deliveries(request: Request, tenant_id: str = "", policy_id: str = "", limit: int = 100):
    principal = _principal(request, "tenant.manage", tenant_id)
    return JSONResponse({"items": store.list_notification_policy_deliveries(principal.tenant_id, policy_id, limit)})


@router.get("/api/admin/governance/mcp-servers")
def list_mcp_servers(request: Request, tenant_id: str = ""):
    principal = _principal(request, "mcp.read", tenant_id)
    return JSONResponse({"items": store.list_mcp_servers(principal.tenant_id)})


@router.post("/api/admin/governance/mcp-servers")
def register_mcp_server(request: Request, data: dict = Body(...)):
    principal = _principal(request, "mcp.manage", str(data.get("tenant_id") or ""))
    try:
        item = store.register_mcp_server(principal.tenant_id, str(data.get("name") or ""),
                                         str(data.get("endpoint") or ""), principal.user_id,
                                         secret_ref_id=str(data.get("secret_ref_id") or ""),
                                         auth_type=str(data.get("auth_type") or "none"))
        return JSONResponse({"server": item}, status_code=201)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/governance/mcp-servers/{server_id}/status")
def change_mcp_status(server_id: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "mcp.manage", str(data.get("tenant_id") or ""))
    try:
        return JSONResponse({"server": store.set_mcp_status(principal.tenant_id, server_id, str(data.get("status") or ""), principal.user_id, principal.role)})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/api/admin/governance/mcp-servers/{server_id}/tools")
def list_mcp_tools(server_id: str, request: Request, tenant_id: str = ""):
    principal = _principal(request, "mcp.read", tenant_id)
    try:
        return JSONResponse({"items": store.list_mcp_tools(principal.tenant_id, server_id)})
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.put("/api/admin/governance/mcp-servers/{server_id}/tools/{tool_name}")
def save_mcp_tool(server_id: str, tool_name: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "mcp.manage", str(data.get("tenant_id") or ""))
    try:
        items = store.upsert_mcp_tool(principal.tenant_id, server_id, tool_name, data.get("description", ""), data.get("input_schema", {}), bool(data.get("high_risk")), principal.user_id, principal.role)
        return JSONResponse({"items": items})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/api/admin/governance/mcp-servers/{server_id}/tools/{tool_name}/policy")
def save_mcp_policy(server_id: str, tool_name: str, request: Request, data: dict = Body(...)):
    principal = _principal(request, "mcp.manage", str(data.get("tenant_id") or ""))
    try:
        store.set_mcp_tool_policy(principal.tenant_id, server_id, tool_name,
                                  allowed_roles=data.get("allowed_roles", []), allowed_agents=data.get("allowed_agents", []),
                                  param_allowlist=data.get("param_allowlist", []), param_denylist=data.get("param_denylist", []),
                                  enabled=bool(data.get("enabled")), actor_user_id=principal.user_id, actor_role=principal.role)
        return JSONResponse({"ok": True})
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/admin/governance/mcp-servers/{server_id}/tools/{tool_name}/execute")
def execute_governance_mcp_tool(server_id: str, tool_name: str, request: Request, data: dict = Body(default={})):
    principal = _principal(request, "mcp.execute", str(data.get("tenant_id") or ""))
    try:
        result = execute_governed_mcp(
            store, principal.tenant_id, server_id, tool_name, principal.user_id,
            principal.role, str(data.get("agent_id") or principal.agent_id),
            data.get("arguments") if isinstance(data.get("arguments"), dict) else {},
        )
        return JSONResponse({"ok": True, "result": result})
    except CapabilityExecutionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
