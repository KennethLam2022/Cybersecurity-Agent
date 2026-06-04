# 网络安全移动运营商智能 Agent

移动运营商网络安全管理领域的智能问答系统，基于 RAG（检索增强生成）架构，覆盖等保测评、数据安全、CII 关基保护、APP 安全、公司安全管理等专业领域。

---

## 项目结构

```
deploy/
├── packages/
│   ├── agent/                    # Agent 服务（Python FastAPI）
│   │   ├── src/
│   │   │   ├── main.py           # FastAPI 入口（聊天 + 管理后台）
│   │   │   ├── agent.py          # RAG Pipeline 核心
│   │   │   ├── memory.py         # SQLite 对话记忆 + 统计看板
│   │   │   ├── llm_provider.py   # LLM 调用层（限流/熔断/重试/兜底）
│   │   │   ├── templates/        # HTML 前端页面
│   │   │   │   ├── index.html    # 聊天界面
│   │   │   │   └── admin.html    # 管理后台（ECharts 看板）
│   │   │   └── static/style.css  # 聊天界面样式
│   │   └── agent_data/           # 运行时数据（LLM 配置 + 对话数据库）
│   ├── preprocessor/             # 文档预处理
│   │   └── src/
│   │       ├── retriever.py      # 检索管道（Chroma + FAISS + BM25 + Reranker）
│   │       ├── batch_process.py  # 批量文档处理
│   │       ├── index_batch.py    # 批量索引构建
│   │       ├── odl_parser.py     # 文档解析
│   │       ├── llm_cleaner.py    # LLM 清洗
│   │       └── build_parent_index.py
│   ├── agent-core/               # TypeScript 共享类型
│   ├── shared/                   # TypeScript 共享工具
│   └── vector_store/             # 向量索引（FAISS + Chroma）
├── .gitignore
├── package.json
├── pnpm-workspace.yaml
├── tsconfig.base.json
└── README.md
```

---

## 启动

```powershell
cd packages/agent
pip install fastapi uvicorn jinja2 httpx requests chromadb langchain-ollama langchain-community faiss-cpu rank-bm25 jieba
python src/main.py
# 聊天：http://127.0.0.1:8000/
# 后台：http://127.0.0.1:8000/admin
```

---

## 核心能力

| 模块 | 说明 |
|:----|:------|
| **RAG 检索** | Chroma + FAISS 双向量库 + BM25 混合检索 + Reranker 重排序 |
| **对话管理** | SQLite 持久化，滑动窗口压缩，多轮上下文保持 |
| **LLM 接入** | OpenAI 兼容接口，支持硅基流动/阿里云百炼/DeepSeek/OpenRouter 等 |
| **可靠性** | 令牌桶限流、熔断器、自动重试、Ollama 本地兜底 |
| **管理后台** | ECharts 看板，对话管理，评分统计，图表钻取分析 |
| **语义评分** | 自动分析用户态度，1-5 星语义评分 + 用户手动评分 |
| **模型管理** | 动态刷新模型列表，多提供商切换，配置持久化 |
| **软删除** | 前台删除后台仍可查看，支持物理删除 |

---

## 技术栈

- **后端**: Python 3.12, FastAPI, SQLite, Uvicorn
- **检索**: ChromaDB, FAISS, BM25, BGE Reranker
- **前端**: HTML/CSS, ECharts, ECharts WordCloud
- **AI**: OpenAI 兼容 API, Ollama 本地兜底
