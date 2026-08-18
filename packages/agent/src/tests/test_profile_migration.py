import json

import profile_migration
from profile_migration import apply_profile_migration, scan_profile_migration


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
