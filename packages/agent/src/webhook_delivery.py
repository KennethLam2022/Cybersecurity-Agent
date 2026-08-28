"""受控 Webhook 事件投递：签名、去重、重试和审计。"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS = {
    "ingestion.completed", "ingestion.failed", "approval.pending", "eval.completed",
    "quota.warning", "jailbreak.alert", "knowledge_gap.created", "external_api.failed",
    "workflow.completed",
}


def _signature(secret: str, event_id: str, body: bytes) -> str:
    message = event_id.encode("utf-8") + b"." + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _safe_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.hostname)


def dispatch_webhook_event(memory, event_type: str, data: dict, tenant_id: str = "",
                           event_id: str | None = None) -> dict:
    """投递一个持久事件；每次尝试都写入 webhook_deliveries。"""
    if event_type not in WEBHOOK_EVENTS:
        return {"event_id": event_id or "", "skipped": True, "reason": "unsupported_event"}
    event_id = event_id or "evt-" + uuid.uuid4().hex
    payload = {"id": event_id, "type": event_type, "created_at": time.time(), "data": data or {}}
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    results = []
    for sub in memory.list_webhook_subscriptions(tenant_id or None, event_type):
        if not sub.get("enabled") or not _safe_url(sub["url"]):
            continue
        secret = memory.get_webhook_secret(sub["id"])
        if not secret:
            logger.warning("Webhook %s 没有可用签名密钥", sub["id"])
            continue
        max_attempts = max(1, min(int(sub.get("max_attempts") or 3), 8))
        delivered = False
        last_error = ""
        status_code = None
        for attempt in range(1, max_attempts + 1):
            try:
                response = requests.post(
                    sub["url"], data=body, timeout=float(sub.get("timeout_seconds") or 10),
                    headers={"Content-Type": "application/json", "User-Agent": "SecureNexus-Webhook",
                             "X-SecureNexus-Event-ID": event_id, "X-SecureNexus-Signature": _signature(secret, event_id, body)},
                )
                status_code = response.status_code
                delivered = 200 <= response.status_code < 300
                last_error = "" if delivered else f"HTTP {response.status_code}"
            except Exception as exc:  # pragma: no cover - requests errors vary by adapter
                last_error = str(exc)[:500]
            memory.record_webhook_delivery(sub["id"], event_id, event_type, payload, attempt,
                                           status_code, last_error, delivered)
            if delivered:
                break
            if attempt < max_attempts:
                time.sleep(min(0.25 * (2 ** (attempt - 1)), 2.0))
        results.append({"subscription_id": sub["id"], "delivered": delivered,
                        "attempts": attempt, "status_code": status_code, "error": last_error})
    return {"event_id": event_id, "results": results}
