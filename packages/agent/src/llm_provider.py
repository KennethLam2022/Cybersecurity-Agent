"""可配置 LLM Provider — OpenAI 兼容接口

稳定性保障（参考知识库 E.3 + H.1）：
  1. 超时分离：connect_timeout=10s, read_timeout=180s
  2. 重试：仅对 timeout/429/5xx 重试 2 次（指数退避+jitter）
  3. 限流：令牌桶，防止 429
  4. 熔断：连续 3 次失败 → OPEN 60s → HALF_OPEN → 探测成功恢复
  5. 降级：主 LLM 失败 → 本地 Ollama 7B 兜底 → 兜底也失败 → 友好拒绝

用法：
  provider = LLMProvider()
  answer = provider.chat("你好")

  # 流式调用
  async for token in provider.chat_stream(messages):
      logger.info(token, end="")
"""
import os
import json
import logging
import time
import random
import asyncio
from typing import Optional
from datetime import datetime, timedelta

import requests
import httpx

from memory import get_llm_config_card

logger = logging.getLogger(__name__)

_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
_DEFAULT_MODEL = "Pro/MiniMaxAI/MiniMax-M2.5"
_OLLAMA_BASE_URL = "http://localhost:11434/v1"
_OLLAMA_MODEL = "qwen2.5:7b"

# ---- 令牌桶（限流） ----


class TokenBucket:
    """简单令牌桶限流器，线程安全（用锁）"""

    def __init__(self, capacity: int = 10, fill_rate: float = 2.0):
        self.capacity = capacity
        self.fill_rate = fill_rate
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = __import__("threading").Lock()

    def _refill(self):
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.fill_rate)
        self.last_refill = now

    def acquire(self, tokens: int = 1) -> float:
        """尝试获取 tokens 个令牌，返回等待时间（秒）。0 表示立即通过"""
        with self._lock:
            self._refill()
            if self.tokens >= tokens:
                self.tokens -= tokens
                return 0.0
            deficit = tokens - self.tokens
            wait = deficit / self.fill_rate
            return wait

# ---- 熔断器 ----


class CircuitBreakerState:
    CLOSED = "CLOSED"       # 正常
    OPEN = "OPEN"           # 熔断开启，请求直接失败
    HALF_OPEN = "HALF_OPEN"  # 半开，允许探测请求


class CircuitBreaker:
    """熔断器：连续 failure_threshold 次失败 → OPEN → open_timeout 秒 → HALF_OPEN → 成功则恢复"""

    def __init__(self, failure_threshold: int = 3, open_timeout: float = 60.0):
        self.failure_threshold = failure_threshold
        self.open_timeout = open_timeout
        self.state = CircuitBreakerState.CLOSED
        self.failure_count = 0
        self.last_failure_time = 0.0
        self._lock = __import__("threading").Lock()

    def call(self, fn, *args, **kwargs):
        """执行调用，受熔断保护"""
        with self._lock:
            if self.state == CircuitBreakerState.OPEN:
                if time.monotonic() - self.last_failure_time > self.open_timeout:
                    self.state = CircuitBreakerState.HALF_OPEN
                    logger.info("熔断器: OPEN → HALF_OPEN，允许探测请求")
                else:
                    logger.warning(
                        f"熔断器: OPEN，拒绝请求（剩余 {self.open_timeout - (time.monotonic() - self.last_failure_time):.0f}s）")
                    raise RuntimeError(f"熔断器 OPEN，服务暂不可用")

        try:
            result = fn(*args, **kwargs)
        except Exception as e:
            with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.monotonic()
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitBreakerState.OPEN
                    logger.warning(f"熔断器: CLOSED → OPEN（连续 {self.failure_count} 次失败）")
            raise e

        with self._lock:
            if self.state == CircuitBreakerState.HALF_OPEN:
                logger.info("熔断器: HALF_OPEN → CLOSED（探测成功）")
            self.state = CircuitBreakerState.CLOSED
            self.failure_count = 0

        return result

    async def call_async(self, fn, *args, **kwargs):
        """异步版本"""
        with self._lock:
            if self.state == CircuitBreakerState.OPEN:
                if time.monotonic() - self.last_failure_time > self.open_timeout:
                    self.state = CircuitBreakerState.HALF_OPEN
                    logger.info("熔断器: OPEN → HALF_OPEN，允许探测请求")
                else:
                    logger.warning(f"熔断器: OPEN，拒绝请求")
                    raise RuntimeError(f"熔断器 OPEN，服务暂不可用")

        try:
            result = await fn(*args, **kwargs)
        except Exception as e:
            with self._lock:
                self.failure_count += 1
                self.last_failure_time = time.monotonic()
                if self.failure_count >= self.failure_threshold:
                    self.state = CircuitBreakerState.OPEN
                    logger.warning(f"熔断器: CLOSED → OPEN（连续 {self.failure_count} 次失败）")
            raise e

        with self._lock:
            if self.state == CircuitBreakerState.HALF_OPEN:
                logger.info("熔断器: HALF_OPEN → CLOSED（探测成功）")
            self.state = CircuitBreakerState.CLOSED
            self.failure_count = 0

        return result

# ---- 重试工具 ----


def _should_retry(e: Exception) -> bool:
    """判断是否值得重试：仅对 timeout/429/5xx"""
    if isinstance(e, (requests.Timeout, httpx.TimeoutException)):
        return True

    resp = None
    if isinstance(e, requests.HTTPError):
        resp = e.response
    elif isinstance(e, httpx.HTTPStatusError):
        resp = e.response
    if resp is not None:
        status = resp.status_code
        if status == 429 or status >= 500:
            return True
    return False


def _backoff(attempt: int, base: float = 2.0, max_wait: float = 30.0) -> float:
    """指数退避 + jitter"""
    sleep = min(base * (2 ** attempt), max_wait)
    jitter = random.uniform(0, sleep * 0.5)
    return sleep + jitter


# ---- 兜底回答 ----
_FALLBACK_MESSAGE = "抱歉，当前 AI 服务暂不可用，请稍后再试。如果问题持续，请联系技术支持。"


class LLMProvider:
    """LLM 调用层，支持 OpenAI 兼容接口 + Ollama 兜底 + 限流/熔断/重试"""

    def __init__(
        self,
        base_url: Optional[str] = None,
        api_key: Optional[str] = None,
        model: Optional[str] = None,
        ollama_base_url: Optional[str] = None,
        ollama_model: Optional[str] = None,
        use_ollama_fallback: bool = True,
        max_retries: int = 2,
        rate_limit_capacity: int = 10,
        rate_limit_fill: float = 2.0,
    ):
        self.base_url = (base_url or _DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or os.environ.get("SILICONFLOW_API_KEY", "")
        self.model = model or _DEFAULT_MODEL

        # 从 DB 读取熔断兜底配置
        fallback_cfg = get_llm_config_card('fallback')
        self.ollama_base_url = (ollama_base_url or fallback_cfg.get(
            'base_url') or _OLLAMA_BASE_URL).rstrip("/")
        self.ollama_model = ollama_model or fallback_cfg.get('model') or _OLLAMA_MODEL
        self.use_ollama_fallback = use_ollama_fallback

        self.max_retries = max_retries
        self.rate_limiter = TokenBucket(capacity=rate_limit_capacity, fill_rate=rate_limit_fill)
        self.circuit_breaker = CircuitBreaker(failure_threshold=3, open_timeout=60.0)
        self._provider_name = "api"

    def reconfigure(self, base_url: str, api_key: str, model: str, provider_name: str = ""):
        """运行时重新配置 LLM 提供商（不重启服务）"""
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        if provider_name:
            self._provider_name = provider_name
        self.circuit_breaker.state = CircuitBreakerState.CLOSED
        self.circuit_breaker.failure_count = 0
        logger.info(f"LLM 提供商已重新配置: model={model}, base_url={base_url}")

    def get_current_provider(self) -> dict:
        return {"name": self._provider_name, "model": self.model, "base_url": self.base_url}

    def test_connection(self, base_url: str, api_key: str, model: str, timeout: int = 15) -> dict:
        """测试指定 LLM 提供商是否可达

        返回: {"ok": bool, "message": str, "model": str}
        """
        base_url = base_url.rstrip("/")
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 10,
            "stream": False,
        }
        t0 = time.time()
        try:
            resp = requests.post(
                f"{base_url}/chat/completions",
                headers=headers,
                json=payload,
                timeout=(10, timeout),
            )
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            elapsed = time.time() - t0
            return {"ok": True, "message": f"连接成功 ({elapsed:.1f}s)", "model": model}
        except requests.exceptions.ConnectionError:
            return {"ok": False, "message": "无法连接，请检查 BASE URL 是否正确"}
        except requests.exceptions.Timeout:
            return {"ok": False, "message": "连接超时，请检查网络或提供商状态"}
        except requests.exceptions.HTTPError as e:
            status = e.response.status_code
            if status == 401:
                return {"ok": False, "message": "认证失败，请检查 API Key 是否正确"}
            elif status == 404:
                return {"ok": False, "message": f"模型 '{model}' 不存在，请检查模型名称"}
            else:
                return {"ok": False, "message": f"HTTP {status}: {str(e)[:60]}"}
        except Exception as e:
            return {"ok": False, "message": f"错误: {str(e)[:80]}"}

    # ---- 辅助 ----
    def _is_timeout_or_server_error(self, e: Exception) -> bool:
        return _should_retry(e)

    def _build_headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    def _call_ollama_chat(self, messages: list[dict], temperature: float, max_tokens: int) -> str:
        """调用本地 Ollama LLM"""
        payload = {
            "model": self.ollama_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        t0 = time.time()
        resp = requests.post(
            f"{self.ollama_base_url}/chat/completions",
            json=payload,
            timeout=(10, 120),
        )
        resp.raise_for_status()
        data = resp.json()
        elapsed = time.time() - t0
        logger.info(f"Ollama 兜底调用完成 ({elapsed:.1f}s, model={self.ollama_model})")
        return data["choices"][0]["message"]["content"].strip()

    async def _ollama_stream_chat(self, messages: list[dict], temperature: float, max_tokens: int):
        """Ollama 流式兜底，异步生成器"""
        payload = {
            "model": self.ollama_model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        t0 = time.time()
        token_count = 0
        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            async with client.stream(
                "POST",
                f"{self.ollama_base_url}/chat/completions",
                json=payload,
            ) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if line.startswith("data: "):
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            break
                        try:
                            data = json.loads(data_str)
                            delta = data["choices"][0].get("delta", {})
                            content = delta.get("content", "")
                            if content:
                                token_count += 1
                                yield content
                        except json.JSONDecodeError:
                            continue
        elapsed = time.time() - t0
        logger.info(f"Ollama 流式兜底完成 ({elapsed:.1f}s, {token_count} tokens)")

    def _try_provider(self, payload: dict, timeout_read: int) -> dict:
        """调用 LLM 提供商，受限流保护，返回完整响应 JSON"""
        wait = self.rate_limiter.acquire()
        if wait > 0:
            logger.info(f"限流等待 {wait:.1f}s")
            time.sleep(wait)

        resp = requests.post(
            f"{self.base_url}/chat/completions",
            headers=self._build_headers(),
            json=payload,
            timeout=timeout_read,
        )
        resp.raise_for_status()
        return resp.json()

    # ---- 主入口 ----
    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        timeout: int = 180,
        enable_thinking: bool = False,
        thinking_budget: int = 1024,
    ) -> dict:
        """调用 LLM 生成回答（同步，非流式）

        返回: {"content": str, "reasoning_content": str|None, "usage": dict}

        当 enable_thinking=True 时，模型会先输出推理过程（reasoning_content）
        再输出最终回答（content）。保持 system prompt 不变。
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if enable_thinking:
            payload.setdefault("extra_body", {})["enable_thinking"] = True
            payload.setdefault("extra_body", {})["thinking_budget"] = thinking_budget

        last_error = None

        # ---- 阶段1：主 LLM 调用，受熔断保护 ----
        try:
            return self.circuit_breaker.call(self._provider_with_retry, payload, timeout)
        except Exception as e:
            last_error = e
            logger.warning(f"LLM 调用失败（熔断或重试耗尽）: {e}")

        # ---- 阶段2：Ollama 本地兜底 ----
        if self.use_ollama_fallback:
            try:
                logger.info("尝试 Ollama 本地模型兜底...")
                text = self._call_ollama_chat(messages, temperature, max_tokens)
                return {"content": text, "reasoning_content": None}
            except Exception as e2:
                logger.error(f"Ollama 兜底也失败: {e2}")
                last_error = e2

        # ---- 阶段3：全失败 → 友好拒绝 ----
        logger.error(f"所有 LLM 调用均失败，返回兜底消息。最后错误: {last_error}")
        return {"content": _FALLBACK_MESSAGE, "reasoning_content": None}

    def _provider_with_retry(self, payload: dict, timeout: int) -> dict:
        """调用 LLM 提供商，带重试，返回 {"content", "reasoning_content"}"""
        last_exc = None
        for attempt in range(self.max_retries + 1):
            try:
                resp_data = self._try_provider(payload, timeout)
                content = resp_data["choices"][0]["message"].get("content", "").strip()
                reasoning = resp_data["choices"][0]["message"].get("reasoning_content")
                return {
                    "content": content,
                    "reasoning_content": reasoning,
                    "usage": resp_data.get("usage") or {},
                    "model": resp_data.get("model") or self.model,
                }
            except Exception as e:
                last_exc = e
                if attempt < self.max_retries and self._is_timeout_or_server_error(e):
                    wait = _backoff(attempt)
                    logger.warning(f"LLM 第 {attempt+1} 次失败: {e}，{wait:.1f}s 后重试")
                    time.sleep(wait)
                else:
                    break
        raise last_exc

    # ---- 流式入口 ----
    async def chat_stream(
        self,
        messages: list[dict],
        temperature: float = 0.3,
        max_tokens: int = 4096,
        enable_thinking: bool = False,
        thinking_budget: int = 1024,
    ):
        """流式调用 LLM，异步生成器逐 token 产出

        产出格式（dict）：
          {"type": "reasoning", "text": "..."}   ← 推理过程（仅推理模型）
          {"type": "content", "text": "..."}      ← 最终回答

        P2-6：加入 Ollama 兜底 + 熔断 + max_retries 次重试
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": True,
        }
        if enable_thinking:
            payload.setdefault("extra_body", {})["enable_thinking"] = True
            payload.setdefault("extra_body", {})["thinking_budget"] = thinking_budget

        # ---- 阶段1：主 LLM 流式调用（熔断检查 + 最多 max_retries 次重试） ----
        cb_open = False
        with self.circuit_breaker._lock:
            if self.circuit_breaker.state == CircuitBreakerState.OPEN:
                if time.monotonic() - self.circuit_breaker.last_failure_time > self.circuit_breaker.open_timeout:
                    self.circuit_breaker.state = CircuitBreakerState.HALF_OPEN
                    logger.info("熔断器: OPEN → HALF_OPEN（流式路径）")
                else:
                    cb_open = True

        if not cb_open:
            last_error = None
            for attempt in range(self.max_retries + 1):
                try:
                    wait = self.rate_limiter.acquire()
                    if wait > 0:
                        await asyncio.sleep(wait)

                    t0 = time.time()
                    token_count = 0
                    reasoning_count = 0
                    async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
                        async with client.stream(
                            "POST",
                            f"{self.base_url}/chat/completions",
                            headers=self._build_headers(),
                            json=payload,
                        ) as resp:
                            resp.raise_for_status()
                            async for line in resp.aiter_lines():
                                if line.startswith("data: "):
                                    data_str = line[6:]
                                    if data_str.strip() == "[DONE]":
                                        break
                                    try:
                                        data = json.loads(data_str)
                                        delta = data["choices"][0].get("delta", {})
                                        content = delta.get("content", "")
                                        reasoning = delta.get("reasoning_content", "")
                                        if reasoning:
                                            reasoning_count += 1
                                            yield {"type": "reasoning", "text": reasoning}
                                        if content:
                                            token_count += 1
                                            yield {"type": "content", "text": content}
                                    except json.JSONDecodeError:
                                        continue
                    elapsed = time.time() - t0
                    logger.info(
                        f"LLM 流式完成 ({elapsed:.1f}s, {token_count} tokens, {reasoning_count} reasoning)")
                    self.circuit_breaker.state = CircuitBreakerState.CLOSED
                    self.circuit_breaker.failure_count = 0
                    return
                except Exception as e:
                    last_error = e
                    self.circuit_breaker.failure_count += 1
                    self.circuit_breaker.last_failure_time = time.monotonic()
                    if self.circuit_breaker.failure_count >= self.circuit_breaker.failure_threshold:
                        self.circuit_breaker.state = CircuitBreakerState.OPEN
                        logger.warning(
                            f"熔断器: CLOSED → OPEN（流式，连续 {self.circuit_breaker.failure_count} 次失败）")
                    if attempt < self.max_retries and self._is_timeout_or_server_error(e):
                        wait = _backoff(attempt)
                        logger.warning(f"流式重试 {attempt+1}/{self.max_retries}: {e}，{wait:.1f}s 后重试")
                        await asyncio.sleep(wait)
                    else:
                        break

            logger.warning(f"LLM 流式调用失败（重试耗尽）: {last_error}")

        # ---- 阶段2：Ollama 本地兜底（非流式，返回单段完整内容） ----
        if self.use_ollama_fallback:
            try:
                logger.info("尝试 Ollama 本地模型流式兜底...")
                async for token in self._ollama_stream_chat(messages, temperature, max_tokens):
                    yield token
                return
            except Exception as e2:
                logger.error(f"Ollama 流式兜底也失败: {e2}")

        logger.error("所有 LLM 流式调用均失败")
        yield _FALLBACK_MESSAGE

    def to_config(self) -> dict:
        return {
            "base_url": self.base_url,
            "model": self.model,
            "api_key_mask": self.api_key[:8] + "..." if len(self.api_key) > 8 else "",
            "ollama_fallback": self.ollama_model if self.use_ollama_fallback else False,
            "max_retries": self.max_retries,
        }


# ---- 评测专用 LLM 工厂 ----

def get_llm():
    """创建后端评测 LLM 实例（读取 promptEval 配置卡片）

    供 prompt_tester.py 等评测模块使用，确保所有评测调用
    统一跟随 ⑥ promptEval 后端评测 LLM 的模型配置：
      - Prompt 全量测试域B/域C评分
      - 综合质量评测
      - 检索质量分析建议

    当 promptEval 卡片未配置时，返回默认 LLMProvider 实例。
    """
    cfg = get_llm_config_card("promptEval")
    if cfg and cfg.get("model") and cfg.get("base_url"):
        return LLMProvider(
            base_url=cfg["base_url"],
            api_key=cfg.get("api_key", ""),
            model=cfg["model"],
            use_ollama_fallback=False,
        )
    return LLMProvider()
