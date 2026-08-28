import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from capability_executor import (
    CapabilityExecutionError,
    _validate_mcp_arguments,
    execute_capability,
    execute_governed_mcp,
)
from governance_store import GovernanceStore
from memory import ConversationMemory


def _scoped_extension(memory, kind, source, manifest=None):
    user = memory.register_user(
        f"executor-{kind}@example.com", "Correct-Horse-30", "Executor",
    )
    extension = memory.create_capability_extension(
        kind, f"Test {kind}", "1.0", source, manifest=manifest or {},
        network_scope="test-local metadata only" if kind == "mcp" else "",
    )
    memory.review_capability_extension(extension["id"], "approved", "admin")
    memory.set_capability_extension_grant(
        extension["id"], user["tenant_id"], user["agent_id"], True, "admin",
    )
    return user, extension


def test_builtin_skill_executes_after_approval_and_records_usage(tmp_path):
    memory = ConversationMemory(str(tmp_path / "executor-skill.db"))
    user, extension = _scoped_extension(
        memory, "skill", "builtin://security-outline",
        {"runtime": "builtin", "operation": "security_outline"},
    )
    result = execute_capability(
        memory, extension["id"], user["tenant_id"], user["agent_id"],
        user["id"], "org_admin", {"query": "制定访问控制制度"},
    )
    assert result["status"] == "success"
    assert result["output"]["sections"]
    assert memory.capability_extension_usage(extension["id"])["calls"] == 1


def test_ppt_skills_create_parameterized_storyboard_with_quality_rules(tmp_path):
    memory = ConversationMemory(str(tmp_path / "executor-ppt.db"))
    user, extension = _scoped_extension(
        memory, "skill", "builtin://critical-infrastructure-ppt",
        {"runtime": "builtin", "operation": "critical_infrastructure_security_ppt"},
    )
    result = execute_capability(
        memory, extension["id"], user["tenant_id"], user["agent_id"],
        user["id"], "org_admin",
        {"query": "制作关键信息基础设施安全汇报 PPT",
         "fields": {"sector": "能源行业", "audience": "管理层", "purpose": "年度安全建设决策",
                    "scope": "关基安全保护", "page_count": 9,
                    "organization_context": "已完成资产梳理，监测能力待完善"}},
    )
    output = result["output"]
    assert output["mode"] == "presentation"
    assert len(output["pages"]) == 9
    assert output["fields"]["sector"] == "能源行业"
    assert "一页一个核心结论" in output["quality_rules"]
    assert all(page["message"] for page in output["pages"])


def test_general_security_ppt_skill_is_not_hardcoded_to_one_industry(tmp_path):
    memory = ConversationMemory(str(tmp_path / "executor-general-ppt.db"))
    user, extension = _scoped_extension(
        memory, "skill", "builtin://security-ppt",
        {"runtime": "builtin", "operation": "security_ppt_storyboard"},
    )
    result = execute_capability(
        memory, extension["id"], user["tenant_id"], user["agent_id"],
        user["id"], "org_admin",
        {"query": "制作网络安全运营汇报 PPT",
         "fields": {"sector": "制造业", "audience": "技术与运维人员", "page_count": 6}},
    )
    output = result["output"]
    assert output["fields"]["sector"] == "制造业"
    assert len(output["pages"]) == 6
    assert "关键信息基础设施" not in output["title"]


def test_python_json_skill_is_confined_to_configured_root(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    entrypoint = root / "skill.py"
    entrypoint.write_text(
        "import json,sys\n"
        "payload=json.load(sys.stdin)\n"
        "print(json.dumps({'received': payload.get('query')}))\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SECURENEXUS_SKILLS_ROOT", str(root))
    memory = ConversationMemory(str(tmp_path / "executor-process.db"))
    user, extension = _scoped_extension(
        memory, "skill", "local://skill",
        {"runtime": "python_json", "entrypoint": "skill.py", "timeout_seconds": 5},
    )
    result = execute_capability(
        memory, extension["id"], user["tenant_id"], user["agent_id"],
        user["id"], "org_admin", {"query": "检查日志"},
    )
    assert result["output"]["received"] == "检查日志"


def test_mcp_tools_call_returns_result_and_records_usage(tmp_path):
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("Content-Length", "0"))
            seen["body"] = json.loads(self.rfile.read(length))
            body = json.dumps({
                "jsonrpc": "2.0", "id": seen["body"]["id"],
                "result": {"content": [{"type": "text", "text": "ok"}]},
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        memory = ConversationMemory(str(tmp_path / "executor-mcp.db"))
        user, extension = _scoped_extension(
            memory, "mcp", f"http://127.0.0.1:{server.server_port}/rpc",
            {"tools": ["lookup"], "timeout_seconds": 5},
        )
        result = execute_capability(
            memory, extension["id"], user["tenant_id"], user["agent_id"],
            user["id"], "org_admin",
            {"tool_name": "lookup", "arguments": {"query": "访问控制"}},
        )
        assert result["status"] == "success"
        assert seen["body"]["method"] == "tools/call"
        assert seen["body"]["params"]["name"] == "lookup"
        assert memory.capability_extension_usage(extension["id"])["calls"] == 1
    finally:
        server.shutdown()


def test_stdio_mcp_executes_with_controlled_command_allowlist(tmp_path, monkeypatch):
    server_script = tmp_path / "stdio_mcp.py"
    server_script.write_text(
        "import json,sys\n"
        "def read_frame():\n"
        "    header=b''\n"
        "    while b'\\r\\n\\r\\n' not in header:\n"
        "        header += sys.stdin.buffer.read(1)\n"
        "    length=0\n"
        "    for line in header.decode().split('\\r\\n'):\n"
        "        if line.lower().startswith('content-length:'):\n"
        "            length=int(line.split(':',1)[1].strip())\n"
        "    return json.loads(sys.stdin.buffer.read(length).decode())\n"
        "def write_frame(payload):\n"
        "    body=json.dumps(payload).encode()\n"
        "    sys.stdout.buffer.write(f'Content-Length: {len(body)}\\r\\n\\r\\n'.encode()+body)\n"
        "    sys.stdout.buffer.flush()\n"
        "init=read_frame()\n"
        "write_frame({'jsonrpc':'2.0','id':init['id'],'result':{'protocolVersion':'2024-11-05','capabilities':{},'serverInfo':{'name':'test','version':'1'}}})\n"
        "read_frame()\n"
        "call=read_frame()\n"
        "write_frame({'jsonrpc':'2.0','id':call['id'],'result':{'content':[{'type':'text','text':call['params']['arguments']['query']} ]}})\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("SECURENEXUS_MCP_COMMAND_ALLOWLIST", "python.exe,python")
    memory = ConversationMemory(str(tmp_path / "executor-stdio-mcp.db"))
    user, extension = _scoped_extension(
        memory, "mcp", "stdio://local-test",
        {"transport": "stdio", "command": sys.executable, "args": [str(server_script)],
         "tools": ["lookup"], "timeout_seconds": 5},
    )
    result = execute_capability(
        memory, extension["id"], user["tenant_id"], user["agent_id"],
        user["id"], "org_admin",
        {"tool_name": "lookup", "arguments": {"query": "stdio-ok"}},
    )
    assert result["transport"] == "stdio"
    assert result["output"]["content"][0]["text"] == "stdio-ok"


def test_builtin_fetch_mcp_is_seeded_and_not_auto_granted(tmp_path):
    memory = ConversationMemory(str(tmp_path / "builtin-fetch.db"))
    user = memory.register_user("builtin-fetch@example.com", "Correct-Horse-30", "Fetch")
    memory.ensure_builtin_capability_extensions()
    extensions = {item["id"]: item for item in memory.list_capability_extensions("mcp")}

    assert "builtin-mcp-fetch" in extensions
    fetch = extensions["builtin-mcp-fetch"]
    assert fetch["status"] == "approved"
    assert fetch["source"] == "stdio://modelcontextprotocol-fetch"
    assert fetch["manifest"]["transport"] == "stdio"
    assert fetch["manifest"]["command"] == "uvx"
    assert fetch["manifest"]["tools"] == ["fetch"]
    assert fetch["manifest"]["block_private_network"] is True
    assert memory.list_enabled_capability_extensions(user["tenant_id"], user["agent_id"]) == []


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/",
        "http://localhost/",
        "http://10.0.0.1/",
        "http://192.168.1.1/",
        "http://[::1]/",
    ],
)
def test_fetch_mcp_rejects_private_or_local_urls(url):
    with pytest.raises(CapabilityExecutionError, match="禁止访问本机或内网地址"):
        _validate_mcp_arguments(
            {"block_private_network": True},
            {"url": url},
        )


def test_fetch_mcp_accepts_public_https_url():
    _validate_mcp_arguments(
        {"block_private_network": True},
        {"url": "https://www.example.com/security/article", "max_length": 2000},
    )


def test_governed_mcp_executes_after_policy_and_redacts_audit(tmp_path, monkeypatch):
    store = GovernanceStore(str(tmp_path / "governed-mcp.db"))
    secret = store.create_secret("tenant-a", "mcp-token", "mcp", "secret-token", "admin", "org_admin")
    server = store.register_mcp_server(
        "tenant-a", "governed", "https://mcp.example.test/rpc", "admin",
        secret_ref_id=secret["id"], auth_type="bearer",
    )
    store.upsert_mcp_tool("tenant-a", server["id"], "search", "lookup", {"type": "object"}, False, "admin", "org_admin")
    store.set_mcp_status("tenant-a", server["id"], "enabled", "admin", "org_admin")
    store.set_mcp_tool_policy(
        "tenant-a", server["id"], "search", allowed_roles=["org_admin"],
        allowed_agents=["agent-a"], param_allowlist=["query"], param_denylist=["token"],
        enabled=True, actor_user_id="admin", actor_role="org_admin",
    )
    seen = {}

    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"jsonrpc": "2.0", "id": "x", "result": {"content": [{"type": "text", "text": "ok"}]}}

    def fake_post(endpoint, json, headers, timeout):
        seen.update({"endpoint": endpoint, "json": json, "headers": headers, "timeout": timeout})
        return Response()

    monkeypatch.setattr("capability_executor.requests.post", fake_post)
    result = execute_governed_mcp(
        store, "tenant-a", server["id"], "search", "user-a", "org_admin", "agent-a", {"query": "访问控制"},
    )
    assert result["status"] == "success"
    assert seen["headers"]["Authorization"] == "Bearer secret-token"
    assert seen["json"]["params"]["name"] == "search"
    with store._connect() as conn:
        audit = conn.execute("SELECT param_summary FROM p8_mcp_call_audit").fetchall()
    assert "访问控制" not in json.dumps([row[0] for row in audit], ensure_ascii=False)
