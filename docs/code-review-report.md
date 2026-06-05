# 代码审查报告

**日期**：2026-06-05  
**审查范围**：`packages/agent/src/` 核心源码  
**审查维度**：安全 / 性能 / 正确性 / 可维护性 / 架构

---

## [CRITICAL] 安全漏洞

### SEC-1. 默认管理 Token `change-me-in-production`

**文件**：[main.py:98](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L98)

```python
_ADMIN_TOKEN = os.environ.get("ADMIN_TOKEN", "change-me-in-production")
```

如果环境变量 `ADMIN_TOKEN` 未设置，默认 Token 是 `change-me-in-production`，攻击者可以直接访问所有受保护的管理 API（对话详情、统计、硬删除对话等）。

**建议**：启动时检查默认值，强制用户在首次启动时修改，或生成随机 Token 打印到日志。

---

### SEC-2. 关键管理 API 暴露为公开路由

**文件**：[main.py:147-169](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L147-L169)

`PUBLIC_ROUTES` 中包含了以下敏感端点：

| 路由 | 风险 |
|:-----|:-----|
| `/api/llm/configs/save` | 保存 LLM API Key（尽管加密存储，但无需认证即可调用） |
| `/api/llm/configs/test` | 测试 LLM 连接，可被用于 SSRF 探测 |
| `/api/llm/refresh-models` | 触发外部 HTTP 请求（SSRF 风险） |
| `/api/documents/` 前缀 | 文件上传、文档入库、向量重建等所有操作均公开 |

**建议**：将 `/api/llm/configs/*` 和 `/api/documents/*` 移入 `_ADMIN_ROUTES`，由 Token 认证保护。

---

### SEC-3. CSRF 无防护

整个应用没有 CSRF Token 机制。所有状态变更接口（POST/PUT/DELETE）都只依赖 `X-Admin-Token` 请求头，但：
- 浏览器跨站请求无法自定义 `X-Admin-Token` 头（受 CORS 限制）
- 但如果用户通过管理后台页面发起的请求，无需 CSRF Token

**建议**：对管理后台 HTML 页面添加 CSRF Token（Double Submit Cookie 模式），所有 POST 请求验证 Token。

---

### SEC-4. SSRF 防护有盲区

**文件**：[main.py:170-196](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L170-L196)

`validate_llm_url()` 域名白名单中包含 `localhost` 和 `127.0.0.1`。如果某 LLM API Key 配置了指向内部服务的 URL，可能被利用。

**建议**：分别维护"允许的外部域名"和"禁止的内部域名"列表。内部服务（如 Ollama）应走独立的配置路径而非通用 LLM 配置。

---

### SEC-5. 文件 XSS — HTML 报告直接输出未转义用户输入

**文件**：[main.py](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py) 多处 HTML 报告生成（检索质量报告、增益对比报告、Prompt 评测）

生成的 HTML 报表中直接嵌入了 query、answer 等用户/LLM内容，如果其中包含恶意 `<script>` 标签，打开 HTML 报告可能导致 XSS。

**建议**：所有 HTML 报告生成时对用户输入内容使用 `html.escape()`。

---

## [MAJOR] 性能问题

### PERF-1. `conversations.db` 无 WAL 模式 + 无索引

**文件**：[memory.py](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/memory.py)

SQLite 使用默认 journal 模式（DELETE），并发读写性能差。`conversations` 表在 `id` 列无显式索引，`llm_configs` 表在 `module_id` 列无索引。

**建议**：
```python
# 初始化时启用
conn.execute("PRAGMA journal_mode=WAL")
conn.execute("PRAGMA synchronous=NORMAL")
# 创建索引
conn.execute("CREATE INDEX IF NOT EXISTS idx_conversations_id ON conversations(id)")
conn.execute("CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, created_at)")
```

---

### PERF-2. LLM Provider 重试逻辑可能导致请求堆积

**文件**：[llm_provider.py:395-400](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/llm_provider.py#L395-L400)

同步 `requests` 重试 + 指数退避在主线程中阻塞，CPU 时间浪费在等待。流式路径使用了 `httpx` 但非流式路径使用 `requests`，HTTP 客户端不一致。

**建议**：统一使用 `httpx`（支持超时/重试/异步），移除 `requests` 依赖。

---

### PERF-3. SSE 事件总线无背压机制

**文件**：[main.py:213-237](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L213-L237)

`EventBus.publish()` 向所有订阅者发送事件时使用 `asyncio.create_task()` 包装 `q.put()`，如果某个订阅者消费慢，内存中队列会无限增长。

**建议**：队列设置最大长度（如 `maxsize=100`），满时跳过或丢弃旧事件。

---

## [MAJOR] 正确性问题

### CORR-1. 认证中间件未正确处理 WebSocket/SSE 升级

**文件**：[main.py:245-255](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L245-L255)

认证中间件对 `/api/admin/stream` 路由加了公开豁免（`PUBLIC_ROUTES`），但如果未来有新的 SSE 端点，可能忘记添加导致连接失败。

**建议**：实现统一的路由认证装饰器，而非手动维护 `PUBLIC_ROUTES` 列表。

---

### CORR-2. 文件上传无文件类型白名单

**文件**：[main.py:1798-1837](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L1798-L1837)

`/api/documents/scan` 接收 `UploadFile` 列表，但只检查了文件大小（50MB上限），没有限制文件类型（扩展名/MIME 类型）。任意文件（.exe、.zip、.html）都可上传暂存。

**建议**：添加文件扩展名白名单（`.pdf`, `.docx`, `.doc`, `.xlsx`, `.xls`, `.txt`, `.md`），在后端校验 `Content-Type`。

---

### CORR-3. eval_30_v3.py 和 eval_e2e.py 直接 `import agent` 依赖运行时路径

**文件**：[eval_30_v3.py:20](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/eval_30_v3.py#L20), [eval_e2e.py](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/eval_e2e.py)

```python
from agent import CyberAgent
```

这些脚本运行时依赖 `PYTHONPATH` 或从 `src/` 目录执行。如果从项目根目录或其他路径运行，会报 `ModuleNotFoundError`。

**建议**：使用 `sys.path` 自适应，或将这些脚本改为 CLI 入口（`if __name__ == "__main__"` 中自动处理路径）。

---

## [MINOR] 可维护性

### MAINT-1. 大量全局变量和魔法数字

| 位置 | 问题 |
|:-----|:------|
| [main.py:213](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L213) | `_ACTIVE_CONVERSATIONS: dict = {}` 全局字典，无类型边界 |
| [main.py:1750](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L1750) | `_CLEAN_COLORS = ["#FF6B6B", "#4ECDC4", ...]` 魔法颜色值 |
| [main.py:1736](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/main.py#L1736) | `_STAGING_SLOTS = 3`, `_STAGING_BATCH = 5` 硬编码 |
| [llm_provider.py:66](file:///d:/学习资料/AI%20COURSE/项目/网络安全-移动运营商智能Agent/packages/agent/src/llm_provider.py#L66) | `CircuitBreaker` 的 `failure_threshold=3, open_timeout=60.0` 硬编码，不可配置 |

---

### MAINT-2. `main.py` 文件过大（3650+ 行）

`main.py` 包含：FastAPI 路由 / 认证 / SSRF 防护 / 文件处理 / 文档入库 / 向量索引 / LLM 配置 / 评测路由 / HTML 报告生成 / 知识库管理等，单一文件承载过多职责。

**建议**：按职责拆分为：
- `routes_chat.py` — 聊天 API
- `routes_admin.py` — 管理 API
- `routes_documents.py` — 文档入库
- `routes_eval.py` — 评测路由
- `auth.py` — 认证中间件
- `report_builder.py` — HTML/Word 报告生成

---

### MAINT-3. eval_30_v3.py 和 eval_e2e.py 大量代码重复

两个评估脚本有高度重叠的 HTML 报告生成、评分调用、结果展示逻辑：

| 重复代码 | 行数估计 |
|:---------|:--------:|
| HTML 报表渲染 | ~200 行 |
| 评分调用管道 | ~80 行 |
| 结果汇总逻辑 | ~60 行 |

**建议**：抽取公共 `_eval_common.py` 模块，共享报告生成 + 评分管道。

---

## [MINOR] 架构与升级建议

### ARC-1. 依赖管理

当前项目没有 `requirements.txt` 或 `pyproject.toml`。依赖分散在代码中：

```python
# llm_provider.py:1
import os, json, logging, time, random, asyncio
import requests, httpx  # HTTP 客户端双持
```

`memory.py` 依赖 `cryptography` 包，但 `import` 失败时静默降级（`_CRYPTO_AVAILABLE = False`），可能导致用户以为加密生效实际未启用。

**建议**：
- 创建 `pyproject.toml` 锁定所有依赖版本
- `cryptography` 不可用时打印警告而非静默降级

---

### ARC-2. 没有单元测试

整个 `packages/agent/src/` 下没有任何 `test_*.py` 文件。核心逻辑（提示注入检测、评分函数、熔断器）没有自动测试覆盖。

**建议**：优先为以下模块添加测试：
- `_eval_generation.py` 的评分函数
- `_eval_context.py` 的评分函数
- `llm_provider.py` 的 `CircuitBreaker`
- `agent.py` 的 `_detect_prompt_injection`

---

### ARC-3. API 版本化

所有 API 路由都在 `/api/` 下但没有版本前缀（如 `/api/v1/chat`）。前端硬编码路由，未来 API 变更会导致前后端耦合。

**建议**：添加 `/api/v1/` 前缀，保留 `/api/` 作为向后兼容的别名。

---

## 评分总结

| 维度 | 评分 | 关键问题 |
|:-----|:----:|:---------|
| **安全性** | ⚠️ 6.5/10 | 默认管理Token、关键API公开、无CSRF、SSRF白名单含localhost |
| **性能** | ✅ 7.5/10 | SQLite 无WAL、无索引、HTTP客户端不统一 |
| **正确性** | ✅ 7.0/10 | 无文件类型校验、脚本路径依赖、认证路由手动维护 |
| **可维护性** | ⚠️ 5.5/10 | main.py 3650+行、eval脚本大量重复、无单元测试 |
| **架构** | ⚠️ 6.0/10 | 无依赖管理、无测试、API无版本 |

### 首要修复项（建议优先级）

1. **[CRITICAL]** 修改默认 `ADMIN_TOKEN`，启动时生成随机值
2. **[CRITICAL]** 将 `/api/documents/*` 和 `/api/llm/configs/*` 移入受保护路由
3. **[MAJOR]** SQLite 启用 WAL 模式 + 创建索引
4. **[MAJOR]** HTML 报告添加 `html.escape()` 防 XSS
5. **[MAJOR]** 文件上传添加扩展名白名单
6. **[MINOR]** 拆分 `main.py` 为多个模块
7. **[MINOR]** 抽取 eval 公共模块消除代码重复