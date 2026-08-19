from pathlib import Path


def test_app_state_uses_lazy_agent_creation():
    content = (Path(__file__).parents[1] / "app_state.py").read_text(encoding="utf-8")

    assert "class LazyCyberAgent" in content
    assert "self._instance = CyberAgent()" in content
    assert "agent = LazyCyberAgent()" in content


def test_lifespan_starts_initialization_in_background():
    content = (Path(__file__).parents[1] / "app_lifespan.py").read_text(encoding="utf-8")

    assert "asyncio.create_task(asyncio.to_thread(_init_on_startup))" in content
    assert "HTTP 服务已就绪" in content
