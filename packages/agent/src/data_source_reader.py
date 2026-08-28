"""Controlled readers for data-source synchronization.

Readers return staged content only. They never write to the RAG index.
"""
from __future__ import annotations

import hashlib
import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

from data_source_security import validate_data_source_config


class _TextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in {"script", "style", "noscript", "svg"}:
            self.skip += 1

    def handle_endtag(self, tag):
        if tag.lower() in {"script", "style", "noscript", "svg"} and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip and data.strip():
            self.parts.append(data.strip())


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _clean_text(raw: bytes, content_type: str) -> str:
    text = raw.decode("utf-8", errors="replace")
    if "html" in content_type.lower() or re.search(r"<html|<body|<article", text, re.I):
        parser = _TextExtractor()
        parser.feed(text)
        text = "\n".join(parser.parts)
    text = html.unescape(text)
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())[:2_000_000]


def read_url(endpoint: str, config: dict | None = None) -> dict:
    config = config or {}
    validation = validate_data_source_config("url", endpoint, config)
    if not validation["valid"]:
        raise ValueError("；".join(validation["errors"]))
    timeout = int(config.get("timeout_seconds", 15))
    max_bytes = int(config.get("max_bytes", 50 * 1024 * 1024))
    current = endpoint
    opener = build_opener(_NoRedirect())
    for _ in range(4):
        parsed = urlparse(current)
        request = Request(current, headers={"User-Agent": "AnShu-SecureNexus/2.0"}, method="GET")
        try:
            with opener.open(request, timeout=timeout) as response:
                raw = response.read(max_bytes + 1)
                if len(raw) > max_bytes:
                    raise ValueError("远程内容超过大小限制")
                content_type = response.headers.get("Content-Type", "text/plain")
                text = _clean_text(raw, content_type)
                if not text:
                    raise ValueError("远程内容为空")
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                return {"source_name": parsed.path.rsplit("/", 1)[-1] or parsed.netloc,
                        "endpoint": current, "content": text, "content_hash": digest,
                        "content_type": content_type, "bytes": len(raw)}
        except Exception as exc:
            # Redirects are handled explicitly so every hop can be validated.
            if getattr(exc, "code", None) in {301, 302, 303, 307, 308}:
                location = exc.headers.get("Location", "")
                current = urljoin(current, location)
                check = validate_data_source_config("url", current, config)
                if not check["valid"]:
                    raise ValueError("重定向目标校验失败：" + "；".join(check["errors"])) from exc
                continue
            raise ValueError(f"远程读取失败：{exc}") from exc
    raise ValueError("重定向次数超过限制")
