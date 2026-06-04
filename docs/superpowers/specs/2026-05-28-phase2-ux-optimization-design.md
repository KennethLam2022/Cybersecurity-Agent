# Phase2: UX 优化 · 6 项需求设计文档

日期：2026-05-28
状态：待实现

---

## 概述

本次 Phase2 聚焦用户体验和管理后台能力增强，涵盖 6 个独立但协作的需求：

1. Ollama 兜底提醒
2. 模型列表只显示 chat 模型
3. 后台对话按日期分类
4. 前台删除保留后台数据（软删除）
5. 后台消息显示评分
6. 图表点击钻取

---

## 需求1: Ollama 兜底提醒

### 现状
`_infer_rating()` 中先调 OpenRouter 免费模型，失败后静默 fallback 到 Ollama `qwen2.5:7b`。用户完全不知道 LLM 提供商可能有问题。

### 改动

#### agent.py
- `_infer_rating()` 增加返回值 `{"used_ollama": True/False, "message": "..."}`
- 主流程中拿到返回值后，通过 yield 抛出 warning 事件

#### main.py (SSE 流)
在 `event_generator()` 中，处理 `_infer_rating()` 的 warning 结果，插入：
```json
{"type": "warning", "message": "⚠️ 语义评分已切换至本地 Ollama 兜底，当前 LLM 提供商可能存在问题，建议联系厂商确认或刷新模型列表。"}
```

#### index.html
1. **新增 warning 事件监听**：在 EventSource 的 message 处理中捕获 `type: "warning"`
2. **顶栏黄色横幅**：在输入框上方插入 `.warning-banner`（可关闭），保留至下次对话或手动关闭
3. **气泡尾部标注**：在 appendRating() 前判断是否有 warning，追加一行灰色提示

CSS 新增：
```css
.warning-banner{background:#fff3cd;border:1px solid #ffc107;padding:8px 14px;border-radius:8px;font-size:12px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
.warning-banner .close{cursor:pointer;font-size:16px;opacity:.6}
.warning-annotation{font-size:11px;color:#999;margin-top:4px;padding-top:4px;border-top:1px dashed #eee}
```

---

## 需求2: 模型列表只显示 chat 模型

### 现状
- 硅基流动 (`/api/llm/refresh-models`)：已有 `exclude_keywords` 过滤 ✅
- 阿里云百炼：无过滤，返回全部模型（含 embedding、image 等）
- 前端预设模型的 `llm_config.json` 中可能混入了非 chat 模型

### 改动

#### main.py — `/api/llm/refresh-models` 阿里云百炼分支
增加过滤逻辑（与硅基流动复用同一组关键词）：
```python
exclude_keywords = ["embedding", "reranker", "image", "video", "audio", "ocr", "asr", "tts", "Kolors", "PaddleOCR", "Captioner", "CosyVoice", "SenseVoice"]
chat_models = [m for m in all_models if not any(k in m.lower() for k in exclude_keywords)]
```

#### llm_config.json
手动清理预设模型列表中的非 chat 模型（如 `text-embedding-v4`、`bge` 等），保留 `models` 子项中只含 chat 模型。

---

## 需求3: 后台对话按日期分类

### 现状
admin.html 侧边栏 475 个对话平铺列表，查找困难。

### 改动（纯前端，不改后端）

#### admin.html — `renderConversations()`
重写为分组渲染：

```
今天 (3)
├── 对话A  10:30
├── 对话B  09:15
└── 对话C  08:00

昨天 (12)
├── 对话D  22:00
...

本周 (25)
...

本月 (48)
...

更早 (387)
```

实现逻辑：
1. 遍历 `conversations`，对每个对话计算日期段：
   - `today()` → "今天"
   - `yesterday()` → "昨天"
   - `thisWeek()` → "本周"
   - `thisMonth()` → "本月"
   - else → "更早"
2. 直接用 JS Date 计算，不依赖后端
3. 每个分组标题可折叠（`▸` / `▾` 箭头）
4. 搜索时自动展开所有组

CSS 新增：
```css
.group-header{font-size:11px;font-weight:600;color:var(--t2);padding:6px 16px 4px;cursor:pointer;user-select:none;display:flex;justify-content:space-between}
.group-header:hover{color:var(--t)}
.group-header .arrow{transition:transform .15s}
.group-header .arrow.collapsed{transform:rotate(-90deg)}
.group-items{overflow:hidden;transition:max-height .2s}
.group-items.collapsed{max-height:0}
```

#### 搜索逻辑增强
`filterConversations()` 中当有搜索词时，调用 `renderConversations()` 时传入 filter 参数，展开所有组并高亮匹配项。

---

## 需求4: 前台删除保留后台数据（软删除）

### 现状
`delete_conversation()` 执行 4 条 `DELETE FROM` 语句，彻底删除数据。

### 改动

#### memory.py

**数据库迁移**：`_init_db()` 中增加：
```python
conn.execute("ALTER TABLE conversations ADD COLUMN deleted INTEGER DEFAULT 0")
```
用 try/except 包裹（兼容已有表）。

**修改 `delete_conversation()`**：
```python
def delete_conversation(self, conversation_id: str):
    with sqlite3.connect(self._db_path) as conn:
        conn.execute("UPDATE conversations SET deleted = 1 WHERE id = ?", (conversation_id,))
```

**新增 `hard_delete_conversation()`**（给管理员用）：
```python
def hard_delete_conversation(self, conversation_id: str):
    """物理删除（管理端清理用）"""
    with sqlite3.connect(self._db_path) as conn:
        conn.execute("DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM session_memory WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM usage_logs WHERE conversation_id = ?", (conversation_id,))
        conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))
```

**修改 `get_conversations()`**：可选的过滤参数
```python
def get_conversations(self, include_deleted: bool = False) -> list[dict]:
    sql = "SELECT id, title, created_at, updated_at FROM conversations"
    if not include_deleted:
        sql += " WHERE deleted IS NULL OR deleted = 0"
    sql += " ORDER BY updated_at DESC, id DESC"
```

#### main.py

`GET /api/conversations` 增加查询参数：
```python
@app.get("/api/conversations")
async def list_conversations(include_deleted: bool = False):
    convs = agent.memory.get_conversations(include_deleted=include_deleted)
    return JSONResponse(conversations)
```

`DELETE /api/conversations/{id}` 改为软删除，返回 `{"ok": true, "soft_delete": true}`。

新增 `DELETE /api/conversations/{id}/hard` 端点（仅后台管理用）。

#### admin.html

1. 对话列表加载时 `include_deleted=true`，但已删除对话标注 `(已删除)` 灰色样式
2. 增加过滤开关（顶部），默认"显示全部"，可选"仅未删除"
3. 对话详情页增加"物理删除"按钮（带二次确认）

已删除对话在侧边栏渲染为：
```html
<div class="conv-item deleted">...</div>
```
CSS：
```css
.conv-item.deleted{opacity:.45;background:#fafafa}
.conv-item.deleted .deleted-badge{font-size:9px;color:var(--r);padding:1px 4px;border:1px solid var(--r);border-radius:4px;margin-left:6px}
```

---

## 需求5: 后台消息显示评分

### 现状
`get_conversation_detail()` 返回的消息中无评分数据，admin.html 消息卡片无评分显示。

### 改动

#### memory.py — `get_conversation_detail()`
SQL 查询中 LEFT JOIN usage_logs 获取评分：
```python
msg_rows = conn.execute("""
    SELECT m.role, m.content, m.sources, m.created_at,
           u.user_rating, u.semantic_rating
    FROM messages m
    LEFT JOIN usage_logs u ON m.id = u.message_id
    WHERE m.conversation_id = ?
    ORDER BY m.id
""", (conversation_id,)).fetchall()
```
消息返回增加字段：
```python
msg["user_rating"] = row[4]
msg["semantic_rating"] = row[5]
```

#### admin.html — `selectConversation()`
在消息卡片的 `msg-header` 中增加评分渲染：
```javascript
const ur = msg.user_rating;
const sr = msg.semantic_rating;
let ratingHtml = '';
if (ur || sr) {
    ratingHtml = `<span style="font-size:11px;color:var(--t2)">
        ${ur ? '⭐'.repeat(ur) : ''}
        ${sr ? `<span style="font-size:10px;color:#999;margin-left:4px">语义:${'⭐'.repeat(sr)}</span>` : ''}
    </span>`;
}
```
渲染到 header 右侧：
```html
<div class="msg-header ${roleCls}">
    <span>${roleLabel} #${n}</span>
    <span>${userRatingHtml}</span>
</div>
```

---

## 需求6: 图表点击钻取

### 现状
10 个 ECharts 图表无交互，只是静态展示。

### 改动

#### memory.py — 新增 `drill_down()` 方法
```python
def drill_down(self, drill_type: str, key: str, limit: int = 50) -> list[dict]:
    """
    drill_type:
      - 'rating': key = "4" (分数)
      - 'keyword': key = "等保测评" (查询关键词)
      - 'error_date': key = "2026-05-28" (日期)
      - 'scatter': key = "message_id"
    """
    with sqlite3.connect(self._db_path) as conn:
        if drill_type == 'rating':
            rows = conn.execute("""
                SELECT u.conversation_id, u.query, u.user_rating, u.semantic_rating,
                       COALESCE(u.user_rating, u.semantic_rating), u.created_at
                FROM usage_logs u
                WHERE COALESCE(u.user_rating, u.semantic_rating) = ?
                ORDER BY u.created_at DESC LIMIT ?
            """, (int(key), limit)).fetchall()
        elif drill_type == 'keyword':
            rows = conn.execute("""
                SELECT u.conversation_id, u.query, u.user_rating, u.semantic_rating,
                       COALESCE(u.user_rating, u.semantic_rating), u.created_at
                FROM usage_logs u
                WHERE u.query LIKE ?
                ORDER BY u.created_at DESC LIMIT ?
            """, (f'%{key}%', limit)).fetchall()
        elif drill_type == 'error_date':
            rows = conn.execute("""
                SELECT u.conversation_id, u.query, u.llm_success, u.was_circuit_break,
                       u.circuit_provider, u.created_at
                FROM usage_logs u
                WHERE DATE(u.created_at) = ? AND (u.llm_success = 0 OR u.was_circuit_break = 1)
                ORDER BY u.created_at DESC LIMIT ?
            """, (key, limit)).fetchall()
        elif drill_type == 'scatter':
            rows = conn.execute("""
                SELECT u.conversation_id, u.query, COALESCE(u.user_rating, u.semantic_rating),
                       u.created_at
                FROM usage_logs u
                WHERE u.id = ?
            """, (int(key),)).fetchall()
        else:
            return []

    return [
        {"conversation_id": r[0], "query": r[1], "detail": dict(zip(fields, r[2:]))}
        for r in rows
    ]
```

#### main.py — 新增 `GET /api/stats/drill-down`

```python
@app.get("/api/stats/drill-down")
async def drill_down(type: str, key: str, limit: int = 50):
    results = agent.memory.drill_down(type, key, limit)
    return results
```

#### admin.html — 4 个图表注册 click 事件

**评分分布饼图** `chartRatingDist`：
```javascript
chartInstances['chartRatingDist'].on('click', function(params) {
    const star = params.name.charAt(0);  // "4星" → "4"
    openDrillDownModal('rating', star, `${star}星评分问答`);
});
```

**查询热点词云** `chartHotspot`：
```javascript
chartInstances['chartHotspot'].on('click', function(params) {
    openDrillDownModal('keyword', params.name, `包含"${params.name}"的问答`);
});
```

**LLM 调用健康** `chartLLM` — 点击失败柱：
```javascript
chartInstances['chartLLM'].on('click', function(params) {
    if (params.seriesName === '失败') {
        openDrillDownModal('error_date', params.name, `${params.name} 错误详情`);
    }
});
```

**评分-置信度散点** `chartConfScatter`：
```javascript
chartInstances['chartConfScatter'].on('click', function(params) {
    const idx = params.dataIndex;
    const msgId = scatterData[idx].message_id;
    openDrillDownModal('scatter', String(msgId), '问答详情');
});
```

**模态框组件** `openDrillDownModal()`：
```javascript
function openDrillDownModal(type, key, title) {
    // 1. 请求 /api/stats/drill-down?type=xxx&key=xxx
    // 2. 渲染模态框：表格形式
    //    | 对话 | 问题 | 评分 | 时间 |
    // 3. 每条对话可点击 → 调用 selectConversation(id)
    // 4. 关闭按钮
}
```

CSS 新增：
```css
.modal-overlay{position:fixed;top:0;left:0;width:100%;height:100%;background:rgba(0,0,0,.4);z-index:1000;display:flex;align-items:center;justify-content:center}
.modal-content{background:#fff;border-radius:12px;padding:24px;max-width:800px;width:90%;max-height:80vh;overflow-y:auto;box-shadow:0 8px 30px rgba(0,0,0,.15)}
.modal-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:16px}
.modal-header h3{font-size:16px;font-weight:600}
.modal-close{font-size:22px;cursor:pointer;opacity:.5;border:none;background:none}
.modal-close:hover{opacity:1}
.modal-table{width:100%;border-collapse:collapse;font-size:13px}
.modal-table th{text-align:left;padding:8px 10px;border-bottom:2px solid var(--b);font-weight:600;font-size:11px;color:var(--t2)}
.modal-table td{padding:8px 10px;border-bottom:1px solid var(--b)}
.modal-table tr{cursor:pointer;transition:background .1s}
.modal-table tr:hover{background:#f0f0f2}
```

---

## 执行计划

### 阶段一（Day 1）

| 序号 | 需求 | 文件 | 预估 |
|------|------|------|------|
| 1 | 需求4: 软删除 | memory.py, main.py, admin.html | 30min |
| 2 | 需求2: 模型过滤 | main.py, llm_config.json, index.html | 15min |
| 3 | 需求3: 日期分组 | admin.html | 40min |
| 4 | 需求5: 评分显示 | memory.py, admin.html | 25min |

→ 测试 → git commit `Phase2-Day1: soft-delete + model filter + date grouping + rating display`

### 阶段二（Day 2）

| 序号 | 需求 | 文件 | 预估 |
|------|------|------|------|
| 5 | 需求1: Ollama提醒 | agent.py, main.py, index.html | 35min |
| 6 | 需求6: 图表钻取 | memory.py, main.py, admin.html | 60min |

→ 测试 → git commit `Phase2-Day2: ollama warning + chart drill-down`

---

## 验证方法

| 需求 | 验证方法 |
|------|---------|
| 需求1 | 断开联网，触发 Ollama 兜底，检查前端是否显示黄色横幅 + 气泡灰色提示 |
| 需求2 | 刷新模型列表，确认阿里云百炼不再出现 embedding/image 模型 |
| 需求3 | 后台侧边栏对话按照今天/昨天/本周/本月/更早分组显示，折叠功能正常 |
| 需求4 | 前台删除对话后，后台仍可查看该对话（灰色已删除标记）；API 仍可查到 |
| 需求5 | 后台对话详情中每条 assistant 消息显示用户评分 ⭐ + 语义评分标签 |
| 需求6 | 点击评分分布饼图中"4星"→弹出模态框列出4分问答；点击词云→列出含该词的问答 |
