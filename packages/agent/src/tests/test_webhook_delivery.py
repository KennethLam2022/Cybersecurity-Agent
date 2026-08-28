import hashlib
import hmac
import json
import sqlite3

from memory import ConversationMemory
from webhook_delivery import dispatch_webhook_event


class _Response:
    def __init__(self, status_code):
        self.status_code = status_code


def test_webhook_creation_encrypts_secret_and_lists_without_revealing_it(tmp_path):
    memory = ConversationMemory(str(tmp_path / "webhook.db"))
    item = memory.create_webhook_subscription(
        "tenant-1", "告警通知", "https://example.com/hook", ["jailbreak.alert"], created_by="admin",
    )
    assert item["signing_secret"].startswith("whsec_")
    listed = memory.list_webhook_subscriptions("tenant-1")
    assert listed[0]["id"] == item["id"]
    assert "signing_secret" not in listed[0]
    assert memory.get_webhook_secret(item["id"]) == item["signing_secret"]


def test_webhook_retries_signs_and_records_each_attempt(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "webhook-retry.db"))
    item = memory.create_webhook_subscription(
        "tenant-1", "评测通知", "https://example.com/hook", ["eval.completed"], max_attempts=3,
    )
    calls = []

    def fake_post(url, data, timeout, headers):
        calls.append((url, data, headers))
        return _Response(500 if len(calls) == 1 else 204)

    monkeypatch.setattr("webhook_delivery.requests.post", fake_post)
    result = dispatch_webhook_event(memory, "eval.completed", {"run_id": "run-1"}, "tenant-1", "evt-1")
    assert result["results"][0]["delivered"] is True
    assert result["results"][0]["attempts"] == 2
    assert len(calls) == 2
    payload = calls[0][1]
    event_id = calls[0][2]["X-SecureNexus-Event-ID"]
    expected = "sha256=" + hmac.new(
        item["signing_secret"].encode(), event_id.encode() + b"." + payload, hashlib.sha256,
    ).hexdigest()
    assert calls[0][2]["X-SecureNexus-Signature"] == expected
    history = memory.list_webhook_deliveries("tenant-1")
    assert [row["attempt"] for row in history[:2]] == [2, 1]
    assert history[0]["delivered"] is True


def test_disabled_webhook_is_not_called(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "webhook-disabled.db"))
    item = memory.create_webhook_subscription(
        "tenant-1", "停用通知", "https://example.com/hook", ["quota.warning"],
    )
    assert memory.update_webhook_subscription_status(item["id"], False)
    monkeypatch.setattr("webhook_delivery.requests.post", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not call")))
    result = dispatch_webhook_event(memory, "quota.warning", {"percent": 90}, "tenant-1", "evt-disabled")
    assert result["results"] == []


def test_unknown_event_is_not_delivered(tmp_path):
    memory = ConversationMemory(str(tmp_path / "webhook-unknown.db"))
    result = dispatch_webhook_event(memory, "conversation.updated", {}, "tenant-1")
    assert result["skipped"] is True


def test_embed_token_is_short_lived_origin_bound_and_revocable(tmp_path):
    memory = ConversationMemory(str(tmp_path / "embed.db"))
    owner = memory.register_user("embed@example.com", "Correct-Horse-30", "Embed")
    app = memory.create_external_app(owner["tenant_id"], "门户应用", ["chat", "embed"], agent_id=owner["agent_id"])
    token = memory.create_embed_token(app["id"], "https://portal.example", ttl_hours=2)
    assert token["token"].startswith("emb_")
    principal = memory.authenticate_embed_token(token["token"], "https://portal.example")
    assert principal["app_id"] == app["id"]
    assert principal["agent_id"] == owner["agent_id"]
    assert memory.authenticate_embed_token(token["token"], "https://other.example") is None
    assert memory.update_embed_token_status(token["id"], False)
    assert memory.authenticate_embed_token(token["token"], "https://portal.example") is None


def test_embed_token_requires_embed_or_chat_scope(tmp_path):
    memory = ConversationMemory(str(tmp_path / "embed-scope.db"))
    owner = memory.register_user("embed-usage@example.com", "Correct-Horse-30", "Embed Usage")
    app = memory.create_external_app(owner["tenant_id"], "用量应用", ["usage"], agent_id=owner["agent_id"])
    try:
        memory.create_embed_token(app["id"])
    except ValueError as exc:
        assert "作用域" in str(exc)
    else:
        raise AssertionError("usage-only app must not create embed token")
