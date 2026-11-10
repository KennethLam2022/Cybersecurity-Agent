"""Custom endpoint checks use synthetic DNS and never call a paid API."""
import socket

import pytest
import requests

from auth import validate_llm_url
from llm_provider import LLMProvider


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.delenv("LLM_EXTRA_DOMAINS", raising=False)
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
    ])


def test_should_allow_custom_provider_without_registration():
    assert validate_llm_url("https://apihub.agnes-ai.com/v1")
    assert validate_llm_url("https://new-provider.example:8443/api/v1")


@pytest.mark.parametrize("url", [
    "http://provider.example/v1", "https://user:secret@provider.example",
    "https://127.0.0.1", "https://10.0.0.1", "https://169.254.169.254",
    "https://[::1]", "https://[fc00::1]", "https://224.0.0.1",
    "https://100.64.0.1", "https://localhost", "https://service.local",
    "https://provider.example:bad", "https://provider.example/#fragment",
    "https://provider.example/?api_key=secret", "https://provider.example\\@other.example",
    "https://provider.example\n", "https://provider.example:0",
    "https://provider.example:-1",
])
def test_should_reject_unsafe_endpoint(url):
    assert not validate_llm_url(url)


def test_should_reject_mixed_public_private_dns(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
        (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::1", 443, 0, 0)),
    ])
    assert not validate_llm_url("https://provider.example")
    assert not validate_llm_url("https://api.openai.com")


def test_should_fail_closed_when_dns_fails(monkeypatch):
    def unavailable(*args, **kwargs):
        raise socket.gaierror("synthetic failure")
    monkeypatch.setattr(socket, "getaddrinfo", unavailable)
    assert not validate_llm_url("https://provider.example")


def test_should_preserve_explicit_local_ollama_only():
    assert validate_llm_url("http://localhost:11434/v1", allow_local=True)
    assert not validate_llm_url("http://localhost:8000", allow_local=True)


@pytest.mark.parametrize("endpoint", ["llm_configs_save_one", "llm_save_config"])
def test_should_reject_private_url_before_saving(endpoint, monkeypatch):
    import routes_api_eval as routes
    def unexpected(*args, **kwargs):
        pytest.fail("invalid endpoint must not change storage or runtime")
    monkeypatch.setattr(routes, "_save_llm_config_card", unexpected)
    monkeypatch.setattr(routes, "_load_llm_config_legacy", unexpected)
    result = getattr(routes, endpoint)({
        "module_id": "chat", "provider": "custom", "model": "demo-model",
        "base_url": "https://127.0.0.1", "api_key": "synthetic-key",
    })
    assert result.status_code == 400


@pytest.mark.parametrize("module_id", ["chat", "embedding"])
def test_should_test_custom_provider_without_following_redirects(module_id, monkeypatch):
    import routes_api_eval as routes
    def redirected(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        response = requests.Response()
        response.status_code = 302
        response.headers["Location"] = "https://127.0.0.1"
        response._content = b'{"choices":[{"message":{"content":"Hi"}}]}'
        return response
    monkeypatch.setattr(requests, "post", redirected)
    monkeypatch.setattr(routes.agent.memory, "record_llm_usage_event", lambda **kwargs: None)
    result = routes.llm_configs_test({
        "module_id": module_id, "model": "demo-model",
        "base_url": "https://new-provider.example/v1", "api_key": "synthetic-key",
    })
    assert isinstance(result, dict)
    assert result["ok"] is False


def test_should_recheck_address_before_main_provider_request(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("private endpoint must not receive a request")
    monkeypatch.setattr(requests, "post", unexpected)
    provider = LLMProvider(base_url="https://127.0.0.1", api_key="synthetic-key")
    with pytest.raises(ValueError):
        provider._try_provider({}, 1)


def test_should_generate_with_custom_provider(monkeypatch):
    def completed(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        assert url == "https://new-provider.example/v1/chat/completions"
        response = requests.Response()
        response.status_code = 200
        response._content = b'{"choices":[{"message":{"content":"OK"}}]}'
        return response
    monkeypatch.setattr(requests, "post", completed)
    provider = LLMProvider(base_url="https://new-provider.example/v1", api_key="synthetic-key",
                           use_ollama_fallback=False)
    assert provider.chat([{"role": "user", "content": "Hi"}])["content"] == "OK"


def test_should_save_custom_provider_without_registration(monkeypatch):
    import routes_api_eval as routes
    saved = {}
    def persist(module_id, provider, model, base_url, api_key):
        saved.update(module=module_id, url=base_url, key=api_key)
        return "masked"
    monkeypatch.setattr(routes, "_save_llm_config_card", persist)
    result = routes.llm_configs_save_one({
        "module_id": "promptEval", "provider": "custom", "model": "demo-model",
        "base_url": "https://new-provider.example/v1", "api_key": "synthetic-key",
    })
    assert result["ok"] is True
    assert saved == {"module": "promptEval", "url": "https://new-provider.example/v1",
                     "key": "synthetic-key"}


@pytest.mark.parametrize("module_id", ["chat", "embedding"])
def test_should_complete_custom_connection_test(module_id, monkeypatch):
    import routes_api_eval as routes
    def completed(url, **kwargs):
        assert kwargs["allow_redirects"] is False
        response = requests.Response()
        response.status_code = 200
        response._content = (b'{"choices":[{"message":{"content":"Hi"}}],'
                             b'"data":[{"embedding":[0.1,0.2]}]}')
        return response
    monkeypatch.setattr(requests, "post", completed)
    monkeypatch.setattr(routes.agent.memory, "record_llm_usage_event", lambda **kwargs: None)
    result = routes.llm_configs_test({
        "module_id": module_id, "model": "demo-model",
        "base_url": "https://apihub.agnes-ai.com/v1", "api_key": "synthetic-key",
    })
    assert result["ok"] is True


def test_should_block_redirect_in_main_provider(monkeypatch):
    def redirected(*args, **kwargs):
        assert kwargs["allow_redirects"] is False
        response = requests.Response()
        response.status_code = 302
        response._content = b'{"choices":[{"message":{"content":"unsafe"}}]}'
        return response
    monkeypatch.setattr(requests, "post", redirected)
    provider = LLMProvider(base_url="https://new-provider.example/v1", api_key="synthetic-key")
    with pytest.raises(ValueError):
        provider._try_provider({}, 1)


def test_should_reject_private_stream_before_network(monkeypatch):
    import asyncio
    import httpx
    def unexpected(*args, **kwargs):
        pytest.fail("private stream must not open an HTTP client")
    monkeypatch.setattr(httpx, "AsyncClient", unexpected)
    provider = LLMProvider(base_url="https://127.0.0.1", api_key="synthetic-key",
                           use_ollama_fallback=False, max_retries=0)
    async def collect():
        return [item async for item in provider.chat_stream([])]
    result = asyncio.run(collect())
    assert result == ["抱歉，当前 AI 服务暂不可用，请稍后再试。如果问题持续，请联系技术支持。"]


def test_should_not_follow_redirects_in_main_stream(monkeypatch):
    import asyncio
    import httpx

    client_options = {}

    class RedirectResponse:
        def raise_for_status(self):
            request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
            response = httpx.Response(302, request=request)
            raise httpx.HTTPStatusError("redirect", request=request, response=response)

    class StreamContext:
        async def __aenter__(self):
            return RedirectResponse()

        async def __aexit__(self, *args):
            return False

    class Client:
        def __init__(self, **kwargs):
            client_options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            return StreamContext()

    monkeypatch.setattr(httpx, "AsyncClient", Client)
    provider = LLMProvider(
        base_url="https://provider.example/v1",
        api_key="synthetic-key",
        use_ollama_fallback=False,
        max_retries=0,
    )

    async def collect():
        return [item async for item in provider.chat_stream([])]

    result = asyncio.run(collect())
    assert client_options["follow_redirects"] is False
    assert result == ["抱歉，当前 AI 服务暂不可用，请稍后再试。如果问题持续，请联系技术支持。"]
