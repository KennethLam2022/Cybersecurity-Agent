"""RAG Pipeline — 检索增强问答代理

流程：
  user_query → retriever.search() → build_context → build_prompt → LLM.chat() → save_memory → response

Prompt 架构设计（参考知识库 三层架构 E.5）：
  第一层 System Prompt  → 角色 + 能力边界 + 行为约束
  第二层 Context        → 结构化参考资料 + 来源标注
  第三层 CoT + 示例     → 思维链引导 + one-shot 示例
"""
import os, sys, logging, json, time, asyncio, httpx
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_SRC = os.path.dirname(os.path.abspath(__file__))
_PREPROCESSOR = os.path.join(_SRC, "..", "..", "preprocessor", "src")
for p in [_SRC, _PREPROCESSOR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from retriever import CyberRetriever
from llm_provider import LLMProvider
from memory import ConversationMemory


# ============================================================
# 第一层：System Prompt — 角色定义 + 行为约束
# 参考：知识库 B.1-角色设定 + B.5-约束控制 + 你的专家Prompt
# ============================================================
SYSTEM_PROMPT = """你是一位**移动运营商网络安全管理体系专家**。专长是运营商怎么管安全——合规怎么过、制度怎么建、风险怎么控。

## 能力范围
- **CII关基安全**：识别定级、保护要求、检测评估、事件管理
- **等保2.0**：定级备案、测评要点、运营商扩展要求（5G）、整改方案
- **数据安全**：分类分级、数据安全法/个保法落地、全生命周期安全
- **APP安全**：安全检测、隐私合规、渠道安全
- **公司安全管理**：组织架构、制度体系、SOC运营、应急响应

## 不做范围（必须委婉引导）
当用户的问题属于以下范围时，不要生硬说"我不能回答"，而是用温和语气引导：

> "我是专注于网络安全的智能助手，主要提供等保测评、数据安全、合规检查、安全管理体系等方面的知识。关于XX的问题，建议您咨询相关领域的专业人员获取更准确的信息。"

| 引导范围 | 例子 |
|---------|------|
| 非通信行业合规 | 教育/医疗/金融（不涉运营商场景） |
| 具体技术操作 | 如何配防火墙、写代码 |
| 厂商/产品咨询 | 该买哪个品牌、哪家产品好 |
| 非中国大陆法规 | GDPR、CCPA |
| 企业内部管理纠纷 | 监控员工聊天、办公室装摄像头 |
| 非网络安全话题 | 厂家背景、产品价格、市场行情、销售渠道 |

## 核心规则

### 1. 首依参考资料，高置信可用自身知识补充
- 【参考资料】能覆盖时：严格基于参考资料，每个事实标注来源 `[来源N: 文档名称 / 章节]`
- 【参考资料】部分覆盖时：优先用参考资料覆盖的部分，对未覆盖部分可以按以下规则用自己的知识补充：
  - 补充内容必须是**普适的法律/标准/行业常识**（如《网络安全法》七章七十九条、等保三级比二级更严格等）
  - 补充前加标注 `[注：此信息来自我的训练知识，建议查阅官方原文确认]`
  - 禁止用自身知识编造具体技术参数、内部管理流程、未公开政策
- 【参考资料】完全未覆盖且自身知识也不确定时：回答"知识库中暂无相关文件，且我无法确认准确信息"

### 2. 每个观点必须有来源标注
- 每句陈述后紧贴 `[来源N: 文档名称 / 章节]`，名称必须与【参考资料】一致。

### 3. 结论与正文一致
- 说"有6项要求"就必须列出6项，不允许前后矛盾。

### 4. 区分合规红线 vs 管理优化
- **红线**：标清法规/标准依据
- **优化**：明确说"建议"而非"必须"

### 5. 直白、接地气，类比辅助
- 如"合规是底线，不是天花板。"

### 6. 对比分析类问题处理
- 问"区别/差异/对比"时，用**拆分对比法**：识别对象 → 分别检索 → 结构化对比
- 推荐表格展示差异，**不给自己设数量限制**，有多少差异列多少

### 7. 用户问厂商/产品/非安全话题时委婉引导
- 用户问厂家背景、产品对比、价格行情等非网络安全话题时，按"不做范围"中的引导话术回应
- 在回答安全问题时，如果必须提及厂商/产品，用通用功能描述替代（如"日志审计系统"替代"Splunk"）

## 具体系统分析推理框架

当用户问某个系统（如5GC、BOSS、CRM等）的安全等级或防护时，按以下三步分析：

**第一步：系统类型研判**
| 系统类别 | 典型系统 | 通常定级 |
|---------|---------|---------|
| 核心网 | 5GC/EPC/IMS/HLR/HSS | 等保三级 |
| 支撑系统 | BOSS/CRM/Billing/计费 | 等保三级 |
| 管理信息系统 | OA/ERP/邮件/HR/门户 | 等保二级 |
| 业务平台 | 短信网关/彩信/支付平台 | 二或三级 |
| 数据中心/云平台 | IDC/私有云 | 等保三级 |
| 网络基础设施 | 路由器/防火墙/4A | 二或三级 |

- 表里没有的按关键词推断（"办公"→管理信息、"计费"→支撑）
- **无法归类才能拒绝**，有推断就要标注"推断"而非冒充事实

**第二步：按级别匹配安全要求**
- 二级→基础访问控制、审计、备份
- 三级→更严格访问控制、双因素、入侵检测、灾备
- 四级→最高级别

**第三步：应急响应建议**
- 通用流程：**止血→排查→恢复→加固→上报**
- 区分合规红线和管理优化

**重要**：回答开头必须说明"这是基于运营商系统分类标准的推理分析"，标注哪些有参考资料支撑、哪些是推断。

## 输出格式

回答包含两部分，之间用 `---` 分隔：

**第一部分：思考过程（让用户看到你在工作）**
```
【思考过程】
我分析了以下参考资料：
- [来源1] 涉及...，直接相关
- [来源2] 相关性中等
- [来源3] 与问题无关
---
```

**第二部分：最终回答**
1. **核心结论**（一句话概括）
2. **详细分析**（分点说明）
3. **操作提醒**（可选）

每个观点仍必须有 `[来源N: 文档名称 / 章节]` 标注。禁止空泛套话。"""


# ============================================================
# 第二层：Context — 结构化参考资料
# ============================================================
MAX_CONTEXT_CHARS = 40000  # 最大上下文字符数，防止超长文档撑爆 LLM 上下文
MAX_HISTORY_CHARS = 8000   # 历史对话最大字符数，超出部分从旧到新截断


def compute_confidence(doc: dict, result_set_size: Optional[int] = None) -> dict:
    """数学方式计算置信度

    策略（基于检索得分的特点）：
    1. 有 rerank_score（cross-encoder 输出，0-1 已校准，越高越相关）→ 直接作为置信度
    2. 只有 score（FAISS 余弦距离 0~2，越低越近；Chroma L2 距离类似）→ 用 1/(1+score) 归一化到 (0.33, 1]
    3. 无得分 → 0

    P2-5 新增：result_set_size 校正因子
    - 总结果数少于 3 条 → 乘以 0.85（信息量不足）
    - 总结果数在 3-5 条 → 乘以 0.95（信息量一般）
    - 大于 15 条 → 乘以 1.05（信息充足，互相印证）
    """
    if doc.get("rerank_score") is not None:
        confidence = doc["rerank_score"]
    elif doc.get("score") is not None:
        distance = doc["score"]
        confidence = 1.0 / (1.0 + distance)
    else:
        confidence = 0.0

    confidence = max(0.0, min(1.0, confidence))

    # 语义校准：根据结果集大小调整
    if result_set_size is not None and confidence > 0:
        if result_set_size < 3:
            confidence *= 0.85
        elif result_set_size < 5:
            confidence *= 0.95
        elif result_set_size >= 15:
            confidence *= 1.05
        confidence = min(1.0, confidence)

    if confidence >= 0.9:
        label = "高"
    elif confidence >= 0.7:
        label = "中"
    else:
        label = "低"

    return {"confidence": round(confidence, 4), "label": label}


def build_context_block(docs: list[dict]) -> str:
    """将检索结果格式化为结构化参考资料块

    格式参考：知识库 Context-Engineering D.1-RAG问答场景的Context设计
    每个来源标注包含数学计算的置信度（0-1归一化）+ 等级标签
    """
    parts = ["【参考资料】（按相关性从高到低排列）："]
    for i, d in enumerate(docs, 1):
        source_tag = f"[来源{i}] {d['file_name']} / {d['section']}"
        conf = compute_confidence(d, len(docs))
        score_str = f"（置信度：{conf['confidence']} — {conf['label']}）"
        parts.append(f"\n{source_tag} {score_str}")
        parts.append(d["content"].strip())
    return "\n".join(parts)


def need_clarification(query: str, docs: list[dict]) -> Optional[str]:
    """判断检索结果是否需要追问澄清

    触发条件（满足任一即追问）：
    1. 无检索结果（已在外部处理，此处直接返回）
    2. 仅有 1-2 条结果且置信度都 < 0.35
    3. 所有结果的置信度都 < 0.3
    """
    if not docs:
        return None

    confs = []
    for d in docs[:5]:
        if d.get("rerank_score") is not None:
            confs.append(d["rerank_score"])
        elif d.get("score") is not None:
            confs.append(1.0 / (1.0 + d["score"]))
        else:
            confs.append(0.0)

    max_conf = max(confs) if confs else 0.0
    avg_conf = sum(confs) / len(confs) if confs else 0.0

    trigger = False
    reason = ""

    if len(docs) <= 2 and max_conf < 0.35:
        trigger = True
        reason = "检索到的相关文档较少，且相关性偏低，可能无法完整覆盖你的问题"
    elif avg_conf < 0.3:
        trigger = True
        reason = "检索到的文档与问题的相关性偏低"

    if not trigger:
        return None

    return (
        f"为了给你更准确的回答，我需要确认一下：你问的这个问题涉及的范围比较广，{reason}。"
        f"请补充以下信息：\n"
        f"1. 你关注的是哪个具体方面（如制度要求、技术措施、合规检查等）？\n"
        f"2. 涉及的具体系统类型是什么（如核心网、支撑系统、管理信息系统等）？\n"
        f"3. 是否有特定的法规或标准需要参考？\n\n"
        f"信息越具体，我能提供的帮助越精准。"
    )


# ============================================================
# 第三层：One-shot 示例 — 引导输出格式
# 参考：知识库 B.2-少样本示例 + RAG冠军方案
# ============================================================
FEW_SHOT_EXAMPLE = """
用户问：等保三级对安全计算环境有什么访问控制要求？

【思考过程】
我分析了以下参考资料：
- [来源1: GB/T 25070-2019 / 第5.2.3节] 直接规定了安全计算环境的访问控制要求，内容为自主访问控制+强制访问控制、最小权限、三权分立，高度相关
- [来源2: 等级保护2.0实践课程 / 第3章] 补充了等保三级整体框架，相关性中等
- [来源3: 数据安全管理办法] 是关于数据安全的，与访问控制无关

基于以上分析，[来源1] 完全覆盖了问题，可以给出完整回答。

---
等保三级对安全计算环境的访问控制，核心要求是 **"自主访问控制 + 强制访问控制"** 两层机制。简单类比：自主访问控制像你家门锁——主人决定谁能进；强制访问控制像大楼保安——系统级别定死了访问规则，用户自己改不了。

具体要求（依据 [来源1: GB/T 25070-2019 / 第5.2.3节]）：
1. 启用访问控制机制，对主体（用户/进程）和客体（文件/数据）设置访问权限
2. 默认"最小权限"原则——只给完成任务所需的最小权限
3. 特权用户权限分离——系统管理员、安全审计员、安全管理员三权分立

这是合规红线，不过的话等保测评直接不合格。实际操作建议：先用堡垒机+4A平台落地权限分离，再在应用层做细粒度访问控制。

---

用户问：《网络安全法》对网络运营者规定了哪些安全保护义务？

【思考过程】
我分析了以下参考资料：
- [来源1: 网络安全法 / 第二十一条] 直接规定了网络运营者应当采取的五项安全保护技术措施，是回答的核心依据
- [来源2: 网络安全法 / 第五十九条] 规定了不履行义务的处罚措施，可以用来补充

[来源1] 完整覆盖了问题，可以直接引用。

---
网络运营者的安全保护义务包含 **5 项核心要求**，规定在《网络安全法》第二十一条。

根据 [来源1: 网络安全法 / 第二十一条]，网络运营者应当：
1. **制定内部安全管理制度和操作规程**，确定安全负责人
2. **采取防范计算机病毒和网络攻击的技术措施**
3. **采取监测、记录网络运行状态的技术措施**，日志留存不少于6个月
4. **采取数据分类、重要数据备份和加密措施**
5. **制定网络安全事件应急预案并定期演练**

这 5 项是合规红线，不履行将面临警告、罚款（最高十万元）甚至吊销营业执照的风险[来源2: 网络安全法 / 第五十九条]。操作上建议先对照5项逐条自查差距，再分阶段整改。

---

用户问：等保三级与等保二级的访问控制要求有什么主要区别？

【思考过程】
我分析了以下参考资料：
1. **识别对比对象**：等保三级 vs 等保二级
2. **提取安全要求**：
   - [来源1: GB/T 22239-2019 / 第7.1.3节] 描述了二级安全区域边界的访问控制：基于源/目的地址、端口、协议进行过滤。
   - [来源2: GB/T 22239-2019 / 第8.1.3节] 描述了三级安全区域边界的访问控制：在二级基础上增加了对虚拟化环境的访问控制、对应用协议内容的过滤要求等。
3. **对比分析**：三级比二级在"协议深度"和"虚拟化支持"上有显著增强。

基于以上分析，可以给出对比回答。

---
等保三级与等保二级的访问控制要求主要在 **"过滤深度"** 和 **"环境适应性"** 上存在差异。

| 维度 | 等保二级 [来源1] | 等保三级 [来源2] | 核心差异 |
|------|----------------|----------------|----------|
| **过滤规则** | 基于源/目的IP、端口、协议 | 同样基于基础信息，但增加协议内容过滤 | 三级要求能够识别应用层协议内容 |
| **会话状态** | 检查会话状态 | 检查会话状态 | 无明显差异 |
| **虚拟化支持** | 未显式强调 | 明确要求虚拟化网络间的访问控制 | 三级必须适配云计算/虚拟化场景 |

总结来说，二级更像是个基础的包过滤防火墙，而三级则要求具备深度包检测（DPI）的能力，并能管住虚拟机之间的流量。这是因为三级系统通常承载更核心的业务，面临的攻击手段更复杂。"""


# ============================================================
# Prompt 组装
# ============================================================
def build_prompt_messages(
    query: str,
    docs: list[dict],
    history: Optional[list[dict]] = None,
    include_example: bool = True,
    session_context: Optional[str] = None,
) -> list[dict]:
    """组装完整的 Prompt 消息序列

    三层架构（参考 知识库 E.5 Prompt→Context→Harness）：
    System Prompt → Session Memory → Context → Example → History → Question
    """
    context_block = build_context_block(docs)

    # 截断过长上下文
    if len(context_block) > MAX_CONTEXT_CHARS:
        doc_ids_low_first = [
            id(d) for d in sorted(
                docs, key=lambda d: compute_confidence(d)["confidence"]
            )
        ]
        keep_ids = set(id(d) for d in docs)
        while len(context_block) > MAX_CONTEXT_CHARS and len(keep_ids) > 3:
            remove_id = doc_ids_low_first.pop(0)
            keep_ids.discard(remove_id)
            keep_docs = [d for d in docs if id(d) in keep_ids]
            context_block = build_context_block(keep_docs)
        logger.warning(
            f"上下文截断: 原 {len(docs)} 个来源 → 保留 {len(keep_ids)} 个"
        )

    system_parts = [SYSTEM_PROMPT.strip()]

    # 跨会话记忆注入
    if session_context:
        system_parts.append(f"\n\n## 用户背景（本轮对话已识别）\n{session_context.strip()}")

    system_parts.append(f"\n\n## 以下是根据你问题检索到的参考资料\n\n{context_block}")
    if include_example:
        system_parts.append(f"\n\n{FEW_SHOT_EXAMPLE}")
    system_content = "\n".join(system_parts)

    # P3-4: 全链路预算 — system+context 超过预算则截断最长的文档块
    system_budget = int(MAX_CONTEXT_CHARS * 0.65)
    if len(system_content) > system_budget:
        overflow = len(system_content) - system_budget
        # 优先截断 FEW_SHOT_EXAMPLE
        if include_example and len(FEW_SHOT_EXAMPLE) > 200:
            example_trimmed = FEW_SHOT_EXAMPLE[: max(200, len(FEW_SHOT_EXAMPLE) - overflow)]
            system_content = system_content.replace(FEW_SHOT_EXAMPLE, example_trimmed)
            logger.warning(f"Prompt 预算: 截断 FEW_SHOT_EXAMPLE ({overflow} 字符)")
        if len(system_content) > system_budget:
            system_content = system_content[:system_budget]
            logger.warning(f"Prompt 预算: 强制截断至 {system_budget} 字符")

    messages = [{"role": "system", "content": system_content}]

    if history:
        history_budget = min(MAX_HISTORY_CHARS, int(MAX_CONTEXT_CHARS * 0.3))
        hist_chars = 0
        for msg in history:
            hist_chars += len(msg.get("content", ""))
        if hist_chars > history_budget:
            truncated = []
            chars = 0
            for msg in reversed(history):
                truncated.insert(0, msg)
                chars += len(msg.get("content", ""))
                if chars >= history_budget:
                    break
            history = truncated
            logger.warning(
                f"历史截断: 原 {hist_chars} 字符 → 保留 {chars} 字符 ({len(history)}/{len(truncated) + len(history)} 条)"
            )

        for msg in history:
            if msg["role"] in ("user", "assistant"):
                messages.append({"role": msg["role"], "content": msg["content"]})

    messages.append({"role": "user", "content": f"【问题】{query}"})

    return messages


QUERY_REWRITE_PROMPT = """你是一个网络安全知识库检索助手。用户的原始问题可能口语化、不完整、缺少关键词。

请将用户的问题改写成**更适合知识库检索的版本**：

改写规则：
1. **提取核心关键词**：法规名称（如网络安全法）、标准编号（如GB/T 22239）、技术术语（如访问控制）
2. **补充领域上下文**：用户没说但隐含的术语（如"等保要求"→"网络安全等级保护 基本要求"）
3. **保持简洁**：去掉口语化修饰词（"帮我看看"、"我想知道"等），只保留核心语义
4. **保持完整性**：如果问题包含对比/否定（"区别"、"不能"），保留这些逻辑关键词
5. **不要编造**：不存在的术语不要加，不确定的不要补

直接输出改写后的结果，不要解释。

原始问题：{query}
改写后："""


SELF_VERIFY_PROMPT = """你是一个网络安全知识库的"事实核查员"。请仔细核对回答中的每个结论是否在参考资料中有明确依据。

规则：
1. 逐句检查，有依据的保留，无依据的删除
2. **只输出清理后的回答文本本身**，不要加任何说明、不要加"需要修改的回答"、"删除以下句子"等前缀/后缀
3. 不要添加新的内容，不要改写原文
4. 如果所有结论都有依据，只输出一个词: [PASS]

参考资料（TOP-5）：
{sources}

需要核查的回答：
{answer}

清理后的回答："""


class CyberAgent:
    """网络安全 RAG Agent"""

    def __init__(
        self,
        llm_config: Optional[dict] = None,
        top_k: int = 10,
        use_rerank: bool = True,
        use_history: bool = True,
        max_history: int = 10,
        include_example: bool = True,
        use_query_rewrite: bool = True,
        use_verification: bool = True,
    ):
        self.retriever = CyberRetriever()
        self.llm = LLMProvider(**(llm_config or {}))
        self.memory = ConversationMemory()
        self.top_k = top_k
        self.use_rerank = use_rerank
        self.use_history = use_history
        self.max_history = max_history
        self.include_example = include_example
        self.use_query_rewrite = use_query_rewrite
        self.use_verification = use_verification

        # 语义寒暄检测原型（启动时预计算一次 embedding）
        self._greeting_prototypes = [
            ("你好", "问候"), ("您好", "问候"), ("嗨", "问候"), ("hello", "问候"), ("hi", "问候"),
            ("早上好", "问候"), ("下午好", "问候"), ("晚上好", "问候"),
            ("你好呀", "问候"), ("hello呀", "问候"),
            ("你是谁", "身份"), ("你叫什么", "身份"),
            ("在吗", "确认"), ("在不在", "确认"), ("are you there", "确认"),
            ("谢谢", "感谢"), ("感谢", "感谢"), ("多谢", "感谢"), ("辛苦了", "感谢"),
            ("再见", "告别"), ("拜拜", "告别"), ("bye", "告别"), ("goodbye", "告别"),
            ("今天天气", "闲聊"), ("你吃了吗", "闲聊"), ("你吃了没", "闲聊"),
            ("你好吗", "闲聊"), ("how are you", "闲聊"),
            ("没事了", "结束"), ("没有问题了", "结束"), ("没其他问题了", "结束"),
            ("你真棒", "赞美"), ("你真厉害", "赞美"), ("good job", "赞美"),
            ("你太好了", "赞美"),
            ("测试", "测试"), ("test", "测试"),
            ("在干嘛", "闲聊"), ("在做什么", "闲聊"),
            ("what are you doing", "闲聊"), ("how are you doing", "闲聊"),
            ("see you later", "告别"),
        ]
        self._greeting_threshold = 0.82
        self._greeting_proto_embs: list[list[float]] | None = None

    def _verify_answer(self, answer: str, sources: list[dict]) -> str:
        """自检：用 LLM 核查回答中的每个结论是否有来源依据"""
        if not self.use_verification or not sources or not answer:
            return answer
        try:
            src_text = "\n---\n".join(
                f"[来源 {i+1}] {s['file_name']} | {s['section']}\n{s.get('content','')[:300]}"
                for i, s in enumerate(sources[:5])
            )
            prompt = SELF_VERIFY_PROMPT.format(sources=src_text, answer=answer)
            t0 = time.time()
            result = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=2048,
                timeout=15,
            )
            elapsed = time.time() - t0
            result = result.strip()

            # 后处理：去掉 LLM 可能残余的说明性前缀/后缀
            for prefix in ["清理后的回答：", "需要修改的回答：", "核查结果：", "修改后：", "删除以下句子："]:
                if result.startswith(prefix):
                    result = result[len(prefix):].strip()
            for suffix in ["[PASS]", "以上是清理后的回答", "以上是核查结果"]:
                if result.endswith(suffix):
                    result = result[:-len(suffix)].strip()

            if result == "[PASS]":
                logger.info(f"自检通过：回答结论全部有依据 ({elapsed:.1f}s)")
                return answer
            if result and result != "[PASS]":
                removed = len(answer) - len(result)
                if removed > 20:
                    logger.warning(f"自检过滤：删除无依据内容 ({elapsed:.1f}s, {removed}字符)")
                return result
        except Exception as e:
            logger.warning(f"自检失败（不影响回答）: {e}")
        return answer

    def _check_offtopic(self, text: str) -> Optional[str]:
        """检测非网络安全话题，返回礼貌引导

        当用户问厂家/产品/价格/市场等非安全话题时，
        在 RAG 检索之前就拦截，返回委婉引导。
        """
        if not text:
            return None
        t = text.strip().lower()

        # 关键词匹配：常见非安全话题模式
        offtopic_keywords = [
            # 厂家/品牌类
            "厂家", "品牌", "厂商", "供应商", "制造商",
            # 产品对比/推荐类
            "哪个好", "怎么选", "推荐", "性价比", "排行榜", "对比", "区别",
            "哪款", "哪个品牌", "哪个厂家",
            # 产品具体型号
            "价格", "报价", "多少钱", "成本",
            # 市场类
            "市场份额", "市场占有率", "销量", "销售",
            # 非安全技术
            "编程", "代码", "开发", "前端", "后端",
            # 招聘/求职
            "招聘", "薪资", "工资", "面试",
        ]
        # 特定公司名（出现在非安全上下文时）
        company_keywords = [
            "华为", "华三", "h3c", "中兴", "cisco", "思科", "juniper",
            "深信服", "启明星辰", "奇安信", "绿盟", "天融信", "山石网科",
            "安恒", "360", "亚信安全", "飞塔", "fortinet", "palo alto",
            "check point", "checkpoint",
        ]

        for kw in offtopic_keywords:
            if kw in t:
                # 但仍需确认是否包含网络安全词，避免误伤
                safe_keywords = ["等保", "等级保护", "合规", "安全要求", "安全措施",
                                 "安全管理制度", "数据安全", "个保法", "网络安全法",
                                 "安全检测", "隐私合规", "应急响应", "安全运维",
                                 "安全管理", "安全组织", "安全意识", "安全培训"]
                has_safe = any(sk in t for sk in safe_keywords)
                if not has_safe:
                    return (
                        "我是专注于网络安全的智能助手，主要提供等保测评、数据安全、"
                        "合规检查、安全管理体系等方面的知识。关于厂家和产品的具体信息，"
                        "建议您咨询相关领域的专业人员获取更准确的答案。"
                    )

        # 公司名 + 非安全语境检测
        for ck in company_keywords:
            if ck in t:
                safe_keywords = ["等保", "等级保护", "合规", "安全要求", "安全措施",
                                 "数据安全", "个保法", "网络安全法", "安全管理",
                                 "安全运维", "应急响应", "安全检测", "漏洞", "防护",
                                 "防火墙安全", "安全产品", "安全方案", "安全技术"]
                has_safe = any(sk in t for sk in safe_keywords)
                # 但如果问的是"哪个好"、"对比"等比较类问题，仍视为offtopic
                compare_keywords = ["哪个好", "怎么选", "对比", "哪个", "推荐", "性价比"]
                has_compare = any(ck2 in t for ck2 in compare_keywords)
                if not has_safe or has_compare:
                    return (
                        "我是专注于网络安全的智能助手，主要提供等保测评、数据安全、"
                        "合规检查、安全管理体系等方面的知识。关于厂家和产品的具体信息，"
                        "建议您咨询相关领域的专业人员获取更准确的答案。"
                    )
        return None

    def _check_greeting(self, text: str) -> Optional[str]:
        """语义检测社交寒暄，返回礼貌回复

        双阶段策略：
        1. 快速精确匹配（常见寒暄，O(1)）
        2. embedding 语义相似度匹配（覆盖无穷变体，O(N)）
        """
        import numpy as np

        if not text:
            return None
        t = text.strip().rstrip(",.!?;:，。！？；：、～~").strip()

        # ========== 阶段1：快速精确匹配 ==========
        exact_replies = {
            "你好": "你好！我是网络安全知识助手，专注移动运营商网络安全管理领域。你可以问我关于等保测评、数据安全、APP安全、合规检查等方面的问题。",
            "您好": "您好！我是网络安全知识助手，专注移动运营商网络安全管理领域。有什么可以帮助你的？",
            "嗨": "嗨！有什么网络安全相关的问题需要帮忙吗？",
            "hi": "Hi there! I'm your cybersecurity assistant. Feel free to ask about telecom security compliance, data protection, or related topics.",
            "hello": "Hello! I'm your cybersecurity assistant. How can I help you today?",
            "在吗": "在的！有什么网络安全方面的问题想了解吗？",
            "在不在": "在的，随时可以提问！",
            "谢谢": "不客气！如果还有其他问题，随时可以问我。",
            "感谢": "感谢你的信任！有新的问题随时来问。",
            "再见": "再见！如果有网络安全方面的问题，随时回来找我。",
            "拜拜": "拜拜！随时欢迎回来咨询。",
            "拜": "拜拜！",
        }
        if t in exact_replies:
            return exact_replies[t]

        prefix_replies = {
            "你是谁": "我是网络安全知识助手，专注于移动运营商网络安全管理领域。我的知识库包含国家法律法规、等保标准、通信行业规范等文件，可以帮助你解答合规、数据安全、APP安全等方面的问题。",
            "你叫什么": "我叫网络安全助手，你可以直接问我关于移动运营商网络安全管理的任何问题！",
            "早上好": "早上好！新的一天，有什么网络安全问题需要处理吗？",
            "下午好": "下午好！有什么可以帮你的？",
            "晚上好": "晚上好！随时欢迎提问。",
            "谢谢你": "不客气！很高兴能帮到你。",
            "多谢你": "不客气！有需要随时找我。",
            "辛苦了": "不辛苦！很高兴能帮到你。",
            "thanks": "不客气！如果还有其他问题，随时可以问我。",
            "thank you": "不客气！如果还有其他问题，随时可以问我。",
            "thank u": "不客气！如果还有其他问题，随时可以问我。",
            "see you": "再见！如果有网络安全方面的问题，随时回来找我。",
            "hi there": "嗨！有什么网络安全相关的问题需要帮忙吗？",
            "hello there": "Hello! How can I help you today?",
        }
        for prefix, reply in prefix_replies.items():
            if t.startswith(prefix):
                return reply

        # 包含式变体
        combined = {
            "你好呀": "你好！我是网络安全知识助手，专注移动运营商网络安全管理领域。",
            "hello呀": "Hello! How can I help you today?",
            "hi呀": "嗨！有什么可以帮你的？",
            "你好吗": "我很好，谢谢关心！有什么网络安全问题需要帮忙吗？",
            "hello你好": "你好！有什么网络安全相关的问题需要帮忙吗？",
            "see you later": "再见！如果有网络安全方面的问题，随时回来找我。",
            "你太好了": "谢谢鼓励！我会继续努力帮你解答网络安全问题。有什么需要帮忙的吗？",
            "what are you doing": "哈哈，我是AI助手，主要擅长网络安全方面的问题。有什么合规、安全方面的问题需要探讨吗？",
            "what are u doing": "哈哈，我是AI助手，主要擅长网络安全方面的问题。有什么合规、安全方面的问题需要探讨吗？",
        }
        for key, reply in combined.items():
            if key in t:
                return reply

        # ========== 阶段2：语义匹配 ==========
        try:
            if self._greeting_proto_embs is None:
                logger.info("预计算语义寒暄原型 embedding...")
                self._greeting_proto_embs = [
                    self.retriever.embed_query(p)
                    for p, _ in self._greeting_prototypes
                ]

            q_emb = self.retriever.embed_query(t)
            q_norm = np.linalg.norm(q_emb)
            if q_norm < 1e-10:
                return None
            q_emb_n = [v / q_norm for v in q_emb]

            best_score = 0.0
            best_idx = -1
            for i, p_emb in enumerate(self._greeting_proto_embs):
                p_norm = np.linalg.norm(p_emb)
                if p_norm < 1e-10:
                    continue
                dot = sum(a * b for a, b in zip(q_emb_n, [v / p_norm for v in p_emb]))
                if dot > best_score:
                    best_score = dot
                    best_idx = i

            if best_idx >= 0 and best_score >= self._greeting_threshold:
                proto_text, category = self._greeting_prototypes[best_idx]
                logger.info(
                    f"语义寒暄检测: query=「{text[:30]}」→ 匹配原型=「{proto_text}」"
                    f" 类别={category} 相似度={best_score:.3f}"
                )

                if category == "问候":
                    return "你好！我是网络安全知识助手，专注移动运营商网络安全管理领域。有什么可以帮助你的？"
                if category == "身份":
                    return "我是网络安全知识助手，专注于移动运营商网络安全管理领域。你可以问我关于等保测评、数据安全、APP安全、合规检查等方面的问题。"
                if category == "感谢":
                    return "不客气！如果还有其他问题，随时可以问我。"
                if category == "告别":
                    return "再见！如果有网络安全方面的问题，随时回来找我。"
                if category == "确认":
                    return "在的！有什么网络安全问题需要帮忙吗？"
                if category == "闲聊":
                    return "哈哈，我是AI助手，主要擅长网络安全方面的问题。有什么合规、安全方面的问题需要探讨吗？"
                if category == "赞美":
                    return "谢谢鼓励！我会继续努力帮你解答网络安全问题。有什么需要帮忙的吗？"
                if category == "测试":
                    return "测试收到！系统运行正常，我可以回答网络安全相关的问题。有什么具体问题需要测试吗？"
                if category == "结束":
                    return "好的，如果后续有网络安全方面的问题，随时来找我。"

        except Exception as e:
            logger.warning(f"语义寒暄检测异常（降级为无匹配）: {e}")

        return None

    def _check_sources_origin(self, answer: str, actual_sources: list[dict]) -> str:
        """核验回答中的[来源N: 文档名] 是否真实存在于知识库来源中

        P2-1 修复：精确替换编造引用标记，不再删除整行。
        如果同一行有真实引用+编造引用，只替换编造部分不伤真实内容。
        """
        import re
        if not answer:
            return answer

        pattern = re.findall(r'\[来源\d+:\s*([^\]]+?)\]', answer)
        if not pattern:
            return answer

        if not actual_sources:
            logger.warning(
                f"来源核验：知识库无检索结果，但回答包含 {len(pattern)} 个来源引用，全部清空"
            )
            return "该问题超出我的知识范围，知识库中暂无相关文件覆盖。"

        real_files = set()
        for s in actual_sources:
            fn = s.get("file_name", "")
            if fn:
                real_files.add(fn)
                base = fn.rsplit(".", 1)[0] if "." in fn else fn
                real_files.add(base)

        replacements = []
        for ref in pattern:
            ref_clean = ref.split("/")[0].split("／")[0].strip()
            found = False
            for real in real_files:
                if ref_clean in real or real in ref_clean:
                    found = True
                    break
            if not found:
                replacements.append(ref_clean)

        if not replacements:
            return answer

        removed_count = 0
        for fake_ref in replacements:
            fake_count = 0
            def _replace_fake(m):
                nonlocal fake_count
                fake_count += 1
                return ""
            answer = re.sub(
                rf'\[来源\d+:\s*{re.escape(fake_ref)}[^\]]*\]',
                _replace_fake,
                answer
            )
            removed_count += fake_count

        # 清理多余的空格和标点残留
        answer = re.sub(r' {2,}', ' ', answer)
        answer = re.sub(r',\s*,', ',', answer)
        answer = re.sub(r'\n{3,}', '\n\n', answer)

        cleaned = answer.strip()
        if removed_count:
            logger.warning(f"来源核验：精确替换 {removed_count} 个编造引用标记（保留原文）")
        return cleaned if cleaned else "该问题超出我的知识范围，无法提供有效回答。"

    def _rewrite_query(self, query: str) -> str:
        """检索前 Query 改写：将口语化问题转为检索友好版本"""
        if not self.use_query_rewrite:
            return query
        try:
            prompt = QUERY_REWRITE_PROMPT.format(query=query)
            t0 = time.time()
            rewritten = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=128,
                timeout=10,
            )
            elapsed = time.time() - t0
            rewritten = (rewritten.get("content", "") if isinstance(rewritten, dict) else rewritten).strip().strip('"').strip("'")
            if rewritten and rewritten != query and len(rewritten) < 200:
                logger.info(f"Query 改写: 「{query[:40]}」→「{rewritten[:60]}」 ({elapsed:.1f}s)")
                return rewritten
        except Exception as e:
            logger.warning(f"Query 改写失败 (不影响检索): {e}")
        return query

    def ask(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        temperature: float = 0.2,
    ) -> dict:
        """执行一次问答

        流程：
          Query 改写 → 检索 → Prompt 组装 → LLM 生成 → 记忆保存

        返回：
          {"answer": "...", "sources": [...], "conversation_id": "...", "rewritten_query": "...", "stats": {...}}
        """
        if not conversation_id:
            conv = self.memory.create_conversation(title=query[:50])
            conversation_id = conv["id"]

        self.memory.add_message(conversation_id, "user", query)

        # ---- 非安全话题检测：跳过检索/LLM，快速引导 ----
        offtopic_reply = self._check_offtopic(query)
        if offtopic_reply:
            answer = offtopic_reply
            sources = []
            self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            self.memory.extract_and_save_memory(conversation_id, query, answer)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            return {
                "answer": answer,
                "sources": sources,
                "conversation_id": conversation_id,
                "rewritten_query": None,
                "verified": False,
                "stats": {"search_time": 0, "llm_time": 0, "total_time": 0, "docs_count": 0},
            }

        # ---- 社交寒暄检测：跳过检索/LLM，快速回复 ----
        greeting_reply = self._check_greeting(query)
        if greeting_reply:
            answer = greeting_reply
            sources = []
            self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            self.memory.extract_and_save_memory(conversation_id, query, answer)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            return {
                "answer": answer,
                "sources": sources,
                "conversation_id": conversation_id,
                "rewritten_query": None,
                "verified": False,
                "stats": {"search_time": 0, "llm_time": 0, "total_time": 0, "docs_count": 0},
            }

        # ---- Query 改写 ----
        search_query = self._rewrite_query(query)

        t0 = time.time()
        docs = self.retriever.search(
            search_query,
            top_k=self.top_k,
            use_rerank=self.use_rerank,
        )
        search_time = time.time() - t0
        logger.info(f"检索完成: {len(docs)} 条 ({search_time:.2f}s)")

        # ---- 空结果预检 + 降级处理 ----
        if not docs:
            # 尝试用更宽泛的查询做二次检索（如"等保定级 应急响应"）
            fallback_queries = ["等保定级 安全等级 系统分类", "应急响应 安全事件 处置流程 灾难恢复", "网络安全 安全防护 管理要求"]
            for fq in fallback_queries:
                fallback_docs = self.retriever.search(fq, top_k=5, use_rerank=False)
                if fallback_docs:
                    logger.info(f"降级检索成功: 「{fq}」→ {len(fallback_docs)} 条")
                    docs = fallback_docs
                    search_query = fq
                    break

        if not docs:
            answer = "该问题超出我的知识范围，知识库中暂无相关文件覆盖。"
            sources = []
            self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            self.memory.extract_and_save_memory(conversation_id, query, answer)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            return {
                "answer": answer,
                "sources": sources,
                "conversation_id": conversation_id,
                "rewritten_query": search_query if search_query != query else None,
                "verified": False,
                "stats": {
                    "search_time": round(search_time, 2),
                    "llm_time": 0,
                    "total_time": round(search_time, 2),
                    "docs_count": 0,
                },
            }

        # ---- 追问/澄清检测：检索置信度不足时主动追问 ----
        clarify = need_clarification(query, docs)
        if clarify:
            answer = clarify
            sources = [
                {
                    "file_name": d["file_name"],
                    "category": d["category"],
                    "section": d["section"],
                    **compute_confidence(d),
                }
                for d in docs[:3]
            ]
            self.memory.add_message(conversation_id, "assistant", answer, sources=sources)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                title = query[:50]
                self.memory.update_title(conversation_id, title)
            return {
                "answer": answer,
                "sources": sources,
                "conversation_id": conversation_id,
                "rewritten_query": search_query if search_query != query else None,
                "clarified": True,
                "stats": {
                    "search_time": round(search_time, 2),
                    "llm_time": 0,
                    "total_time": round(search_time, 2),
                    "docs_count": len(docs),
                },
            }

        # ---- 历史对话（滑动窗口+压缩） ----
        history = None
        if self.use_history:
            history = self.memory.get_compressed_history(
                conversation_id,
                llm_provider=self.llm,
                keep_rounds=5,
            )

        # ---- 跨会话记忆（用户角色、提及的标准） ----
        session_memory = self.memory.get_session_memory(conversation_id)
        session_context = ""
        if session_memory.get("user_role"):
            session_context += f"用户身份：{session_memory['user_role']}。\n"
        if session_memory.get("mentioned_standards"):
            stds = session_memory["mentioned_standards"][-5:]
            session_context += f"用户提到的标准/法规：{', '.join(stds)}。\n"

        messages = build_prompt_messages(
            query=query,
            docs=docs,
            history=history[:-1] if history else None,
            include_example=self.include_example,
            session_context=session_context if session_context else None,
        )

        t1 = time.time()
        llm_result = self.llm.chat(messages, temperature=temperature)
        answer = llm_result.get("content", "")
        reasoning = llm_result.get("reasoning_content")
        llm_time = time.time() - t1

        sources = [
            {
                "file_name": d["file_name"],
                "category": d["category"],
                "section": d["section"],
                **compute_confidence(d),
            }
            for d in docs
        ]

        # ---- 自检 ----
        was_verified = False
        verified = self._verify_answer(answer, sources)
        if verified != answer:
            logger.info(f"自检对回答进行了修正: {len(answer)} → {len(verified)} 字符")
            answer = verified
            was_verified = True

        # ---- 来源核验：删除引用知识库中不存在文件的句子（如编造"宪法""电信条例"等） ----
        answer = self._check_sources_origin(answer, sources)

        self.memory.add_message(conversation_id, "assistant", answer, sources=sources)

        # ---- 抽取并保存跨会话记忆（角色、标准等） ----
        self.memory.extract_and_save_memory(conversation_id, query, answer)

        first_msg = self.memory.get_history(conversation_id)
        if len([m for m in first_msg if m["role"] == "user"]) == 1:
            title = query[:50]
            self.memory.update_title(conversation_id, title)

        return {
            "answer": answer,
            "reasoning_content": reasoning,
            "sources": sources,
            "conversation_id": conversation_id,
            "rewritten_query": search_query if search_query != query else None,
            "verified": was_verified,
            "stats": {
                "search_time": round(search_time, 2),
                "llm_time": round(llm_time, 2),
                "total_time": round(search_time + llm_time, 2),
                "docs_count": len(docs),
            },
        }

    async def ask_stream(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        temperature: float = 0.2,
    ):
        """流式问答 — 异步生成器，分阶段 yield 事件

        事件类型：
          {"type": "status",    "stage": "retrieving"|"generating", "message": "..."}
          {"type": "token",     "content": "..."}        ← LLM 逐个 token
          {"type": "done",      "sources": [...], "conversation_id": "..."}
        """
        if not conversation_id:
            conv = self.memory.create_conversation(title=query[:50])
            conversation_id = conv["id"]

        self.memory.add_message(conversation_id, "user", query)

        reasoning_list = []
        t_start = time.time()
        t_rewrite = 0.0
        t_search = 0.0
        t_llm = 0.0
        was_truncated = False
        llm_success = True
        was_circuit_break = False
        circuit_provider = None

        # ---- 语义兜底评分（异步，不阻塞主流程） ----
        rating_result = {}
        try:
            rating_result = self._infer_rating(conversation_id, query) or {}
        except Exception:
            pass

        # ---- 非安全话题检测（流式路径） ----
        offtopic_reply = self._check_offtopic(query)
        if offtopic_reply:
            msg_id = self.memory.add_message(conversation_id, "assistant", offtopic_reply, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(conversation_id, msg_id, query, off_topic=True, total_time=round(time.time()-t_start, 3))
            yield {"type": "token", "content": offtopic_reply}
            yield {"type": "done", "sources": [], "conversation_id": conversation_id, "message_id": msg_id}
            return

        # ---- 社交寒暄检测（流式路径） ----
        greeting_reply = self._check_greeting(query)
        if greeting_reply:
            msg_id = self.memory.add_message(conversation_id, "assistant", greeting_reply, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(conversation_id, msg_id, query, off_topic=True, total_time=round(time.time()-t_start, 3))
            yield {"type": "token", "content": greeting_reply}
            yield {"type": "done", "sources": [], "conversation_id": conversation_id, "message_id": msg_id}
            return

        # Stage 1: 检索知识库
        yield {"type": "status", "stage": "retrieving", "message": "正在检索知识库..."}

        t0_rw = time.time()
        search_query = self._rewrite_query(query)
        t_rewrite = time.time() - t0_rw

        loop = asyncio.get_event_loop()
        t0_sr = time.time()
        docs = await loop.run_in_executor(
            None,
            lambda: self.retriever.search(
                search_query,
                top_k=self.top_k,
                use_rerank=self.use_rerank,
            ),
        )
        t_search = time.time() - t0_sr
        logger.info(f"流式检索完成: {len(docs)} 条")

        # ---- 空结果预检 + 降级处理 ----
        if not docs:
            fallback_queries = ["等保定级 安全等级 系统分类", "应急响应 安全事件 处置流程 灾难恢复", "网络安全 安全防护 管理要求"]
            for fq in fallback_queries:
                t0_fb = time.time()
                fallback_docs = await loop.run_in_executor(
                    None,
                    lambda q=fq: self.retriever.search(q, top_k=5, use_rerank=False),
                )
                t_search += time.time() - t0_fb
                if fallback_docs:
                    logger.info(f"流式降级检索成功: 「{fq}」→ {len(fallback_docs)} 条")
                    docs = fallback_docs
                    break

        if not docs:
            answer = "该问题超出我的知识范围，知识库中暂无相关文件覆盖。"
            sources = []
            total_time = round(time.time() - t_start, 3)
            msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(conversation_id, msg_id, query, rewrite_time=round(t_rewrite, 3), total_time=total_time, returned_count=0)
            yield {"type": "token", "content": answer}
            yield {"type": "done", "sources": sources, "conversation_id": conversation_id, "message_id": msg_id}
            return

        # ---- 追问/澄清检测（流式路径） ----
        clarify = need_clarification(query, docs)
        if clarify:
            total_time = round(time.time() - t_start, 3)
            msg_id = self.memory.add_message(conversation_id, "assistant", clarify, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(conversation_id, msg_id, query, rewrite_time=round(t_rewrite, 3), total_time=total_time, returned_count=len(docs))
            yield {"type": "token", "content": clarify}
            yield {"type": "done", "sources": [], "conversation_id": conversation_id, "message_id": msg_id}
            return

        # Stage 2: 生成回答
        yield {"type": "status", "stage": "generating", "message": "正在生成回答..."}

        history = None
        if self.use_history:
            history = self.memory.get_compressed_history(
                conversation_id, self.llm, keep_rounds=self.max_history
            )

        messages = build_prompt_messages(
            query=query,
            docs=docs,
            history=history[:-1] if history else None,
            include_example=self.include_example,
        )

        t0_llm = time.time()
        full_content = ""
        async for chunk in self.llm.chat_stream(messages, temperature=temperature):
            if isinstance(chunk, dict):
                chunk_type = chunk.get("type", "content")
                chunk_text = chunk.get("text", "")
                if chunk_type == "reasoning":
                    reasoning_list.append(chunk_text)
                    yield {"type": "reasoning", "content": chunk_text}
                else:
                    full_content += chunk_text
                    yield {"type": "token", "content": chunk_text}
            else:
                full_content += chunk
                yield {"type": "token", "content": chunk}
        t_llm = time.time() - t0_llm
        total_time = round(time.time() - t_start, 3)

        # 保存到记忆
        sources = [
            {
                "file_name": d["file_name"],
                "category": d["category"],
                "section": d["section"],
                **compute_confidence(d),
            }
            for d in docs
        ]
        msg_id = self.memory.add_message(conversation_id, "assistant", full_content, sources=sources)

        # ---- 来源核验：流式路径同样做后处理 ----
        corrected = self._check_sources_origin(full_content, sources)
        if corrected != full_content:
            logger.warning(f"流式路径来源核验：删除了编造引用内容，已修正记忆中的版本")
            self.memory._update_last_message(conversation_id, corrected, sources=sources)
            full_content = corrected

        first_msgs = self.memory.get_history(conversation_id)
        if len([m for m in first_msgs if m["role"] == "user"]) == 1:
            self.memory.update_title(conversation_id, query[:50])

        self.memory.log_usage(
            conversation_id=conversation_id,
            message_id=msg_id,
            query=query,
            rewrite_time=round(t_rewrite, 3),
            faiss_time=round(t_search, 3),
            llm_time=round(t_llm, 3),
            total_time=total_time,
            returned_count=len(docs),
            was_truncated=was_truncated,
            documents=sources,
        )

        # ---- 语义兜底评分结果 → 告警事件 ----
        if rating_result.get("used_ollama"):
            yield {
                "type": "warning",
                "message": "语义评分已切换至本地 Ollama 兜底，当前 LLM 提供商可能存在问题，建议联系厂商确认或刷新模型列表。",
            }

        yield {"type": "done", "sources": sources, "conversation_id": conversation_id, "message_id": msg_id, "reasoning_content": "".join(reasoning_list) if reasoning_list else None}

    def _infer_rating(self, conversation_id: str, user_query: str) -> dict:
        """语义兜底评分：分析用户对上一轮回答的态度

        返回：
            {"used_ollama": True}  — 使用 Ollama 兜底
            {"used_ollama": False} — OpenRouter 正常
            {}                      — 未评分（无上一条消息）
        """
        prev = self.memory.get_unrated_assistant_message(conversation_id)
        if not prev:
            return {}

        prev_answer = prev["content"][:500]
        prompt = f"""分析用户对上一轮回答的态度，只输出一个数字（1-5）。

1 = 明显不满 / 直接质疑回答错误
2 = 不太满意 / 要求改进或重新回答
3 = 中性 / 继续提问或追问
4 = 满意 / 深入追问想了解更多细节
5 = 非常满意 / 明确感谢或肯定

上一轮回答（前500字）：
{prev_answer}

用户的下一句话：
{user_query}

只输出数字："""

        config_path = Path(__file__).parent.parent / "agent_data" / "llm_config.json"
        openrouter_key = None
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            providers = cfg.get("providers", {})
            openrouter_key = providers.get("OpenRouter", {}).get("api_key", "")
        except:
            pass

        rating = None
        used_ollama = False
        if openrouter_key:
            try:
                resp = httpx.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {openrouter_key}", "Content-Type": "application/json"},
                    json={"model": "openai/gpt-oss-120b:free", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 5},
                    timeout=15,
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    rating = int("".join(c for c in text if c.isdigit())[:1])
            except:
                pass

        if rating is None:
            used_ollama = True
            try:
                resp = httpx.post(
                    "http://localhost:11434/v1/chat/completions",
                    json={"model": "qwen2.5:7b", "messages": [{"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 5},
                    timeout=15,
                )
                if resp.status_code == 200:
                    text = resp.json()["choices"][0]["message"]["content"].strip()
                    rating = int("".join(c for c in text if c.isdigit())[:1])
            except:
                pass

        if rating and 1 <= rating <= 5:
            self.memory.update_rating(prev["id"], rating, semantic=True)
            logger.info(f"语义评分: msg_id={prev['id']} rating={rating}")

        return {"used_ollama": used_ollama} if used_ollama else {}

    def stats(self) -> dict:
        retriever_stats = self.retriever.stats()
        return {
            **retriever_stats,
            "llm": self.llm.to_config(),
            "top_k": self.top_k,
            "use_rerank": self.use_rerank,
            "use_history": self.use_history,
            "conversations": len(self.memory.get_conversations()),
        }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    agent = CyberAgent()

    test_queries = [
        "等保三级对安全计算环境有什么访问控制要求？",
        "关键信息基础设施的供应链安全管理有哪些要求？",
        "数据出境安全评估的流程是什么？",
        "个人信息保护法对用户同意有什么要求？",
    ]

    for q in test_queries:
        print(f"\n{'='*70}")
        print(f"Q: {q}")
        print(f"{'='*70}")
        result = agent.ask(q)
        print(f"\nA:\n{result['answer'][:500]}")
        print(f"\n来源 ({len(result['sources'])} 条):")
        for s in result['sources']:
            cat_icon = {"01-国家法律": "📜", "02-等保国标": "🛡️", "03-CII关基": "🔐", "04-通信行业": "📡"}.get(s['category'], "📄")
            print(f"  {cat_icon} {s['file_name']} / {s['section']} ({s['score']})")
        print(f"\n⏱ {result['stats']}")