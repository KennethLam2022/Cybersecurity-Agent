"""Single event fan-out for in-app notifications, Webhook, and admin SSE."""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from webhook_delivery import WEBHOOK_EVENTS, dispatch_webhook_event
from governance_store import GovernanceStore

logger = logging.getLogger(__name__)


def _governance_store_for(memory):
    return GovernanceStore(str(memory._db_path))

NOTIFICATION_EVENTS = {
    "ingestion.completed",
    "ingestion.failed",
    "approval.pending",
    "eval.completed",
    "quota.warning",
    "jailbreak.alert",
    "knowledge_gap.created",
    "external_api.failed",
    "generation.completed",
    "workflow.completed",
}

_TEMPLATES: dict[str, dict[str, str]] = {
    "ingestion.completed": {
        "severity": "success", "title": "文档入库已完成",
        "body": "入库任务已完成，可查看处理明细与失败项。",
        "link_path": "/admin?tab=indexing",
    },
    "ingestion.failed": {
        "severity": "error", "title": "文档入库失败",
        "body": "入库任务未完成，请查看失败原因后重试。",
        "link_path": "/admin?tab=indexing",
    },
    "approval.pending": {
        "severity": "warning", "title": "有新的账号审批待处理",
        "body": "企业身份首次登录正在等待管理员确认。",
        "link_path": "/admin?tab=sso",
    },
    "eval.completed": {
        "severity": "info", "title": "Agent 评测已完成",
        "body": "新的评测结果已生成，可查看评分、案例与发布门禁。",
        "link_path": "/admin?tab=agentEval",
    },
    "quota.warning": {
        "severity": "warning", "title": "模型用量接近配额",
        "body": "请检查当前模型用量、预算和限额配置。",
        "link_path": "/admin?tab=overview",
    },
    "jailbreak.alert": {
        "severity": "critical", "title": "检测到高风险提示注入",
        "body": "系统已记录安全事件，请在对话详情中复核。",
        "link_path": "/admin?tab=detail",
    },
    "knowledge_gap.created": {
        "severity": "info", "title": "发现新的知识缺口",
        "body": "高频未解决问题已形成待补资料任务。",
        "link_path": "/admin?tab=gaps",
    },
    "external_api.failed": {
        "severity": "error", "title": "开放 API 调用失败",
        "body": "外部应用调用出现异常，请检查应用凭证、限流与审计记录。",
        "link_path": "/admin?tab=externalapi",
    },
    "generation.completed": {
        "severity": "success", "title": "生成任务已完成",
        "body": "文档或演示文稿已生成，可在生成记录中下载。",
        "link_path": "/",
    },
    "workflow.completed": {
        "severity": "success", "title": "工作流运行已完成",
        "body": "工作流已完成，可查看节点 Trace 和输出结果。",
        "link_path": "/admin?tab=workflows",
    },
}


def _notification_fields(event_type: str, data: dict[str, Any]) -> dict[str, str]:
    template = _TEMPLATES.get(event_type, {})
    title = str(data.get("notification_title") or template.get("title") or "系统通知")
    body = str(data.get("notification_body") or template.get("body") or "")
    link_path = str(data.get("link_path") or template.get("link_path") or "")
    severity = str(data.get("severity") or template.get("severity") or "info")
    return {"title": title, "body": body, "link_path": link_path, "severity": severity}


def publish_system_event(memory, event_bus, event_type: str, data: dict | None = None) -> dict:
    """Persist the in-app event, dispatch configured Webhooks, then notify SSE clients."""
    payload = dict(data or {})
    tenant_id = str(payload.get("tenant_id") or "local-default")
    fields = _notification_fields(event_type, payload)
    decision = _governance_store_for(memory).notification_decision(tenant_id, event_type, fields["severity"])
    channels = set(decision["channels"])
    payload["notification_policy_ids"] = decision["policy_ids"]
    if event_type in NOTIFICATION_EVENTS:
        if "in_app" in channels:
            recipients = decision.get("recipient_user_ids") or [str(payload.get("user_id") or "")]
            notification_ids = []
            for recipient_user_id in recipients:
                notification = memory.create_notification(
                    tenant_id=tenant_id, user_id=recipient_user_id,
                    agent_id=str(payload.get("agent_id") or ""), event_type=event_type,
                    title=fields["title"], body=fields["body"], severity=fields["severity"],
                    link_path=fields["link_path"], payload=payload,
                )
                notification_ids.append(notification["id"])
            payload["notification_id"] = notification_ids[0] if notification_ids else ""
            payload["notification_ids"] = notification_ids
        if "email" in channels:
            _dispatch_email(memory, event_type, fields, payload)
    if event_type in WEBHOOK_EVENTS and "webhook" in channels:
        try:
            threading.Thread(
                target=dispatch_webhook_event,
                args=(memory, event_type, payload, tenant_id),
                daemon=True,
                name="webhook-dispatch",
            ).start()
        except Exception as exc:  # pragma: no cover - thread start is platform-specific
            logger.warning("Webhook 事件调度失败: %s", exc)
    if event_bus is not None:
        try:
            asyncio.get_running_loop()
            asyncio.create_task(event_bus.publish(event_type, payload))
        except RuntimeError:
            asyncio.run(event_bus.publish(event_type, payload))
    return payload


def _dispatch_email(memory, event_type: str, fields: dict[str, str], payload: dict[str, Any]) -> None:
    try:
        config = memory.get_email_notification_config(include_secret=True)
        if not config.get("enabled") or event_type not in set(config.get("events") or []):
            return
        from email_delivery import send_email
        last_error = ""
        for attempt in range(1, 4):
            try:
                result = send_email(config, fields["title"], fields["body"])
                memory.record_email_delivery(
                    payload.get("tenant_id", "local-default"), event_type,
                    payload.get("notification_id", ""), attempt, bool(result.get("sent")),
                    int(result.get("recipient_count", 0)), str(result.get("reason", "")),
                )
                if result.get("sent"):
                    return
                last_error = str(result.get("reason", "disabled"))
            except Exception as exc:
                last_error = str(exc)
                memory.record_email_delivery(
                    payload.get("tenant_id", "local-default"), event_type,
                    payload.get("notification_id", ""), attempt, False, 0, last_error,
                )
        logger.warning("邮件通知重试耗尽: %s", last_error)
        return
    except Exception as exc:
        logger.warning("邮件通知发送失败: %s", exc)
        memory.record_email_delivery(
            payload.get("tenant_id", "local-default"), event_type,
            payload.get("notification_id", ""), 1, False, 0, str(exc),
        )
