"""
Generate professional diagrams v3 - increased font sizes, fixed overlaps, added LLM flow chart
"""
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from pathlib import Path

OUT = Path(__file__).parent.parent / "docs" / "images"
OUT.mkdir(parents=True, exist_ok=True)

C_BLUE = '#1A73E8'; C_LIGHT_BLUE = '#E8F0FE'; C_DARK_BLUE = '#0D47A1'
C_GREEN = '#28A745'; C_LIGHT_GREEN = '#D4EDDA'
C_ORANGE = '#F0AD4E'; C_LIGHT_ORANGE = '#FFF3CD'
C_RED = '#DC3545'; C_LIGHT_RED = '#F8D7DA'
C_DARK = '#333333'; C_MED = '#666666'; C_WHITE = '#FFFFFF'

plt.rcParams.update({'font.family': 'Microsoft YaHei', 'font.size': 14, 'axes.unicode_minus': False})

def add_box(ax, x, y, w, h, text, color=C_BLUE, text_color=C_WHITE, fontsize=14, sub_text=None):
    box = FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.12", facecolor=color, edgecolor='none', zorder=2)
    ax.add_patch(box)
    ax.text(x + w/2, y + h/2, text, ha='center', va='center', fontsize=fontsize, color=text_color, fontweight='bold', zorder=3)
    if sub_text:
        ax.text(x + w/2, y + h/2 - h*0.2, sub_text, ha='center', va='top', fontsize=fontsize-1, color=text_color, alpha=0.9, zorder=3)

def add_arrow(ax, x1, y1, x2, y2, color=C_MED, lw=2):
    ax.annotate('', xy=(x2, y2), xytext=(x1, y1), arrowprops=dict(arrowstyle='->', color=color, lw=lw, mutation_scale=22), zorder=1)

def add_box_outline(ax, x, y, w, h, text, color=C_LIGHT_BLUE, border=C_BLUE, fontsize=13):
    rect = plt.Rectangle((x, y), w, h, facecolor=color, edgecolor=border, linewidth=2, zorder=2)
    ax.add_patch(rect)
    ax.text(x + w/2, y + h/2, text, ha='center', va='center', fontsize=fontsize, color=C_DARK, fontweight='bold', zorder=3)


# ================================================================
# 1. Architecture Overview (FIXED: bigger boxes, bigger fonts)
# ================================================================
def draw_architecture():
    fig, ax = plt.subplots(figsize=(20, 12))
    ax.set_xlim(0, 20); ax.set_ylim(0, 12); ax.axis('off')
    ax.text(10, 11.6, '网络安全智能 Agent — 系统架构图', ha='center', fontsize=24, fontweight='bold', color=C_DARK_BLUE)

    # User layer
    add_box(ax, 0.5, 9.8, 8.0, 1.4, '用户接入层', C_BLUE, C_WHITE, 17)
    add_box(ax, 1.0, 10.0, 3.5, 1.0, '聊天界面\nchat.html + SSE 流式', C_LIGHT_BLUE, C_DARK, 14)
    add_box(ax, 4.9, 10.0, 3.3, 1.0, '管理后台\nadmin.html + ECharts', C_LIGHT_BLUE, C_DARK, 14)

    # Gateway
    add_box(ax, 0.5, 7.6, 8.0, 1.4, 'FastAPI 网关层', C_GREEN, C_WHITE, 17)
    add_box(ax, 1.0, 7.8, 3.5, 1.0, 'API 路由\n57 个处理器', C_LIGHT_GREEN, C_DARK, 14)
    add_box(ax, 4.9, 7.8, 3.3, 1.0, '认证中间件\nToken + 密码', C_LIGHT_GREEN, C_DARK, 14)
    add_arrow(ax, 2.8, 9.8, 2.8, 9.2); add_arrow(ax, 6.6, 9.8, 6.6, 9.2)

    # Agent core
    add_box(ax, 0.5, 5.4, 8.0, 1.7, 'Agent 核心层', C_ORANGE, C_WHITE, 17)
    items = [('CyberAgent\nagent.py (1,444行)', 0.8, 6.0), ('对话记忆\nmemory.py', 2.8, 6.0),
             ('LLM 配置\n8 张配置卡', 4.8, 6.0), ('系统提示词\n三级 + 热加载', 0.8, 5.5),
             ('输出过滤\n品牌+价格+XSS', 2.8, 5.5), ('越狱检测\n18 种模式', 4.8, 5.5)]
    for t, x, y in items:
        add_box(ax, x, y, 1.8, 0.6, t, C_LIGHT_ORANGE, C_DARK, 12)
    add_arrow(ax, 4.5, 7.6, 4.5, 7.1)

    # RAG retrieval
    add_box(ax, 9.5, 5.4, 10.0, 1.7, 'RAG 检索层 — CyberRetriever', C_BLUE, C_WHITE, 17, '多路召回 → RRF → Reranker → 父文档')
    rag = [('FAISS\n7,316 向量', 9.8, 6.0), ('ChromaDB\n7,316 分块', 12.2, 6.0),
           ('BM25\njieba 分词', 14.6, 6.0), ('Reranker\nbge-reranker-v2-m3', 17.0, 6.0)]
    for t, x, y in rag:
        add_box(ax, x, y, 2.0, 0.6, t, C_LIGHT_BLUE, C_DARK, 12)

    # Preprocessing
    add_box(ax, 9.5, 3.0, 10.0, 1.7, '文档预处理层', C_RED, C_WHITE, 17)
    pre = [('3 层去重\nDeduplicator', 9.8, 3.6), ('3 引擎解析\nODL/docx/MuPDF', 12.2, 3.6),
           ('LLM 清洗\nMiniMax-M2.5', 14.6, 3.6), ('分块+嵌入\nBGE Embedding', 17.0, 3.6)]
    for t, x, y in pre:
        add_box(ax, x, y, 2.0, 0.6, t, C_LIGHT_RED, C_DARK, 12)

    # Eval
    add_box(ax, 9.5, 1.2, 10.0, 1.3, '质量评估层', C_GREEN, C_WHITE, 17)
    ev = [('Prompt 评估\n5 维评分', 10.0, 1.6), ('端到端评估\nLLM 裁判', 13.0, 1.6), ('版本管理\nA/B 测试', 16.0, 1.6)]
    for t, x, y in ev:
        add_box(ax, x, y, 2.5, 0.6, t, C_LIGHT_GREEN, C_DARK, 12)

    add_arrow(ax, 4.5, 5.4, 4.5, 5.0); add_arrow(ax, 11.0, 5.4, 11.0, 5.0)
    add_arrow(ax, 11.0, 3.0, 11.0, 2.7); add_arrow(ax, 11.0, 1.2, 11.0, 0.9)

    for i, (c, t) in enumerate([(C_BLUE, '用户/检索'), (C_GREEN, '网关/评估'), (C_ORANGE, 'Agent'), (C_RED, '预处理')]):
        ax.add_patch(plt.Rectangle((1.0 + i*4.0, 0.15), 0.7, 0.4, facecolor=c, zorder=2))
        ax.text(1.9 + i*4.0, 0.35, t, fontsize=13, color=C_DARK)
    ax.text(10, 0.05, '数据流向：从上到下', ha='center', fontsize=12, color=C_MED)
    plt.tight_layout(); plt.savefig(str(OUT / '01_system_architecture.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[1/7] {OUT / "01_system_architecture.png"}')


# ================================================================
# 2. QA Flow
# ================================================================
def draw_qa_flow():
    fig, ax = plt.subplots(figsize=(18, 12))
    ax.set_xlim(0, 18); ax.set_ylim(0, 12); ax.axis('off')
    ax.text(9, 11.6, '用户提问 → 回答流水线（13 步）', ha='center', fontsize=22, fontweight='bold', color=C_DARK_BLUE)

    # Left: security + retrieval
    left_steps = [
        ('[01] Prompt 注入检测', '18 条正则模式'),
        ('[02] 渐进式越狱检测', '行为模式识别'),
        ('[03] 对话记忆', 'SQLite 多轮上下文'),
        ('[04] 查询改写（可选）', 'LLM 改写为关键词'),
    ]
    for i, (t, d) in enumerate(left_steps):
        y = 10.0 - i * 1.6
        add_box_outline(ax, 0.5, y, 7.0, 1.0, f'{t}\n{d}', C_LIGHT_RED, C_RED, 13)
        if i < len(left_steps)-1:
            add_arrow(ax, 4.0, y-0.05, 4.0, y-1.1)

    add_box_outline(ax, 0.5, 3.4, 7.0, 1.1, '[05] 多路检索\nChroma + FAISS + BM25 并行 + Reranker', C_LIGHT_GREEN, C_GREEN, 14)
    add_arrow(ax, 4.0, 3.4, 4.0, 2.9)
    add_box_outline(ax, 0.5, 1.8, 7.0, 1.0, '[06] 父文档注入 + RRF 融合\n标准 ID 匹配 + k=60 排序', C_LIGHT_GREEN, C_GREEN, 13)

    # Right: processing
    right_steps = [
        ('[07] RRF 融合排序', '3 路融合 k=60'),
        ('[08] Reranker 重排序', 'bge-reranker-v2-m3'),
        ('[09] 上下文组装', 'build_context()'),
        ('[10] 提示词构建', '三级架构'),
        ('[11] LLM 生成', 'DeepSeek-V4-Flash'),
        ('[12] 输出过滤', '品牌 + 价格 + XSS'),
        ('[13] 来源审计', '逐行检查 [SourceN]'),
    ]
    for i, (t, d) in enumerate(right_steps):
        y = 10.0 - i * 1.2
        add_box_outline(ax, 9.5, y, 7.5, 0.8, f'{t}  {d}', C_LIGHT_BLUE, C_BLUE, 14)
        if i < len(right_steps)-1:
            add_arrow(ax, 13.25, y-0.05, 13.25, y-0.65)

    ax.annotate('', xy=(9.3, 6.2), xytext=(7.7, 6.2), arrowprops=dict(arrowstyle='->', color=C_BLUE, lw=2.5), zorder=1)
    ax.text(8.5, 6.4, '检索结果', ha='center', fontsize=12, color=C_BLUE, fontweight='bold')
    add_box(ax, 0.5, 0.2, 17.0, 0.7, '输入安全: ~15ms  |  检索: ~1.8s  |  Reranker: ~1.5s  |  LLM 生成: ~3s  |  端到端总计: ~5-6s', C_DARK, C_WHITE, 13)

    plt.tight_layout(); plt.savefig(str(OUT / '02_qa_flow.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[2/7] {OUT / "02_qa_flow.png"}')


# ================================================================
# 3. RAG Pipeline
# ================================================================
def draw_rag_pipeline():
    fig, ax = plt.subplots(figsize=(18, 9))
    ax.set_xlim(0, 18); ax.set_ylim(0, 9); ax.axis('off')
    ax.text(9, 8.6, 'RAG 检索流水线 — 4 阶段架构', ha='center', fontsize=22, fontweight='bold', color=C_DARK_BLUE)

    add_box(ax, 0.3, 6.3, 3.0, 1.0, '用户查询', C_DARK_BLUE, C_WHITE, 14)
    add_box_outline(ax, 3.8, 6.0, 4.5, 0.8, '阶段 1：多路召回', C_LIGHT_BLUE, C_BLUE, 14)
    for i, (t, x) in enumerate([('ChromaDB', 4.0), ('FAISS', 5.7), ('BM25', 7.4)]):
        add_box(ax, x, 4.6, 1.5, 0.8, t, C_LIGHT_BLUE, C_DARK, 12)

    add_box_outline(ax, 9.5, 6.0, 3.5, 0.8, '阶段 2：RRF 融合', C_LIGHT_ORANGE, C_ORANGE, 14)
    add_box(ax, 9.5, 4.6, 3.5, 0.8, '得分 = Σ 1/(k+rank)\nk=60, 去重', C_LIGHT_ORANGE, C_DARK, 12)

    add_box_outline(ax, 13.8, 6.0, 3.8, 0.8, '阶段 3：Reranker', C_LIGHT_GREEN, C_GREEN, 14)
    add_box(ax, 13.8, 4.6, 3.8, 0.8, 'bge-reranker-v2-m3\n熔断保护', C_LIGHT_GREEN, C_DARK, 12)

    add_box_outline(ax, 13.8, 2.7, 3.8, 0.8, '阶段 4：父文档聚合', C_LIGHT_RED, C_RED, 14)
    add_box(ax, 13.8, 1.2, 3.8, 0.9, '按 parent_id 分组\n每组最高分 → Top-K', C_LIGHT_RED, C_DARK, 12)

    add_box(ax, 0.3, 1.2, 3.0, 1.0, '最终结果\nTop-K 文档', C_DARK_BLUE, C_WHITE, 14)
    add_arrow(ax, 3.3, 6.8, 3.8, 6.4)
    add_arrow(ax, 6.1, 4.6, 9.5, 5.0); add_arrow(ax, 13.0, 5.0, 13.8, 5.0)
    add_arrow(ax, 15.7, 4.6, 15.7, 3.5); add_arrow(ax, 15.7, 2.7, 15.7, 2.1)
    add_arrow(ax, 13.8, 1.2, 3.3, 1.7)

    add_box_outline(ax, 0.3, 3.1, 5.0, 0.7, '负向检索：否定检测 + 惩罚', C_LIGHT_ORANGE, C_ORANGE, 12)
    add_arrow(ax, 1.8, 6.3, 1.8, 3.8)
    add_box_outline(ax, 5.8, 3.1, 5.5, 0.7, '文档预过滤：标准 ID 提取 + 分类', C_LIGHT_GREEN, C_GREEN, 12)
    add_arrow(ax, 6.5, 4.6, 8.5, 3.8)

    for i, m in enumerate(['Recall@5 >= 85%', 'MRR >= 0.65', 'CP >= 0.82', 'CR >= 0.75', 'P95 < 2s']):
        add_box(ax, 0.5 + i*3.2, 0.1, 2.8, 0.5, m, C_GREEN, C_WHITE, 11)

    plt.tight_layout(); plt.savefig(str(OUT / '03_rag_pipeline.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[3/7] {OUT / "03_rag_pipeline.png"}')


# ================================================================
# 4. Preprocessing
# ================================================================
def draw_preprocessing():
    fig, ax = plt.subplots(figsize=(18, 8))
    ax.set_xlim(0, 18); ax.set_ylim(0, 8); ax.axis('off')
    ax.text(9, 7.6, '文档预处理流水线', ha='center', fontsize=22, fontweight='bold', color=C_DARK_BLUE)
    ax.text(9, 7.1, '117 篇源文档 → 7,316 个分块 → 可搜索向量索引', ha='center', fontsize=13, color=C_MED)

    add_box(ax, 0.3, 5.8, 3.0, 0.9, '源文档\nPDF / DOC / DOCX', C_DARK_BLUE, C_WHITE, 14)
    add_box_outline(ax, 3.8, 5.5, 4.0, 0.7, '阶段 1：3 层去重', C_LIGHT_RED, C_RED, 13)
    for i, (t, x) in enumerate([('L1 硬去重\n毫秒级', 4.0), ('L2 SimHash\n秒级', 5.5), ('L3 语义\n嵌入后', 7.0)]):
        add_box(ax, x, 4.3, 1.3, 0.7, t, C_LIGHT_RED, C_DARK, 12)

    add_box_outline(ax, 8.5, 5.5, 4.0, 0.7, '阶段 2：3 引擎解析', C_LIGHT_ORANGE, C_ORANGE, 13)
    for i, (t, x) in enumerate([('ODL\n复杂 PDF', 8.7), ('python-docx\nWord 文件', 10.2), ('PyMuPDF\n普通 PDF', 11.7)]):
        add_box(ax, x, 4.3, 1.3, 0.7, t, C_LIGHT_ORANGE, C_DARK, 12)

    add_box_outline(ax, 13.2, 5.5, 4.5, 0.7, '阶段 3：LLM 清洗', C_LIGHT_GREEN, C_GREEN, 13)
    add_box(ax, 13.4, 4.3, 4.1, 0.7, 'MiniMax-M2.5\nTemp=0.1, JSON 输出', C_LIGHT_GREEN, C_DARK, 12)

    add_box_outline(ax, 3.8, 2.7, 6.5, 0.7, '阶段 4：分块 → 嵌入 → 存储', C_LIGHT_BLUE, C_BLUE, 13)
    for i, (t, x) in enumerate([('分块 300t+50t', 4.0), ('BGE-small-zh\n512 维', 5.9), ('ChromaDB\n持久化', 7.8)]):
        add_box(ax, x, 1.5, 1.7, 0.7, t, C_LIGHT_BLUE, C_DARK, 12)

    add_box(ax, 11.8, 2.5, 5.8, 1.0, '输出产物', C_DARK_BLUE, C_WHITE, 14)
    for i, o in enumerate(['FAISS: 7,316 向量', 'Chroma: 7,316 分块', 'BM25: 词索引', 'parent_texts.json']):
        ax.text(12.0, 2.0 - i*0.25, f'  * {o}', fontsize=12, color=C_DARK)

    add_arrow(ax, 3.3, 6.25, 3.8, 5.85); add_arrow(ax, 7.8, 4.65, 8.5, 5.85)
    add_arrow(ax, 12.5, 4.65, 13.2, 5.85); add_arrow(ax, 15.45, 4.3, 15.45, 3.4); add_arrow(ax, 10.5, 2.7, 10.5, 2.2)

    for i, (t1, t2) in enumerate([('117 文档', '4 个分类'), ('7,316 分块', '512 维向量'), ('~18 分钟总计', '6 批次')]):
        add_box(ax, 0.5 + i*3.5, 0.2, 3.0, 0.6, f'{t1}\n{t2}', C_GREEN, C_WHITE, 12)

    plt.tight_layout(); plt.savefig(str(OUT / '04_preprocessing.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[4/7] {OUT / "04_preprocessing.png"}')


# ================================================================
# 5. Security Layers
# ================================================================
def draw_security():
    fig, ax = plt.subplots(figsize=(16, 10))
    ax.set_xlim(0, 16); ax.set_ylim(0, 10); ax.axis('off')
    ax.text(8, 9.6, '7 层安全防护体系', ha='center', fontsize=22, fontweight='bold', color=C_DARK_BLUE)
    ax.text(8, 9.1, '从用户输入到 LLM 输出的全覆盖防护', ha='center', fontsize=13, color=C_MED)

    layers = [
        ('L1', '用户输入', 'Prompt 注入检测', '18 条正则模式', C_LIGHT_RED, C_RED),
        ('L2', '用户输入', '渐进式越狱检测', '行为模式识别', C_LIGHT_RED, C_RED),
        ('L3', '检索文档', '文档内容注入过滤', '可疑模式过滤', C_LIGHT_ORANGE, C_ORANGE),
        ('L4', '系统提示词', '三条红线约束', '来源/品牌/杜撰', C_LIGHT_ORANGE, C_ORANGE),
        ('L5', 'LLM 输出', 'XSS 转义', 'html.escape', C_LIGHT_GREEN, C_GREEN),
        ('L6', 'LLM 输出', '品牌+价格过滤', '正则替换', C_LIGHT_GREEN, C_GREEN),
        ('L7', 'LLM 输出', '来源标注审计', '逐行 [SourceN] 检查', C_LIGHT_BLUE, C_BLUE),
    ]

    for i, (num, layer, mechanism, detail, bg, border) in enumerate(layers):
        y = 7.8 - i * 0.9
        add_box(ax, 0.5, y, 1.3, 0.65, num, border, C_WHITE, 13)
        add_box_outline(ax, 2.2, y, 2.5, 0.65, layer, bg, border, 13)
        add_box_outline(ax, 5.2, y, 3.5, 0.65, mechanism, bg, border, 13)
        add_box_outline(ax, 9.2, y, 3.5, 0.65, detail, C_WHITE, border, 13)
        if i < len(layers)-1:
            add_arrow(ax, 8.0, y-0.05, 8.0, y-0.55)

    for t, c, y in [('请求侧', C_RED, 7.5), ('处理侧', C_ORANGE, 5.7), ('响应侧', C_GREEN, 3.9)]:
        ax.add_patch(plt.Rectangle((13.2, y-0.1), 2.3, 0.4, facecolor=c, alpha=0.2, edgecolor=c, linewidth=1.5, zorder=2))
        ax.text(14.35, y+0.1, t, ha='center', fontsize=12, fontweight='bold', color=c)

    for i, (t, c) in enumerate([('零安全事件', C_GREEN), ('来源标注率 100%', C_BLUE), ('品牌过滤率 100%', C_ORANGE)]):
        add_box(ax, 1.5 + i*4.5, 0.2, 4.0, 0.7, t, c, C_WHITE, 14)

    plt.tight_layout(); plt.savefig(str(OUT / '05_security_layers.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[5/7] {OUT / "05_security_layers.png"}')


# ================================================================
# 6. Evaluation Framework
# ================================================================
def draw_eval():
    fig, ax = plt.subplots(figsize=(18, 9))
    ax.set_xlim(0, 18); ax.set_ylim(0, 9); ax.axis('off')
    ax.text(9, 8.6, 'SRAG 质量评估体系', ha='center', fontsize=22, fontweight='bold', color=C_DARK_BLUE)
    ax.text(9, 8.1, '受控测试 → 多维度评分 → 版本管理 → 趋势分析', ha='center', fontsize=13, color=C_MED)

    # 1 Test Set
    add_box_outline(ax, 0.5, 6.6, 4.0, 1.1, '1. 测试集', C_LIGHT_BLUE, C_BLUE, 14)
    add_box(ax, 0.7, 6.8, 3.6, 0.4, '20 题 × 8 个领域', C_LIGHT_BLUE, C_DARK, 12)
    add_box(ax, 0.7, 6.2, 3.6, 0.4, '覆盖越狱/边缘/多轮', C_LIGHT_BLUE, C_DARK, 12)

    # 2 Scoring
    add_box_outline(ax, 5.0, 6.6, 4.0, 1.1, '2. 多维度评分', C_LIGHT_GREEN, C_GREEN, 14)
    for i, s in enumerate(['来源标注 25%', '品牌禁止 25%', '知识准确 25%', '离题拒绝 15%', '首答完整 10%']):
        add_box(ax, 5.2, 6.9 - i*0.24, 3.6, 0.22, s, C_LIGHT_GREEN, C_DARK, 11)

    # 3 Version
    add_box_outline(ax, 9.5, 6.6, 4.0, 1.1, '3. 版本管理 + A/B', C_LIGHT_ORANGE, C_ORANGE, 14)
    add_box(ax, 9.7, 6.8, 3.6, 0.35, 'v1 → v2 → v3 → v4 (当前)', C_LIGHT_ORANGE, C_DARK, 12)
    add_box(ax, 9.7, 6.2, 3.6, 0.35, '切换/回滚/A/B 对比', C_LIGHT_ORANGE, C_DARK, 12)

    # 4 Charts
    add_box_outline(ax, 14.0, 6.6, 3.5, 1.1, '4. ECharts 可视化', C_LIGHT_RED, C_RED, 14)
    for i, c in enumerate(['雷达图', '柱状图', '趋势图', '偏差图']):
        add_box(ax, 14.2, 6.9 - i*0.24, 3.1, 0.22, c, C_LIGHT_RED, C_DARK, 11)

    mids = [(0.5, 4.2, '端到端评估\n20 题 × LLM 裁判', C_DARK_BLUE),
            (5.0, 4.2, 'E6 退化告警\n双重检测 + 告警', C_ORANGE),
            (9.5, 4.2, 'E7 趋势看板\n5 指标折线图', C_GREEN),
            (14.0, 4.2, '代码质量\n43/45 (+18)', C_BLUE)]
    for x, y, txt, c in mids:
        add_box(ax, x, y, 4.0, 1.0, txt, c, C_WHITE, 13)

    for xy in [(4.5, 7.15, 5.0, 7.15), (9.0, 7.15, 9.5, 7.15), (13.5, 7.15, 14.0, 7.15),
               (2.5, 6.6, 2.5, 5.2), (7.0, 6.6, 7.0, 5.2), (11.5, 6.6, 11.5, 5.2), (15.75, 6.6, 15.75, 5.2)]:
        add_arrow(ax, *xy)

    for i, (t, v) in enumerate([('加权总分', '>= 85/100'), ('忠实度', '>= 0.87'), ('退化检测', '连续 2 次下降'), ('代码评分', '43/45')]):
        add_box(ax, 0.5 + i*4.5, 0.2, 4.0, 0.7, f'{t}\n{v}', C_GREEN, C_WHITE, 12)

    plt.tight_layout(); plt.savefig(str(OUT / '06_eval_framework.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[6/7] {OUT / "06_eval_framework.png"}')


# ================================================================
# 7. Admin Dashboard
# ================================================================
def draw_admin():
    fig, ax = plt.subplots(figsize=(18, 8))
    ax.set_xlim(0, 18); ax.set_ylim(0, 8); ax.axis('off')
    ax.text(9, 7.6, '管理后台功能总览', ha='center', fontsize=22, fontweight='bold', color=C_DARK_BLUE)
    ax.text(9, 7.1, 'admin.html + ECharts — 12 个功能标签页', ha='center', fontsize=13, color=C_MED)

    tabs = [
        ('概览', '退化告警+趋势', C_BLUE),
        ('对话', '日期分组+越狱', C_GREEN),
        ('文档', '上传+扫描+状态', C_ORANGE),
        ('前端模型', '6 个预设+测试', C_RED),
        ('后端模型', '8 张配置卡', C_DARK_BLUE),
        ('Prompt 评估', '20 题评估+弹性', C_GREEN),
        ('端到端评估', '全链路 LLM 评分', C_ORANGE),
        ('评估历史', '版本+趋势', C_RED),
        ('索引质量', 'FAISS+Chroma 趋势', C_BLUE),
        ('检索质量', 'Recall/MRR/Precision', C_GREEN),
        ('用量看板', 'LLM 调用+延迟', C_DARK_BLUE),
        ('配置', '系统+审计日志', C_MED),
    ]
    for i, (name, desc, color) in enumerate(tabs):
        col, row = i % 4, i // 4
        x, y = 0.3 + col * 4.5, 5.5 - row * 1.8
        add_box(ax, x, y, 4.0, 1.3, f'{name}\n{desc}', color, C_WHITE, 13)

    plt.tight_layout(); plt.savefig(str(OUT / '07_admin_dashboard.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[7/7] {OUT / "07_admin_dashboard.png"}')


# ================================================================
# 8. NEW: Two LLM Call Chains (for section 6.2)
# ================================================================
def draw_llm_flow():
    fig, ax = plt.subplots(figsize=(18, 7))
    ax.set_xlim(0, 18); ax.set_ylim(0, 7); ax.axis('off')
    ax.text(9, 6.6, '两条 LLM 调用链 — 8 张配置卡', ha='center', fontsize=20, fontweight='bold', color=C_DARK_BLUE)

    # Chain A - top
    add_box_outline(ax, 0.3, 4.8, 17.0, 0.6, '链路 A：用户交互路径', C_LIGHT_BLUE, C_BLUE, 14)
    add_box(ax, 0.5, 3.6, 2.5, 0.8, '用户查询', C_DARK_BLUE, C_WHITE, 13)
    add_box(ax, 3.5, 3.6, 2.5, 0.8, '[chat]\n主 LLM', C_GREEN, C_WHITE, 13)
    add_box(ax, 6.5, 3.6, 2.5, 0.8, '[jailbreak]\n越狱检测', C_ORANGE, C_WHITE, 12)
    add_box(ax, 9.5, 3.6, 2.5, 0.8, '[scoring]\n语义评分', C_ORANGE, C_WHITE, 12)
    add_box(ax, 12.5, 3.6, 2.5, 0.8, '[fallback]\nOllama 本地', C_LIGHT_RED, C_RED, 12)

    add_arrow(ax, 3.0, 4.0, 3.5, 4.0)
    add_arrow(ax, 6.0, 4.0, 6.5, 4.0)
    add_arrow(ax, 9.0, 4.0, 9.5, 4.0)

    # Fallback arrow
    ax.annotate('', xy=(5.5, 3.3), xytext=(5.5, 3.6), arrowprops=dict(arrowstyle='->', color=C_RED, lw=2, linestyle='dashed'), zorder=1)
    ax.annotate('', xy=(12.5, 3.3), xytext=(6.5, 3.3), arrowprops=dict(arrowstyle='->', color=C_RED, lw=2, linestyle='dashed'), zorder=1)
    ax.text(9.5, 3.1, '失败时降级', fontsize=11, color=C_RED, ha='center')

    # Chain B - bottom
    add_box_outline(ax, 0.3, 2.4, 17.0, 0.6, '链路 B：后端处理路径', C_LIGHT_GREEN, C_GREEN, 14)
    add_box(ax, 0.5, 1.2, 2.8, 0.8, 'RAG 检索', C_DARK_BLUE, C_WHITE, 13)
    add_box(ax, 3.8, 1.2, 2.3, 0.8, '[embedding]\n向量化', C_GREEN, C_WHITE, 12)
    add_box(ax, 6.6, 1.2, 2.3, 0.8, '[reranker]\n重排序', C_GREEN, C_WHITE, 12)
    add_box(ax, 9.4, 1.2, 2.3, 0.8, 'Prompt 评估', C_DARK_BLUE, C_WHITE, 13)
    add_box(ax, 12.2, 1.2, 2.3, 0.8, '[promptEval]\n测试与评分', C_BLUE, C_WHITE, 12)
    add_box(ax, 15.0, 1.2, 2.5, 0.8, '文档预处理', C_DARK_BLUE, C_WHITE, 13)
    add_box(ax, 15.0, 0.4, 2.5, 0.8, '[chunk]\nLLM 清洗', C_BLUE, C_WHITE, 12)

    add_arrow(ax, 3.3, 1.6, 3.8, 1.6)
    add_arrow(ax, 6.1, 1.6, 6.6, 1.6)
    add_arrow(ax, 8.9, 1.6, 9.4, 1.6)
    add_arrow(ax, 11.7, 1.6, 12.2, 1.6)

    plt.tight_layout(); plt.savefig(str(OUT / '08_llm_flow.png'), dpi=200, bbox_inches='tight', facecolor='white'); plt.close()
    print(f'[8/8] {OUT / "08_llm_flow.png"}')


# ================================================================
if __name__ == '__main__':
    draw_architecture()
    draw_qa_flow()
    draw_rag_pipeline()
    draw_preprocessing()
    draw_security()
    draw_eval()
    draw_admin()
    draw_llm_flow()
    print('\nAll 8 diagrams regenerated successfully')
