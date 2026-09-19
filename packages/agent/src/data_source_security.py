"""Validation rules for externally configured data sources.

This module deliberately does not fetch remote content. It only validates a
sync plan before a later controlled worker performs the actual read operation.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from urllib.parse import urlparse


_WRITE_SQL = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|replace|grant|revoke|attach|detach)\b",
    re.IGNORECASE,
)
_ALLOWED_TYPES = {"local_upload", "url", "web_directory", "database_readonly", "excel_csv"}


def validate_remote_url(value: str) -> str:
    """Return a user-facing validation error, or an empty string when allowed."""
    parsed = urlparse(str(value or "").strip())
    if parsed.scheme not in {"https", "http"}:
        return "远程数据源只允许 HTTP/HTTPS"
    if parsed.username or parsed.password:
        return "URL 不允许携带账号密码"
    host = (parsed.hostname or "").lower().rstrip(".")
    if not host:
        return "URL 缺少主机名"
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".localhost"):
        return "禁止访问 localhost"
    try:
        address = ipaddress.ip_address(host)
        if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
            return "禁止访问内网、回环或保留地址"
    except ValueError:
        try:
            resolved = {item[4][0] for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)}
            for value in resolved:
                address = ipaddress.ip_address(value)
                if address.is_private or address.is_loopback or address.is_link_local or address.is_reserved:
                    return "域名解析到内网、回环或保留地址"
        except (OSError, ValueError):
            # Unresolved public hosts are left for the worker to report clearly.
            pass
    return ""


# Backward-compatible name used by existing callers and tests.
_validate_remote_url = validate_remote_url


def validate_data_source_config(source_type: str, endpoint: str = "",
                                config: dict | None = None) -> dict:
    config = config or {}
    errors: list[str] = []
    source_type = str(source_type or "").strip()
    endpoint = str(endpoint or "").strip()
    if source_type not in _ALLOWED_TYPES:
        errors.append("数据源类型不支持")
    if source_type in {"url", "web_directory"}:
        error = _validate_remote_url(endpoint)
        if error:
            errors.append(error)
    elif source_type == "database_readonly":
        if not endpoint:
            errors.append("只读数据库数据源必须提供连接标识")
        if config.get("read_only") is not True:
            errors.append("数据库数据源必须显式声明 read_only=true")
        query = str(config.get("query") or "")
        if _WRITE_SQL.search(query):
            errors.append("数据库查询包含写操作或 DDL 关键词")
    elif source_type in {"local_upload", "excel_csv"} and not endpoint:
        errors.append("本地或批量数据源必须提供文件/目录标识")
    try:
        timeout = int(config.get("timeout_seconds", 15))
        if timeout < 1 or timeout > 60:
            errors.append("超时必须在 1-60 秒之间")
    except (TypeError, ValueError):
        errors.append("timeout_seconds 必须是整数")
    try:
        max_bytes = int(config.get("max_bytes", 50 * 1024 * 1024))
        if max_bytes < 1 or max_bytes > 500 * 1024 * 1024:
            errors.append("max_bytes 必须在 1B-500MB 之间")
    except (TypeError, ValueError):
        errors.append("max_bytes 必须是整数")
    return {"valid": not errors, "errors": errors}
