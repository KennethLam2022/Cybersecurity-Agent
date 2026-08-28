import json

import profile_migration
import access_migration
from profile_migration import (
    apply_profile_migration,
    confirm_profile_migration,
    profile_assignment_history,
    scan_profile_migration,
)
from access_migration import confirm_access_migration, rollback_access_migration


def test_scan_profile_migration_splits_general_industry_and_pending(tmp_path, monkeypatch):
    cleaned = tmp_path / "03_cleaned"
    (cleaned / "01-国家法律").mkdir(parents=True)
    (cleaned / "04-通信行业标准-运营商").mkdir(parents=True)
    (cleaned / "上传文档").mkdir(parents=True)
    (cleaned / "01-国家法律" / "网络安全法.md").write_text("# 网络安全法", encoding="utf-8")
    (cleaned / "04-通信行业标准-运营商" / "5G核心网.md").write_text("# 5G核心网", encoding="utf-8")
    (cleaned / "上传文档" / "客户材料.md").write_text("# 客户材料", encoding="utf-8")

    monkeypatch.setattr(profile_migration, "_CLEANED_DIR", cleaned)

    result = scan_profile_migration()

    assert result["total"] == 3
    assert result["summary"]["general"] == 1
    assert result["summary"]["industry/telecom"] == 1
    assert result["summary"]["pending"] == 1
    assert result["pending"] == 1


def test_apply_profile_migration_writes_normalized_sidecar(tmp_path, monkeypatch):
    cleaned = tmp_path / "03_cleaned"
    (cleaned / "01-国家法律").mkdir(parents=True)
    md_path = cleaned / "01-国家法律" / "网络安全法.md"
    md_path.write_text("# 网络安全法", encoding="utf-8")

    monkeypatch.setattr(profile_migration, "_CLEANED_DIR", cleaned)

    result = apply_profile_migration(update_vector_stores=False)
    sidecar = json.loads(md_path.with_suffix(".meta.json").read_text(encoding="utf-8"))

    assert result["written_sidecars"] == 1
    assert sidecar["profile"] == "general"
    assert sidecar["scope"] == "general"
    assert sidecar["profile_confirmed"] is True
    assert "profile_confidence" in sidecar


def test_confirm_profile_migration_writes_manual_confirmation(tmp_path, monkeypatch):
    cleaned = tmp_path / "03_cleaned"
    (cleaned / "上传文档").mkdir(parents=True)
    md_path = cleaned / "上传文档" / "客户材料.md"
    md_path.write_text("# 客户材料", encoding="utf-8")

    monkeypatch.setattr(profile_migration, "_CLEANED_DIR", cleaned)

    result = confirm_profile_migration(
        paths=[str(md_path)],
        profile="industry/finance",
        category="11-金融行业安全",
        update_vector_stores=False,
    )
    sidecar = json.loads(md_path.with_suffix(".meta.json").read_text(encoding="utf-8"))

    assert result["written_sidecars"] == 1
    assert sidecar["profile"] == "industry/finance"
    assert sidecar["scope"] == "industry"
    assert sidecar["industry"] == "finance"
    assert sidecar["profile_confirmed"] is True
    assert sidecar["profile_source"] == "manual_confirmed"


def test_confirm_profile_migration_rejects_outside_cleaned_dir(tmp_path, monkeypatch):
    cleaned = tmp_path / "03_cleaned"
    cleaned.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("# outside", encoding="utf-8")

    monkeypatch.setattr(profile_migration, "_CLEANED_DIR", cleaned)

    try:
        confirm_profile_migration(paths=[str(outside)], profile="general", update_vector_stores=False)
    except ValueError as exc:
        assert "outside cleaned data directory" in str(exc)
    else:
        raise AssertionError("expected outside path to be rejected")


def test_reclassifying_confirmed_document_requires_reason_and_writes_audit(tmp_path, monkeypatch):
    cleaned = tmp_path / "03_cleaned"
    category = cleaned / "04-通信行业标准-运营商"
    category.mkdir(parents=True)
    md_path = category / "行业资料.md"
    md_path.write_text("# 行业资料", encoding="utf-8")
    md_path.with_suffix(".meta.json").write_text(json.dumps({
        "profile": "industry/telecom",
        "category": "04-通信行业标准-运营商",
        "scope": "industry",
        "industry": "telecom",
        "profile_confirmed": True,
        "source_url": "https://example.test/source",
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(profile_migration, "_CLEANED_DIR", cleaned)

    try:
        confirm_profile_migration(
            paths=[str(md_path)], profile="industry/finance",
            category="11-金融行业安全", update_vector_stores=False,
        )
    except ValueError as exc:
        assert "必须填写修改原因" in str(exc)
    else:
        raise AssertionError("expected confirmed-document reclassification to require a reason")

    result = confirm_profile_migration(
        paths=[str(md_path)], profile="industry/finance",
        category="11-金融行业安全", change_reason="人工初判错误，修正为金融行业",
        changed_by="admin-a", update_vector_stores=False,
    )
    sidecar = json.loads(md_path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    history = profile_assignment_history(str(md_path))

    assert result["reclassified"] == 1
    assert sidecar["profile"] == "industry/finance"
    assert sidecar["source_url"] == "https://example.test/source"
    assert sidecar["profile_change_reason"] == "人工初判错误，修正为金融行业"
    assert history[0]["old"]["profile"] == "industry/telecom"
    assert history[0]["new"]["profile"] == "industry/finance"


def test_access_migration_can_be_rolled_back(tmp_path, monkeypatch):
    cleaned = tmp_path / "03_cleaned"
    category = cleaned / "上传文档"
    category.mkdir(parents=True)
    path = category / "资料.md"
    path.write_text("# 资料", encoding="utf-8")
    path.with_suffix(".meta.json").write_text(json.dumps({
        "file_name": "资料", "document_id": "doc-access-1", "visibility": "public",
    }, ensure_ascii=False), encoding="utf-8")
    store = tmp_path / "04_vector_store"
    store.mkdir()
    db_path = tmp_path / "identity.db"
    from memory import ConversationMemory
    memory = ConversationMemory(str(db_path))
    user = memory.register_user("owner@example.com", "Correct-Horse-13", "Owner")

    monkeypatch.setattr(access_migration, "_CLEANED", cleaned)
    monkeypatch.setattr(access_migration, "_STORE", store)
    monkeypatch.setattr(access_migration, "_PARENT", store / "parent_texts.json")
    monkeypatch.setattr(access_migration, "_AUDIT", cleaned / ".access_assignment_audit.jsonl")

    result = confirm_access_migration([str(path)], {
        "visibility": "tenant", "tenant_id": user["tenant_id"],
    }, str(db_path))
    event_time = access_migration.access_assignment_history(str(path))[0]["changed_at"]
    assert result["updated_sidecars"] == 1
    assert json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))["visibility"] == "tenant"

    rollback_access_migration(str(path), event_time, str(db_path), change_reason="测试回滚")
    restored = json.loads(path.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert restored["visibility"] == "public"
