from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]


def test_linux_service_is_production_safe_and_single_worker():
    service = (ROOT / "deploy" / "linux" / "securenexus.service").read_text(encoding="utf-8")
    assert "Environment=APP_ENV=production" in service
    assert "Environment=APP_HOST=127.0.0.1" in service
    assert "Environment=ALLOW_LEGACY_LOCAL_WORKSPACE=0" in service
    assert "Environment=UVICORN_WORKERS=1" in service
    assert "EnvironmentFile=-/etc/securenexus/securenexus.env" in service
    assert "User=securenexus" in service
    assert "Group=securenexus" in service
    assert "ExecStart=/usr/bin/env bash /opt/securenexus/scripts/start-linux.sh" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=full" in service


def test_nginx_keeps_sse_streaming_unbuffered():
    config = (ROOT / "deploy" / "linux" / "nginx-securenexus.conf").read_text(encoding="utf-8")
    assert "listen 443 ssl" in config
    assert "return 301 https://" in config
    assert "client_max_body_size 512m" in config
    assert "proxy_pass http://127.0.0.1:8000" in config
    assert "proxy_buffering off" in config
    assert "proxy_http_version 1.1" in config
    assert "proxy_read_timeout" in config
