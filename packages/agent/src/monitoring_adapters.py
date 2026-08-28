"""Allowlisted, review-first monitoring change adapters.

The default adapter is deliberately side-effect free. Deployments may register
a platform-specific adapter only after its credentials, allowlist and rollback
contract have been reviewed outside the AI diagnosis path.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Any, Protocol
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ChangeAdapter(Protocol):
    name: str

    def execute(self, plan: dict, context: dict) -> dict: ...
    def verify(self, plan: dict, context: dict) -> dict: ...
    def rollback(self, plan: dict, context: dict) -> dict: ...


@dataclass(frozen=True)
class ManualHandoffAdapter:
    name: str = "manual_handoff"

    def execute(self, plan: dict, context: dict) -> dict:
        return {
            "status": "manual_required",
            "adapter": self.name,
            "side_effects": False,
            "message": "已生成人工执行交接，不会自动修改任何平台配置。",
            "plan": plan,
        }

    def verify(self, plan: dict, context: dict) -> dict:
        return {"status": "manual_required", "adapter": self.name, "side_effects": False,
                "message": "请管理员按验证方案执行并回填验证结果。"}

    def rollback(self, plan: dict, context: dict) -> dict:
        return {"status": "manual_required", "adapter": self.name, "side_effects": False,
                "message": "请管理员按回滚方案人工执行；系统未自动回滚。"}


@dataclass(frozen=True)
class WebhookChangeAdapter:
    """Environment-configured adapter for an organization's change gateway."""
    endpoint: str
    token: str
    name: str = "webhook_change"

    def _call(self, action: str, plan: dict, context: dict) -> dict:
        payload = {"action": action, "plan": plan, "context": {
            "tenant_id": str(context.get("tenant_id") or ""),
            "actor_id": str(context.get("actor_id") or ""),
            "report_id": str(context.get("report_id") or ""),
        }}
        headers = {"Content-Type": "application/json", "User-Agent": "SecureNexus-Monitoring-Adapter/1.0"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        request = Request(self.endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"), headers=headers, method="POST")
        try:
            with urlopen(request, timeout=20) as response:  # nosec B310 - endpoint is HTTPS and host allowlisted below
                body = response.read(16_384).decode("utf-8", errors="replace")
                try:
                    remote = json.loads(body) if body else {}
                except ValueError:
                    remote = {"message": body[:1000]}
                if not isinstance(remote, dict):
                    remote = {"message": str(remote)[:1000]}
                return {"status": str(remote.get("status") or ("executed" if action != "verify" else "verified")),
                        "adapter": self.name, "side_effects": action in {"execute", "rollback"},
                        "message": str(remote.get("message") or "平台变更网关已响应")[:2000],
                        "remote_request_id": str(remote.get("request_id") or "")[:200]}
        except Exception as exc:
            return {"status": "failed", "adapter": self.name, "side_effects": False,
                    "message": str(exc)[:1000]}

    def execute(self, plan: dict, context: dict) -> dict:
        return self._call("execute", plan, context)

    def verify(self, plan: dict, context: dict) -> dict:
        return self._call("verify", plan, context)

    def rollback(self, plan: dict, context: dict) -> dict:
        return self._call("rollback", plan, context)


def _webhook_adapter_from_environment() -> WebhookChangeAdapter:
    endpoint = str(os.environ.get("CYBER_AGENT_MONITORING_WEBHOOK_URL") or "").strip()
    allowed_hosts = {item.strip().lower() for item in str(
        os.environ.get("CYBER_AGENT_MONITORING_WEBHOOK_ALLOWED_HOSTS") or ""
    ).split(",") if item.strip()}
    parsed = urlparse(endpoint)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host or host not in allowed_hosts:
        raise ValueError("Webhook 变更适配器未配置 HTTPS 端点或允许主机")
    return WebhookChangeAdapter(endpoint, str(os.environ.get("CYBER_AGENT_MONITORING_WEBHOOK_TOKEN") or ""))


def validate_change_plan(plan: Any) -> dict:
    if not isinstance(plan, dict):
        raise ValueError("变更预案必须是对象")
    required = ("change", "rollback", "validation")
    missing = [key for key in required if not str(plan.get(key) or "").strip()]
    if missing:
        raise ValueError(f"变更预案缺少：{', '.join(missing)}")
    adapter = str(plan.get("adapter") or "manual_handoff").strip()
    if adapter not in {"manual_handoff", "webhook_change"}:
        raise ValueError("不支持该变更适配器")
    parameters = plan.get("parameters")
    if parameters is not None and not isinstance(parameters, dict):
        raise ValueError("变更适配器 parameters 必须是对象")
    return {"adapter": adapter, "change": str(plan["change"])[:4000],
            "rollback": str(plan["rollback"])[:4000], "validation": str(plan["validation"])[:4000],
            "target": str(plan.get("target") or "")[:300], "parameters": parameters or {}}


def get_change_adapter(name: str = "manual_handoff") -> ChangeAdapter:
    name = str(name or "manual_handoff")
    if name == "manual_handoff":
        return ManualHandoffAdapter()
    if name == "webhook_change":
        return _webhook_adapter_from_environment()
    raise ValueError("当前环境未注册该变更适配器")


def execute_approved_change(report: dict, context: dict | None = None) -> dict:
    return run_approved_change_action(report, "execute", context)


def run_approved_change_action(report: dict, action: str, context: dict | None = None) -> dict:
    if not report or report.get("state") != "approved":
        raise ValueError("只有评审通过的问题报告才能调用变更适配器")
    if action not in {"execute", "verify", "rollback"}:
        raise ValueError("不支持该变更适配器动作")
    plan = validate_change_plan((report.get("report") or {}).get("change_plan"))
    adapter = get_change_adapter(plan["adapter"])
    method = getattr(adapter, action)
    return method(plan, context or {})
