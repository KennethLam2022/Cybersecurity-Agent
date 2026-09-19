from pathlib import Path


def test_release_check_lists_mcp_confirmation():
    script = Path(__file__).resolve().parents[4] / "scripts" / "release_check.py"
    text = script.read_text(encoding="utf-8")
    assert "MCP production execution rehearsal" in text


def test_release_check_forces_non_interactive_dependency_install():
    script = Path(__file__).resolve().parents[4] / "scripts" / "release_check.py"
    text = script.read_text(encoding="utf-8")
    assert 'env["CI"] = "true"' in text
    assert "env=env" in text
