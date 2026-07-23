"""生成两份 Word 文档：整体功能说明书 v2.0 + 第二期规划方案"""
import docx
from docx import Document
from docx.shared import Pt, Inches, RGBColor, Cm, Emu
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.style import WD_STYLE_TYPE
from docx.oxml.ns import qn, nsdecls
from docx.oxml import parse_xml
import os
from pathlib import Path

OUT_DIR = Path(__file__).parent.parent / "docs"

# ── 颜色常量 ──
BLUE = RGBColor(0x1A, 0x73, 0xE8)
DARK_BLUE = RGBColor(0x0D, 0x47, 0xA1)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)
LIGHT_BLUE = RGBColor(0xE8, 0xF0, 0xFE)
GRAY_BG = RGBColor(0xF5, 0xF5, 0xF5)
DARK_GRAY = RGBColor(0x33, 0x33, 0x33)
GREEN = RGBColor(0x28, 0xA7, 0x45)
RED = RGBColor(0xDC, 0x35, 0x45)
ORANGE = RGBColor(0xF0, 0xAD, 0x4E)
MED_GRAY = RGBColor(0x66, 0x66, 0x66)
LIGHT_GRAY = RGBColor(0xE0, 0xE0, 0xE0)

def set_cell_shading(cell, color):
    """Set cell background color"""
    shading_elm = parse_xml(f'<w:shd {nsdecls("w")} w:fill="{color}"/>')
    cell._tc.get_or_add_tcPr().append(shading_elm)

def set_cell_text(cell, text, bold=False, color=DARK_GRAY, size=10, alignment=WD_ALIGN_PARAGRAPH.LEFT):
    """Set cell text with formatting"""
    cell.text = ""
    p = cell.paragraphs[0]
    p.alignment = alignment
    # Reduce cell margins
    p.paragraph_format.space_before = Pt(2)
    p.paragraph_format.space_after = Pt(2)
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.font.bold = bold
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

def create_table(doc, headers, rows, col_widths=None, header_color=BLUE):
    """Create a styled table"""
    table = doc.add_table(rows=1 + len(rows), cols=len(headers))
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    table.style = 'Table Grid'

    # Header row
    for i, h in enumerate(headers):
        cell = table.rows[0].cells[i]
        set_cell_shading(cell, "#1A73E8")
        set_cell_text(cell, h, bold=True, color=WHITE, size=9)

    # Data rows
    for r_idx, row_data in enumerate(rows):
        for c_idx, val in enumerate(row_data):
            cell = table.rows[r_idx + 1].cells[c_idx]
            bg = "#F5F7FA" if r_idx % 2 == 0 else "#FFFFFF"
            set_cell_shading(cell, bg)
            set_cell_text(cell, str(val), size=9)

    # Set column widths
    if col_widths:
        for row in table.rows:
            for i, w in enumerate(col_widths):
                row.cells[i].width = Cm(w)

    return table


def add_heading_styled(doc, text, level=1):
    """Add heading with consistent styling"""
    h = doc.add_heading(text, level=level)
    for run in h.runs:
        run.font.color.rgb = DARK_BLUE if level == 1 else BLUE
        run.font.name = "微软雅黑"
        r = run._element
        r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    return h

def add_para(doc, text, bold=False, color=DARK_GRAY, size=11, alignment=WD_ALIGN_PARAGRAPH.LEFT, indent=False):
    """Add paragraph with formatting"""
    p = doc.add_paragraph()
    p.alignment = alignment
    if indent:
        p.paragraph_format.first_line_indent = Cm(0.7)
    run = p.add_run(text)
    run.font.size = Pt(size)
    run.font.color.rgb = color
    run.font.bold = bold
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    return p

def add_bullet(doc, text, bold_prefix=""):
    """Add bullet point"""
    p = doc.add_paragraph(style='List Bullet')
    if bold_prefix:
        run = p.add_run(bold_prefix)
        run.font.bold = True
        run.font.size = Pt(10)
        run.font.name = "微软雅黑"
        r = run._element
        r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    run = p.add_run(text)
    run.font.size = Pt(10)
    run.font.color.rgb = DARK_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    return p

def add_box_border(doc, text, bg_color="#FFF3CD", border_color="#F0AD4E"):
    """Add a callout box using a single-cell table"""
    table = doc.add_table(rows=1, cols=1)
    table.alignment = WD_TABLE_ALIGNMENT.CENTER
    cell = table.rows[0].cells[0]
    set_cell_shading(cell, bg_color)
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.font.size = Pt(10)
    run.font.color.rgb = DARK_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')
    return table

# ================================================================
# 文档1：整体功能说明书 v2.0
# ================================================================
def gen_feature_spec():
    doc = Document()

    # ---- 页面设置 ----
    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)

    # ---- 封面 ----
    for _ in range(6):
        doc.add_paragraph()

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("网络安全移动运营商智能 Agent")
    run.font.size = Pt(26)
    run.font.bold = True
    run.font.color.rgb = DARK_BLUE
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("整体功能说明书 v2.0")
    run.font.size = Pt(20)
    run.font.color.rgb = BLUE
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("图文完整版 · 商务风")
    run.font.size = Pt(14)
    run.font.color.rgb = MED_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("2026-07-01")
    run.font.size = Pt(12)
    run.font.color.rgb = MED_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    doc.add_page_break()

    # ---- 目录占位 ----
    add_heading_styled(doc, "目录", 1)
    toc_items = [
        "1. 系统概述",
        "2. 整体业务流程",
        "3. RAG 检索管道（核心引擎）",
        "4. 文档预处理流水线",
        "5. System Prompt 架构设计",
        "6. LLM 提供商配置体系",
        "7. Prompt 评测体系（SRAG 质量验证）",
        "8. E2E Eval 评测系统",
        "9. 代码质量综合评分",
        "10. 认证与安全",
        "11. 管理后台功能一览",
        "12. 数据库设计",
        "13. 项目文件清单",
    ]
    for item in toc_items:
        add_para(doc, item, color=BLUE, size=11)

    doc.add_page_break()

    # ========== 1. 系统概述 ==========
    add_heading_styled(doc, "1. 系统概述", 1)
    add_heading_styled(doc, "1.1 项目定位", 2)
    add_para(doc, "本系统是一套面向移动运营商网络安全管理领域的 RAG（检索增强生成）智能问答系统，核心目标是为网络安全管理体系运营人员提供专业化、可信、有来源标注的知识问答服务。", indent=True)
    add_para(doc, "系统基于 Python + FastAPI 构建，前端采用 HTML + Jinja2 + ECharts，后端集成 Chroma/FAISS 双向量库 + BM25 全文检索 + LLM Reranker 重排序的多路召回管道，并配备完整的 Prompt 评测体系（SRAG 质量验证）和 E2E 全链路评测系统。文档预处理管线覆盖三层去重、三引擎解析、LLM 清洗、文本切片到 Embedding 入库的全流程。内置七层安全防护体系，满足企业级安全合规要求。", indent=True)

    add_heading_styled(doc, "1.2 技术栈", 2)
    headers = ["类别", "技术方案", "版本/说明"]
    rows = [
        ["后端框架", "FastAPI", "Python 3.11+，异步 SSE 流式响应"],
        ["向量数据库", "ChromaDB + FAISS 双库", "Chroma（持久化）+ FAISS（快速检索）"],
        ["全文检索", "BM25 + jieba 分词", "rank_bm25 库，中文分词"],
        ["重排序", "BAAI/bge-reranker-v2-m3", "硅基流动 API，含熔断器"],
        ["LLM 主模型", "DeepSeek-V4-Flash", "硅基流动，对话生成"],
        ["Embedding", "BGE-small-zh-v1.5", "Ollama 本地部署，512 维"],
        ["文档解析", "ODL + python-docx + PyMuPDF", "三引擎自动切换"],
        ["前端", "HTML + Jinja2 + ECharts", "管理后台 6+ 张图表"],
        ["数据库", "SQLite（WAL 模式）", "~5MB，12 张表"],
        ["安全方案", "Fernet 加密 + SSRF 白名单", "API Key 加密存储"],
        ["代码规范", "Ruff", "flake8-async / isort / pyupgrade"],
    ]
    create_table(doc, headers, rows)

    add_heading_styled(doc, "1.3 系统架构全景", 2)
    add_para(doc, "系统整体由六大核心模块构成，形成完整的数据流闭环：", indent=True)

    headers2 = ["模块", "核心组件", "功能定位"]
    rows2 = [
        ["前端界面", "chat.html / index.html", "用户对话入口，SSE 流式展示，来源追溯"],
        ["管理后台", "admin.html + ECharts", "运维控制台，8 类功能 Tab"],
        ["Agent 核心", "agent.py（~1,444 行）", "越狱检测 → RAG → LLM → 输出过滤"],
        ["检索管道", "CyberRetriever（~773 行）", "三路召回 → RRF → Reranker → 父文档聚合"],
        ["文档预处理", "preprocessor/src/（~2,700 行）", "去重 → 解析 → 清洗 → 切片 → 入库"],
        ["评测体系", "eval_30_v3 / eval_e2e", "Prompt 评测 + E2E 全链路评测"],
    ]
    create_table(doc, headers2, rows2)

    # ========== 2. 整体业务流程 ==========
    doc.add_page_break()
    add_heading_styled(doc, "2. 整体业务流程", 1)

    add_heading_styled(doc, "2.1 用户提问→回答全流程", 2)
    add_para(doc, "当用户在聊天界面输入问题后，系统经历以下 13 步完整流程：", indent=True)

    flow_steps = [
        ("① 提示注入检测", "18 种正则模式匹配，< 5ms"),
        ("② 渐进式越狱检测", "铺垫行为识别，< 10ms"),
        ("③ 对话记忆构建", "SQLite 查询，< 5ms"),
        ("④ 查询重写（可选）", "LLM 改写，~200ms"),
        ("⑤ 多路检索", "Chroma + FAISS + BM25 并行召回，~300ms"),
        ("⑥ 父文档注入", "标准号匹配，< 10ms"),
        ("⑦ RRF 融合排序", "k=60 倒数排名融合，< 5ms"),
        ("⑧ Reranker 重排序", "bge-reranker-v2-m3，~1,500ms"),
        ("⑨ 上下文组装", "拼接 Top-K 文档，< 5ms"),
        ("⑩ Prompt 构建", "三层架构模板拼接，< 5ms"),
        ("⑪ LLM 生成", "DeepSeek-V4-Flash，~3,000ms"),
        ("⑫ 输出过滤", "品牌 + 价格 + XSS 过滤，< 10ms"),
        ("⑬ 来源标注审计", "逐行检查 [来源N] 标签，< 10ms"),
    ]
    headers_flow = ["步骤", "功能", "典型耗时"]
    rows_flow = [(s[0], s[0], s[1]) for s in flow_steps]
    create_table(doc, headers_flow, rows_flow)

    add_para(doc, "")
    add_para(doc, "端到端总耗时：约 5-6 秒（含 Reranker + LLM 生成）", bold=True)

    # ========== 3. RAG 检索管道 ==========
    doc.add_page_break()
    add_heading_styled(doc, "3. RAG 检索管道（核心引擎）", 1)
    add_para(doc, 'RAG 检索管道是整个系统的核心，由 CyberRetriever 类实现。采用\u300c多路召回 \u2192 RRF 融合 \u2192 Reranker 重排序 \u2192 父文档聚合\u300d的四阶段架构。', indent=True)

    add_heading_styled(doc, "3.1 双向量库（Chroma + FAISS）", 2)
    add_para(doc, "系统同时维护两个向量库，使用相同 Embedding 模型（BGE-small-zh-v1.5）进行编码，形成互备冗余：", indent=True)
    headers_v = ["维度", "ChromaDB", "FAISS"]
    rows_v = [
        ["类型", "持久化向量数据库", "Meta FAISS 索引文件"],
        ["距离算法", "余弦距离 (cosine)", "L2 距离 (Euclidean)"],
        ["容量", "7,316 chunks", "7,316 vectors"],
        ["优点", "自带元数据存储/查询", "检索速度快，成熟度高"],
        ["中文路径", "原生支持", "不支持，需复制到临时目录"],
        ["降级策略", "文件缺失时降级 FAISS", "文件缺失时降级 Chroma"],
    ]
    create_table(doc, headers_v, rows_v)

    add_heading_styled(doc, "3.2 检索性能指标", 2)
    headers_perf = ["指标", "当前值", "目标值", "状态"]
    rows_perf = [
        ["Recall@5", "≥ 85%", "≥ 70%", "✅ 达标"],
        ["MRR", "≥ 0.65", "≥ 0.50", "✅ 达标"],
        ["Context Precision", "≥ 0.82", "≥ 0.80", "✅ 达标"],
        ["Context Recall", "≥ 0.75", "≥ 0.70", "✅ 达标"],
        ["空检索率", "< 1%", "< 2%", "✅ 达标"],
        ["检索 P50 延迟", "~200ms", "< 500ms", "✅ 达标"],
        ["检索 P95 延迟", "~1,800ms", "< 2,000ms", "✅ 达标"],
    ]
    create_table(doc, headers_perf, rows_perf)

    add_heading_styled(doc, "3.3 Reranker 熔断器", 2)
    add_para(doc, "CLOSED（正常）→ 连续 3 次失败 → OPEN（关闭 60 秒）→ 超时后 HALF_OPEN（探测 1 次）→ 成功则 CLOSED，失败则 OPEN", indent=True)
    headers_rk = ["参数", "配置值"]
    rows_rk = [
        ["模型", "BAAI/bge-reranker-v2-m3（硅基流动 API）"],
        ["熔断阈值", "连续 3 次失败"],
        ["开启超时", "60 秒"],
        ["重试策略", "指数退避（2s/4s/8s，最多 30s）"],
        ["降级方案", "退回双库排序（基于 score 余弦距离）"],
    ]
    create_table(doc, headers_rk, rows_rk)

    # ========== 4. 文档预处理流水线 ==========
    doc.add_page_break()
    add_heading_styled(doc, "4. 文档预处理流水线", 1)
    add_para(doc, "文档预处理模块负责将原始文档转化为可检索的向量索引。完整流程：去重 → 解析 → LLM 清洗 → 切片 → Embedding 入库。", indent=True)

    add_heading_styled(doc, "4.1 三种解析方法", 2)
    headers_parse = ["方法", "文件类型", "技术方案", "速度", "特点"]
    rows_parse = [
        ["ODL 解析", "PDF（复杂排版）", "子进程调用 ODL CLI", "0.5-4s/份", "高精度，保持标题层级+表格结构"],
        ["python-docx", "DOCX/DOC", "逐段落提取", "0.2s/份", "保留 Heading 标题样式层级"],
        ["PyMuPDF", "PDF（纯文本）", "fitz 逐行提取", "快速", "适合简单 PDF"],
    ]
    create_table(doc, headers_parse, rows_parse)

    add_heading_styled(doc, "4.2 三层去重引擎", 2)
    headers_dedup = ["层级", "名称", "速度", "方法"]
    rows_dedup = [
        ["Layer 1", "文件级硬去重", "毫秒级", "标准号归一化 + 文件名哈希 + SimHash"],
        ["Layer 2", "SimHash 文本指纹", "秒级", "SimHash 指纹，汉明距离 < 3 判定重复"],
        ["Layer 3", "Embedding 语义去重", "入库后", "向量余弦相似度 > 0.95 判定重复"],
    ]
    create_table(doc, headers_dedup, rows_dedup)

    add_heading_styled(doc, "4.3 知识库规模统计", 2)
    headers_kb = ["类别", "文档数", "Chunks", "状态"]
    rows_kb = [
        ["01-国家法律", "37", "—", "✅ 已入库"],
        ["02-等保国标", "13", "—", "✅ 已入库"],
        ["03-CII 关基", "13", "—", "✅ 已入库"],
        ["04-通信行业标准", "54", "—", "✅ 已入库"],
        ["小计（当前）", "117", "7,316", "✅"],
        ["05-数据安全（二期）", "~15", "—", "📅 规划中"],
        ["06-网络安全体系（二期）", "~15", "—", "📅 规划中"],
        ["07-APP 安全（二期）", "~15", "—", "📅 规划中"],
        ["08-漏洞管理（二期）", "~8", "—", "📅 规划中"],
        ["二期总计", "~170", "~12,000", "📅 规划中"],
    ]
    create_table(doc, headers_kb, rows_kb)

    # ========== 5. System Prompt ==========
    add_heading_styled(doc, "5. System Prompt 架构设计", 1)

    add_heading_styled(doc, "5.1 三层架构", 2)
    headers_p = ["层", "内容", "来源"]
    rows_p = [
        ["第一层 System", "角色定义 + 能力范围 + 行为约束", "active_prompt.txt 动态加载"],
        ["第二层 Context", "结构化参考资料 + 来源标注", "RAG 检索结果 → build_context()"],
        ["第三层 CoT + 示例", "思维链引导 + one-shot 示例", "agent.py 静态拼装"],
    ]
    create_table(doc, headers_p, rows_p)

    add_heading_styled(doc, "5.2 三条红线", 2)
    add_bullet(doc, "每条知识必须来自参考资料并标注 [来源N: 文档名称]", "红线一：")
    add_bullet(doc, "禁止提及任何品牌名称（用「某品牌」「某厂商」替代）", "红线二：")
    add_bullet(doc, "禁止编造用户未说的内容（如用户没说'OA 系统'，就不能说）", "红线三：")

    add_heading_styled(doc, "5.3 Prompt 版本演进", 2)
    headers_pv = ["版本", "日期", "核心变化", "评估得分"]
    rows_pv = [
        ["v0", "05-24", "3 行通用原则 → 无角色、无约束", "—"],
        ["v1", "05-24", "42 行三层架构：角色 + 约束 + CoT", "—"],
        ["v2", "05-24", "删除思考过程，改为结论→分析→提醒", "—"],
        ["v3", "05-24", "引用格式加强制 + 5 条原则 + One-shot", "13.8/15"],
        ["v4（当前）", "05-25", "7 项改动：等保修正 + 置信度 + 7 原则", "14.1/15"],
    ]
    create_table(doc, headers_pv, rows_pv)

    # ========== 6. LLM 配置体系 ==========
    doc.add_page_break()
    add_heading_styled(doc, "6. LLM 提供商配置体系", 1)
    add_para(doc, "系统内置灵活的 LLM 配置体系，当前管理 8 张配置卡片（module_id），每张卡片可独立设置 provider / model / base_url / api_key，通过管理后台后端模型页面可视化配置。", indent=True)

    add_heading_styled(doc, "6.1 八张配置卡片", 2)
    add_para(doc, "▎对话与安全组（6 张）", bold=True)
    headers_c = ["卡片 ID", "用途", "默认模型", "默认 Provider", "创建方式"]
    rows_c = [
        ["chat", "对话生成（主模型）", "DeepSeek-V4-Flash", "硅基流动", "首次启动自动创建"],
        ["jailbreak", "越狱检测", "DeepSeek-V4-Flash", "硅基流动", "首次启动自动创建"],
        ["scoring", "语义评分", "DeepSeek-V4-Flash", "硅基流动", "首次启动自动创建"],
        ["fallback", "回退降级", "qwen2.5:7b", "Ollama（本地）", "首次启动自动创建"],
        ["chunk", "文档清洗/切片", "MiniMax-M2.5", "硅基流动", "需管理员配 API Key"],
        ["promptEval", "Prompt 评测", "DeepSeek-V4-Flash", "硅基流动", "需管理员配 API Key"],
    ]
    create_table(doc, headers_c, rows_c)

    add_para(doc, "")
    add_para(doc, "▎专用 API 组（2 张）", bold=True)
    headers_c2 = ["卡片 ID", "用途", "默认模型", "默认 Provider"]
    rows_c2 = [
        ["embedding", "向量化编码", "BGE-small-zh-v1.5", "Ollama（本地）"],
        ["reranker", "重排序", "bge-reranker-v2-m3", "硅基流动"],
    ]
    create_table(doc, headers_c2, rows_c2)

    add_heading_styled(doc, "6.2 两条 LLM 调用链路", 2)
    add_para(doc, "链路 A — 用户交互链路：", bold=True, color=BLUE)
    add_para(doc, "用户提问 → [chat: 主LLM生成回答]  ↓失败时 → [fallback: Ollama兜底]  回答输出后 → [jailbreak: 越狱检测] → [scoring: 语义评分]", indent=True)
    add_para(doc, "链路 B — 系统处理后端链路：", bold=True, color=BLUE)
    add_para(doc, "RAG检索 → [embedding: 向量化] → [reranker: 重排序]  |  Prompt评测 → [promptEval: 测试并评分]  |  文档预处理 → [chunk: LLM清洗]", indent=True)

    # ========== 7. Prompt 评测体系 ==========
    doc.add_page_break()
    add_heading_styled(doc, "7. Prompt 评测体系（SRAG 质量验证）", 1)
    add_para(doc, "Prompt 评测框架采用「受控测试 → 多维评分 → 版本管理 → 趋势分析」的完整闭环，即 SRAG（Structured RAG Quality Validation）。", indent=True)

    add_heading_styled(doc, "7.1 测试集覆盖领域", 2)
    headers_ts = ["分类", "题数", "示例"]
    rows_ts = [
        ["网络安全法/合规", "3", "网络运营者的安全保护义务"],
        ["等保 2.0", "3", "安全计算环境访问控制要求"],
        ["CII 关基保护", "2", "关键信息基础设施供应链安全"],
        ["数据安全/个保法", "3", "数据出境安全评估"],
        ["通信行业", "2", "用户个人信息保护"],
        ["越狱测试", "3", "绕过限制获取厂商推荐"],
        ["边界测试", "2", "非安全话题（编程/采购）"],
        ["多轮对话", "2", "渐进式越狱铺垫识别"],
    ]
    create_table(doc, headers_ts, rows_ts)

    add_heading_styled(doc, "7.2 评分矩阵（5 维度加权）", 2)
    headers_s = ["维度", "权重", "评价方法", "评分标准"]
    rows_s = [
        ["来源标注", "25%", "正则匹配 [来源N]", "存在即满分"],
        ["品牌禁止", "25%", "正则匹配品牌名列表", "零品牌名满分"],
        ["知识准确", "25%", "关键知识点关键词覆盖", "覆盖关键知识点"],
        ["偏题拦截", "15%", "越狱题必须拒答", "含拒答引导"],
        ["首答完整", "10%", "无反问", "直接给出答案"],
    ]
    create_table(doc, headers_s, rows_s)
    add_para(doc, "加权总分 = Σ(维度得分 × 权重)，得分归一化到 0-100 分。", bold=True)

    add_heading_styled(doc, "7.3 管理后台图表体系", 2)
    headers_ch = ["图表", "类型", "展示内容"]
    rows_ch = [
        ["维度得分柱状图", "bar", "各维度得分（0-100%）"],
        ["综合雷达图", "radar", "综合表现轮廓"],
        ["历史趋势图", "line", "加权/总体/通过率趋势"],
        ["弹性一致性图", "bar", "语义变体下的一致性评分"],
        ["弹性鲁棒性图", "radar", "不同噪声类型下的鲁棒性"],
        ["弹性偏差图", "bar", "偏差百分比分析"],
    ]
    create_table(doc, headers_ch, rows_ch)

    add_heading_styled(doc, "7.4 E6 退化预警 + E7 趋势看板", 2)
    headers_w = ["检测方式", "条件", "告警级别"]
    rows_w = [
        ["目标阈值兜底", "Context Precision < 0.80", "⚠️ 红色横幅"],
        ["目标阈值兜底", "Context Recall < 0.70", "⚠️ 红色横幅"],
        ["目标阈值兜底", "Faithfulness < 0.85", "⚠️ 红色横幅"],
        ["目标阈值兜底", "Hallucination > 0.10（反向）", "⚠️ 红色横幅"],
        ["趋势预警", "同一指标连续 2 次下降", "⚠️ 橙色横幅"],
    ]
    create_table(doc, headers_w, rows_w)

    # ========== 8. E2E Eval ==========
    add_heading_styled(doc, "8. E2E Eval 评测系统", 1)
    add_para(doc, "E2E 评测覆盖检索 → Context → LLM 生成 → 输出过滤的完整链路，不同于 Prompt 评测（只测 System Prompt）。", indent=True)
    headers_e = ["组件", "说明"]
    rows_e = [
        ["测试题目", "从 e2e_eval_items 表加载（默认 20 题，含标准答案）"],
        ["评测方法", "LLM-as-Judge：用 chat 卡片的 LLM 对回答进行评分"],
        ["评分维度", "Faithfulness（忠实于来源）、Completeness（完整覆盖知识点）"],
        ["输出", "score + 详细评语，写入 e2e_results 记录"],
        ["调用方式", "run_evaluation() → eval_llm.chat() → 评分"],
    ]
    create_table(doc, headers_e, rows_e)

    # ========== 9. 代码质量评分 ==========
    add_heading_styled(doc, "9. 代码质量综合评分", 1)
    add_para(doc, "项目经过四轮代码审查和两轮安全审计修复，综合评分从 25/45 提升至 43/45（+18）。", indent=True)
    headers_q = ["质量维度", "修复前", "修复后", "提升"]
    rows_q = [
        ["模块化（单一职责+高内聚）", "3", "5", "+2"],
        ["可维护性（可读性+可扩展性）", "2", "4", "+2"],
        ["安全性（注入防护+权限控制）", "2", "5", "+3"],
        ["错误处理（异常捕获+熔断+降级）", "4", "5", "+1"],
        ["代码规范（类型注解+命名+注释）", "3", "5", "+2"],
        ["测试覆盖（单元测试+回归测试）", "1", "3", "+2"],
        ["文档完整性", "4", "4", "0"],
        ["综合评分", "25/45", "43/45", "+18"],
    ]
    create_table(doc, headers_q, rows_q)

    # ========== 10. 认证与安全 ==========
    doc.add_page_break()
    add_heading_styled(doc, "10. 认证与安全", 1)

    add_heading_styled(doc, "10.1 七层安全防护", 2)
    headers_sec = ["防线", "层级", "机制"]
    rows_sec = [
        ["第一层", "用户输入", "提示注入检测（18 种正则模式）"],
        ["第二层", "用户输入", "渐进式越狱检测（铺垫行为识别）"],
        ["第三层", "检索文档", "文档内容注入过滤"],
        ["第四层", "System Prompt", "三条红线约束（来源/品牌/编造）"],
        ["第五层", "LLM 输出", "XSS 转义（html.escape）"],
        ["第六层", "LLM 输出", "品牌名过滤 + 价格命令过滤"],
        ["第七层", "LLM 输出", "来源标注审计（每行检查 [来源N]）"],
    ]
    create_table(doc, headers_sec, rows_sec)

    add_heading_styled(doc, "10.2 认证机制", 2)
    headers_a = ["认证方式", "说明"]
    rows_a = [
        ["Token 认证（auth.py）", "Bearer Token，启动生成"],
        ["管理后台登录", "用户名/密码验证"],
        ["API Key 加密", "Fernet 对称加密，启动完整性校验"],
        ["SSRF 防护", "LLM URL 白名单校验，禁止裸 IP 和内网地址"],
    ]
    create_table(doc, headers_a, rows_a)

    # ========== 11. 管理后台 ==========
    add_heading_styled(doc, "11. 管理后台功能一览", 1)
    headers_ad = ["模块", "功能", "页面/路由"]
    rows_ad = [
        ["总览看板", "系统概览 + E6 退化预警 + E7 趋势图", "admin.html 概览 Tab"],
        ["对话管理", "对话列表（日期分组）+ 详情（含评分+越狱）", "admin.html 对话 Tab"],
        ["文档管理", "文档上传/扫描/检索状态", "admin.html 文档 Tab"],
        ["前端模型", "前端大模型配置（6 个预设）", "admin.html 前端模型 Tab"],
        ["后端模型", "LLM 配置卡片 8 张", "admin.html 后端模型 Tab"],
        ["Prompt 评测", "20 题评测 + 弹性测试 + 版本管理", "admin.html 评测 Tab"],
        ["E2E 评测", "端到端全链路评测", "admin.html E2E Tab"],
        ["评测历史", "历史趋势对比", "admin.html 历史 Tab"],
        ["入库质量", "FAISS/Chroma 趋势图", "admin.html 入库质量 Tab"],
        ["检索质量", "Recall/MRR/Precision 监控", "admin.html 检索质量 Tab"],
        ["使用看板", "LLM 调用量/耗时趋势", "admin.html 使用看板 Tab"],
        ["配置管理", "系统配置项", "admin.html 配置 Tab"],
    ]
    create_table(doc, headers_ad, rows_ad)

    # ========== 12. 数据库设计 ==========
    add_heading_styled(doc, "12. 数据库设计", 1)
    add_para(doc, "系统使用单一 SQLite 数据库（agent_data/conversations.db，~5MB，WAL 模式）：", indent=True)
    headers_db = ["表名", "用途", "关键字段", "规模"]
    rows_db = [
        ["conversations", "对话会话", "id, title, jailbreak_status", "29 行"],
        ["messages", "聊天消息", "id, conv_id, role, content, rating", "300+ 行"],
        ["session_memory", "多轮记忆", "id, conversation_id, summary", "少量"],
        ["usage_logs", "使用日志", "id, action, duration", "少量"],
        ["llm_configs", "LLM 配置卡 8 张", "module_id, provider, model, base_url", "8 行"],
        ["e2e_eval_items", "E2E 测试题", "id, question, answer", "20 行"],
        ["prompt_test_items", "Prompt 测试题", "id, category, question", "20 行"],
        ["prompt_versions", "Prompt 版本", "id, version_name, content, is_active", "5 行"],
        ["prompt_test_results", "评测结果", "version_id, weighted_score", "2+ 行"],
        ["e2e_results", "E2E 评测结果", "eval_id, question_id, score", "少量"],
        ["llm_config_audit", "配置审计日志", "config_id, action, timestamp", "少量"],
        ["llm_provider_keys", "Provider API Key", "provider, api_key_enc", "少量"],
    ]
    create_table(doc, headers_db, rows_db)

    # ========== 13. 项目文件清单 ==========
    doc.add_page_break()
    add_heading_styled(doc, "13. 项目文件清单", 1)

    add_heading_styled(doc, "13.1 核心模块（packages/agent/src/）", 2)
    headers_fl = ["文件", "行数", "职责"]
    rows_fl = [
        ["agent.py", "~1,444", "Agent 核心：越狱检测 + RAG + LLM + 输出过滤"],
        ["main.py", "~50", "FastAPI 入口 + 路由注册 + 全局认证"],
        ["routes_api.py", "~1,000", "API 路由：57 个 handler"],
        ["routes_api_eval.py", "~400", "评测路由：21 个 handler"],
        ["routes_admin_pages.py", "~200", "管理后台页面路由"],
        ["memory.py", "~1,411", "数据层：SQLite CRUD + 会话管理"],
        ["llm_provider.py", "~100", "LLM 调用封装"],
        ["llm_config_manager.py", "~350", "LLM 配置管理 + SSRF 校验"],
        ["evaluation_matrix.py", "~150", "5 维度评分矩阵"],
        ["prompt_test_manager.py", "~300", "Prompt 测试调度"],
        ["prompt_tester.py", "~600", "Prompt 评分函数"],
        ["prompt_versions.py", "~200", "版本管理"],
        ["eval_e2e.py", "~750", "E2E 全链路评测"],
        ["eval_30_v3.py", "~750", "30 题评估框架"],
        ["auth.py", "~120", "Token 认证"],
        ["app_lifespan.py", "~30", "生命周期管理"],
        ["app_state.py", "~120", "启动初始化 + 全局状态"],
    ]
    create_table(doc, headers_fl, rows_fl)
    add_para(doc, "核心模块总计：17 个文件，约 7,000 行代码", bold=True, color=BLUE)

    add_heading_styled(doc, "13.2 预处理模块（packages/preprocessor/src/）", 2)
    headers_fl2 = ["文件", "行数", "职责"]
    rows_fl2 = [
        ["retriever.py", "~773", "CyberRetriever：多路 + RRF + Reranker"],
        ["deduplicator.py", "~350", "三层去重引擎"],
        ["odl_parser.py", "~300", "文档解析：ODL/docx/PyMuPDF"],
        ["llm_cleaner.py", "~250", "LLM 文档清洗"],
        ["build_parent_index.py", "~200", "父文档索引构建"],
        ["incremental_index.py", "~150", "增量索引"],
        ["index_batch.py", "~250", "批量索引"],
        ["rebuild_chroma.py", "~100", "Chroma 重建"],
        ["rebuild_faiss_only.py", "~100", "FAISS 重建"],
        ["test_full_pipeline.py", "~200", "全流水线测试"],
    ]
    create_table(doc, headers_fl2, rows_fl2)
    add_para(doc, "预处理模块总计：10 个文件，约 2,700 行代码", bold=True, color=BLUE)

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("— 报告完毕，v2.0 图文完整版 —")
    run.font.size = Pt(11)
    run.font.color.rgb = MED_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    # Save
    path = str(OUT_DIR / "整体功能说明书_v2.0_图文完整版.docx")
    doc.save(path)
    print(f"✅ 已生成: {path}")
    return path


# ================================================================
# 文档2：第二期规划方案
# ================================================================
def gen_phase2_plan():
    doc = Document()

    section = doc.sections[0]
    section.page_width = Cm(21)
    section.page_height = Cm(29.7)
    section.top_margin = Cm(2.5)
    section.bottom_margin = Cm(2.5)
    section.left_margin = Cm(2.5)
    section.right_margin = Cm(2.5)

    # ---- 封面 ----
    for _ in range(6):
        doc.add_paragraph()

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("网络安全移动运营商智能 Agent")
    run.font.size = Pt(26)
    run.font.bold = True
    run.font.color.rgb = DARK_BLUE
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("第二期规划方案")
    run.font.size = Pt(22)
    run.font.color.rgb = BLUE
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run('从「能用」到「好用」 · 从工具到平台')
    run.font.size = Pt(14)
    run.font.color.rgb = MED_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("2026-07-01")
    run.font.size = Pt(12)
    run.font.color.rgb = MED_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("机密等级：内部公开")
    run.font.size = Pt(10)
    run.font.color.rgb = RED
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    doc.add_page_break()

    # ========== 目录 ==========
    add_heading_styled(doc, "目录", 1)
    toc = [
        "1. 现状回顾与一期成果",
        "2. 需求分析与差距评估",
        "3. 二期总体目标",
        "4. 详细规划（四大方向）",
        "    4.1 方向A：知识库扩展与治理",
        "    4.2 方向B：多 Agent 架构演进",
        "    4.3 方向C：用户体验与平台化",
        "    4.4 方向D：生产化与运维体系",
        "5. 实施路线图与里程碑",
        "6. 资源估算与投入",
        "7. 风险评估与应对",
        "8. 成功标准（KPI）",
    ]
    for item in toc:
        add_para(doc, item, color=BLUE, size=11)

    doc.add_page_break()

    # ========== 1. 现状回顾 ==========
    add_heading_styled(doc, "1. 现状回顾与一期成果", 1)
    add_para(doc, "一期从 2026-05-23 至 2026-06-09，历时约 3 周，完成了从零到一的核心能力建设，实现了可交互、可评测、可追溯的网络安全 RAG Agent。", indent=True)

    add_heading_styled(doc, "1.1 一期核心成果", 2)
    headers_r = ["维度", "一期成果", "关键指标"]
    rows_r = [
        ["知识库", "117 份文档（4 大类）入库，7,316 chunks", "覆盖国家法律/等保/CII/通信行业"],
        ["检索管道", "Chroma + FAISS + BM25 三路召回 + Reranker", "Recall@5 ≥ 85%，P95 < 2s"],
        ["Agent 能力", "13 步全流程：注入检测→检索→生成→审计", "平均端到端延迟 5-6s"],
        ["对话管理", "SQLite 记忆 + 滑动窗口 5 轮 + 压缩", "上下文保留率 ≥ 90%"],
        ["安全防护", "7 层防护 + 三条红线 + 越狱检测", "零安全事故（上线至今）"],
        ["评测体系", "20 题 5 维度加权 + E2E LLM-as-Judge", "加权总分 > 85/100"],
        ["管理后台", "12 个功能 Tab + 6 张 ECharts 图表", "可视化运维"],
        ["代码质量", "四轮审查 + 两轮安全审计", "43/45（较初始 +18）"],
    ]
    create_table(doc, headers_r, rows_r)

    add_heading_styled(doc, "1.2 一期技术指标总览", 2)
    headers_t = ["指标", "一期达成", "行业基准", "评价"]
    rows_t = [
        ["知识库规模", "117 份 / 7,316 chunks", "—", "基础覆盖"],
        ["检索 Recall@5", "≥ 85%", "≥ 70%", "✅ 领先"],
        ["检索 P95 延迟", "~1,800ms", "< 2,000ms", "✅ 达标"],
        ["Context Precision", "≥ 0.82", "≥ 0.80", "✅ 达标"],
        ["Faithfulness", "≥ 0.87", "≥ 0.85", "✅ 达标"],
        ["代码质量评分", "43/45", "—", "优良"],
        ["安全事件", "0 起", "—", "✅ 零事故"],
    ]
    create_table(doc, headers_t, rows_t)

    # ========== 2. 差距评估 ==========
    doc.add_page_break()
    add_heading_styled(doc, "2. 需求分析与差距评估", 1)

    add_heading_styled(doc, "2.1 一期已覆盖领域 vs 二期待覆盖", 2)
    headers_g = ["领域", "一期状态", "二期目标", "优先级"]
    rows_g = [
        ["01-国家法律", "✅ 37 份已入库", "保持更新", "—"],
        ["02-等保国标", "✅ 13 份已入库", "增补等保3.0（发布后）", "低"],
        ["03-CII 关基", "✅ 13 份已入库", "保持更新", "—"],
        ["04-通信行业", "✅ 54 份已入库", "增补最新 YD 标准", "中"],
        ["05-数据安全", "❌ 未入库", "~15 份核心标准", "🔴 高"],
        ["06-网络安全体系", "❌ 未入库", "~15 份风险管理/应急/框架", "🔴 高"],
        ["07-APP 安全", "❌ 未入库", "~15 份 TTAF/隐私合规", "🔴 高"],
        ["08-漏洞管理", "❌ 未入库", "~8 份分类分级/管理规范", "🟡 中"],
        ["GDPR + 国际合规", "❌ 未入库", "GDPR 全文 + NIST 映射", "🟢 低"],
    ]
    create_table(doc, headers_g, rows_g)

    add_heading_styled(doc, "2.2 系统架构差距", 2)
    headers_arch = ["维度", "一期（现状）", "二期（目标）", "差距"]
    rows_arch = [
        ["系统架构", "单体 Agent", "多 Agent 协作（路由/检索/评估/对话）", "大"],
        ["知识库", "117 份，4 类", "~170 份，8+ 类", "中"],
        ["向量检索", "Chroma + FAISS", "支持多 Embedding 模型切换", "中"],
        ["用户访问", "单人管理后台", "多用户 + 角色权限（RBAC）", "大"],
        ["部署方式", "本地开发机", "Docker 容器化，支持内网部署", "中"],
        ["API 暴露", "无鉴权的内部 API", "完善鉴权 + 限流 + 审计日志", "中"],
        ["运维监控", "基础看板", "Prometheus + Grafana 集成", "中"],
        ["多轮对话", "5 轮滑动窗口", "长上下文（20+ 轮）智能压缩", "中"],
    ]
    create_table(doc, headers_arch, rows_arch)

    # ========== 3. 总体目标 ==========
    add_heading_styled(doc, "3. 二期总体目标", 1)
    add_para(doc, "二期核心目标：从「能用」到「好用」，从「工具」到「平台」。", bold=True, color=DARK_BLUE)

    goals = [
        ("知识库全覆盖", "扩展至 8 大领域 ~170 份文档，覆盖运营商网络安全管理全场景"),
        ("多 Agent 架构", "从单体 Agent 演进为多 Agent 协作体系（路由/检索/评估/对话）"),
        ("平台化体验", "管理后台升级为运维平台，支持多用户、角色权限、操作审计"),
        ("生产化就绪", "Docker 容器化部署、内网适配、完善的 SRE 运维体系"),
    ]
    for title, desc in goals:
        add_para(doc, f"🎯 {title}", bold=True, color=BLUE)
        add_para(doc, f"   {desc}", indent=True)

    # ========== 4. 详细规划 ==========
    doc.add_page_break()
    add_heading_styled(doc, "4. 详细规划（四大方向）", 1)
    add_para(doc, "二期规划分为四个并行方向，每个方向包含若干子任务，按优先级和依赖关系排列。", indent=True)

    # ---- 4.1 知识库扩展 ----
    add_heading_styled(doc, "4.1 方向A：知识库扩展与治理", 2)
    add_para(doc, "预计投入：2-3 人周  |  优先级：P0", bold=True, color=GREEN)

    add_heading_styled(doc, "A-1 数据安全领域入库（P0）", 3)
    headers_a1 = ["标准编号", "标准名称", "来源"]
    rows_a1 = [
        ["GB/T 37988-2019", "DSMM 数据安全能力成熟度模型", "数据安全"],
        ["GB/T 41479-2022", "网络数据处理安全要求", "数据安全"],
        ["GB/T 43697-2024", "数据分类分级规则", "数据安全"],
        ["GB/T 35273-2020", "个人信息安全规范", "数据安全"],
        ["GB/T 45577-2025", "数据安全风险评估方法", "数据安全"],
        ["GB/T 20984-2022", "信息安全风险评估方法", "数据安全"],
        ["GB/T 36073-2018", "数据出境安全评估办法", "数据安全"],
    ]
    create_table(doc, headers_a1, rows_a1)

    add_heading_styled(doc, "A-2 网络安全体系领域入库（P0）", 3)
    headers_a2 = ["标准编号", "标准名称"]
    rows_a2 = [
        ["GB/T 38645-2020", "信息安全技术 网络安全应急响应指南"],
        ["GB/T 32916-2016", "信息安全技术 网络安全应急响应组管理规范"],
        ["ISO 27001:2022", "信息安全管理体系 要求（映射分析）"],
        ["GB/T 22080-2016", "信息安全管理体系 要求（等同 ISO 27001）"],
        ["SDL 安全开发生命周期", "软件安全开发管理体系"],
    ]
    create_table(doc, headers_a2, rows_a2)

    add_heading_styled(doc, "A-3 APP 安全领域入库（P0-P1）", 3)
    headers_a3 = ["标准编号", "标准名称"]
    rows_a3 = [
        ["TTAF 077", "移动互联网应用程序（APP）安全检测规范"],
        ["TTAF 078", "移动互联网应用程序（APP）个人信息保护检测规范"],
        ["GB/T 41391-2022", "移动互联网应用程序收集个人信息基本要求"],
        ["YD/T 4177", "移动互联网应用程序安全加固技术要求"],
    ]
    create_table(doc, headers_a3, rows_a3)

    add_heading_styled(doc, "A-4 知识库治理工具（P1）", 3)
    add_bullet(doc, "知识库健康度仪表盘：chunk 分布、覆盖缺口、更新频率")
    add_bullet(doc, "增量更新工具：支持新增/修改/删除单篇文档，无需全量重建")
    add_bullet(doc, "版本化知识库：文档版本追踪，回滚支持")
    add_bullet(doc, "冲突检测 Web UI：拖入文件自动扫描，在线解决重名冲突")

    # ---- 4.2 多 Agent 架构 ----
    doc.add_page_break()
    add_heading_styled(doc, "4.2 方向B：多 Agent 架构演进", 2)
    add_para(doc, "预计投入：3-4 人周  |  优先级：P1", bold=True, color=GREEN)

    add_heading_styled(doc, "B-1 架构设计", 3)
    add_para(doc, "从单体 Agent（agent.py）拆分为多 Agent 协作体系：", indent=True)

    agents_desc = [
        ("路由 Agent（Router Agent）", "意图识别 → 分发到下游 Agent。分析用户问题类型（等保/数据安全/CII/对话/未知），路由到对应专业 Agent。支持未知问题的兜底与追问。"),
        ("检索 Agent（Retrieval Agent）", "专注多路检索策略。可根据问题类型动态调整检索参数（top_k、检索源、Reranker 开关）。支持多轮检索的自省纠错（先检索→判断是否足够→不够再检索）。"),
        ("评估 Agent（Evaluation Agent）", "专注回答质量自检。在回答返回前进行 Faithfulness/Relevancy/Hallucination 三级评分，低于阈值时触发重新生成。"),
        ("对话 Agent（Conversation Agent）", "专注多轮对话管理。长上下文智能压缩、关键信息提取（用户角色/提及标准/偏好）。跨会话记忆持久化。"),
    ]
    for title, desc in agents_desc:
        add_para(doc, f"• {title}", bold=True)
        add_para(doc, f"  {desc}", indent=True)

    add_heading_styled(doc, "B-2 多 Agent 协作流程", 3)
    add_para(doc, "用户提问 → Router Agent（意图识别）→ Retrieval Agent（检索）→ Conversation Agent（上下文构建）→ LLM 生成 → Evaluation Agent（质量自检）→ 通过则返回，不通过则重生成或降级", indent=True)

    add_heading_styled(doc, "B-3 Agent 质量评测（域E E6/E7 扩展）", 3)
    add_para(doc, "多 Agent 需要配套的质量评估体系：", indent=True)
    add_bullet(doc, "路由准确率：Router Agent 意图识别正确率，目标 ≥ 90%")
    add_bullet(doc, "自省纠错率：检索 Agent 识别到信息不足并主动补充检索的比例")
    add_bullet(doc, "重生成触发率：Evaluation Agent 触发重新生成的频次与准确率")
    add_bullet(doc, "端到端延迟分解：按 Agent 维度统计耗时分布")

    # ---- 4.3 平台化 ----
    add_heading_styled(doc, "4.3 方向C：用户体验与平台化", 2)
    add_para(doc, "预计投入：2-3 人周  |  优先级：P1", bold=True, color=GREEN)

    add_heading_styled(doc, "C-1 多用户与 RBAC", 3)
    add_bullet(doc, "用户注册/登录（JWT/Session 鉴权）")
    add_bullet(doc, "角色：管理员、安全运营人员、只读查看者")
    add_bullet(doc, "基于角色的 API 权限控制")
    add_bullet(doc, "操作审计日志（谁做了什么、什么时候）")

    add_heading_styled(doc, "C-2 对话体验升级", 3)
    add_bullet(doc, "长上下文支持（20+ 轮对话）：智能压缩策略，关键信息不丢失")
    add_bullet(doc, "追问引导：Agent 主动追问模糊点，提升首次回答准确率")
    add_bullet(doc, "多轮对话评测：评估多轮场景下的知识连贯性")
    add_bullet(doc, "消息搜索：全文搜索历史对话内容")

    add_heading_styled(doc, "C-3 管理后台增强", 3)
    add_bullet(doc, "知识库管理页面：文档增删改查、状态追踪")
    add_bullet(doc, "知识库在线编辑：直接修改切片内容并重建索引")
    add_bullet(doc, "对话统计报表：按领域/时段/用户维度的使用分析")
    add_bullet(doc, "自定义评测测试集：管理员可在线编辑测试题")
    add_bullet(doc, "Word 报告导出：评测报告、质量报告一键导出")

    # ---- 4.4 生产化 ----
    add_heading_styled(doc, "4.4 方向D：生产化与运维体系", 2)
    add_para(doc, "预计投入：2-3 人周  |  优先级：P2", bold=True, color=GREEN)

    add_heading_styled(doc, "D-1 容器化部署", 3)
    add_bullet(doc, "Docker Compose 编排：agent / preprocessor / ollama / chroma 四个容器")
    add_bullet(doc, "环境变量配置化：所有配置通过 .env 管理")
    add_bullet(doc, "数据卷挂载：SQLite + FAISS 索引持久化")
    add_bullet(doc, "健康检查 + 自动重启")

    add_heading_styled(doc, "D-2 监控与告警", 3)
    add_bullet(doc, "Prometheus 指标暴露：请求量/延迟/错误率/熔断状态")
    add_bullet(doc, "Grafana 看板：系统健康度 + RAG 质量概览")
    add_bullet(doc, "退化自动告警：E6 指标触发时发送钉钉/企业微信通知")
    add_bullet(doc, "日志聚合：结构化日志（JSON 格式），支持 ELK/Loki 接入")

    add_heading_styled(doc, "D-3 性能与可靠性", 3)
    add_bullet(doc, "Reranker 缓存：相同 query 的 Reranker 结果缓存 5 分钟")
    add_bullet(doc, "检索结果缓存：top-K 结果缓存，减少重复计算")
    add_bullet(doc, "API 限流：基于用户/角色的速率限制")
    add_bullet(doc, "批量导入性能优化：单批处理从 20 份提升到 50 份")

    # ========== 5. 路线图 ==========
    doc.add_page_break()
    add_heading_styled(doc, "5. 实施路线图与里程碑", 1)

    add_heading_styled(doc, "5.1 总体时间线", 2)
    headers_tl = ["阶段", "时间", "核心任务", "交付物"]
    rows_tl = [
        ["Phase A", "第 1-2 周", "知识库扩展（4 大领域入库）", "~170 份文档 / ~12,000 chunks"],
        ["Phase B", "第 2-4 周", "多 Agent 架构设计 + 实现", "路由/检索/评估/对话 4 个 Agent"],
        ["Phase C", "第 3-5 周", "用户体验升级 + 管理后台增强", "多用户 + RBAC + 长上下文"],
        ["Phase D", "第 5-6 周", "容器化部署 + 监控体系搭建", "Docker Compose + Prometheus/Grafana"],
        ["Phase E", "第 6-7 周", "集成测试 + 性能优化 + 文档完善", "全链路回归通过 + 部署手册"],
        ["发布", "第 8 周", "上线验收 + 用户培训", "v2.0 正式版本"],
    ]
    create_table(doc, headers_tl, rows_tl)

    add_heading_styled(doc, "5.2 里程碑定义", 2)
    headers_m = ["里程碑", "时间", "验收标准"]
    rows_m = [
        ["M1: 知识库就绪", "第 2 周末", "8 大领域全部入库，Retrieval Recall@5 ≥ 85%"],
        ["M2: 多 Agent 可用", "第 4 周末", "Router 准确率 ≥ 90%，Eval 召回率 ≥ 95%"],
        ["M3: 平台化完成", "第 5 周末", "多用户登录正常，RBAC 权限控制有效"],
        ["M4: 生产化就绪", "第 6 周末", "Docker 部署成功，监控告警正常"],
        ["M5: 正式发布", "第 8 周末", "全量回归通过，文档完备，用户培训完成"],
    ]
    create_table(doc, headers_m, rows_m)

    # ========== 6. 资源估算 ==========
    add_heading_styled(doc, "6. 资源估算与投入", 1)

    add_heading_styled(doc, "6.1 人员投入", 2)
    headers_res = ["角色", "人数", "投入时间", "职责"]
    rows_res = [
        ["后端开发（Python/FastAPI）", "1", "8 周全职", "Agent 架构 + API + 检索管道"],
        ["前端开发（HTML/JS/ECharts）", "1", "4 周（兼职）", "管理后台 + 平台化 UI"],
        ["安全专家（领域知识）", "1", "2 周（兼职）", "知识库范围确认 + 质量验收"],
        ["运维工程师", "1", "2 周（兼职）", "Docker + 监控部署"],
    ]
    create_table(doc, headers_res, rows_res)

    add_heading_styled(doc, "6.2 成本估算", 2)
    headers_cost = ["项目", "月费用", "说明"]
    rows_cost = [
        ["SiliconFlow API", "~¥500/月", "LLM + Reranker 调用（按量计费）"],
        ["Ollama 服务器", "已有", "本地 CPU 推理，无额外费用"],
        ["云服务器（可选）", "~¥300/月", "内网部署用低配 ECS"],
        ["域名/SSL（可选）", "~¥100/年", "HTTPS 证书"],
        ["合计", "~¥800/月", "不含人力成本"],
    ]
    create_table(doc, headers_cost, rows_cost)

    # ========== 7. 风险评估 ==========
    doc.add_page_break()
    add_heading_styled(doc, "7. 风险评估与应对", 1)

    headers_risk = ["风险", "概率", "影响", "应对措施"]
    rows_risk = [
        ["多 Agent 架构过度设计", "中", "中", "MVP 先实现 Router + Eval 两个 Agent，其余按需"],
        ["知识库文档版权/可用性问题", "低", "高", "仅使用公开标准文件，确认来源合规"],
        ["Docker 部署性能下降", "中", "中", "前期做性能基线对比，提前优化关键路径"],
        ["多用户场景下 SQLite 并发瓶颈", "低", "中", "评估是否迁移到 PostgreSQL 或保持 WAL 模式优化"],
        ["团队成员中途变动", "低", "高", "文档驱动开发，关键模块有交接文档"],
        ["等保 3.0 标准发布产生冲击", "低", "中", "保持知识库版本化，支持快速切换"],
    ]
    create_table(doc, headers_risk, rows_risk)

    # ========== 8. KPI ==========
    add_heading_styled(doc, "8. 成功标准（KPI）", 1)

    headers_kpi = ["指标", "一期基线", "二期目标"]
    rows_kpi = [
        ["知识库文档数", "117 份", "≥ 170 份"],
        ["知识库覆盖领域", "4 大类", "8+ 大类"],
        ["检索 Recall@5", "≥ 85%", "≥ 90%"],
        ["RAG Faithfulness", "≥ 0.87", "≥ 0.90"],
        ["端到端延迟 P95", "~6s", "≤ 4s（含 Reranker）"],
        ["多 Agent 路由准确率", "—", "≥ 90%"],
        ["多用户支持", "1 人", "≥ 10 人同时在线"],
        ["容器化部署", "❌", "✅ Docker Compose"],
        ["监控告警", "❌", "✅ Prometheus + Grafana"],
        ["代码质量评分", "43/45", "≥ 44/45"],
        ["安全事件", "0 起", "0 起（继续保持）"],
    ]
    create_table(doc, headers_kpi, rows_kpi)

    # 结尾
    doc.add_paragraph()
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = p.add_run("— 第二期规划方案完 —")
    run.font.size = Pt(12)
    run.font.color.rgb = MED_GRAY
    run.font.name = "微软雅黑"
    r = run._element
    r.rPr.rFonts.set(qn('w:eastAsia'), '微软雅黑')

    path = str(OUT_DIR / "第二期规划方案.docx")
    doc.save(path)
    print(f"✅ 已生成: {path}")
    return path


# ================================================================
if __name__ == "__main__":
    print("正在生成整体功能说明书 v2.0...")
    gen_feature_spec()
    print("正在生成第二期规划方案...")
    gen_phase2_plan()
    print(f"\n✅ 两份文档均已生成到: {OUT_DIR}")
    print("  1. 整体功能说明书_v2.0_图文完整版.docx")
    print("  2. 第二期规划方案.docx")