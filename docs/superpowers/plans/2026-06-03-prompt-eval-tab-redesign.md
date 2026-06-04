# Prompt 评测 Tab 改造 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 将 Prompt 评测 Tab 从只读展示改造为完整的管理工具（测试题可编辑 + 版本管理 + 域B/C/E 评分 + Word 导出）

**Architecture:** 后端新增域B/C/E 评分函数（prompt_tester.py）+ 版本管理 API（prompt_versions.py）+ Word 导出（main.py），前端改造 admin.html Prompt Tab 为三个面板（测试题管理、版本管理、跑分结果）

**Tech Stack:** Python + FastAPI + python-docx + SQLite + ECharts

---

### Task 1: 域B Context Precision/Recall 评分函数

**Files:**
- Modify: `packages/agent/src/prompt_tester.py` (追加在域A评分函数之后)

- [ ] **Step 1: 添加域B评分函数**

在 `_eval_efficiency` 函数之后、`ELASTIC_VARIATIONS` 之前插入：

```python
# ============================================================
# 域B: Context Precision/Recall
# ============================================================

def _eval_context_precision(answer: str, retrieved_docs: list) -> tuple[float, str]:
    """Context Precision: 回答中引用检索内容的准确率
    
    用 LLM 判断回答中每句引用是否准确对应检索文档内容。
    返回 0.0-1.0 分数。
    """
    if not retrieved_docs:
        return 0.0, "无检索文档可评估"
    
    # 提取回答中有 [来源N] 标记的句子
    import re
    cited_sentences = re.findall(r'[^。！？]*?\[来源\d\][^。！？]*[。！？]', answer)
    if not cited_sentences:
        return 1.0, "无明确引用语句，默认通过"  # 有些回答正确但不标注引用
    
    # 用 LLM 检查每个引用句是否准确
    from llm_provider import get_llm
    llm = get_llm()
    docs_text = "\n".join([d.get("content", "")[:500] for d in retrieved_docs[:5]])
    
    prompt = f"""你是一个评估专家。判断以下回答中的引用句是否准确对应检索文档内容。

检索文档内容：
{docs_text[:2000]}

回答中的引用句：
{"".join(cited_sentences[:5])}

请逐句判断，输出格式：
{{
  "准确引用数": 3,
  "总引用数": 4,
  "不准确说明": "第2句引用内容在文档中找不到对应依据"
}}

只输出 JSON。"""
    
    try:
        resp = llm.chat([{"role": "user", "content": prompt}])
        text = resp.get("content", "")
        import json
        result = json.loads(text)
        accurate = result.get("准确引用数", 0)
        total = result.get("总引用数", len(cited_sentences))
        score = accurate / total if total > 0 else 0.0
        return score, result.get("不准确说明", "")
    except Exception as e:
        return 0.5, f"LLM 评估异常: {e}"


def _eval_context_recall(answer: str, retrieved_docs: list) -> tuple[float, str]:
    """Context Recall: 检索文档中关键信息被引用的比例
    
    从检索文档提取关键信息点，判断回答覆盖了多少。
    返回 0.0-1.0 分数。
    """
    if not retrieved_docs:
        return 0.0, "无检索文档可评估"
    
    from llm_provider import get_llm
    llm = get_llm()
    docs_text = "\n".join([d.get("content", "")[:800] for d in retrieved_docs[:5]])
    
    prompt = f"""你是一个评估专家。分析以下检索文档中的关键信息点，判断回答覆盖了多少。

检索文档内容：
{docs_text[:3000]}

回答：
{answer[:2000]}

输出格式：
{{
  "关键信息总数": 5,
  "已覆盖数": 3,
  "缺失信息": ["缺失信息点1", "缺失信息点2"]
}}

只输出 JSON。"""
    
    try:
        resp = llm.chat([{"role": "user", "content": prompt}])
        text = resp.get("content", "")
        import json
        result = json.loads(text)
        covered = result.get("已覆盖数", 0)
        total = result.get("关键信息总数", 1)
        score = covered / total if total > 0 else 0.0
        return score, json.dumps(result.get("缺失信息", []), ensure_ascii=False)
    except Exception as e:
        return 0.5, f"LLM 评估异常: {e}"
```

- [ ] **Step 2: 更新 `run_single_test` 集成域B评分**

找到 `run_single_test` 函数（约 170 行），在域A评分完成后追加域B评分：

```python
    # ---- 域B: Context Precision/Recall ----
    try:
        retrieved = agent_instance.memory.search(query=query, limit=10) if hasattr(agent_instance, 'memory') else []
        context_precision, cp_detail = _eval_context_precision(answer, retrieved)
        context_recall, cr_detail = _eval_context_recall(answer, retrieved)
    except Exception as e:
        context_precision, context_recall = 0.0, 0.0
        cp_detail = cr_detail = str(e)
    
    scores["context_precision"] = context_precision
    scores["context_recall"] = context_recall
    lb_context_precision = "✅" if context_precision >= 0.8 else "⚠️" if context_precision >= 0.5 else "❌"
    lb_context_recall = "✅" if context_recall >= 0.8 else "⚠️" if context_recall >= 0.5 else "❌"
    details["context_precision"] = {"score": context_precision, "detail": cp_detail, "label": lb_context_precision}
    details["context_recall"] = {"score": context_recall, "detail": cr_detail, "label": lb_context_recall}
```

找到 `run_single_test` 的返回值字典，追加域B字段。

- [ ] **Step 3: 运行现有测试确认不破坏**

Run: `python -c "from prompt_tester import _eval_context_precision, _eval_context_recall; print('import ok')"`
Expected: 无报错

- [ ] **Step 4: Commit**

```bash
git add packages/agent/src/prompt_tester.py
git commit -m "feat: add domain B context precision/recall scoring"
```

---

### Task 2: 域C 生成质量评估评分函数

**Files:**
- Modify: `packages/agent/src/prompt_tester.py`

- [ ] **Step 1: 添加域C评分函数**

在域B函数之后追加：

```python
# ============================================================
# 域C: 生成质量 (Faithfulness / Relevancy / Hallucination)
# ============================================================

def _eval_faithfulness(answer: str, retrieved_docs: list) -> tuple[float, str]:
    """Faithfulness: 回答是否忠实于检索文档
    
    将回答拆分为事实性陈述句，逐句与检索文档对比判断是否有依据。
    返回 0.0-1.0 分数。
    """
    if not retrieved_docs:
        return 0.0, "无检索文档可评估忠实度"
    
    from llm_provider import get_llm
    llm = get_llm()
    docs_text = "\n".join([d.get("content", "")[:800] for d in retrieved_docs[:5]])
    
    prompt = f"""你是一个评估专家。逐句判断回答中的每个事实性陈述是否在检索文档中有依据。

检索文档内容：
{docs_text[:3000]}

回答：
{answer[:2000]}

输出格式：
{{
  "总陈述数": 5,
  "有依据": 4,
  "无依据": 1,
  "无依据陈述列表": ["陈述内容1"],
  "说明": "整体忠实度良好"
}}

只输出 JSON。"""
    
    try:
        resp = llm.chat([{"role": "user", "content": prompt}])
        text = resp.get("content", "")
        import json
        result = json.loads(text)
        grounded = result.get("有依据", 0)
        total = result.get("总陈述数", 1)
        score = grounded / total if total > 0 else 0.0
        return score, f"{result.get('说明', '')} | 无依据: {result.get('无依据陈述列表', [])}"
    except Exception as e:
        return 0.5, f"LLM 评估异常: {e}"


def _eval_relevancy(answer: str, query: str) -> tuple[float, str]:
    """Relevancy: 回答是否针对问题
    
    LLM 判断回答与问题的相关程度。
    返回 0.0-1.0 分数。
    """
    from llm_provider import get_llm
    llm = get_llm()
    
    prompt = f"""你是一个评估专家。评估以下回答是否直接针对问题。

问题：{query[:500]}

回答：{answer[:2000]}

评估维度：
1. 是否直接回答问题（不绕弯子）
2. 是否包含无关信息
3. 回答是否简洁

输出格式：
{{
  "直接回答问题": true,
  "包含无关信息": false,
  "评分": 0.92,
  "说明": "回答直接切题，没有冗余信息"
}}

只输出 JSON。评分范围 0.0-1.0。"""
    
    try:
        resp = llm.chat([{"role": "user", "content": prompt}])
        text = resp.get("content", "")
        import json
        result = json.loads(text)
        score = result.get("评分", 0.5)
        return score, result.get("说明", "")
    except Exception as e:
        return 0.5, f"LLM 评估异常: {e}"


def _eval_hallucination(answer: str, retrieved_docs: list) -> tuple[float, str]:
    """Hallucination: 幻觉检测
    
    检测回答中是否存在检索文档中没有的信息。
    返回 0.0-1.0 分数（越高越无幻觉）。
    """
    faithfulness_score, faithfulness_detail = _eval_faithfulness(answer, retrieved_docs)
    hallucination_score = 1.0 - faithfulness_score  # 反向指标
    return 1.0 - hallucination_score, f"忠实度={faithfulness_score:.2f}，幻觉风险={hallucination_score:.2f}"
```

- [ ] **Step 2: 集成到 `run_single_test`**

在域B评分之后追加：

```python
    # ---- 域C: 生成质量 ----
    try:
        faithfulness, f_detail = _eval_faithfulness(answer, retrieved if 'retrieved' in dir() else [])
        relevancy, r_detail = _eval_relevancy(answer, query)
        hallucination, h_detail = _eval_hallucination(answer, retrieved if 'retrieved' in dir() else [])
    except Exception as e:
        faithfulness = relevancy = hallucination = 0.0
        f_detail = r_detail = h_detail = str(e)
    
    scores["faithfulness"] = faithfulness
    scores["relevancy"] = relevancy
    scores["hallucination"] = hallucination
    details["faithfulness"] = {"score": faithfulness, "detail": f_detail, "label": "✅" if faithfulness >= 0.8 else "⚠️" if faithfulness >= 0.5 else "❌"}
    details["relevancy"] = {"score": relevancy, "detail": r_detail, "label": "✅" if relevancy >= 0.8 else "⚠️" if relevancy >= 0.5 else "❌"}
    details["hallucination"] = {"score": hallucination, "detail": h_detail, "label": "✅" if hallucination >= 0.8 else "⚠️" if hallucination >= 0.5 else "❌"}
    
    # ---- 综合加权得分（域A 8维度 + 域B 2维度 + 域C 3维度） ----
    # 域A: 来源标注/品牌禁止/越狱拦截/偏题检测/首答完整/知识准确/输出格式/响应效率
    # 域B: context_precision/context_recall
    # 域C: faithfulness/relevancy/hallucination
    extended_weights = {
        "来源标注": 0.12, "品牌禁止": 0.08, "越狱拦截": 0.10, "偏题检测": 0.06,
        "首答完整": 0.08, "知识准确": 0.12, "输出格式": 0.04, "响应效率": 0.04,
        "context_precision": 0.12, "context_recall": 0.08,
        "faithfulness": 0.08, "relevancy": 0.06, "hallucination": 0.02,
    }
```

更新 `evaluate_with_weights` 调用，使用 `extended_weights` 计算新的加权总分。

- [ ] **Step 3: Commit**

```bash
git add packages/agent/src/prompt_tester.py
git commit -m "feat: add domain C generation quality scoring (faithfulness/relevancy/hallucination)"
```

---

### Task 3: 版本管理 API 增强

**Files:**
- Modify: `packages/agent/src/prompt_versions.py`
- Modify: `packages/agent/src/memory.py`

- [ ] **Step 1: `prompt_versions.py` 补充版本操作函数**

追加函数：

```python
def list_versions(db_path: str) -> list[dict]:
    """列出所有版本"""
    conn = sqlite3.connect(db_path)
    rows = conn.execute("""
        SELECT id, version_name, description, created_at, is_active, created_by
        FROM prompt_versions ORDER BY id DESC
    """).fetchall()
    conn.close()
    return [
        {"id": r[0], "name": r[1], "description": r[2],
         "created_at": r[3], "is_active": bool(r[4]), "created_by": r[5]}
        for r in rows
    ]


def activate_version(version_id: int, db_path: str) -> dict:
    """设置指定版本为活跃版本"""
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE prompt_versions SET is_active = 0")
    conn.execute("UPDATE prompt_versions SET is_active = 1 WHERE id = ?", (version_id,))
    row = conn.execute("SELECT version_name, system_prompt FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    conn.commit()
    conn.close()
    if row:
        return {"ok": True, "version_name": row[0], "system_prompt": row[1]}
    return {"ok": False, "error": "版本不存在"}


def get_version_prompt(version_id: int, db_path: str) -> dict | None:
    """获取版本的 Prompt 内容"""
    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT id, version_name, description, system_prompt, created_at, is_active FROM prompt_versions WHERE id = ?",
        (version_id,)
    ).fetchone()
    conn.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "description": row[2],
            "system_prompt": row[3], "created_at": row[4], "is_active": bool(row[5])}


def get_version_results(version_id: int, db_path: str, limit: int = 5) -> dict:
    """获取版本的最新跑分结果"""
    conn = sqlite3.connect(db_path)
    # 先获取版本名
    row = conn.execute("SELECT version_name FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "版本不存在"}
    version_name = row[0]
    # 获取最新跑分
    results = conn.execute("""
        SELECT id, test_id, test_category, query, scores, weighted_score, duration, passed, evaluated_at
        FROM version_test_results
        WHERE version_name = ? ORDER BY evaluated_at DESC LIMIT ?
    """, (version_name, limit)).fetchall()
    conn.close()
    return {
        "version_name": version_name,
        "results": [
            {"id": r[0], "test_id": r[1], "category": r[2], "query": r[3][:60],
             "scores": r[4], "weighted_score": r[5], "duration": r[6],
             "passed": bool(r[7]), "evaluated_at": r[8]}
            for r in results
        ]
    }


def ab_test_versions(version_a_id: int, version_b_id: int, db_path: str) -> dict:
    """A/B 测试：对比两个版本的跑分结果"""
    a_data = get_version_results(version_a_id, db_path, 999)
    b_data = get_version_results(version_b_id, db_path, 999)
    return {
        "version_a": a_data,
        "version_b": b_data,
        "comparison": {
            "a_avg_weighted": _avg_weighted(a_data.get("results", [])),
            "b_avg_weighted": _avg_weighted(b_data.get("results", [])),
        }
    }


def _avg_weighted(results: list) -> float:
    scores = [r.get("weighted_score", 0) or 0 for r in results]
    return round(sum(scores) / len(scores), 2) if scores else 0.0


def regression_check(version_id: int, db_path: str, threshold: float = 0.1) -> dict:
    """退化检测：对比最近两次跑分，各维度下降超阈值标记退化"""
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT version_name FROM prompt_versions WHERE id = ?", (version_id,)).fetchone()
    if not row:
        conn.close()
        return {"error": "版本不存在"}
    vname = row[0]
    # 获取最近两次跑分的每次结果（按 test_id 分组的最新两条）
    runs = conn.execute("""
        SELECT test_id, scores, evaluated_at FROM version_test_results
        WHERE version_name = ? ORDER BY evaluated_at DESC
    """, (vname,)).fetchall()
    conn.close()
    
    from collections import defaultdict
    by_test = defaultdict(list)
    for r in runs:
        by_test[r[0]].append((r[1], r[2]))
    
    regressions = []
    for test_id, entries in by_test.items():
        if len(entries) < 2:
            continue
        try:
            scores_new = json.loads(entries[0][0])
            scores_old = json.loads(entries[1][0])
        except (json.JSONDecodeError, TypeError):
            continue
        for dim in scores_new:
            old_val = scores_old.get(dim, 0)
            new_val = scores_new.get(dim, 0)
            if old_val > 0 and (old_val - new_val) / old_val > threshold:
                regressions.append({
                    "test_id": test_id, "dimension": dim,
                    "old_score": old_val, "new_score": new_val,
                    "drop_pct": round((old_val - new_val) / old_val * 100, 1)
                })
    return {
        "version_name": vname,
        "regression_count": len(regressions),
        "regressions": regressions,
    }
```

- [ ] **Step 2: `memory.py` 补充域B/C 结果存储方法**

在 `Prompt 测试集 CRUD` 区域追加：

```python
    def save_domain_bc_scores(self, test_id: str, version_name: str, scores: dict) -> bool:
        """保存域B/C 评分到 version_test_results 的 scores 字段（已有 scores 列）"""
        # scores 已经是 JSON 文本，直接更新可能不必要
        # version_test_results 表已有 scores TEXT 列，run_single_test 的 save_result 已保存
        # 此方法保留供显式调用
        return True
```

- [ ] **Step 3: Commit**

```bash
git add packages/agent/src/prompt_versions.py packages/agent/src/memory.py
git commit -m "feat: enhance version management with regression check and AB test"
```

---

### Task 4: 后端 API 路由

**Files:**
- Modify: `packages/agent/src/main.py`

- [ ] **Step 1: 版本管理 API**

在 `# ========== Prompt 评测接口 ==========` 区域追加：

```python
# ========== 版本管理 API ==========

@app.get("/api/prompt/versions")
async def prompt_list_versions():
    """列出所有 Prompt 版本"""
    from prompt_versions import list_versions
    return {"versions": list_versions(_get_db_path())}


@app.get("/api/prompt/versions/{version_id}")
async def prompt_get_version(version_id: int):
    """获取版本详情"""
    from prompt_versions import get_version_prompt
    v = get_version_prompt(version_id, _get_db_path())
    if not v:
        return JSONResponse({"error": "版本不存在"}, status_code=404)
    return v


@app.post("/api/prompt/versions")
async def prompt_create_version(data: dict):
    """创建新版本"""
    from prompt_versions import create_version
    result = create_version(
        version_name=data["name"],
        description=data.get("description", ""),
        system_prompt=data["system_prompt"],
        db_path=_get_db_path(),
    )
    return result


@app.put("/api/prompt/versions/{version_id}/activate")
async def prompt_activate_version(version_id: int):
    """设置活跃版本"""
    from prompt_versions import activate_version
    result = activate_version(version_id, _get_db_path())
    if result.get("ok"):
        # 重新加载 agent 的 system prompt
        agent.set_prompt(result["system_prompt"])
    return result


@app.get("/api/prompt/versions/{version_id}/results")
async def prompt_version_results(version_id: int, limit: int = 5):
    """获取版本跑分结果"""
    from prompt_versions import get_version_results
    return get_version_results(version_id, _get_db_path(), limit)


@app.get("/api/prompt/versions/{id1}/compare/{id2}")
async def prompt_compare_versions(id1: int, id2: int):
    """对比两个版本"""
    from prompt_versions import ab_test_versions
    return ab_test_versions(id1, id2, _get_db_path())


@app.get("/api/prompt/versions/{version_id}/regression")
async def prompt_regression_check(version_id: int):
    """退化检测"""
    from prompt_versions import regression_check
    return regression_check(version_id, _get_db_path())


@app.get("/api/prompt/test/export")
async def prompt_export_report():
    """导出 Prompt 评测报告 Word 文档"""
    from prompt_versions import list_versions, get_version_results
    from prompt_tester import load_suite
    import docx
    from docx.shared import Pt, Inches, RGBColor
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from io import BytesIO
    from datetime import datetime
    
    db_path = _get_db_path()
    suite = load_suite()
    cases = suite.get("test_cases", [])
    versions = list_versions(db_path)
    
    doc = docx.Document()
    style = doc.styles['Normal']
    style.font.name = '微软雅黑'
    style.font.size = Pt(10)
    
    h = doc.add_heading("Prompt 评测报告", level=1)
    h.alignment = 1
    
    p = doc.add_paragraph(f"生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    for r in p.runs: r.font.size = Pt(9)
    
    # 摘要
    doc.add_heading("测试集概览", level=2)
    categories = {}
    for c in cases:
        cat = c.get("category", "其他")
        categories[cat] = categories.get(cat, 0) + 1
    summary_text = f"测试题总数: {len(cases)} | "
    for cat, cnt in categories.items():
        summary_text += f"{cat}: {cnt} | "
    doc.add_paragraph(summary_text)
    
    # 版本信息
    doc.add_heading("版本信息", level=2)
    tbl = doc.add_table(rows=1 + len(versions), cols=4)
    tbl.style = 'Table Grid'
    for ci, h_text in enumerate(["版本名", "描述", "活跃", "创建时间"]):
        cell = tbl.cell(0, ci); cell.text = h_text
        for par in cell.paragraphs:
            for r in par.runs: r.bold = True; r.font.size = Pt(9)
    for ri, v in enumerate(versions):
        tbl.cell(1+ri, 0).text = v.get("name", "")
        tbl.cell(1+ri, 1).text = v.get("description", "")[:30]
        tbl.cell(1+ri, 2).text = "✅" if v.get("is_active") else ""
        tbl.cell(1+ri, 3).text = str(v.get("created_at", ""))[:16]
    
    # 活跃版本的跑分结果
    active_v = [v for v in versions if v.get("is_active")]
    if active_v:
        doc.add_heading(f"活跃版本跑分结果", level=2)
        results = get_version_results(active_v[0]["id"], db_path, 100)
        if results.get("results"):
            r_tbl = doc.add_table(rows=1+len(results["results"]), cols=5)
            r_tbl.style = 'Table Grid'
            for ci, h_text in enumerate(["测试ID", "分类", "查询", "加权得分", "评估时间"]):
                cell = r_tbl.cell(0, ci); cell.text = h_text
                for par in cell.paragraphs:
                    for r in par.runs: r.bold = True; r.font.size = Pt(9)
            for ri, rr in enumerate(results["results"]):
                r_tbl.cell(1+ri, 0).text = rr.get("test_id", "")
                r_tbl.cell(1+ri, 1).text = rr.get("category", "")
                r_tbl.cell(1+ri, 2).text = rr.get("query", "")
                r_tbl.cell(1+ri, 3).text = str(rr.get("weighted_score", ""))
                r_tbl.cell(1+ri, 4).text = str(rr.get("evaluated_at", ""))[:16]
    
    doc.add_paragraph()
    footer_p = doc.add_paragraph("网络安全移动运营商智能 Agent - 自动生成")
    footer_p.alignment = 2
    for r in footer_p.runs:
        r.font.size = Pt(8); r.font.color.rgb = RGBColor(0x6E, 0x6E, 0x73)
    
    buf = BytesIO()
    doc.save(buf)
    buf.seek(0)
    return StreamingResponse(buf, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                             headers={"Content-Disposition": "attachment; filename=prompt_eval_report.docx"})
```

- [ ] **Step 2: 更新 `run_all_tests` 传入检索文档**

找到 `prompt_test_run` 路由，修改 `run_all_tests` 调用链，确保传入检索结果。修改 `prompt_tester.py` 中的 `run_all_tests`：

```python
def run_all_tests(agent_instance) -> dict:
    """运行全部测试用例"""
    suite = load_suite()
    cases = suite["test_cases"]
    ...
    for case in cases:
        # 为域B/C 获取检索文档
        try:
            retrieved = agent_instance.memory.search(query=case["query"], limit=10) if hasattr(agent_instance, 'memory') else []
        except:
            retrieved = []
        res = run_single_test(agent_instance, case, retrieved_docs=retrieved)
        ...
```

修改 `run_single_test` 签名增加 `retrieved_docs` 参数。

- [ ] **Step 3: Commit**

```bash
git add packages/agent/src/main.py packages/agent/src/prompt_tester.py
git commit -m "feat: add version management APIs and Word report export"
```

---

### Task 5: 前端版本管理面板

**Files:**
- Modify: `packages/agent/src/templates/admin.html`

- [ ] **Step 1: 在 Prompt Tab 添加版本管理按钮栏**

在 `tabPrompt` 内的视图切换工具栏后追加：

```html
<!-- 版本管理工具栏 -->
<div style="display:flex;gap:8px;padding:8px 0;flex-wrap:wrap;align-items:center">
  <span style="font-size:13px;font-weight:600;color:var(--t1)">📦 版本管理</span>
  <button onclick="loadVersions()" style="padding:4px 12px;background:var(--b2);color:var(--t1);border:none;border-radius:4px;cursor:pointer;font-size:12px">🔄 刷新版本</button>
  <button onclick="showCreateVersion()" style="padding:4px 12px;background:var(--a);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">✚ 新建版本</button>
  <button onclick="window.open('/api/prompt/test/export')" style="padding:4px 12px;background:var(--g);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:12px">📄 导出报告</button>
</div>
<!-- 版本列表容器 -->
<div id="versionList" style="margin-bottom:12px;font-size:12px"></div>
<!-- 创建版本弹窗 -->
<div id="createVersionModal" style="display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.4);z-index:999;display:none;align-items:center;justify-content:center">
  <div style="background:var(--card);border-radius:12px;padding:24px;width:600px;max-width:90vw;max-height:80vh;overflow-y:auto">
    <h3 style="margin:0 0 12px">新建 Prompt 版本</h3>
    <input id="newVerName" placeholder="版本名称" style="width:100%;margin-bottom:8px;padding:6px 10px;border:1px solid var(--b2);border-radius:6px;font-size:13px">
    <input id="newVerDesc" placeholder="版本描述（可选）" style="width:100%;margin-bottom:8px;padding:6px 10px;border:1px solid var(--b2);border-radius:6px;font-size:13px">
    <textarea id="newVerPrompt" placeholder="System Prompt 内容" rows="8" style="width:100%;margin-bottom:12px;padding:6px 10px;border:1px solid var(--b2);border-radius:6px;font-size:12px;font-family:monospace"></textarea>
    <div style="display:flex;gap:8px;justify-content:flex-end">
      <button onclick="document.getElementById('createVersionModal').style.display='none'" style="padding:6px 16px;background:var(--b2);border:none;border-radius:6px;cursor:pointer">取消</button>
      <button onclick="doCreateVersion()" style="padding:6px 16px;background:var(--a);color:#fff;border:none;border-radius:6px;cursor:pointer">创建</button>
    </div>
  </div>
</div>
```

- [ ] **Step 2: 版本管理 JS 函数**

```javascript
async function loadVersions() {
  try {
    const r = await fetch('/api/prompt/versions');
    const d = await r.json();
    const versions = d.versions || [];
    const container = document.getElementById('versionList');
    if (versions.length === 0) {
      container.innerHTML = '<span style="color:var(--t2)">暂无版本</span>';
      return;
    }
    container.innerHTML = versions.map(v => `
      <div style="display:flex;align-items:center;gap:8px;padding:8px 12px;border:1px solid var(--b2);border-radius:8px;margin-bottom:6px;background:${v.is_active ? 'rgba(52,199,89,0.08)' : 'var(--card)'}">
        <span style="font-weight:600;font-size:13px;flex:1">${v.name}</span>
        <span style="font-size:11px;color:var(--t2);flex:1">${(v.description||'').slice(0,40)}</span>
        <span style="font-size:11px">${(v.created_at||'').slice(5,16)}</span>
        ${v.is_active ? '<span style="font-size:11px;color:var(--g);font-weight:600">✅ 活跃</span>' : 
          `<button onclick="doActivateVersion(${v.id})" style="padding:2px 10px;background:var(--a);color:#fff;border:none;border-radius:4px;cursor:pointer;font-size:11px">设为活跃</button>`}
        <button onclick="showVersionDetail(${v.id})" style="padding:2px 10px;background:var(--b2);color:var(--t1);border:none;border-radius:4px;cursor:pointer;font-size:11px">详情</button>
      </div>
    `).join('');
  } catch(e) {
    document.getElementById('versionList').innerHTML = `<span style="color:var(--r)">加载失败: ${e.message}</span>`;
  }
}

function showCreateVersion() {
  document.getElementById('createVersionModal').style.display = 'flex';
  document.getElementById('newVerName').value = 'v' + new Date().toISOString().slice(0,10);
  document.getElementById('newVerDesc').value = '';
  document.getElementById('newVerPrompt').value = '';
}

async function doCreateVersion() {
  const name = document.getElementById('newVerName').value.trim();
  const desc = document.getElementById('newVerDesc').value.trim();
  const prompt = document.getElementById('newVerPrompt').value.trim();
  if (!name || !prompt) { showToast('请填写版本名称和 Prompt 内容', ''); return; }
  try {
    const r = await fetch('/api/prompt/versions', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, description:desc, system_prompt:prompt})});
    const d = await r.json();
    if (d.error) { showToast('❌ '+d.error, ''); return; }
    showToast('✅ 版本已创建');
    document.getElementById('createVersionModal').style.display = 'none';
    await loadVersions();
  } catch(e) { showToast('❌ '+e.message, ''); }
}

async function doActivateVersion(id) {
  try {
    const r = await fetch(`/api/prompt/versions/${id}/activate`, {method:'PUT'});
    const d = await r.json();
    if (d.error) { showToast('❌ '+d.error, ''); return; }
    showToast(`✅ 已切换到版本: ${d.version_name}`);
    await loadVersions();
  } catch(e) { showToast('❌ '+e.message, ''); }
}

async function showVersionDetail(id) {
  try {
    const [vr, rr] = await Promise.all([
      fetch(`/api/prompt/versions/${id}`),
      fetch(`/api/prompt/versions/${id}/results?limit=50`)
    ]);
    const v = await vr.json();
    const r = await rr.json();
    const results = r.results || [];
    const detailHtml = `
      <div style="position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.4);z-index:999;display:flex;align-items:center;justify-content:center">
        <div style="background:var(--card);border-radius:12px;padding:24px;width:700px;max-width:90vw;max-height:80vh;overflow-y:auto">
          <h3 style="margin:0 0 8px">${v.name}</h3>
          <p style="font-size:12px;color:var(--t2);margin-bottom:12px">${v.description||'无描述'} | ${v.created_at} | ${v.is_active ? '✅ 活跃' : '非活跃'}</p>
          <pre style="background:#f5f5f7;padding:12px;border-radius:8px;font-size:11px;max-height:200px;overflow-y:auto;white-space:pre-wrap">${v.system_prompt||'无'}</pre>
          ${results.length > 0 ? `
          <h4 style="margin:12px 0 8px">跑分结果 (${results.length})</h4>
          <table style="width:100%;font-size:11px;border-collapse:collapse">
            <thead><tr>
              <th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--b2)">测试ID</th>
              <th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--b2)">分类</th>
              <th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--b2)">加权分</th>
              <th style="text-align:right;padding:4px 8px;border-bottom:1px solid var(--b2)">耗时</th>
            </tr></thead>
            <tbody>${results.map(rr => `<tr>
              <td style="padding:4px 8px;border-bottom:1px solid #eee">${rr.test_id}</td>
              <td style="padding:4px 8px;border-bottom:1px solid #eee">${rr.category}</td>
              <td style="padding:4px 8px;border-bottom:1px solid #eee;text-align:right;font-weight:${(rr.weighted_score||0)>=60?'':'bold'};color:${(rr.weighted_score||0)>=60?'var(--g)':'var(--r)'}">${(rr.weighted_score||0).toFixed(1)}</td>
              <td style="padding:4px 8px;border-bottom:1px solid #eee;text-align:right">${rr.duration?.toFixed(1)}s</td>
            </tr>`).join('')}</tbody>
          </table>` : '<p style="color:var(--t2);font-size:12px">暂无跑分结果</p>'}
          <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:12px">
            <button onclick="this.closest('[style*=\"fixed\"]').remove()" style="padding:6px 16px;background:var(--b2);border:none;border-radius:6px;cursor:pointer">关闭</button>
          </div>
        </div>
      </div>`;
    document.body.insertAdjacentHTML('beforeend', detailHtml);
  } catch(e) { showToast('❌ '+e.message, ''); }
}
```

- [ ] **Step 3: 在 `switchTab` 中加载版本列表**

找到 `switchTab` 函数中处理 `prompt` 的分支，追加版本加载：

```javascript
if (name === 'prompt') {
    loadPromptEval();
    loadVersions();  // 新增
    return;
}
```

- [ ] **Step 4: Commit**

```bash
git add packages/agent/src/templates/admin.html
git commit -m "feat: add version management UI panel to Prompt tab"
```

---

### Task 6: 前端域B/C 结果展示

**Files:**
- Modify: `packages/agent/src/templates/admin.html`

- [ ] **Step 1: 在全量测试结果展示区追加域B/C 评分列**

找到 `loadPromptEval` 函数中渲染测试结果表格的部分，在响应效率列后追加域B/C 列：

```javascript
// 在全量测试结果表格中追加域B/C 列
// 找到表格表头行追加：
// <th>CP</th><th>CR</th><th>Faith</th><th>Rel</th><th>Halu</th>
// 每行数据追加：
// <td style="color:${...}">${cp_label}</td>
// <td style="color:${...}">${cr_label}</td>
// <td style="color:${...}">${faith_label}</td>
// <td style="color:${...}">${rel_label}</td>
// <td style="color:${...}">${halu_label}</td>
```

具体代码：在 `loadPromptEval` 的结果处理循环中，从 `result.details` 读取域B/C 得分，追加单元格。

- [ ] **Step 2: 在摘要卡片区加域B/C 平均分展示**

```javascript
// 在现有摘要卡片后追加：
const avg_cp = (results.reduce((s,r) => s + (r.details?.context_precision?.score || 0), 0) / results.length * 100).toFixed(0);
const avg_cr = (results.reduce((s,r) => s + (r.details?.context_recall?.score || 0), 0) / results.length * 100).toFixed(0);
const avg_faith = (results.reduce((s,r) => s + (r.details?.faithfulness?.score || 0), 0) / results.length * 100).toFixed(0);
```

- [ ] **Step 3: Commit**

```bash
git add packages/agent/src/templates/admin.html
git commit -m "feat: display domain B/C scores in Prompt eval results table"
```

---

### Task 7: 前端弹窗操作修正

- [ ] **Step 1: 修复创建版本弹窗 `display` 冲突**

`createVersionModal` 同时有 `display:none`（内联）和 `display:none`（style 内），修复为 `display:none` 仅由 JS 控制。

- [ ] **Step 2: 测试全部 API**

Run: 重启服务后访问 `/admin`，检查：
1. Prompt Tab 加载正常
2. 版本列表正常显示
3. 新建版本弹窗正常
4. 导出报告按钮可用

- [ ] **Step 3: 最终提交**

```bash
git add .
git commit -m "fix: version modal display and final integration"
```
