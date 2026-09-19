from pathlib import Path


def test_model_config_reload_button_does_not_call_missing_restart_api():
    page = Path(__file__).parents[1] / "static" / "admin_model_preview.html"
    content = page.read_text(encoding="utf-8")
    assert "/api/llm/configs/restart" not in content
    assert "重新加载配置" in content
    assert "配置已热更新，无需重启服务" in content
