import sqlite3

from backup_manager import create_backup, restore_backup, verify_backup


def test_backup_manifest_covers_database_and_rag_state(tmp_path):
    project = tmp_path / "project"
    db = project / "agent_data" / "conversations.db"
    cleaned = project / "RAG_DATA" / "03_cleaned" / "general"
    vector = project / "RAG_DATA" / "04_vector_store" / "chroma_db"
    cleaned.mkdir(parents=True)
    vector.mkdir(parents=True)
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
        conn.execute("INSERT INTO marker VALUES ('before')")
    (cleaned / "policy.md").write_text("policy", encoding="utf-8")
    (vector / "index.bin").write_bytes(b"vector")

    backup = create_backup(str(db), str(project), str(tmp_path / "backups"))
    assert backup["file_count"] == 3
    assert verify_backup(backup["path"])["valid"] is True

    (cleaned / "policy.md").write_text("changed", encoding="utf-8")
    with sqlite3.connect(db) as conn:
        conn.execute("UPDATE marker SET value='changed'")
    restored = restore_backup(backup["path"], str(db), str(project))
    assert restored["restored"] is True
    assert (cleaned / "policy.md").read_text(encoding="utf-8") == "policy"
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT value FROM marker").fetchone()[0] == "before"


def test_tampered_backup_is_rejected(tmp_path):
    project = tmp_path / "project"
    db = project / "agent_data" / "conversations.db"
    db.parent.mkdir(parents=True)
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE marker (value TEXT)")
    backup = create_backup(str(db), str(project), str(tmp_path / "backups"))
    (tmp_path / "backups" / backup["id"] / "agent_data" / "conversations.db").write_bytes(b"tampered")
    result = verify_backup(backup["path"])
    assert result["valid"] is False
