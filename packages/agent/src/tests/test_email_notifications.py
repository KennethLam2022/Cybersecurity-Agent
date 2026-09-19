from email_delivery import send_email, validate_email_config
from memory import ConversationMemory
from notification_delivery import publish_system_event
from governance_store import GovernanceStore


def _config(**overrides):
    value = {
        "enabled": True, "host": "smtp.example.com", "port": 465,
        "security": "ssl", "sender": "agent@example.com",
        "username": "agent@example.com", "password": "secret",
        "recipients": ["admin@example.com"], "events": ["eval.completed"],
    }
    value.update(overrides)
    return value


def test_email_config_validates_allowed_events_and_recipients():
    config = validate_email_config(_config(events=["eval.completed", "not-supported"]))
    assert config["events"] == ["eval.completed"]
    assert config["recipients"] == ["admin@example.com"]


def test_email_config_is_encrypted_and_password_is_masked(tmp_path):
    memory = ConversationMemory(str(tmp_path / "email.db"))
    memory.save_email_notification_config(_config())
    public = memory.get_email_notification_config()
    assert public["has_password"] is True
    assert "password" not in public
    assert memory.get_email_notification_config(include_secret=True)["password"] == "secret"


def test_notification_email_delivery_is_audited_when_sender_fails(tmp_path, monkeypatch):
    memory = ConversationMemory(str(tmp_path / "email.db"))
    memory.save_email_notification_config(_config())

    def fail(*args, **kwargs):
        raise OSError("SMTP unavailable")

    monkeypatch.setattr("smtplib.SMTP_SSL", fail)
    publish_system_event(memory, None, "eval.completed", {"tenant_id": "tenant-a"})
    deliveries = memory.list_email_deliveries("tenant-a")
    assert len(deliveries) == 3
    assert deliveries[0]["sent"] is False
    assert "SMTP unavailable" in deliveries[0]["error"]


def test_notification_policy_controls_real_event_fanout(tmp_path):
    memory = ConversationMemory(str(tmp_path / "policy-events.db"))
    owner = memory.register_user("policy-owner@example.com", "Correct-Horse-30", "Policy Workspace")
    reviewer = memory.create_workspace_user(owner["tenant_id"], "policy-reviewer@example.com", "Correct-Horse-31", "Reviewer")["id"]
    GovernanceStore(str(memory._db_path)).upsert_notification_policy(
        owner["tenant_id"], owner["id"], {"name": "eval-only", "event_types": ["eval.completed"],
                                  "channels": ["in_app"], "recipient_user_ids": [reviewer],
                                  "cooldown_minutes": 60},
    )
    publish_system_event(memory, None, "eval.completed", {"tenant_id": owner["tenant_id"]})
    publish_system_event(memory, None, "eval.completed", {"tenant_id": owner["tenant_id"]})
    items = memory.list_notifications(owner["tenant_id"], reviewer)
    assert len(items) == 1
    assert items[0]["event_type"] == "eval.completed"


def test_send_email_returns_disabled_without_network():
    assert send_email({"enabled": False}, "title", "body")["reason"] == "disabled"
