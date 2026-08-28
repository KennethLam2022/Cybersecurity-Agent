"""Conservative, auditable workflow execution for SecureNexus."""
from __future__ import annotations

import json
import time
from typing import Any

from capability_router import build_outline, route_capability
from capability_executor import execute_capability

ALLOWED_NODE_TYPES = {
    "input", "intent_router", "retrieval", "tool_gate", "human_approval",
    "generation", "notification",
}

WORKFLOW_TEMPLATES = [
    {"key": "compliance_qa", "name": "合规问答", "description": "受控路由、授权检索范围和回答准备。",
     "nodes": [{"id": "input", "type": "input", "label": "输入"}, {"id": "intent", "type": "intent_router", "label": "意图识别"}, {"id": "retrieval", "type": "retrieval", "label": "知识检索"}, {"id": "notify", "type": "notification", "label": "通知"}],
     "edges": [["input", "intent"], ["intent", "retrieval"], ["retrieval", "notify"]]},
    {"key": "report_generation", "name": "报告生成", "description": "生成前收集边界，人工批准后只创建可编辑提纲。",
     "nodes": [{"id": "input", "type": "input", "label": "输入"}, {"id": "intent", "type": "intent_router", "label": "意图识别"}, {"id": "retrieval", "type": "retrieval", "label": "知识检索"}, {"id": "approval", "type": "human_approval", "label": "人工审核"}, {"id": "generation", "type": "generation", "label": "LLM 生成"}, {"id": "notify", "type": "notification", "label": "通知"}],
     "edges": [["input", "intent"], ["intent", "retrieval"], ["retrieval", "approval"], ["approval", "generation"], ["generation", "notify"]]},
    {"key": "ppt_generation", "name": "PPT 生成", "description": "PPT 范围确认、人工审核和可编辑提纲准备。",
     "nodes": [{"id": "input", "type": "input", "label": "输入"}, {"id": "intent", "type": "intent_router", "label": "意图识别"}, {"id": "tool", "type": "tool_gate", "label": "工具调用"}, {"id": "approval", "type": "human_approval", "label": "人工审核"}, {"id": "generation", "type": "generation", "label": "LLM 生成"}, {"id": "notify", "type": "notification", "label": "通知"}],
     "edges": [["input", "intent"], ["intent", "approval"], ["approval", "tool"], ["tool", "generation"], ["generation", "notify"]]},
    {"key": "incident_record", "name": "事件处置记录", "description": "对事件记录进行范围确认、人工审核和可审计交付准备。",
     "nodes": [{"id": "input", "type": "input", "label": "输入"}, {"id": "retrieval", "type": "retrieval", "label": "知识检索"}, {"id": "approval", "type": "human_approval", "label": "人工审核"}, {"id": "generation", "type": "generation", "label": "LLM 生成"}, {"id": "notify", "type": "notification", "label": "通知"}],
     "edges": [["input", "retrieval"], ["retrieval", "approval"], ["approval", "generation"], ["generation", "notify"]]},
]


def workflow_templates() -> list[dict]:
    return json.loads(json.dumps(WORKFLOW_TEMPLATES, ensure_ascii=False))


def validate_workflow_contract(nodes: list, edges: list) -> dict:
    """Validate the release-time safety contract, beyond graph syntax."""
    validate_workflow_graph(nodes, edges)
    by_id = {str(node["id"]): node for node in nodes}
    followers = {node_id: [] for node_id in by_id}
    predecessors = {node_id: [] for node_id in by_id}
    for source, target in edges:
        followers[source].append(target)
        predecessors[target].append(source)

    inputs = [node_id for node_id, node in by_id.items() if node["type"] == "input"]
    if len(inputs) != 1 or predecessors[inputs[0]]:
        raise ValueError("发布前必须存在且仅存在一个无入边的输入节点")

    def reaches(start: str, target_type: str, reverse: bool = False) -> bool:
        graph = predecessors if reverse else followers
        pending = list(graph[start])
        seen = set()
        while pending:
            current = pending.pop(0)
            if current in seen:
                continue
            seen.add(current)
            if by_id[current]["type"] == target_type:
                return True
            pending.extend(graph[current])
        return False

    generation_nodes = [node_id for node_id, node in by_id.items() if node["type"] == "generation"]
    approval_nodes = [node_id for node_id, node in by_id.items() if node["type"] == "human_approval"]
    for generation_id in generation_nodes:
        if not reaches(generation_id, "human_approval", reverse=True):
            raise ValueError("生成节点前必须经过人工审批节点")
    for tool_id, node in by_id.items():
        if node["type"] != "tool_gate":
            continue
        config = node.get("config") or {}
        allowed_tools = config.get("allowed_tools")
        if allowed_tools is not None and (not isinstance(allowed_tools, list) or "*" in allowed_tools):
            raise ValueError("工具门禁必须使用明确的工具白名单，不能使用通配符")
        if generation_nodes and not any(reaches(tool_id, "human_approval") and
                                        reaches(generation_id, "human_approval", reverse=True)
                                        for generation_id in generation_nodes):
            raise ValueError("工具调用必须在生成前经过人工审批")
    return {"ok": True, "input_node": inputs[0], "approval_required": bool(generation_nodes),
            "generation_nodes": generation_nodes, "approval_nodes": approval_nodes}


def validate_workflow_graph(nodes: list, edges: list) -> None:
    if not isinstance(nodes, list) or not isinstance(edges, list) or not nodes:
        raise ValueError("工作流必须包含节点和连线")
    ids: set[str] = set()
    for node in nodes:
        if not isinstance(node, dict):
            raise ValueError("节点格式不合法")
        node_id = str(node.get("id") or "").strip()
        node_type = str(node.get("type") or "").strip()
        if not node_id or len(node_id) > 80 or node_id in ids:
            raise ValueError("节点 ID 不能为空、不能重复且长度不超过 80")
        if node_type not in ALLOWED_NODE_TYPES:
            raise ValueError("存在不支持的工作流节点类型")
        config = node.get("config", {})
        if config is not None and not isinstance(config, dict):
            raise ValueError("节点参数必须是对象")
        if isinstance(config, dict) and "top_k" in config:
            try:
                top_k = int(config["top_k"])
            except (TypeError, ValueError) as exc:
                raise ValueError("检索节点 top_k 必须是整数") from exc
            if not 1 <= top_k <= 50:
                raise ValueError("检索节点 top_k 必须在 1 到 50 之间")
        ids.add(node_id)
    if not any(node.get("type") == "input" for node in nodes):
        raise ValueError("工作流必须包含输入节点")
    for edge in edges:
        if not isinstance(edge, (list, tuple)) or len(edge) != 2 or edge[0] not in ids or edge[1] not in ids:
            raise ValueError("连线必须引用已有节点")
    _topological_nodes(nodes, edges)


def _topological_nodes(nodes: list[dict], edges: list[list[str]]) -> list[dict]:
    by_id = {node["id"]: node for node in nodes}
    indegree = {node_id: 0 for node_id in by_id}
    followers = {node_id: [] for node_id in by_id}
    for source, target in edges:
        followers[source].append(target)
        indegree[target] += 1
    queue = [node["id"] for node in nodes if indegree[node["id"]] == 0]
    ordered: list[dict] = []
    while queue:
        node_id = queue.pop(0)
        ordered.append(by_id[node_id])
        for target in followers[node_id]:
            indegree[target] -= 1
            if indegree[target] == 0:
                queue.append(target)
    if len(ordered) != len(nodes):
        raise ValueError("工作流不能包含循环依赖")
    return ordered


def _summary(value: Any, limit: int = 240) -> str:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[:limit] + "..."


def execute_workflow(memory, run: dict, approval: bool | None = None, agent=None, notifier=None) -> dict:
    """Execute or resume a workflow run, recording every node trace."""
    version = memory.get_workflow_version(run["workflow_id"], run["version"])
    if not version:
        raise ValueError("工作流版本不存在")
    nodes = _topological_nodes(version["nodes"], version["edges"])
    output = dict(run.get("output") or {})
    state = dict(output.get("state") or {})
    completed = set(state.get("completed_nodes") or [])
    context = dict(run.get("input") or {})
    query = str(context.get("query") or "").strip()
    fields = context.get("fields") if isinstance(context.get("fields"), dict) else {}
    subject_user_id = str(context.get("user_id") or run.get("created_by") or "workflow")
    if not query:
        raise ValueError("运行工作流需要 query")
    state.setdefault("query", query)
    state.setdefault("fields", fields)
    for sequence, node in enumerate(nodes, start=1):
        node_id = node["id"]
        if node_id in completed:
            continue
        started = time.perf_counter()
        node_type = node["type"]
        try:
            if node_type == "input":
                result = {"query": query, "fields": fields, "config": node.get("config") or {}}
            elif node_type == "intent_router":
                result = route_capability(query, fields)
                result["config"] = node.get("config") or {}
                state["route"] = result
            elif node_type == "retrieval":
                if agent is None:
                    result = {"status": "scope_confirmed", "query": query, "tenant_id": run["tenant_id"],
                              "agent_id": run["agent_id"], "message": "仅确认本工作区和 Agent 的授权检索范围。",
                              "config": node.get("config") or {}}
                else:
                    from generation_evidence import collect_generation_references
                    evidence = collect_generation_references(
                        agent.retriever, query, fields, run["tenant_id"],
                        subject_user_id, run["agent_id"],
                        top_k=int((node.get("config") or {}).get("top_k", 5)),
                    )
                    result = {
                        "status": evidence.get("status"), "query": query,
                        "tenant_id": run["tenant_id"], "agent_id": run["agent_id"],
                        "references": evidence.get("references", []),
                        "scope": evidence.get("scope", {}),
                        "error": evidence.get("error", ""),
                        "message": "已完成授权范围内的真实检索，生成节点只使用这些引用元数据。",
                        "config": node.get("config") or {},
                    }
                state["retrieval"] = result
            elif node_type == "tool_gate":
                enabled = memory.list_enabled_capability_extensions(run["tenant_id"], run["agent_id"])
                config = node.get("config") or {}
                selected_id = str(config.get("extension_id") or "").strip()
                available = [{"id": item["id"], "kind": item["kind"], "name": item["name"]}
                             for item in enabled]
                result = {"enabled_extensions": available, "config": config}
                if selected_id:
                    if selected_id not in {item["id"] for item in enabled}:
                        raise ValueError("工具节点指定的扩展未审核通过或未授权")
                    result["execution"] = execute_capability(
                        memory, selected_id, run["tenant_id"], run["agent_id"],
                        subject_user_id, str(run.get("created_by") or "workflow"),
                        {"query": query, "fields": fields,
                         "tool_name": str(config.get("tool_name") or "").strip(),
                         "arguments": config.get("arguments") if isinstance(config.get("arguments"), dict) else {}},
                    )
                    result["message"] = "已执行指定的受控 Skill/MCP，并记录调用审计。"
                else:
                    result["message"] = "未指定具体扩展；已完成授权扩展清单检查。"
                state["tool_gate"] = result
            elif node_type == "human_approval":
                if approval is not True:
                    result = {"message": "等待管理员人工审核后继续", "approval_required": True}
                    memory.add_workflow_run_trace(run["id"], node_id, node_type, sequence, "awaiting_approval", _summary(state), _summary(result), int((time.perf_counter() - started) * 1000))
                    state["pending_node"] = node_id
                    output["state"] = state
                    output["awaiting_approval"] = result
                    memory.update_workflow_run(run["id"], run["tenant_id"], run["agent_id"], "awaiting_approval", output)
                    return memory.get_workflow_run(run["id"], run["tenant_id"], run["agent_id"]) or run
                result = {"approved": True, "approved_at": "recorded_in_audit",
                          "config": node.get("config") or {}}
                state.pop("pending_node", None)
            elif node_type == "generation":
                route = state.get("route") or route_capability(query, fields)
                if route.get("mode") == "chat":
                    result = {"status": "chat_handoff", "route": route, "message": "普通问答交由既有受控问答链路完成。"}
                elif route.get("clarification"):
                    result = {"status": "clarification_required", "route": route}
                elif agent is not None:
                    from generation_manager import artifact_path, render_artifact
                    outline = build_outline(route["mode"], query, route["fields"])
                    references = (state.get("retrieval") or {}).get("references") or []
                    artifact_id = f"workflow-{run['id']}-{node_id}"
                    output_path = artifact_path(
                        run["tenant_id"], subject_user_id, artifact_id,
                        outline.get("title", "网络安全交付物"), route["mode"], 1,
                    )
                    render_artifact(route["mode"], outline, output_path, 1, references)
                    result = {
                        "status": "artifact_created", "route": route, "outline": outline,
                        "artifact_id": artifact_id, "artifact_path": str(output_path),
                        "references": references,
                        "message": "已在人工审批后生成受控初稿，文件仍需管理员复核后对外使用。",
                        "config": node.get("config") or {},
                    }
                else:
                    result = {"status": "outline_ready", "route": route, "outline": build_outline(route["mode"], query, route["fields"]), "message": "已生成可编辑提纲；确认后再创建实际文件。", "config": node.get("config") or {}}
                state["generation"] = result
            elif node_type == "notification":
                payload = {
                    "tenant_id": run["tenant_id"], "agent_id": run["agent_id"],
                    "workflow_id": run["workflow_id"], "run_id": run["id"],
                    "link_path": "/admin?tab=workflows",
                    "notification_body": "工作流运行已完成，可查看节点 Trace 和输出结果。",
                }
                if notifier is not None:
                    published = notifier("workflow.completed", payload) or payload
                    result = {"event_type": "workflow.completed", "published": True,
                              "notification_id": published.get("notification_id", ""),
                              "message": "已发布站内通知并调度已配置的 Webhook。",
                              "config": node.get("config") or {}}
                else:
                    result = {"event_type": "workflow.completed", "published": False,
                              "ready": True, "message": "通知回调未配置，已保留待发布事件。",
                              "config": node.get("config") or {}}
                state["notification"] = result
            else:
                raise ValueError("不支持的节点类型")
            completed.add(node_id)
            state["completed_nodes"] = list(completed)
            memory.add_workflow_run_trace(run["id"], node_id, node_type, sequence, "completed", _summary(state), _summary(result), int((time.perf_counter() - started) * 1000))
        except Exception as exc:
            memory.add_workflow_run_trace(run["id"], node_id, node_type, sequence, "failed", _summary(state), "", int((time.perf_counter() - started) * 1000), str(exc))
            output["state"] = state
            memory.update_workflow_run(run["id"], run["tenant_id"], run["agent_id"], "failed", output, str(exc))
            raise
    output["state"] = state
    output["result"] = state.get("generation") or state.get("retrieval") or {"status": "completed"}
    memory.update_workflow_run(run["id"], run["tenant_id"], run["agent_id"], "completed", output)
    return memory.get_workflow_run(run["id"], run["tenant_id"], run["agent_id"]) or run
