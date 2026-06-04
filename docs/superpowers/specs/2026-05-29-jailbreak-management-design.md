# 越狱检测 + 管理端对话分类改造设计

## 1. 概要

- 用户评分覆盖 LLM 语义评分
- LLM 语义评分时增加模型回答越狱检测
- 管理端对话列表按分类（用户/Prompt Test）分组、越狱过滤、操作工作流
- 对话排序和删除过滤整合

## 2. 数据库改动

### conversations 表 — 新增字段

```sql
ALTER TABLE conversations ADD COLUMN jailbreak_status TEXT DEFAULT NULL;   -- 'pending'|'downloaded'|'false_alarm'|'handled'
ALTER TABLE conversations ADD COLUMN jailbreak_reason TEXT DEFAULT NULL;   -- 越狱原因简述
```

### usage_logs 表 — 新增字段

```sql
ALTER TABLE usage_logs ADD COLUMN answer_jailbreak INTEGER DEFAULT 0;     -- 模型回答越狱标记
```

## 3. 后端改动

### 3.1 模型回答越狱检测 — agent.py `_eval_answer()`

**改造前**：
```
语义评分 (Rating 1-5) → 写入 semantic_rating
```

**改造后**：
```
语义评分 (Rating 1-5) → 写入 semantic_rating
↓
回答安全检测 (是否越狱) → 写入 answer_jailbreak + conversations.jailbreak_status
```

实现方式：在现有 `_eval_answer()` 末尾追加一个 LLM 检测步骤，用精简 prompt 分析模型回答是否含违规/不安全内容。

若检测到越狱：
- `usage_logs.answer_jailbreak = 1`
- `conversations.jailbreak_status = 'pending'`
- `conversations.jailbreak_reason = '...'`

若未检测到越狱但已有 `off_topic`（用户越狱）：
- 也标记 `conversations.jailbreak_status = 'pending'`
- `conversations.jailbreak_reason = '用户诱导越狱'`

### 3.2 API 新增 — main.py

| 方法 | 路径 | 说明 |
|:----|:----|:-----|
| `PUT` | `/api/conversations/{id}/jailbreak-status` | Body: `{"status":"downloaded"}` |
| `GET` | `/api/conversations/{id}/jailbreak-report` | 生成 Word 格式 `.doc` 文件下载 |

### 3.3 API 改造 — main.py

`GET /api/conversations` 新增参数：
- `jailbreak: str = "all"` — `all`(全部) / `pending`(只看越狱) / `clean`(只看正常)

### 3.4 memory.py 改造

- `get_conversations()` 新增 `jailbreak` 参数
- `get_conversation_detail()` 返回 `jailbreak_status` 和 `jailbreak_reason`
- `update_rating()` 改造为写入 `user_rating` 时清空 `semantic_rating`（或标记覆盖）
- 新增 `update_jailbreak_status(conv_id, status)`

## 4. 管理端前端改动 — admin.html

### 4.1 侧边栏对话列表

**过滤栏改造**：
```
┌─────────────────────────────────────────┐
│ 🔍 搜索框                                │
│                                         │
│ 分类: [全部] [用户▼] [Prompt Test▼]      │ ← 默认"用户"
│ 越狱: [全部] [正常] [越狱]               │ ← 越狱过滤
│ 已删除: [隐藏] [显示]                    │ ← 整合在分类组内
│ 共 514 个对话 · 越狱 3                  │
├─────────────────────────────────────────┤
│ 今天 [▾]                                │
│   🔴 [越狱] 对话标题  [📄下载] [❌] [✅]│
│   👤 普通对话标题                        │
│ 昨天 [▾]                                │
│   ...                                   │
```

**排序**：
- 分组倒序：今天 → 昨天 → 本周 → 本月 → 更早
- 组内按 `updated_at DESC`
- 越狱对话在组内置顶（排在普通对话前）

**对话条目改造**：
- 越狱对话：红色 `[越狱]` 标签 + 直接显示操作按钮
- 普通用户对话：蓝色 `[用户]` 标签
- Prompt Test 对话：橙色 `[Prompt Test]` 标签
- 已删除对话：灰色删除线

### 4.2 对话详情页

**越狱对话详情顶部**：
```
🔴 越狱检测结果
原因：[越狱原因]
操作: [📄 下载报告] [❌ 误报] [✅ 已处理]
状态栏：🔴待处理 → 🟡已下载 → 🟢已处理/已误报
```

**普通对话**：保持现有样式

### 4.3 按钮行为

| 按钮 | 效果 | 颜色变化 |
|:----|:----|:--------|
| 下载报告 | 生成 `.doc` 文件自动下载 | 🔴红 → 🟡黄 |
| 误报 | `jailbreak_status='false_alarm'` | 🔴红 → 🟢绿 |
| 已处理 | `jailbreak_status='handled'` | 🔴红 → 🟢绿 |

按钮状态持久化到 DB，刷新不丢失。

### 4.4 Word 报告格式

- 标题：对话标题
- 元信息：对话ID、创建时间、更新时间、分类、状态
- 越狱信息：越狱状态、越狱原因
- 轮次记录：每轮的用户消息 + 模型回答 + 评分

## 5. 影响面评估

| 改动点 | 影响范围 | 注意事项 |
|:------|:--------|:--------|
| `conversations` 表加列 | `memory.py: ALTER TABLE` | 自动迁移，旧兼容 |
| `usage_logs` 表加列 | `memory.py: ALTER TABLE` | 同上 |
| `_eval_answer()` 改造 | `agent.py` 末尾追加 | 不改变现有评分逻辑 |
| `get_conversations()` | `memory.py` + `main.py` | 保持默认行为兼容 |
| `get_conversation_detail()` | `memory.py` | 多返回 2 个字段 |
| admin.html 对话列表 | 整个 `renderConversations` | 重写，保留现有布局风格 |
| admin.html 详情页 | `selectConversation` | 追加越狱状态栏 |
| 下载 Word 报告 | 纯前端生成 `.doc` | 参考现有 `exportWordReport` |

## 6. 未包含（YAGNI）

- 不增加 IDE Test / Other Test 分类标签和过滤
- 不改动前台 index.html（只改管理端）
- 不改动看板图表（dashboard 统计）
