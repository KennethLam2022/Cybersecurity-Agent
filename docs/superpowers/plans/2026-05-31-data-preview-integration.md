# data_preview 集成到管理后台 实施计划

> **Goal:** 将 data_preview.html（文档入库管理页面）作为第5个 Tab 集成到 admin.html 管理后

**Architecture:** data_preview.html 保持独立完整页面（已批准的最终稿），通过 admin.html 新增的"文档入库"Tab 加载。使用 iframe 嵌入方式，保持两者的主题独立（admin 为浅色，data_preview 为深色）。

**Tech Stack:** HTML/CSS/JS · Jinja2 · FastAPI · StaticFiles

**文件清单:**
- `packages/agent/src/main.py` — 新增路由 `/admin/documents` 和 `/api/documents/*`
- `packages/agent/src/templates/admin.html` — 新增第5个 Tab + iframe 容器
- `packages/agent/src/static/data_preview.html` — 已批准，保持不变

---

### 任务1: 在 admin.html 中添加"文档入库"Tab

**文件:** `packages/agent/src/templates/admin.html`

- [ ] **步骤1: Tab Bar 增加第5个 tab 按钮**

在现有4个 tab 按钮后追加：

```html
<div class="tab-item" data-tab="documents" onclick="switchTab('documents')">📂 文档入库</div>
```

- [ ] **步骤2: Tab Content 区增加文档入库容器**

在 `</div> <!-- tab-content -->` 之前（最后一个 tab 之后），追加：

```html
<div class="tab-content" id="tabDocuments" style="padding:0;background:transparent;overflow:hidden;height:calc(100vh - 180px)">
  <iframe id="docFrame" src="/static/data_preview.html"
    style="width:100%;height:100%;border:none;border-radius:0;display:block;background:#0f0f13"
    allow="clipboard-read;clipboard-write"
    loading="lazy">
  </iframe>
</div>
```

- [ ] **步骤3: 在 switchTab() 函数中，匹配 'documents' 时触发 iframe resize**  

在 switchTab 函数末尾（赋值 `chartInstances.initialized = true` 之前），追加：

```javascript
if (name === 'documents') {
  const frame = document.getElementById('docFrame');
  if (frame) {
    setTimeout(() => {
      frame.style.height = (document.querySelector('.main').offsetHeight - 10) + 'px';
    }, 100);
  }
}
```

修改 HTML 中的对应 tab 数据属性：将 `tabBar` 中所有 tab-item 的 `onclick` 中的 `switchTab` 调用调整为支持 `documents` 参数（现有 `switchTab` 函数已支持）。

---

### 任务2: 为 data_preview.html 添加独立后端路由

**文件:** `packages/agent/src/main.py`

- [ ] **步骤1: 添加 `/admin/documents` 路由**（确保 data_preview 可直接访问）

```python
@app.get("/admin/documents", response_class=HTMLResponse)
async def admin_documents():
    """文档入库管理页面"""
    html_path = _STATIC / "data_preview.html"
    if html_path.exists():
        return HTMLResponse(html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>页面未找到</h1>")
```

- [ ] **步骤2: 添加 `/api/documents/scan` Mock API 端点**（为前端 mock 提供后端占位）

```python
@app.post("/api/documents/scan")
async def documents_scan(data: dict = Body(...)):
    """扫描文件元数据 + 重名校验（Mock，返回前端 mock 数据）"""
    return JSONResponse({"status": "ok", "files": []})

@app.post("/api/documents/start-processing")
async def documents_start(data: dict = Body(...)):
    """开始处理文件（Mock）"""
    return JSONResponse({"status": "ok", "task_id": ""})

@app.get("/api/documents/status/{task_id}")
async def documents_status(task_id: str):
    """获取处理状态（Mock）"""
    return JSONResponse({"status": "completed", "progress": 100})
```

---

### 任务3: 验证集成效果

- [ ] **步骤1: 确认静态文件可访问**

```bash
curl http://localhost:8000/static/data_preview.html
```
预期: 返回完整 HTML（原 data_preview.html 内容）

- [ ] **步骤2: 确认 admin 页面正常加载**

```bash
curl http://localhost:8000/admin
```
预期: 返回 admin.html，包含5个 tab，第5个为"📂 文档入库"

- [ ] **步骤3: 确认 Tab 切换正确**

在浏览器中打开 `/admin` → 点击"文档入库"tab → iframe 加载 data_preview.html → 拖拽/点击拖拽区 → 模拟扫描流程正常
