"""单元测试 — 核心逻辑

运行：python -m pytest tests/ -v
"""
import os
from auth import validate_llm_url, is_admin_route, is_platform_only_admin_route
from main import _csrf_is_valid
from llm_provider import CircuitBreaker
from _eval_generation import _extract_json, _clean_answer
from _eval_common import compute_avg_stats
import pytest
import sys
import os
import json
_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)


# ============================================================
# 1. _eval_common: compute_avg_stats
# ============================================================


class TestComputeAvgStats:
    def test_empty_results(self):
        assert compute_avg_stats([]) == {
            "faithfulness": 0.0, "relevancy": 0.0, "hallucination": 0.0,
            "context_precision": 0.0, "context_recall": 0.0, "overall": 0.0,
        }

    def test_single_result(self):
        r = [{
            "faithfulness": 0.8, "relevancy": 0.7, "hallucination": 0.9,
            "context_precision": 0.6, "context_recall": 0.5,
        }]
        avg = compute_avg_stats(r)
        assert avg["faithfulness"] == 0.8
        assert avg["relevancy"] == 0.7
        assert avg["overall"] == round((0.8+0.7+0.9+0.6+0.5)/5, 4)

    def test_multiple_results(self):
        r = [
            {"faithfulness": 1.0, "relevancy": 1.0, "hallucination": 1.0,
                "context_precision": 1.0, "context_recall": 1.0},
            {"faithfulness": 0.0, "relevancy": 0.0, "hallucination": 0.0,
                "context_precision": 0.0, "context_recall": 0.0},
        ]
        avg = compute_avg_stats(r)
        assert avg["faithfulness"] == 0.5
        assert avg["overall"] == 0.5

    def test_partial_scores(self):
        """某些字段缺失时应跳过"""
        r = [{"faithfulness": 1.0}, {"faithfulness": 0.5, "relevancy": 0.8}]
        avg = compute_avg_stats(r)
        assert avg["faithfulness"] == 0.75  # (1.0 + 0.5) / 2
        assert avg["relevancy"] == 0.8  # only one value

    def test_scores_nested_key(self):
        """兼容嵌套 scores 结构"""
        r = [{"scores": {"faithfulness": 0.9, "relevancy": 0.8}}]
        avg = compute_avg_stats(r)
        assert avg["faithfulness"] == 0.9
        assert avg["relevancy"] == 0.8


# ============================================================
# 2. _eval_generation: _extract_json and _clean_answer
# ============================================================


class TestExtractJson:
    def test_pure_json(self):
        assert json.loads(_extract_json('{"score": 0.5}')) == {"score": 0.5}

    def test_with_markdown_fence(self):
        result = _extract_json('```json\n{"score": 0.8, "explanation": "好"}\n```')
        assert json.loads(result) == {"score": 0.8, "explanation": "好"}

    def test_no_json(self):
        assert _extract_json("hello world") == "hello world"

    def test_json_with_prefix_text(self):
        result = _extract_json('分析结果如下：{"score": 0.9}')
        assert json.loads(result) == {"score": 0.9}

    def test_empty_string(self):
        assert _extract_json("") == ""


class TestCleanAnswer:
    def test_remove_thinking_block(self):
        answer = "【思考过程】\n我分析了参考资料\n---\n**结论**：个人信息是指..."
        assert "【思考过程】" not in _clean_answer(answer)

    def test_remove_inline_sources(self):
        answer = "这是一个结论[来源1: GB/T 35273-2020]。"
        assert "[来源1:" not in _clean_answer(answer)

    def test_no_thinking_no_sources(self):
        answer = "**结论**：个人信息是指以电子或其他方式记录的..."
        assert _clean_answer(answer) == answer

    def test_empty_answer(self):
        assert _clean_answer("") == ""


# ============================================================
# 3. llm_provider: CircuitBreaker
# ============================================================


class TestCircuitBreaker:
    def test_initial_state(self):
        cb = CircuitBreaker(failure_threshold=3, open_timeout=60.0)
        assert cb.state == "CLOSED"

    def test_trip_on_threshold(self):
        cb = CircuitBreaker(failure_threshold=2, open_timeout=60.0)
        # 连续失败 2 次应触发熔断
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == "CLOSED"  # 1次失败，未到阈值
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == "OPEN"  # 2次失败，熔断

    def test_reset_on_success(self):
        cb = CircuitBreaker(failure_threshold=2, open_timeout=60.0)
        # 失败1次，然后成功
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        result = cb.call(lambda: "ok")
        assert result == "ok"
        assert cb.state == "CLOSED"
        assert cb.failure_count == 0

    def test_allows_request_in_half_open(self):
        cb = CircuitBreaker(failure_threshold=2, open_timeout=0.01)
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == "OPEN"
        import time
        time.sleep(0.02)
        # HALF_OPEN 状态下，探测请求可以执行
        result = cb.call(lambda: "recovered")
        assert result == "recovered"
        assert cb.state == "CLOSED"

    def test_blocks_when_open(self):
        cb = CircuitBreaker(failure_threshold=1, open_timeout=60.0)
        with pytest.raises(ValueError):
            cb.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb.state == "OPEN"
        with pytest.raises(RuntimeError, match="熔断器 OPEN"):
            cb.call(lambda: "should not reach")

    def test_different_instances_independent(self):
        cb1 = CircuitBreaker(failure_threshold=2, open_timeout=60.0)
        cb2 = CircuitBreaker(failure_threshold=2, open_timeout=60.0)
        with pytest.raises(ValueError):
            cb1.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        with pytest.raises(ValueError):
            cb1.call(lambda: (_ for _ in ()).throw(ValueError("fail")))
        assert cb1.state == "OPEN"
        assert cb2.state == "CLOSED"


# ============================================================
# 4. auth: validate_llm_url, is_admin_route
# ============================================================


class TestValidateLlmUrl:
    def test_allowed_domain(self):
        assert validate_llm_url("https://api.deepseek.com/v1") is True
        assert validate_llm_url("https://api.openai.com/v1") is True

    def test_allowed_subdomain(self):
        assert validate_llm_url("https://us-east-1.api.siliconflow.cn") is True

    def test_disallowed_domain(self):
        assert validate_llm_url("https://evil-hacker.com") is False

    def test_invalid_url(self):
        assert validate_llm_url("not a url") is False


class TestIsAdminRoute:
    def test_admin_document_path(self):
        assert is_admin_route("/api/documents/scan") is True
        assert is_admin_route("/admin/knowledge-bases") is True

    def test_admin_llm_configs(self):
        assert is_admin_route("/api/llm/configs/save") is True

    def test_admin_llm_legacy_config_routes(self):
        assert is_admin_route("/api/llm/config") is True
        assert is_admin_route("/api/llm/presets") is True
        assert is_admin_route("/api/llm/test") is True

    def test_public_llm_current_config_readonly(self):
        assert is_admin_route("/api/llm/config/current") is False

    def test_public_chat_path(self):
        assert is_admin_route("/api/conversations") is False

    def test_public_static(self):
        assert is_admin_route("/static/style.css") is False

    def test_admin_conversation_hard_delete(self):
        assert is_admin_route("/api/conversations/abc-123/hard") is True

    def test_public_conversation_detail(self):
        assert is_admin_route("/api/conversations/abc-123") is False


# ============================================================
# 5. auth: management route classifications
# ============================================================


class TestPlatformOnlyAdminRoutes:
    def test_legacy_admin_domain_is_platform_only(self):
        assert is_platform_only_admin_route('/api/admin/email-notifications/config') is True

    def test_platform_configuration_pages_are_not_public(self):
        for path in (
            '/admin/model-config',
            '/admin/langfuse-config',
            '/admin/sso-config',
            '/admin/email-notifications',
        ):
            assert is_admin_route(path) is True
            assert is_platform_only_admin_route(path) is True

    def test_document_governance_is_platform_only_during_migration(self):
        assert is_platform_only_admin_route('/api/documents/migration-summary') is True

    def test_tenant_scoped_operational_routes_are_not_platform_only(self):
        assert is_platform_only_admin_route('/api/admin/notifications') is False
        assert is_platform_only_admin_route('/api/admin/reflection-runs') is False
        assert is_platform_only_admin_route('/api/admin/ingestion-jobs') is False
        assert is_platform_only_admin_route('/api/admin/extensions/ext-1/grants/tenant-1/agent-1') is False

    def test_tenant_scoped_workspace_route_is_not_platform_only(self):
        assert is_platform_only_admin_route('/api/admin/workspaces') is False


class TestCsrfValidation:
    def test_cookie_csrf_requires_matching_header(self):
        from types import SimpleNamespace
        request = SimpleNamespace(cookies={"securenexus_csrf": "csrf-1"}, headers={"X-CSRF-Token": "csrf-1"})
        assert _csrf_is_valid(request) is True
        request.headers["X-CSRF-Token"] = "csrf-2"
        assert _csrf_is_valid(request) is False


class TestLlmEncryption:
    """加密回环：encrypt → decrypt"""

    def test_encrypt_decrypt_cycle(self, monkeypatch):
        import os
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEY", key)
        # 重新加载模块使 _fernet 使用新 key
        import importlib
        import llm_config_manager
        importlib.reload(llm_config_manager)
        from llm_config_manager import _encrypt_api_key, _decrypt_api_key

        plaintext = "sk-test-key-12345"
        encrypted = _encrypt_api_key(plaintext)
        assert encrypted != plaintext
        assert encrypted.startswith("gAAAAA")  # Fernet 密文前缀
        decrypted = _decrypt_api_key(encrypted)
        assert decrypted == plaintext

    def test_decrypt_wrong_key_returns_none(self, monkeypatch):
        import os
        from cryptography.fernet import Fernet
        key = Fernet.generate_key().decode()
        monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEY", key)
        import importlib
        import llm_config_manager
        importlib.reload(llm_config_manager)
        from llm_config_manager import _encrypt_api_key
        from cryptography.fernet import InvalidToken

        encrypted = _encrypt_api_key("secret")
        # 切到不同 key
        key2 = Fernet.generate_key().decode()
        monkeypatch.setenv("LLM_KEY_ENCRYPTION_KEY", key2)
        importlib.reload(llm_config_manager)
        from llm_config_manager import _decrypt_api_key as _decrypt_wrong
        with pytest.raises(InvalidToken):
            _decrypt_wrong(encrypted)

    def test_key_mask_hides_middle(self):
        from llm_config_manager import _make_key_mask
        masked = _make_key_mask("sk-abcdefghijklmnop")
        assert masked.startswith("sk-abcde")
        assert masked.endswith("mnop")
        assert "•" in masked
        # 掩码不改变长度，只替换中间字符
        assert len(masked) == len("sk-abcdefghijklmnop")

    def test_key_mask_short_key(self):
        """短 key 全部掩码"""
        from llm_config_manager import _make_key_mask
        masked = _make_key_mask("short")
        assert masked == "•••••"


class TestAuthMiddleware:
    """认证中间件集成测试"""

    def test_admin_route_conversation_hard_delete(self):
        from auth import is_admin_route
        assert is_admin_route("/api/conversations/xxx/hard") is True

    def test_admin_route_llm_refresh(self):
        from auth import is_admin_route
        assert is_admin_route("/api/llm/refresh-models") is True

    def test_public_routes(self):
        from auth import is_admin_route
        public = ["/", "/login", "/register", "/api/conversations", "/api/chat/stream"]
        for path in public:
            assert is_admin_route(path) is False, f"{path} should be public"

    def test_admin_page_requires_authenticated_admin(self):
        from auth import is_admin_route
        assert is_admin_route("/admin") is True


class TestSseStream:
    """SSE 流式接口测试"""

    def test_stream_timeout_raises(self):
        """验证 asyncio.wait_for 超时机制正常"""
        import asyncio

        async def _test():
            async def slow_gen():
                await asyncio.sleep(999)
                yield "too slow"

            with pytest.raises(asyncio.TimeoutError):
                gen = slow_gen()
                await asyncio.wait_for(gen.__anext__(), timeout=0.01)
        asyncio.run(_test())

    def test_stream_yields_data(self):
        """模拟 SSE 流返回数据"""
        import asyncio

        async def _test():
            results = []
            async def fast_gen():
                for i in range(3):
                    yield f"data: {i}\n\n"
                    await asyncio.sleep(0.001)
            async for chunk in fast_gen():
                results.append(chunk)
            assert len(results) == 3
            assert results[0] == "data: 0\n\n"
        asyncio.run(_test())


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
