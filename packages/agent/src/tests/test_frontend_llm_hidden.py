from pathlib import Path


def test_frontend_does_not_poll_or_display_llm_configuration():
    template = Path(__file__).parents[1] / "templates" / "index.html"
    content = template.read_text(encoding="utf-8")

    assert "startConfigMonitor" not in content
    assert "/api/llm/config/current" not in content
    assert "模型已更换" not in content
