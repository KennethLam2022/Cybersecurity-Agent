from memory import ConversationMemory
from notification_delivery import publish_system_event


def test_notification_visibility_is_scoped_to_tenant_and_user(tmp_path):
    memory = ConversationMemory(str(tmp_path / "notifications.db"))
    memory.create_notification("tenant-a", "eval.completed", "工作区通知")
    mine = memory.create_notification("tenant-a", "generation.completed", "我的通知", user_id="user-a")
    memory.create_notification("tenant-a", "generation.completed", "其他用户通知", user_id="user-b")
    memory.create_notification("tenant-b", "eval.completed", "其他工作区通知")

    items = memory.list_notifications("tenant-a", "user-a")
    assert {item["title"] for item in items} == {"工作区通知", "我的通知"}
    assert memory.notification_summary("tenant-a", "user-a") == {"unread": 2}

    changed = memory.update_notifications_status(
        "tenant-a", [mine["id"]], "read", user_id="user-a",
    )
    assert changed == 1
    assert memory.notification_summary("tenant-a", "user-a") == {"unread": 1}
    assert memory.list_notifications("tenant-a", "user-a", status="read")[0]["id"] == mine["id"]


def test_notification_status_update_cannot_cross_user_boundary(tmp_path):
    memory = ConversationMemory(str(tmp_path / "notifications.db"))
    other = memory.create_notification("tenant-a", "generation.completed", "其他用户", user_id="user-b")
    assert memory.update_notifications_status(
        "tenant-a", [other["id"]], "archived", user_id="user-a",
    ) == 0
    assert memory.list_notifications("tenant-a", "user-b")[0]["status"] == "unread"


def test_publish_system_event_persists_template_and_payload(tmp_path):
    memory = ConversationMemory(str(tmp_path / "notifications.db"))
    result = publish_system_event(memory, None, "eval.completed", {
        "tenant_id": "tenant-a", "run_id": "run-001", "profile": "general",
    })
    assert result["notification_id"].startswith("ntf-")
    item = memory.list_notifications("tenant-a")[0]
    assert item["event_type"] == "eval.completed"
    assert item["severity"] == "info"
    assert item["payload"]["run_id"] == "run-001"
    assert item["link_path"] == "/admin?tab=agentEval"


def test_archived_notifications_are_excluded_from_default_unread_summary(tmp_path):
    memory = ConversationMemory(str(tmp_path / "notifications.db"))
    item = memory.create_notification("tenant-a", "jailbreak.alert", "高风险事件", severity="critical")
    assert memory.update_notifications_status("tenant-a", [item["id"]], "archived", admin=True) == 1
    assert memory.notification_summary("tenant-a") == {"unread": 0}
    archived = memory.list_notifications("tenant-a", status="archived")
    assert archived[0]["severity"] == "critical"
