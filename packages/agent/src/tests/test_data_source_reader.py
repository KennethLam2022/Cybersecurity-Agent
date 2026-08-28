import io

import pytest

import data_source_reader


class _Response:
    def __init__(self, body, content_type="text/html"):
        self._body = body
        self.headers = {"Content-Type": content_type}

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self, size=-1):
        return self._body[:size]


class _Opener:
    def __init__(self, response):
        self.response = response

    def open(self, request, timeout):
        return self.response


def test_read_url_cleans_html_and_returns_hash(monkeypatch):
    response = _Response(b"<html><script>bad()</script><article>Hello &amp; security</article></html>")
    monkeypatch.setattr(data_source_reader, "build_opener", lambda *_: _Opener(response))
    result = data_source_reader.read_url("https://example.com/advisory")
    assert result["content"] == "Hello & security"
    assert result["content_hash"]
    assert result["bytes"] > 0


def test_read_url_rejects_content_over_limit(monkeypatch):
    response = _Response(b"123456789", "text/plain")
    monkeypatch.setattr(data_source_reader, "build_opener", lambda *_: _Opener(response))
    with pytest.raises(ValueError, match="超过大小限制"):
        data_source_reader.read_url("https://example.com/advisory", {"max_bytes": 5})
