"""Controlled execution adapters for approved Skill and MCP capabilities.

The execution boundary is intentionally narrow:
* only an approved and Agent-granted extension can run;
* Skills are either built-in operations or JSON subprocesses below the
  configured skills root;
* MCP uses JSON-RPC tools/call and never accepts credentials from a manifest;
* every attempt is recorded through the existing extension usage ledger.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any

import requests


class CapabilityExecutionError(RuntimeError):
    """A controlled extension execution failed."""


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)


def _skills_root() -> Path:
    configured = os.environ.get("SECURENEXUS_SKILLS_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    return (Path(__file__).resolve().parents[3] / "skills").resolve()


def _safe_child(root: Path, candidate: str) -> Path:
    path = Path(candidate).expanduser()
    if not path.is_absolute():
        path = root / path
    resolved = path.resolve()
    if resolved != root and root not in resolved.parents:
        raise CapabilityExecutionError("Skill entrypoint 必须位于受控 Skill 目录")
    return resolved


def _builtin_skill(operation: str, payload: dict[str, Any]) -> dict[str, Any]:
    query = str(payload.get("query") or "").strip()
    if operation == "echo":
        return {"status": "success", "operation": operation, "output": payload}
    if operation == "security_outline":
        if not query:
            raise CapabilityExecutionError("security_outline 需要 query")
        return {
            "status": "success",
            "operation": operation,
            "output": {
                "title": "网络安全任务执行提纲",
                "query": query,
                "sections": ["目标与范围", "风险识别", "控制措施", "验证与交付"],
                "note": "内置 Skill 仅生成结构化提纲，具体结论必须引用授权知识库。",
            },
        }
    if operation in {"security_ppt_storyboard", "critical_infrastructure_security_ppt"}:
        if not query:
            raise CapabilityExecutionError(f"{operation} 需要 query")
        fields = payload.get("fields") if isinstance(payload.get("fields"), dict) else {}
        page_count = _bounded_int(fields.get("page_count") or payload.get("page_count"), 8, 6, 15)
        audience = str(fields.get("audience") or "网络安全相关管理与技术人员").strip()
        purpose = str(fields.get("purpose") or "形成可执行的安全决策").strip()
        scope = str(fields.get("scope") or "网络安全通用场景").strip()
        sector = str(fields.get("sector") or fields.get("industry") or "待确认行业").strip()
        organization_context = str(fields.get("organization_context") or "待补充组织现状").strip()
        critical = operation == "critical_infrastructure_security_ppt"
        title = str(fields.get("title") or (
            f"{sector}关键信息基础设施安全" if critical else "网络安全专题汇报"
        )).strip()
        sections = (
            ["封面", "为什么现在要关注", "保护对象与范围", "现状与关键风险",
             "治理责任与制度", "技术与运营控制", "监测、响应与恢复",
             "实施路线与优先级", "验收指标与证据", "结论与行动清单"]
            if critical else
            ["封面", "目标与受众", "现状与问题", "风险判断", "控制策略",
             "实施路线", "证据与指标", "结论与下一步"]
        )
        sections = sections[:page_count]
        while len(sections) < page_count:
            sections.insert(-1, f"专题分析 {len(sections) - 1}")
        pages = []
        for index, section in enumerate(sections, 1):
            if index == 1:
                points = [title, f"受众：{audience}", f"目的：{purpose}"]
            elif section == "为什么现在要关注":
                points = ["用一个核心变化说明紧迫性", "只保留与本次决策直接相关的 2-3 个事实", "事实必须绑定授权资料或明确标注待核验"]
            elif section == "现状与关键风险":
                points = [f"组织现状：{organization_context}", "按影响与可能性排序风险", "每个风险对应证据、责任人和下一步动作"]
            elif section == "验收指标与证据":
                points = ["指标必须可量化、可复核", "说明数据来源、频率和责任人", "避免使用无法验证的绝对安全表述"]
            elif section == "结论与行动清单":
                points = ["明确本次需要决策的 1-3 件事", "列出优先级、负责人、时间和验收证据", "保留待确认事项，不擅自补齐"]
            else:
                points = [f"{section}的核心结论", "关键依据与适用边界", "建议动作、负责人和验证方式"]
            pages.append({
                "page": index, "title": section, "message": points[0],
                "points": points, "visual": "结论型标题 + 少量证据 + 图表/流程/清单",
                "speaker_note": "本页只服务一个结论；法规、标准和组织现状必须回到授权资料核验。",
            })
        return {
            "status": "success", "operation": operation,
            "output": {"title": title, "mode": "presentation", "query": query,
                       "fields": {**fields, "audience": audience, "purpose": purpose,
                                  "scope": scope, "sector": sector,
                                  "organization_context": organization_context},
                       "quality_rules": [
                           "一页一个核心结论", "标题表达结论而非主题",
                           "证据可追溯且区分事实与建议", "避免页面文字过载",
                           "关基安全范围、行业和组织现状不清晰时先追问",
                       ],
                       "pages": pages},
        }
    raise CapabilityExecutionError(f"未知内置 Skill 操作: {operation}")


def _bounded_int(value: Any, default: int, minimum: int, maximum: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        number = default
    return max(minimum, min(number, maximum))


def _execute_process_skill(extension: dict, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = extension.get("manifest") or {}
    entrypoint = str(manifest.get("entrypoint") or "").strip()
    if not entrypoint:
        raise CapabilityExecutionError("Skill 未声明 entrypoint")
    root = _skills_root()
    path = _safe_child(root, entrypoint)
    if not path.is_file() or path.suffix.lower() != ".py":
        raise CapabilityExecutionError("Skill entrypoint 必须是受控目录中的 Python 文件")
    expected_hash = str(manifest.get("sha256") or "").strip().lower()
    if expected_hash:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != expected_hash:
            raise CapabilityExecutionError("Skill entrypoint 校验摘要不匹配")
    timeout = max(1, min(int(manifest.get("timeout_seconds", 30)), 120))
    process = subprocess.run(
        [sys.executable, str(path)],
        input=json.dumps(payload, ensure_ascii=False),
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(root),
        env={**os.environ, "PYTHONPATH": str(root)},
        shell=False,
        check=False,
    )
    if process.returncode != 0:
        raise CapabilityExecutionError(
            f"Skill 执行失败（退出码 {process.returncode}）"
        )
    try:
        result = json.loads(process.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CapabilityExecutionError("Skill 输出不是合法 JSON") from exc
    if not isinstance(result, dict):
        raise CapabilityExecutionError("Skill 输出必须是 JSON 对象")
    return {"status": "success", "operation": "python_json", "output": result}


def _mcp_url_allowed(endpoint: str) -> bool:
    parsed = urllib.parse.urlparse(endpoint)
    if parsed.scheme == "https" and parsed.hostname:
        return True
    if os.environ.get("APP_ENV", "development").lower() in {"production", "prod"}:
        return False
    return parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost", "::1"}


def _execute_mcp(extension: dict, payload: dict[str, Any]) -> dict[str, Any]:
    manifest = extension.get("manifest") or {}
    transport = str(manifest.get("transport") or "http").strip().lower()
    tool_name = str(payload.get("tool_name") or manifest.get("default_tool") or "").strip()
    if not tool_name:
        raise CapabilityExecutionError("MCP 未指定 tool_name")
    declared_tools = manifest.get("tools") or []
    if declared_tools and tool_name not in declared_tools:
        raise CapabilityExecutionError("MCP 工具不在登记的工具白名单中")
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        arguments = {}
    _validate_mcp_arguments(manifest, arguments)
    timeout = max(1, min(int(manifest.get("timeout_seconds", 15)), 60))
    if transport == "stdio":
        return _execute_stdio_mcp(manifest, tool_name, arguments, timeout)
    endpoint = str(extension.get("source") or "").strip()
    if not _mcp_url_allowed(endpoint):
        raise CapabilityExecutionError("MCP 仅允许 HTTPS；开发环境只允许本机 HTTP 测试地址")
    return _execute_http_mcp(endpoint, tool_name, arguments, timeout)


def _execute_http_mcp(endpoint: str, tool_name: str, arguments: dict[str, Any],
                      timeout: int, headers: dict[str, str] | None = None) -> dict[str, Any]:
    request_id = uuid.uuid4().hex
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    response = requests.post(
        endpoint,
        json={"jsonrpc": "2.0", "id": request_id, "method": "tools/call",
              "params": {"name": tool_name, "arguments": arguments}},
        headers=request_headers,
        timeout=timeout,
    )
    response.raise_for_status()
    body = response.json()
    if body.get("error"):
        raise CapabilityExecutionError(str(body["error"].get("message") or "MCP 返回错误"))
    return {"status": "success", "operation": "tools/call", "tool_name": tool_name,
            "output": _jsonable(body.get("result", {}))}


def _validate_mcp_arguments(manifest: dict[str, Any], arguments: dict[str, Any]) -> None:
    if not manifest.get("block_private_network"):
        return
    candidates: list[str] = []
    for key in ("url", "uri", "href"):
        value = arguments.get(key)
        if isinstance(value, str):
            candidates.append(value)
    urls = arguments.get("urls")
    if isinstance(urls, list):
        candidates.extend(str(item) for item in urls if isinstance(item, str))
    for value in candidates:
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise CapabilityExecutionError("MCP Fetch 仅允许访问明确的 HTTP/HTTPS 公网 URL")
        host = parsed.hostname.strip().lower()
        if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
            raise CapabilityExecutionError("MCP Fetch 禁止访问本机或内网地址")
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            continue
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved:
            raise CapabilityExecutionError("MCP Fetch 禁止访问本机或内网地址")


def _allowed_mcp_command(command: str) -> str:
    command_name = Path(command).name.lower()
    allowed = {
        item.strip().lower()
        for item in os.environ.get(
            "SECURENEXUS_MCP_COMMAND_ALLOWLIST", "npx,npx.cmd,node,python,python.exe,uvx,uvx.exe"
        ).split(",")
        if item.strip()
    }
    if command_name not in allowed:
        raise CapabilityExecutionError("MCP stdio 命令不在管理员白名单中")
    resolved = shutil.which(command) or command
    return resolved


def _write_mcp_frame(stdin, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode("ascii") + body)
    stdin.flush()


def _read_mcp_frame(stdout) -> dict[str, Any]:
    header = b""
    while b"\r\n\r\n" not in header:
        chunk = stdout.read(1)
        if not chunk:
            raise CapabilityExecutionError("MCP stdio 未返回响应")
        header += chunk
        if len(header) > 8192:
            raise CapabilityExecutionError("MCP stdio 响应头过大")
    length = 0
    for line in header.decode("ascii", errors="ignore").split("\r\n"):
        name, _, value = line.partition(":")
        if name.lower() == "content-length":
            length = int(value.strip())
    if length <= 0 or length > 10 * 1024 * 1024:
        raise CapabilityExecutionError("MCP stdio 响应长度不合法")
    return json.loads(stdout.read(length).decode("utf-8"))


def _execute_stdio_mcp(manifest: dict[str, Any], tool_name: str,
                       arguments: dict[str, Any], timeout: int) -> dict[str, Any]:
    command = str(manifest.get("command") or "").strip()
    args = manifest.get("args") or []
    if not command or not isinstance(args, list):
        raise CapabilityExecutionError("MCP stdio 必须声明 command 和 args")
    argv = [_allowed_mcp_command(command), *[str(item) for item in args]]
    process = subprocess.Popen(
        argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        shell=False, text=False,
    )
    responses: queue.Queue[dict[str, Any] | Exception] = queue.Queue()

    def reader() -> None:
        try:
            while process.poll() is None:
                responses.put(_read_mcp_frame(process.stdout))
        except Exception as exc:  # pragma: no cover - exercised through timeout/error handling
            responses.put(exc)

    def next_response(expected_id: str, deadline: float) -> dict[str, Any]:
        while time.perf_counter() < deadline:
            try:
                body = responses.get(timeout=max(0.05, min(0.25, deadline - time.perf_counter())))
            except queue.Empty:
                continue
            if isinstance(body, Exception):
                raise CapabilityExecutionError(str(body)) from body
            if body.get("id") == expected_id:
                return body
        raise CapabilityExecutionError("MCP stdio 调用超时")

    try:
        if process.stdin is None or process.stdout is None:
            raise CapabilityExecutionError("MCP stdio 管道初始化失败")
        threading.Thread(target=reader, daemon=True).start()
        deadline = time.perf_counter() + timeout
        initialize_id = uuid.uuid4().hex
        _write_mcp_frame(process.stdin, {
            "jsonrpc": "2.0", "id": initialize_id, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "SecureNexus", "version": "1.0"},
            },
        })
        init_body = next_response(initialize_id, deadline)
        if init_body.get("error"):
            raise CapabilityExecutionError(str(init_body["error"].get("message") or "MCP 初始化失败"))
        _write_mcp_frame(process.stdin, {
            "jsonrpc": "2.0", "method": "notifications/initialized", "params": {},
        })
        call_id = uuid.uuid4().hex
        _write_mcp_frame(process.stdin, {
            "jsonrpc": "2.0", "id": call_id, "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        })
        body = next_response(call_id, deadline)
        if body.get("error"):
            raise CapabilityExecutionError(str(body["error"].get("message") or "MCP 返回错误"))
        return {"status": "success", "operation": "tools/call", "tool_name": tool_name,
                "transport": "stdio", "output": _jsonable(body.get("result", {}))}
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.communicate(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()


def execute_governed_mcp(store, tenant_id: str, server_id: str, tool_name: str,
                         user_id: str, role: str, agent_id: str,
                         arguments: dict[str, Any] | None = None) -> dict[str, Any]:
    """Execute a governance-registered MCP tool after policy authorization."""
    params = arguments if isinstance(arguments, dict) else {}
    started = time.perf_counter()
    try:
        if not store.authorize_mcp_call(
            tenant_id, server_id, tool_name, user_id=user_id, role=role,
            agent_id=agent_id, params=params,
        ):
            store.record_mcp_call(tenant_id, server_id, tool_name, user_id, agent_id, params, "denied", 0)
            raise CapabilityExecutionError("MCP 工具未启用、未授权或参数不符合策略")
        server = store.get_mcp_server(tenant_id, server_id)
        timeout = 15
        headers: dict[str, str] = {}
        auth_type = str(server.get("auth_type") or "none")
        if auth_type != "none":
            secret = store.get_secret(
                tenant_id, str(server.get("secret_ref_id") or ""), user_id, role,
                reason=f"execute mcp {server_id}:{tool_name}", include_value=True,
            )
            value = str(secret.get("value") or "")
            if auth_type in {"bearer", "oauth2"}:
                headers["Authorization"] = f"Bearer {value}"
            elif auth_type == "api_key":
                headers["X-API-Key"] = value
        result = _execute_http_mcp(str(server.get("endpoint") or ""), tool_name, params, timeout, headers)
        duration_ms = int((time.perf_counter() - started) * 1000)
        store.record_mcp_call(tenant_id, server_id, tool_name, user_id, agent_id, params, "success", duration_ms)
        return {"server_id": server_id, "kind": "mcp", "status": "success",
                "duration_ms": duration_ms, **result}
    except CapabilityExecutionError:
        raise
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        store.record_mcp_call(tenant_id, server_id, tool_name, user_id, agent_id, params, type(exc).__name__, duration_ms)
        raise CapabilityExecutionError("治理中心 MCP 执行失败") from exc


def execute_capability(memory, extension_id: str, tenant_id: str, agent_id: str,
                       user_id: str, role: str, payload: dict[str, Any] | None = None) -> dict:
    """Execute one approved, granted capability and record a redacted result."""
    extension = memory.get_capability_extension(extension_id)
    if not extension:
        raise CapabilityExecutionError("扩展不存在")
    enabled = {item["id"] for item in memory.list_enabled_capability_extensions(tenant_id, agent_id)}
    if extension_id not in enabled:
        raise CapabilityExecutionError("扩展未审核通过或未授权给当前 Agent")
    request_payload = payload if isinstance(payload, dict) else {}
    started = time.perf_counter()
    try:
        if extension["kind"] == "skill":
            manifest = extension.get("manifest") or {}
            runtime = str(manifest.get("runtime") or "builtin").strip()
            result = (_builtin_skill(str(manifest.get("operation") or "echo"), request_payload)
                      if runtime == "builtin"
                      else _execute_process_skill(extension, request_payload))
        elif extension["kind"] == "mcp":
            result = _execute_mcp(extension, request_payload)
        else:
            raise CapabilityExecutionError("不支持的扩展类型")
        duration_ms = int((time.perf_counter() - started) * 1000)
        memory.record_capability_extension_call(
            extension_id, tenant_id, agent_id, "success", duration_ms,
        )
        memory.log_audit(
            tenant_id, user_id, agent_id, "extension.execute", "extension",
            extension_id, {"kind": extension["kind"], "status": "success",
                           "duration_ms": duration_ms, "role": role},
        )
        return {"extension_id": extension_id, "kind": extension["kind"],
                "status": "success", "duration_ms": duration_ms, **result}
    except Exception as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        try:
            memory.record_capability_extension_call(
                extension_id, tenant_id, agent_id, "error", duration_ms, type(exc).__name__,
            )
            memory.log_audit(
                tenant_id, user_id, agent_id, "extension.execute", "extension",
                extension_id, {"kind": extension["kind"], "status": "error",
                               "duration_ms": duration_ms, "error_type": type(exc).__name__},
            )
        finally:
            if isinstance(exc, CapabilityExecutionError):
                raise
        raise CapabilityExecutionError("扩展执行失败") from exc
