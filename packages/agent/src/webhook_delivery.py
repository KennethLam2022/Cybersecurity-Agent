"""受控 Webhook 事件投递：签名、去重、重试和审计。"""
import hashlib
import hmac
import json
import logging
import time
import uuid
from urllib.parse import urljoin

import requests

from data_source_security import validate_remote_url

logger = logging.getLogger(__name__)

WEBHOOK_EVENTS = {
    "ingestion.completed", "ingestion.failed", "approval.pending", "eval.completed",
    "quota.warning", "jailbreak.alert", "knowledge_gap.created", "external_api.failed",
    "workflow.completed",
}


def _signature(secret: str, event_id: str, body: bytes) -> str:
    message = event_id.encode("utf-8") + b"." + body
    return "sha256=" + hmac.new(secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def _post_webhook(url: str, body: bytes, secret: str, event_id: str, timeout: float):
    """POST once without automatic redirects; validate every explicit hop."""
    current_url = url
    for _hop in range(6):
        error = validate_remote_url(current_url)
        if error:
            raise ValueError(error)
        response = requests.post(
            current_url, data=body, timeout=timeout, allow_redirects=False,
            headers={"Content-Type": "application/json", "User-Agent": "SecureNexus-Webhook",
                     "X-SecureNexus-Event-ID": event_id,
                     "X-SecureNexus-Signature": _signature(secret, event_id, body)},
        )
        if 300 <= response.status_code < 400:
            location = response.headers.get("Location", "")
            if response.status_code not in {307, 308} or not location:
                raise ValueError(f"Webhook 不允许 HTTP {response.status_code} 重定向")
            current_url = urljoin(current_url, location)
            continue
        return response
    raise ValueError("Webhook 重定向次数过多")


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
        url_error = validate_remote_url(sub["url"])
        if not sub.get("enabled") or url_error:
            if url_error:
                memory.record_webhook_delivery(
                    sub["id"], event_id, event_type, payload, 1, None, url_error, False,
                )
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
                response = _post_webhook(
                    sub["url"], body, secret, event_id,
                    float(sub.get("timeout_seconds") or 10),
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
