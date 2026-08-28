from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_linux_service_is_production_safe_and_single_worker():
    service = (ROOT / "deploy" / "linux" / "securenexus.service").read_text(encoding="utf-8")
    assert "Environment=APP_ENV=production" in service
    assert "Environment=ALLOW_LEGACY_LOCAL_WORKSPACE=0" in service
    assert "Environment=UVICORN_WORKERS=1" in service
    assert "NoNewPrivileges=true" in service


def test_nginx_keeps_sse_streaming_unbuffered():
    config = (ROOT / "deploy" / "linux" / "nginx-securenexus.conf").read_text(encoding="utf-8")
    assert "proxy_buffering off" in config
    assert "proxy_http_version 1.1" in config
    assert "proxy_read_timeout" in config
