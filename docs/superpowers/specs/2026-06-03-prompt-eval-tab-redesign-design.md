# Prompt 评测 Tab 改造设计

## 1. 现状

| 项目 | 状态 |
|:----|:----:|
| 测试题 | 31 条已从 JSON 迁入 `prompt_test_items` 表 |
| 版本表 | `prompt_versions` + `version_test_results` 已建表 |
| 域A 评分 | 8 维度（来源标注/品牌禁止/越狱拦截/偏题检测/首答完整/知识准确/输出格式/响应效率）已实现 |
| 域B/C/E | 未实现 |
| 前端管理 | 无测试题编辑界面、无版本管理界面、无域B/C/E 展示 |

## 2. 架构变更

```
现有：
  prompt_tester.py (域A 8维度) → API → 前端只读展示

改造后：
  prompt_tester.py (域A + 域B + 域C) 
  prompt_versions.py (版本管理 + 域E 端到端评估)
  memory.py (新增方法)
  main.py (API 层)
  admin.html (完整管理面板)
```

## 3. 新增/修改的模块

### 3.1 测试题管理面板（前端）

- 表格展示所有测试题（query / category / difficulty / is_active）
- 支持：编辑（点单元格编辑）、新增、软删除
- 与检索质量测试集面板一致的操作风格

### 3.2 版本管理面板（前端 + 后端）

**后端 API（prompt_versions.py 已有骨架，补充）：**

| API | 功能 |
|:----|:-----|
| `GET /api/prompt/versions` | 列出所有版本 |
| `POST /api/prompt/versions` | 创建新版本 |
| `PUT /api/prompt/versions/{id}/activate` | 切换活跃版本 |
| `GET /api/prompt/versions/{id1}/compare/{id2}` | 版本 Prompt 对比 |
| `GET /api/prompt/versions/{id}/results` | 版本跑分结果 |
| `POST /api/prompt/versions/ab-test` | A/B 测试（两版本同时跑） |

**前端面板：**
- 版本列表：名称 / 描述 / 是否活跃 / 创建时间 / 跑分次数
- 点击展开：查看该版本系统 Prompt 全文
- "设为活跃"按钮
- "对比"按钮 → 并排显示两个版本的 Prompt diff 和跑分结果
- "A/B 测试"按钮 → 两个版本同时跑分，结果并排展示

### 3.3 域B Context Precision/Recall（prompt_tester.py 新增）

```
eval_context_precision(answer, retrieved_docs) → precision_score
eval_context_recall(answer, retrieved_docs) → recall_score
```

**Context Precision**：回答中被引用的检索文档句子数 / 回答中所有引用句子数
- 用 LLM 识别回答中的引用句子（含 [来源N] 标记的句子）
- 判断每个引用是否准确对应检索内容

**Context Recall**：检索文档中关键信息被引用的比例
- 从检索文档提取关键信息点
- 判断回答覆盖了多少

分值范围：0.0 - 1.0

### 3.4 域C 生成质量评估（prompt_tester.py 新增）

```
eval_faithfulness(answer, retrieved_docs) → faithfulness_score
eval_relevancy(answer, query) → relevancy_score
eval_hallucination(answer, retrieved_docs) → hallucination_score
```

**Faithfulness（忠实度）**：
- 将回答拆分为事实性陈述句
- 逐句与检索文档对比，判断是否有依据
- Faithfulness = 有依据的句子数 / 总事实性句子数

**Relevancy（相关性）**：
- LLM 判断回答与问题的相关程度
- 维度：是否直接回答问题、是否包含无关信息、是否简洁

**Hallucination（幻觉检测）**：
- 检测回答中是否存在检索文档中没有的信息
- 结合 Faithfulness 结果，标记"无依据陈述"
- Hallucination = 1 - 无依据陈述数 / 总陈述数

以上均通过 LLM 评分（复用 promptEval 卡片的 LLM），不依赖外部模型。

### 3.5 域E 端到端评估（prompt_versions.py 新增 + 前端展示）

**回归测试**：
- 每次跑分后自动记录到 `version_test_results` 表
- 前端展示历史跑分趋势图（折线图：日期 vs 加权总分）

**退化预警**：
- 对比最后两次跑分的各维度得分
- 任一维度下降超过 10% 时标记为"退化"
- 前端用红色标记退化维度

**版本对比**：
- 并排展示两个版本的跑分结果（雷达图 + 表格）
- 支持 "当前活跃版 vs 候选版" 的对比

### 3.6 Word 报告导出

`GET /api/prompt/test/export` → `prompt_eval_report.docx`

- 测试集摘要（总题数、维度分布）
- 当前活跃版本信息
- 最近一次跑分的各维度得分 + 加权总分
- 各维度的详细评分明细

## 4. 数据结构变更

无新增表。复用已有：
- `prompt_test_items` — 测试题
- `prompt_versions` — 版本
- `version_test_results` — 跑分结果

## 5. 前后端数据流

```
跑分流程：
  用户点"运行全部测试" 
  → POST /api/prompt/test/run 
  → prompt_tester.run_all_tests() 
    → 域A 8维度 (现有)
    → 域B Context Precision/Recall (新增)
    → 域C Faithfulness/Relevancy/Hallucination (新增)
  → save_result() 存入 version_test_results
  → 返回完整评分结果
  → 前端刷新图表

版本切换流程：
  用户点"设为活跃"
  → PUT /api/prompt/versions/{id}/activate
  → 更新 prompt_versions.is_active
  → agent.reload_prompt() 重新加载活跃版本
```

## 6. 不涉及

- 不改动现有域A 评分逻辑
- 不改动 prompt_test_suite.json（保留但不再读取）
- 不改动现有检索质量/入库质量 Tab
- 不需要 Ollama 以外的新依赖
