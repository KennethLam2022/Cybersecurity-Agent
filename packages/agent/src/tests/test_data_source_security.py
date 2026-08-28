import pytest

from data_source_security import validate_data_source_config
from memory import ConversationMemory


def test_remote_source_rejects_private_hosts_and_credentials():
    assert not validate_data_source_config("url", "http://127.0.0.1/internal")["valid"]
    assert not validate_data_source_config("url", "https://user:pass@example.com/a")["valid"]
    assert validate_data_source_config("url", "https://example.com/a")["valid"]


def test_database_source_requires_read_only_and_rejects_write_sql():
    invalid = validate_data_source_config(
        "database_readonly", "db://reporting", {"query": "SELECT 1", "read_only": False},
    )
    assert not invalid["valid"]
    dangerous = validate_data_source_config(
        "database_readonly", "db://reporting", {"query": "SELECT 1; DROP TABLE users", "read_only": True},
    )
    assert not dangerous["valid"]
    assert validate_data_source_config(
        "database_readonly", "db://reporting", {"query": "SELECT id FROM users", "read_only": True},
    )["valid"]


def test_data_source_creation_applies_security_validation(tmp_path):
    memory = ConversationMemory(str(tmp_path / "source-security.db"))
    user = memory.register_user("source-security@example.com", "Correct-Horse-26", "Source Security")
    with pytest.raises(ValueError, match="localhost|内网"):
        memory.create_data_source(user["tenant_id"], "内网 URL", "url", "http://localhost/admin")
    with pytest.raises(ValueError, match="read_only"):
        memory.create_data_source(
            user["tenant_id"], "数据库", "database_readonly", "db://reporting",
            {"query": "SELECT 1"},
        )


def test_data_source_sync_runs_support_retry_and_attempt_history(tmp_path):
    memory = ConversationMemory(str(tmp_path / "source-runs.db"))
    user = memory.register_user("run-owner@example.com", "Correct-Horse-27", "Run Owner")
    source = memory.create_data_source(
        user["tenant_id"], "公告源", "url", "https://example.com/advisories",
    )
    run = memory.create_data_source_sync_run(source["id"], user["tenant_id"])
    assert run["status"] == "queued"
    assert memory.update_data_source_sync_run(run["id"], "running")
    assert memory.update_data_source_sync_run(run["id"], "failed", "连接超时")
    retry = memory.retry_data_source_sync_run(run["id"], user["tenant_id"])
    assert retry["attempt"] == 2
    assert retry["trigger"] == "retry"
    with pytest.raises(ValueError, match="失败或已取消"):
        memory.retry_data_source_sync_run(retry["id"], user["tenant_id"])
    assert len(memory.list_data_source_sync_runs(source["id"], user["tenant_id"])) == 2
