from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_container_runtime_uses_configurable_host_and_port():
    source = (ROOT / "packages" / "agent" / "src" / "main.py").read_text(encoding="utf-8")
    assert 'os.environ.get("APP_HOST", "127.0.0.1")' in source
    assert 'os.environ.get("APP_PORT", "8000")' in source


def test_docker_healthcheck_matches_health_route():
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    assert "/api/stats/health" in dockerfile
    assert "/api/stats/health" in compose
    assert "http://127.0.0.1:8000/health" not in dockerfile
    assert "http://127.0.0.1:8000/health" not in compose
