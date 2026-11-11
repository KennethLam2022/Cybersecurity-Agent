from pathlib import Path


PAGE = Path(__file__).parents[1] / "static" / "admin_backups.html"


def test_backup_ui_confirms_restore_without_issuing_request_on_cancel():
    page = PAGE.read_text(encoding="utf-8")
    assert "function closeRestoreDialog()" in page
    assert "function confirmRestore()" in page
    assert "value !== selectedBackup) return" in page
    assert "'/api/admin/backups/restore'" in page
    assert "onclick=\"closeRestoreDialog()\"" in page
    assert "将覆盖当前应用数据库、RAG_DATA/03_cleaned 和 RAG_DATA/04_vector_store" in page
    assert "[hidden]{display:none!important}" in page
    assert "document.getElementById('status').textContent = `恢复完成" in page


def test_backup_ui_escapes_ids_and_reports_empty_and_error_states():
    page = PAGE.read_text(encoding="utf-8")
    assert "function escapeHtml(value)" in page
    assert "暂无备份" in page
    assert "备份清单加载失败" in page
    assert "校验失败：${error.message}" in page
    assert "创建失败：${error.message}" in page
