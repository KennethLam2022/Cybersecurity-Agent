"""Controlled SMTP delivery for selected system notifications."""
from __future__ import annotations

import smtplib
import ssl
from email.message import EmailMessage
from typing import Any


EMAIL_EVENTS = {
    "approval.pending", "eval.completed", "quota.warning", "jailbreak.alert",
    "ingestion.failed", "external_api.failed",
}


def validate_email_config(config: dict[str, Any]) -> dict[str, Any]:
    host = str(config.get("host") or "").strip()
    if not host:
        raise ValueError("SMTP 主机不能为空")
    port = int(config.get("port") or 465)
    if not 1 <= port <= 65535:
        raise ValueError("SMTP 端口无效")
    security = str(config.get("security") or "ssl").lower()
    if security not in {"ssl", "starttls", "none"}:
        raise ValueError("SMTP 加密方式仅支持 ssl、starttls 或 none")
    sender = str(config.get("sender") or "").strip()
    if "@" not in sender:
        raise ValueError("发件人邮箱无效")
    events = [str(item) for item in (config.get("events") or []) if str(item) in EMAIL_EVENTS]
    recipients = [str(item).strip() for item in (config.get("recipients") or []) if "@" in str(item)]
    if not recipients:
        raise ValueError("至少配置一个通知收件人")
    return {
        "enabled": bool(config.get("enabled")), "host": host, "port": port,
        "security": security, "sender": sender,
        "username": str(config.get("username") or "").strip(),
        "events": events, "recipients": recipients,
    }


def send_email(config: dict[str, Any], title: str, body: str) -> dict[str, Any]:
    if not config.get("enabled"):
        return {"sent": False, "reason": "disabled"}
    message = EmailMessage()
    message["Subject"] = str(title or "SecureNexus notification")
    message["From"] = config["sender"]
    message["To"] = ", ".join(config["recipients"])
    message.set_content(str(body or ""))
    context = ssl.create_default_context()
    security = config.get("security", "ssl")
    if security == "ssl":
        client = smtplib.SMTP_SSL(config["host"], config["port"], context=context, timeout=15)
    else:
        client = smtplib.SMTP(config["host"], config["port"], timeout=15)
    with client:
        client.ehlo()
        if security == "starttls":
            client.starttls(context=context)
            client.ehlo()
        if config.get("username"):
            client.login(config["username"], config.get("password", ""))
        client.send_message(message)
    return {"sent": True, "recipient_count": len(config["recipients"])}
