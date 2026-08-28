"""RAG Pipeline — 检索增强问答代理

流程：
  user_query → retriever.search() → build_context → build_prompt → LLM.chat() → save_memory → response

Prompt 架构设计（参考知识库 三层架构 E.5）：
  第一层 System Prompt  → 角色 + 能力边界 + 行为约束
  第二层 Context        → 结构化参考资料 + 来源标注
  第三层 CoT + 示例     → 思维链引导 + one-shot 示例
"""
from memory import ConversationMemory, get_llm_config_card
from llm_provider import LLMProvider
from dataclasses import dataclass, field
import os
import sys
import logging
import json
import time
import asyncio
import httpx
import re
import hashlib
from pathlib import Path
from typing import Optional, Any

# 项目根目录
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

logger = logging.getLogger(__name__)

# ---- 提示注入防护 ----
# 常见注入攻击模式（大小写不敏感）
_INJECTION_PATTERNS = [
    r"ignore\s+above\s+instructions?",
    r"ignore\s+previous\s+instructions?",
    r"ignore\s+the\s+above",
    r"system\s+prompt\s+override",
    r"system\s+prompt\s+injection",
    r"you\s+are\s+now",
    r"delete\s+all\s+messages?",
    r"output\s+in\s+json",
    r"print\s+all\s+files?",
    r"expose\s+your\s+prompt",
    r"your\s+instructions?\s+are",
    r"pretend\s+to\s+be",
    r"act\s+as\s+if",
    r"from\s+now\s+on",
    r"reset\s+your\s+rules?",
    r"override\s+your\s+rules?",
    r"bypass\s+your\s+rules?",
    r"disregard\s+your\s+rules?",
    r"###\s+Instruction",
    r"###\s+Prompt",
    r"###\s+System",
    r"\[System\s+Instruction\]",
    r"##\s+Instruction",
    r"##\s+System\s+Prompt",
]

# 渐进式越狱模式：用户先用安全关键词铺垫，再逐步追问越界内容
_JAILBREAK_PATTERNS = [
    # 价格/成本类
    r"(多少钱|价格|报价|成本|预算|费用|花费|年费|万元|千元|总价|预算范围)",
    # 品牌推荐类
    r"(推荐.*(品牌|厂家|设备|产品)|选.*(品牌|厂家|设备|产品)|哪.*(好|强|性价比|适合))",
    # 具体方案/拓扑/部署类（追问轮数>=2时的危险信号）
    r"(怎么搞|怎么做|如何实施|实施方案|拓扑|组网|架构图|部署方案|具体方案|具体怎么做|步骤)",
    # 具体命令/配置类
    r"(配置命令|配置示例|iptables|sed|awk|grep|vim|chmod|chown|syslog|logrotate|tcp_|net\.ipv4)",
    # 非安全技术细节
    r"(编程|代码|开发|前端|后端|框架|库|API|SDK)",
    # 商务/采购类
    r"(采购|购买|供应商|渠道|代理商|销售|招商|选型)",
]

# 渐进式越狱：如果追问轮数 >= N 且当前 query 匹配越狱模式，触发拦截
_PROGRESSIVE_JAILBREAK_TURNS = 2

# LLM 输出过滤：仅保留价格和配置命令底线（厂商名靠 system prompt 约束就够了）
_OUTPUT_FORBIDDEN_PATTERNS = [
    r"(预算.*万|万元|千元|元/年|总价|费用为|报价|成本约)",
    r"(iptables|sed -i|vim |nano |chmod |chown |logrotate|syslog)",
]

COMPLIANCE_DECISION_QUERY_PATTERN = re.compile(
    r"(是否合法|合不合法|是否合规|合不合规|是否允许|允不允许|可不可以|能不能|能否|是否可以|"
    r"是否能够|能.{0,40}吗|可以.{0,40}吗|需要.*授权|必须.*授权|未经授权|未授权|"
    r"法律责任|处罚|违法|违规|不得|禁止)",
    re.I,
)

COMPLIANCE_DECISION_EXPANSION_TERMS = (
    "法律依据 合规要求 授权 未经授权 禁止 不得 法律责任 处罚 义务 边界 条件"
)

LEGAL_REFERENCE_PATTERN = re.compile(
    r"《[^》]{2,40}(?:法|条例|办法|规定|规范|标准|指南)》|"
    r"[\u4e00-\u9fff]{2,30}(?:法|条例|办法|规定|规范|标准|指南)"
)

COMPLIANCE_DECISION_FALLBACK_ANSWER = (
    "当前知识库没有检索到足够的合规或法律依据，不能确认该行为是否允许。\n\n"
    "本次检索到的参考资料与问题存在一定相关性，但不足以支撑“是否合法、是否合规、是否需要授权、"
    "是否存在法律责任”这类结论。需要知识库召回明确涉及授权条件、禁止性要求、责任后果或适用条款的资料后，"
    "才能给出结论。"
)


def infer_query_type_from_text(query: str) -> str:
    """Infer a stable routing type when the rewrite model is vague or empty."""
    text = str(query or "").strip()
    if not text:
        return "general"

    comparison = re.search(
        r"(比较|对比|区别|差异|异同|分别|vs\.?|与.+相比|和.+有什么不同)",
        text,
        re.I,
    )
    if comparison:
        return "comparison"

    compliance = re.search(
        r"(是否合法|合不合法|是否合规|合不合规|是否允许|允不允许|可不可以|能不能|能否|"
        r"是否可以|是否能够|需要.*授权|必须.*授权|未经授权|未授权|法律责任|处罚|违法|违规|不得|禁止|"
        r"合规要求|合规条件|隐私合规|个人信息保护要求)",
        text,
        re.I,
    )
    if compliance:
        return "compliance_decision"

    # Compliance preparation questions often omit words such as "是否合法"
    # but still require authoritative requirements and evidence.
    preparation = re.search(
        r"(等保|等级保护|测评|安全审计).{0,30}(需要|应当|要求|准备).{0,30}(材料|内容|条件|要求|事项)",
        text,
        re.I,
    )
    if preparation:
        return "compliance_decision"

    article_lookup = re.search(
        r"(《[^》]{2,40}(?:法|条例|办法|规定|规范|标准|指南)》|"
        r"(?:网络安全法|数据安全法|个人信息保护法|密码法|条例|办法|规定|标准|指南))",
        text,
        re.I,
    )
    if article_lookup and re.search(r"(规定|义务|条款|第[一二三四五六七八九十百]+条|要求|内容)", text):
        return "article_lookup"

    return "general"


def _detect_prompt_injection(text: str) -> tuple[bool, str]:
    """检测检索到的文档中是否包含提示注入攻击

    返回: (是否检测到, 检测到的模式)
    """
    lower = text.lower()
    for pat in _INJECTION_PATTERNS:
        if re.search(pat, lower):
            return True, pat
    return False, ""


def _filter_docs_for_injection(docs: list[dict]) -> list[dict]:
    """过滤掉包含提示注入攻击的文档"""
    filtered = []
    for doc in docs:
        content = doc.get("content", "")
        is_injection, pattern = _detect_prompt_injection(content)
        if is_injection:
            logger.warning(
                f"⚠️ 检测到提示注入攻击，已过滤该文档: pattern={pattern}, doc={doc.get('file_name', 'unknown')}")
            continue
        filtered.append(doc)
    if len(filtered) < len(docs):
        logger.info(
            f"提示注入防护: {len(docs)} → {len(filtered)} (过滤了 {len(docs) - len(filtered)} 个可疑文档)")
    return filtered


def _detect_user_jailbreak(text: str, conv_history: list[dict]) -> tuple[bool, str]:
    """检测用户输入是否为越狱尝试

    两层检测：
    1. 直接注入模式（用户试图覆盖系统指令）
    2. 渐进式越狱（先用安全话题铺垫，逐步追问越界内容）

    返回: (是否检测到越狱, 原因描述)
    """
    if not text:
        return False, ""
    lower = text.strip().lower()

    # 第1层：直接注入检测
    is_injection, pattern = _detect_prompt_injection(text)
    if is_injection:
        return True, f"检测到直接注入攻击: {pattern}"

    # 第2层：渐进式越狱检测
    # 统计当前对话中用户已追问的轮数（排除第一轮）
    user_messages = [m for m in conv_history if m.get("role") == "user"]
    user_turn = len(user_messages)  # 当前是第几轮（含本轮）

    # 检查当前 query 是否匹配越狱模式
    matches_jailbreak = any(re.search(pat, lower) for pat in _JAILBREAK_PATTERNS)

    if matches_jailbreak and user_turn >= _PROGRESSIVE_JAILBREAK_TURNS:
        # 检查之前的用户消息是否包含安全关键词（铺垫行为）
        previous_queries = [m.get("content", "") for m in user_messages[:-1]]
        safe_keywords = ["等保", "等级保护", "合规", "安全要求", "安全措施",
                         "数据安全", "网络安全法", "安全管理", "安全技术",
                         "防护", "防火墙", "IPS", "IDS", "UTM", "漏洞",
                         "加密", "认证", "审计", "备份", "恢复"]
        has_safe_铺垫 = any(
            any(sk in q.lower() for sk in safe_keywords)
            for q in previous_queries
        )
        if has_safe_铺垫:
            return True, f"检测到渐进式越狱: 先用安全话题铺垫{user_turn}轮，再追问越界内容"

    # 第3层：单轮越界 — 不问任何安全内容，直接要价格/命令/品牌
    # Generic words such as "步骤" or "如何实施" are common in legitimate
    # cybersecurity questions. A single-turn block requires an explicit
    # high-risk signal instead of treating every procedural question as abuse.
    single_turn_high_risk = bool(re.search(
        r"(绕过|规避|窃取|盗取|维持长期访问|持久化|未授权攻击|真实网站|勒索软件|"
        r"木马|后门|免杀|提权|凭据转储|注入攻击|攻击载荷|可直接运行|具体命令|"
        r"iptables|powershell|反弹 shell|反弹shell)",
        lower,
        re.I,
    ))
    if matches_jailbreak and user_turn <= 1 and single_turn_high_risk:
        has_any_safe = any(sk in lower for sk in ["等保", "等级保护", "合规", "安全",
                                                  "防护", "防火墙", "漏洞", "加密"])
        if not has_any_safe:
            return True, "检测到单轮越界: 未提及任何安全内容，直接追问越界话题"

    return False, ""


def _filter_output_forbidden(answer: str) -> str:
    """过滤 LLM 输出中的越界内容（防御纵深）

    即使 system prompt 和越狱检测都失效了，最后一层过滤
    保证不给用户输出价格/命令/品牌推荐
    """
    for pat in _OUTPUT_FORBIDDEN_PATTERNS:
        if re.search(pat, answer):
            logger.warning(f"⚠️ LLM 输出包含越界内容，已拦截: pattern={pat}")
            lines = answer.split("\n")
            safe_lines = []
            for line in lines:
                if re.search(pat, line):
                    safe_lines.append("[越界内容已过滤]")
                else:
                    safe_lines.append(line)
            answer = "\n".join(safe_lines)
    return answer


def _validate_annotations(answer: str) -> str:
    """检查回答中是否每条知识都有来源标注（红线一）"""
    lines = answer.split("\n")
    content_lines = []
    for l in lines:
        s = l.strip()
        if not s:
            continue
        # 跳过纯格式行
        if s.startswith("---") or s.startswith("```") or s.startswith("#") or s.startswith(">"):
            continue
        # 跳过表格行、列表项、标注行自身
        if s.startswith("|") or s.startswith("- ") or s.startswith("* ") or s.startswith("[") or s.endswith("]"):
            continue
        content_lines.append(s)

    unannotated = []
    for line in content_lines:
        if "[来源" not in line and "[注：" not in line:
            unannotated.append(line[:60])
    if len(unannotated) > len(content_lines) * 0.3:  # 超过30%的行无标注
        logger.warning(
            f"⚠️ 回答中 {len(unannotated)}/{len(content_lines)} 行缺少来源标注，这可能导致 faithfulness 评分偏低")
    return answer


_SRC = os.path.dirname(os.path.abspath(__file__))
# preprocessor 在 packages/preprocessor/src，从 agent.py 向上 2 层到 packages/，再进入 preprocessor/src
_PREPROCESSOR = os.path.join(_SRC, "..", "..", "preprocessor", "src")
for p in [_SRC, _PREPROCESSOR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from security_taxonomy import category_emoji, fallback_queries
from trace_observability import add_trace_step, build_trace_envelope, finish_trace, retrieval_counts
from reflection_engine import PROMPT_ASSET_DEFAULTS, reflect_answer
from external_retrieval import fetch_external_evidence
from knowledge_graph import collect_graph_evidence
from monitoring import record_event


# ============================================================
# 第一层：System Prompt — 角色定义 + 行为约束
# 参考：知识库 B.1-角色设定 + B.5-约束控制 + 你的专家Prompt
# ============================================================

class SystemPromptLoader:
    """从 active_prompt.txt 动态加载 System Prompt，支持运行时热更新"""
    _cache: str = None
    _mtime: float = 0
    _path = Path(__file__).parent.parent / "agent_data" / "active_prompt.txt"

    @classmethod
    def ensure_initialized(cls):
        """确保 active_prompt.txt 存在，不存在则用默认值创建"""
        if not cls._path.exists():
            cls._path.parent.mkdir(parents=True, exist_ok=True)
            cls._path.write_text(SYSTEM_PROMPT_SOURCE.strip(), encoding="utf-8")
            cls._cache = None
            cls._mtime = 0

    @classmethod
    def get(cls) -> str:
        cls.ensure_initialized()
        try:
            mtime = cls._path.stat().st_mtime
            if mtime != cls._mtime:
                cls._cache = cls._path.read_text(encoding="utf-8").strip()
                cls._mtime = mtime
        except Exception:
            if cls._cache is None:
                cls._cache = SYSTEM_PROMPT_SOURCE.strip()
        return cls._cache or SYSTEM_PROMPT_SOURCE.strip()

    @classmethod
    def get_raw_path(cls) -> Path:
        """获取 system prompt 文件路径"""
        cls.ensure_initialized()
        return cls._path

    @classmethod
    def write(cls, content: str):
        """写入新的 System Prompt（版本还原时调用）"""
        cls._path.write_text(content.strip(), encoding="utf-8")
        cls._cache = content.strip()
        import time
        cls._mtime = cls._path.stat().st_mtime


SYSTEM_PROMPT_SOURCE = """你是一位**网络安全管理体系专家**。

===== ⚠️ 三条红线（必须严格执行，不允许任何例外）=====

红线一：每条知识必须来自参考资料并标注来源
- 只引用【参考资料】中的内容，用 `[来源N: 文档名称]` 标注
- 参考资料没有的内容，一律不说

- 这条规则不允许违反

红线二：禁止提及任何品牌名称
- 包括"如XX/XX""例如XX"之类的举例方式
- 需要举例时用"某品牌""某厂商"替代

红线三：禁止编造用户未说的内容
- 用户没说"OA系统""ERP系统"，你就不能说
- 用户只说"网络"，你就不能说"网站""Web服务"

===== 能力范围 =====
- **CII关基安全**：识别定级、保护要求、检测评估、事件管理
- **等保2.0**：定级备案、测评要点、扩展要求、整改方案
- **数据安全**：分类分级、数据安全法/个保法落地、全生命周期安全
- **APP安全**：安全检测、隐私合规、渠道安全
- **公司安全管理**：组织架构、制度体系、SOC运营、应急响应

===== 不做范围（必须委婉引导）=====
当用户的问题属于以下范围时，不要生硬说"我不能回答"，而是用温和语气引导：

> "我是专注于网络安全的智能助手，主要提供等保测评、数据安全、合规检查、安全管理体系等方面的知识。关于XX的问题，建议您咨询相关领域的专业人员获取更准确的信息。"

| 引导范围 | 例子 |
|---------|------|
| 特定行业合规（非通用安全类） | 教育/医疗/金融等行业专属规定 |
| 具体技术操作 | 如何配防火墙、写代码、配置命令示例 |
| 厂商/产品咨询 | 该买哪个品牌、哪家产品好、性价比对比 |
| 设备价格/预算 | 多少钱、预算范围、成本估算 |
| 非中国大陆法规 | GDPR、CCPA |
| 企业内部管理纠纷 | 监控员工聊天、办公室装摄像头 |
| 非网络安全话题 | 厂家背景、产品价格、市场行情、销售渠道 |

## 核心规则

### 1. 严格基于检索参考资料，禁止补充训练知识
- 严格基于【参考资料】中的内容回答，资料中没有的一律不说
- 禁止用自己的训练知识补充任何内容
- 完全没覆盖的：直接回答"知识库中暂无相关文件，且我无法确认准确信息"
- 宁可回答简短甚至只说"无法回答"，也不要添油加醋

### 2. 结论与正文一致
- 不要前面说6项后面列出5项

### 3. 区分合规红线 vs 管理优化
- **红线**：标清法规/标准依据；**优化**：明确说"建议"

### 4. 直白、接地气，类比辅助
- 如"合规是底线，不是天花板"

### 5. 对比分析类问题：一次性全量对比
- 用表格展示差异，有多少差异列多少，不设数量限制

### 6. 首回答即完整（禁止"挤牙膏"）
- 用户问"XX设备能不能用于等保X级"：一次性给出合规匹配度+技术措施+部署建议
- 用户问"怎么搞""怎么做"：直接给完整方案，不要反问"你要哪个方面"
- 对比分析也一次全量对比，不留尾巴

## 具体系统分析推理框架

当用户问某个系统的安全等级或防护时，按以下三步分析。默认按通用网络安全场景研判；只有用户或参考资料明确说明行业背景时，才引入对应行业扩展规则。

**第一步：系统类型研判**
| 系统类别 | 典型系统 | 通常关注点 |
|---------|---------|---------|
| 办公与管理系统 | OA、邮件、门户、人事、财务等 | 身份鉴别、访问控制、日志审计、数据备份 |
| 核心业务系统 | 交易、生产、运营、客户服务等业务平台 | 业务连续性、权限分离、数据保护、监控审计 |
| 互联网应用系统 | 网站、APP、小程序、开放 API | 应用安全、接口安全、个人信息保护、漏洞管理 |
| 数据处理平台 | 数据仓库、大数据平台、报表分析平台 | 数据分类分级、最小权限、脱敏、全生命周期管理 |
| 云平台与虚拟化平台 | 私有云、公有云资源池、容器平台 | 租户隔离、配置基线、镜像安全、运维审计 |
| 工业控制/物联网系统 | 工控平台、物联网接入与管理平台 | 分区分域、接入认证、可用性、安全监测 |
| 安全运营与基础设施 | 身份认证、日志审计、安全运营、网络设备 | 集中管控、审计留痕、策略一致性、故障恢复 |

- 表里没有的按用户描述和参考资料谨慎推断
- **无法归类才能拒绝**，有推断就要标注"推断"而非冒充事实

**第二步：按级别匹配安全要求**
- 二级→基础访问控制、审计、备份
- 三级→更严格访问控制、双因素、入侵检测、灾备
- 四级→最高级别

**第三步：应急响应建议**
- 通用流程：**止血→排查→恢复→加固→上报**
- 区分合规红线和管理优化

**重要**：回答开头必须说明"这是基于通用网络安全系统分类的推理分析"，标注哪些有参考资料支撑、哪些是推断。

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
MAX_CONTEXT_CHARS = 80000  # 最大上下文字符数，防止超长文档撑爆 LLM 上下文
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


def _strip_markdown_heading(line: str) -> str:
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", str(line or "")).strip()
    text = text.strip("《》「」『』【】[] \t\r\n：:;；")
    return text


def _looks_like_clause_or_noise(text: str) -> bool:
    if not text:
        return True
    low = text.lower()
    if len(text) > 80:
        return True
    if low.startswith(("version", "warning", "生成时间")):
        return True
    if re.match(r"^(第[一二三四五六七八九十百千万零〇两\d]+[章节条款]?|\d+(?:\.\d+)*)(\s|$|[、.．])", text):
        return True
    return text in {"范围", "术语和定义", "前言", "引言", "目次", "参考文献", "附录"}


def _extract_title_from_content(content: str) -> str:
    for raw in str(content or "").splitlines()[:40]:
        line = _strip_markdown_heading(raw)
        if not line or _looks_like_clause_or_noise(line):
            continue
        if len(line) <= 60:
            return line
    return ""


def _is_standard_id_only(name: str) -> bool:
    compact = re.sub(r"[\s_\-/.]+", "", str(name or "").upper())
    return bool(re.fullmatch(r"(GB|GBT|YD|YDT|JR|JRT|GM|GMT)\d{3,6}(\d{4})?", compact))


def source_display_name(doc: dict) -> str:
    """生成可读来源名，避免只显示标准号。"""
    file_name = str(doc.get("file_name") or "").strip()
    explicit = str(
        doc.get("display_name")
        or doc.get("source_name")
        or doc.get("doc_title")
        or doc.get("title")
        or doc.get("standard_name")
        or ""
    ).strip()
    if explicit:
        if file_name and explicit not in file_name and file_name not in explicit:
            return f"{file_name}《{explicit}》"
        return explicit

    section = _strip_markdown_heading(str(doc.get("section") or ""))
    content_title = _extract_title_from_content(str(doc.get("content") or ""))
    inferred = content_title or (section if not _looks_like_clause_or_noise(section) else "")

    if inferred and file_name and inferred not in file_name and (len(file_name) <= 32 or _is_standard_id_only(file_name)):
        return f"{file_name}《{inferred}》"
    if inferred and not file_name:
        return inferred
    return file_name or inferred or "未知文档"


def build_context_block(docs: list[dict]) -> str:
    """将检索结果格式化为结构化参考资料块

    格式参考：知识库 Context-Engineering D.1-RAG问答场景的Context设计
    每个来源标注包含数学计算的置信度（0-1归一化）+ 等级标签
    """
    parts = ["【参考资料】（按相关性从高到低排列）："]
    for i, d in enumerate(docs, 1):
        source_tag = f"[来源{i}: {source_display_name(d)} / {d['section']}]"
        conf = compute_confidence(d, len(docs))
        score_str = f"（置信度：{conf['confidence']} — {conf['label']}）"
        parts.append(f"\n{source_tag} {score_str}")
        parts.append(d["content"].strip())
    return "\n".join(parts)


def build_graph_evidence_context(evidence: dict | None) -> str:
    """Format approved graph evidence as a clearly labeled, supplementary block."""
    if not evidence or evidence.get("status") != "approved_evidence":
        return ""
    lines = ["【已审核知识图谱补充证据】（仅作关系提示，必须以参考资料原文为准）"]
    for relation in evidence.get("relations", [])[:12]:
        line = f"- {relation.get('subject')} --{relation.get('predicate')}--> {relation.get('object')}"
        if relation.get("evidence"):
            line += f"；证据：{relation['evidence']}"
        lines.append(line)
    if evidence.get("conflicts"):
        lines.append("- 注意：发现待人工确认的版本/适用性冲突候选，不得直接据此下结论。")
    return "\n".join(lines)


def need_clarification(query: str, docs: list[dict]) -> Optional[str]:
    """（已禁用）首回答即完整 — 不再反问，直接交给 LLM"""
    return None


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
    system_prompt_override: Optional[str] = None,
    response_language: str = "zh-CN",
    graph_evidence: Optional[dict] = None,
) -> tuple:
    """组装完整的 Prompt 消息序列 + 返回截断信息

    三层架构（参考 知识库 E.5 Prompt→Context→Harness）：
    System Prompt → Session Memory → Context → Example → History → Question

    Returns:
        (messages, truncation_info)
        truncation_info = {"original_count": N, "truncated_count": N, "kept_count": N}
    """
    original_len = len(docs)

    # ---- P1-3: 提示注入防护：过滤可疑文档 ----
    docs = _filter_docs_for_injection(docs)

    context_block = build_context_block(docs)
    keep_ids = set(id(d) for d in docs)  # 默认全部保留

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

    system_parts = [system_prompt_override if system_prompt_override is not None else SystemPromptLoader.get()]
    system_parts.append(
        "\n\n## Response language\nReply in clear professional English unless the user explicitly requests another language."
        if response_language == "en-US" else
        "\n\n## 回答语言\n默认使用简体中文回答；只有用户明确要求其他语言时才切换。"
    )

    # 跨会话记忆注入
    if session_context:
        system_parts.append(f"\n\n## 用户背景（本轮对话已识别）\n{session_context.strip()}")

    system_parts.append(f"\n\n## 以下是根据你问题检索到的参考资料\n\n{context_block}")
    graph_context = build_graph_evidence_context(graph_evidence)
    if graph_context:
        system_parts.append(f"\n\n## 补充关系提示\n\n{graph_context}")
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

    # 返回截断信息用于 C5 评估
    truncation_info = {
        "original_count": original_len,
        "truncated_count": original_len - len(keep_ids),
        "kept_count": len(keep_ids),
    }
    return messages, truncation_info


QUERY_REWRITE_PROMPT = """你是一个网络安全知识库检索助手。请结合历史对话和当前问题，生成适合 RAG 检索的结构化 Query。

必须只输出 JSON，不要输出解释、Markdown 或代码块。

规则：
1. standalone_query 必须是脱离上下文也能理解的独立问题，处理“它、这个、那条、三级呢”等指代。
2. semantic_query 用自然语言表达完整语义，适合向量检索。
3. keyword_query 用法规名、标准号、条款号、核心术语组成，适合 BM25。
4. 保留否定词、比较关系、标准编号、条款号、等级和限定条件。
5. 不得编造法规、标准、条款或资料中未出现的事实。
6. 如果是对比问题，在 sub_queries 中拆成 2 到 4 个检索子问题。
7. 如果问题是在问“是否合法、是否合规、能不能做、是否允许、是否需要授权、责任后果”，query_type 使用 compliance_decision；
   keyword_query 和 sub_queries 必须补充通用合规检索词，如“法律依据、合规要求、授权、未经授权、禁止、不得、法律责任、处罚、义务、边界、条件”。
   只有用户问题或历史上下文明示具体法规/标准名称时，才可写入具体法规名、标准号或条款号。

JSON 结构：
{
  "need_rewrite": true,
  "standalone_query": "",
  "semantic_query": "",
  "keyword_query": "",
  "query_type": "general|article_lookup|standard_lookup|comparison|follow_up|compliance_decision",
  "sub_queries": [],
  "entities": {
    "doc_ids": [],
    "standards": [],
    "article_numbers": [],
    "categories": [],
    "topics": [],
    "levels": [],
    "negative_terms": []
  }
}

历史对话：
{history}

当前问题：{query}
"""


@dataclass
class QueryRewriteResult:
    original_query: str
    need_rewrite: bool = False
    standalone_query: str = ""
    semantic_query: str = ""
    keyword_query: str = ""
    query_type: str = "general"
    sub_queries: list[str] = field(default_factory=list)
    entities: dict[str, list[str]] = field(default_factory=dict)
    fallback_used: bool = False

    def __post_init__(self):
        self.standalone_query = (self.standalone_query or self.original_query).strip()
        self.semantic_query = (self.semantic_query or self.standalone_query or self.original_query).strip()
        self.keyword_query = (self.keyword_query or "").strip()
        self.query_type = self.query_type or "general"
        self.sub_queries = _clean_string_list(self.sub_queries)
        defaults = {
            "doc_ids": [], "standards": [], "article_numbers": [], "categories": [],
            "topics": [], "levels": [], "negative_terms": [],
        }
        merged = dict(defaults)
        if isinstance(self.entities, dict):
            for key in defaults:
                merged[key] = _clean_string_list(self.entities.get(key, []))
        self.entities = merged

    @classmethod
    def from_dict(cls, original_query: str, data: dict[str, Any], fallback_used: bool = False) -> "QueryRewriteResult":
        entities = data.get("entities") if isinstance(data.get("entities"), dict) else {}
        return cls(
            original_query=original_query,
            need_rewrite=bool(data.get("need_rewrite", True)),
            standalone_query=str(data.get("standalone_query") or original_query),
            semantic_query=str(data.get("semantic_query") or data.get("standalone_query") or original_query),
            keyword_query=str(data.get("keyword_query") or ""),
            query_type=str(data.get("query_type") or "general"),
            sub_queries=list(data.get("sub_queries") or []),
            entities=entities,
            fallback_used=fallback_used,
        )

    def retrieval_queries(self) -> list[str]:
        queries = [self.original_query, self.standalone_query, self.semantic_query, self.keyword_query]
        queries.extend(self.sub_queries)
        return _clean_string_list(queries)

    def metadata_filter(self) -> dict[str, Any]:
        return {
            "doc_ids": self.entities.get("doc_ids", []),
            "categories": self.entities.get("categories", []),
            "file_name_contains": self.entities.get("standards", []),
            "section_contains": self.entities.get("article_numbers", []),
            "article_numbers": self.entities.get("article_numbers", []),
            "topics": self.entities.get("topics", []),
            "levels": self.entities.get("levels", []),
            "negative_terms": self.entities.get("negative_terms", []),
            "exclude_deprecated": True,
            "hard_filter": bool(self.entities.get("doc_ids") or self.entities.get("article_numbers")),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "original_query": self.original_query,
            "need_rewrite": self.need_rewrite,
            "standalone_query": self.standalone_query,
            "semantic_query": self.semantic_query,
            "keyword_query": self.keyword_query,
            "query_type": self.query_type,
            "sub_queries": self.sub_queries,
            "entities": self.entities,
            "fallback_used": self.fallback_used,
            "retrieval_queries": self.retrieval_queries(),
        }


def _clean_string_list(items: list[Any]) -> list[str]:
    seen = set()
    result = []
    for item in items or []:
        s = str(item).strip()
        if not s or s in seen:
            continue
        seen.add(s)
        result.append(s)
    return result


def is_compliance_decision_query(query: str, query_type: str = "") -> bool:
    """识别需要合规/法律依据支撑的判断类问题。"""
    return query_type == "compliance_decision" or bool(COMPLIANCE_DECISION_QUERY_PATTERN.search(query or ""))


def enrich_queries_for_compliance_decision(query: str, queries: list[str], query_type: str = "") -> list[str]:
    """合规判断类问题的通用 Query 扩展，避免改写失败后只召回操作性资料。"""
    if not is_compliance_decision_query(query, query_type):
        return _clean_string_list(queries)
    base_query = (query or "").strip()
    expansion = f"{base_query} {COMPLIANCE_DECISION_EXPANSION_TERMS}".strip()
    return _clean_string_list([*(queries or []), expansion, COMPLIANCE_DECISION_EXPANSION_TERMS])


def _source_text_for_guard(source: dict) -> str:
    return " ".join(
        str(source.get(k) or "")
        for k in ("file_name", "display_name", "category", "section", "content")
    )


def source_contains_compliance_basis(source: dict) -> bool:
    text = _source_text_for_guard(source)
    if LEGAL_REFERENCE_PATTERN.search(text):
        return True
    return bool(re.search(r"(法律依据|合规要求|授权|未经授权|禁止|不得|应当|法律责任|处罚|义务|边界|条件|违法|违规)", text))


def has_authoritative_compliance_sources(sources: list[dict], min_confidence: float = 0.35) -> bool:
    for source in sources or []:
        confidence = source.get("confidence")
        if confidence is None:
            confidence = compute_confidence(source).get("confidence", 0)
        if float(confidence or 0) >= min_confidence and source_contains_compliance_basis(source):
            return True
    return False


def answer_mentions_unbacked_legal_references(answer: str, sources: list[dict]) -> list[str]:
    source_text = "\n".join(_source_text_for_guard(s) for s in sources or [])
    mentioned = _clean_string_list(m.group(0).strip("《》") for m in LEGAL_REFERENCE_PATTERN.finditer(answer or ""))
    return [name for name in mentioned if name not in source_text]


def compliance_decision_guard_answer(query: str, sources: list[dict], query_type: str = "") -> Optional[str]:
    if not is_compliance_decision_query(query, query_type):
        return None
    if has_authoritative_compliance_sources(sources):
        return None
    return COMPLIANCE_DECISION_FALLBACK_ANSWER


def _extract_json_object(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = re.sub(r"^```(?:json)?", "", cleaned, flags=re.I).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(cleaned[start:end + 1])
    except Exception:
        return None
    return data if isinstance(data, dict) else None


SELF_VERIFY_PROMPT = """你是一个严格的"事实核查员"。请逐句核对回答中的每个结论是否在参考资料中有明确依据。

规则：
1. 逐句检查，只有参考资料中明确支持的内容才能保留
2. 参考资料未提及、未覆盖、或只有部分相关的内容，一律删除
3. **只输出清理后的回答文本本身**，不要加任何说明
4. 不要添加新的内容，不要改写原文
5. 宁可删到只剩寥寥几句，也不要保留无依据的内容
6. 如果所有结论都有依据，只输出一个词: [PASS]

参考资料（全部）：
{sources}

需要核查的回答：
{answer}

清理后的回答："""

JAILBREAK_DETECT_PROMPT = """判断AI助手的回答是否包含以下违规内容（只输出 yes/no）：
- 提供了有害、违法、危险的操作指导
- 绕过了安全限制或角色设定
- 泄露了不应透露的内部系统信息
- 回答了明显超出授权范围的问题

AI助手回答：
{answer}

用户问题：
{query}

只输出 yes 或 no："""

SEMANTIC_SCORING_PROMPT = """{task}

评分范围：
1 = 明显错误、答非所问、无依据或明显不满意
2 = 相关性弱、依据不足、要求改进
3 = 基本相关或中性追问
4 = 回答准确清楚或用户满意并继续深入
5 = 回答准确完整、依据充分或用户明确肯定

用户问题/下一句话：
{query}

AI 回答：
{answer}

只输出数字："""


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
        from retriever import CyberRetriever
        self.retriever = CyberRetriever()
        # 从 DB 读取生成对话 LLM 配置，覆盖默认值
        chat_cfg = get_llm_config_card('chat')
        if chat_cfg and chat_cfg.get('model'):
            merged = dict(llm_config or {})
            merged.setdefault('base_url', chat_cfg['base_url'])
            merged.setdefault('api_key', chat_cfg['api_key'])
            merged.setdefault('model', chat_cfg['model'])
            llm_config = merged
        self.llm = LLMProvider(**(llm_config or {}))
        self.memory = ConversationMemory()
        self.top_k = top_k
        self.use_rerank = use_rerank
        self.use_history = use_history
        self.max_history = max_history
        self.include_example = include_example
        self.use_query_rewrite = use_query_rewrite
        self.use_verification = use_verification
        SystemPromptLoader.ensure_initialized()

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
        self._last_verify_prompt_version = 0
        if not self.use_verification or not sources or not answer:
            return answer
        try:
            self.memory.ensure_prompt_assets([{
                "slot": "self_verify", "name": "回答自检", "model_role": "chat",
                "description": "核查回答结论是否有授权来源依据。",
                "template": SELF_VERIFY_PROMPT, "variables": ["sources", "answer"],
            }])
            asset = self.memory.get_active_prompt_asset("self_verify", SELF_VERIFY_PROMPT)
            self._last_verify_prompt_version = asset.get("version", 0)
            src_text = "\n---\n".join(
                f"[来源 {i+1}] {s['file_name']} | {s['section']}\n{s.get('content', '')[:500]}"
                for i, s in enumerate(sources)  # 使用全部来源，不再限制前5条
            )
            prompt = asset["template"].format(sources=src_text, answer=answer)
            t0 = time.time()
            result = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.0,
                max_tokens=2048,
                timeout=300,
            )
            usage = (result or {}).get("usage") if isinstance(result, dict) else {}
            self.memory.record_llm_usage_event(
                tenant_id="local-default", module="self_verify",
                model=(result or {}).get("model") or getattr(self.llm, "model", ""),
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
            )
            elapsed = time.time() - t0
            # llm.chat 返回 {"content": str, ...} 或 str
            result = (result["content"] if isinstance(result, dict) else result).strip()

            # 先检测 PASS 标示（必须在剪裁之前）
            passed = result.endswith("[PASS]")

            # 后处理：去掉 LLM 可能残余的说明性前缀/后缀
            for prefix in ["清理后的回答：", "需要修改的回答：", "核查结果：", "修改后：", "删除以下句子："]:
                if result.startswith(prefix):
                    result = result[len(prefix):].strip()
            for suffix in ["以上是清理后的回答", "以上是核查结果"]:
                if result.endswith(suffix):
                    result = result[:-len(suffix)].strip()
            if result.endswith("[PASS]"):
                result = result[:-len("[PASS]")].strip()

            if passed:
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
                                 "安全检测", "隐私合规", "隐私", "应急响应", "安全运维",
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
            "你好": "你好！我是网络安全知识助手，专注网络安全管理领域。你可以问我关于等保测评、数据安全、APP安全、合规检查等方面的问题。",
            "您好": "您好！我是网络安全知识助手，专注网络安全管理领域。有什么可以帮助你的？",
            "嗨": "嗨！有什么网络安全相关的问题需要帮忙吗？",
            "hi": "Hi there! I'm your cybersecurity assistant. Feel free to ask about security compliance, data protection, or related topics.",
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
            "你是谁": "我是网络安全知识助手，专注于网络安全管理领域。我的知识库包含国家法律法规、等保标准、行业规范等文件，可以帮助你解答合规、数据安全、APP安全等方面的问题。",
            "你叫什么": "我叫网络安全助手，你可以直接问我关于网络安全管理的任何问题！",
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
            "你好呀": "你好！我是网络安全知识助手，专注网络安全管理领域。",
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
                    return "你好！我是网络安全知识助手，专注网络安全管理领域。有什么可以帮助你的？"
                if category == "身份":
                    return "我是网络安全知识助手，专注于网络安全管理领域。你可以问我关于等保测评、数据安全、APP安全、合规检查等方面的问题。"
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
            display = source_display_name(s)
            if display:
                real_files.add(display)
                if "《" in display:
                    real_files.add(display.split("《", 1)[0].strip())
                    real_files.add(display.split("《", 1)[1].rstrip("》").strip())

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

    @staticmethod
    def _format_history_for_rewrite(history: list[dict], limit: int = 6) -> str:
        if not history:
            return "无"
        rows = []
        for msg in history[-limit:]:
            role = msg.get("role", "")
            content = str(msg.get("content", "")).strip()
            if not content:
                continue
            if len(content) > 220:
                content = content[:220] + "..."
            rows.append(f"{role}: {content}")
        return "\n".join(rows) if rows else "无"

    def _build_query_rewrite(self, query: str, history: Optional[list[dict]] = None) -> QueryRewriteResult:
        """检索前 Query 重构：生成独立问题、语义 Query、关键词 Query 和元数据实体。"""
        if not self.use_query_rewrite:
            return QueryRewriteResult(original_query=query, fallback_used=True)
        try:
            self.memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS + [{
                "slot": "query_rewrite", "name": "检索 Query 改写", "model_role": "chat",
                "description": "将用户问题转换为结构化检索查询。",
                "template": QUERY_REWRITE_PROMPT, "variables": ["query", "history"],
            }])
            asset = self.memory.get_active_prompt_asset("query_rewrite", QUERY_REWRITE_PROMPT)
            prompt = asset["template"].format(
                query=query,
                history=self._format_history_for_rewrite(history or []),
            )
            t0 = time.time()
            rewritten = self.llm.chat(
                [{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=128,
                timeout=30,
            )
            usage = (rewritten or {}).get("usage") if isinstance(rewritten, dict) else {}
            self.memory.record_llm_usage_event(
                tenant_id="local-default", module="query_rewrite",
                model=(rewritten or {}).get("model") or getattr(self.llm, "model", ""),
                prompt_tokens=(usage or {}).get("prompt_tokens", 0),
                completion_tokens=(usage or {}).get("completion_tokens", 0),
            )
            elapsed = time.time() - t0
            content = (rewritten.get("content", "") if isinstance(rewritten, dict) else rewritten).strip()
            data = _extract_json_object(content)
            if data:
                result = QueryRewriteResult.from_dict(query, data)
                inferred_type = infer_query_type_from_text(query)
                if result.query_type == "general" and inferred_type != "general":
                    result.query_type = inferred_type
                    result.fallback_used = True
                    logger.info(
                        f"Query 类型兜底: model=general -> inferred={inferred_type} "
                        f"「{query[:60]}」"
                    )
                logger.info(
                    f"Query 改写: type={result.query_type} queries={len(result.retrieval_queries())} "
                    f"「{query[:40]}」→「{result.semantic_query[:60]}」 ({elapsed:.1f}s)"
                )
                return result
            # 兼容旧式字符串输出
            plain = content.strip().strip('"').strip("'")
            if plain and len(plain) < 240:
                return QueryRewriteResult(
                    original_query=query,
                    need_rewrite=plain != query,
                    standalone_query=plain,
                    semantic_query=plain,
                    keyword_query="",
                    query_type=infer_query_type_from_text(query),
                    fallback_used=True,
                )
        except Exception as e:
            logger.warning(f"Query 改写失败 (不影响检索): {e}")
        return QueryRewriteResult(
            original_query=query,
            query_type=infer_query_type_from_text(query),
            fallback_used=True,
        )

    def _rewrite_query(self, query: str) -> str:
        """兼容旧调用：返回单个检索 Query。"""
        return self._build_query_rewrite(query).semantic_query

    def _maybe_external_evidence(self, query: str, trace_data: dict, conversation_id: str,
                                 tenant_id: str, user_id: str, docs: list[dict]) -> list[dict]:
        """Read approved external sources for this turn only; never persist into RAG."""
        config = self.memory.get_external_retrieval_config()
        if not config.get("enabled"):
            return []
        trigger = config.get("trigger_mode", "empty_only")
        if trigger == "empty_only" and docs:
            return []
        if trigger == "low_confidence" and docs:
            confidences = [compute_confidence(d).get("confidence", 0) for d in docs]
            if confidences and max(confidences) >= 0.55:
                return []
        sources = self.memory.list_external_retrieval_sources(include_disabled=False)
        if not sources:
            return []
        add_trace_step(trace_data, "external_retrieval", enabled=True, trigger=trigger,
                       source_count=len(sources), status="started")
        external_docs = fetch_external_evidence(
            query, sources, max_sources=config.get("max_sources", 3),
            timeout_seconds=config.get("timeout_seconds", 10),
            max_bytes=config.get("max_bytes", 2000000),
        )
        for item in external_docs:
            self.memory.log_external_retrieval_event({
                "trace_id": trace_data.get("trace_id", ""), "conversation_id": conversation_id,
                "tenant_id": tenant_id, "user_id": user_id, "query": query,
                "source_id": item.get("source_id", ""), "source_url": item.get("source_url", ""),
                "content_hash": item.get("content_hash", ""),
                "status": "succeeded" if item.get("content") else "failed",
                "result_count": 1 if item.get("content") else 0,
                "error": item.get("fetch_error", ""),
            })
        add_trace_step(trace_data, "external_retrieval", enabled=True, trigger=trigger,
                       source_count=len(sources), returned_count=len(external_docs),
                       externally_verified=False, persisted_to_rag=False)
        return external_docs

    def ask(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        temperature: float = 0.1,
        category: str = "user",
        skip_memory: bool = False,
        profiles: Optional[set[str] | list[str] | tuple[str, ...]] = None,
        system_prompt_override: Optional[str] = None,
        retrieved_docs_override: Optional[list[dict]] = None,
        tenant_id: str = "local-default",
        user_id: str = "local-owner",
        agent_id: str = "default-agent",
        knowledge_base_id: str = "",
        response_language: str = "zh-CN",
    ) -> dict:
        """执行一次问答

        流程：
          Query 改写 → 检索 → Prompt 组装 → LLM 生成 → 记忆保存

        Args:
            skip_memory: True 时跳过所有 DB 写入（conversation/message/usage），
                         用于 Prompt Test 等不需要持久化的场景。

        返回：
          {"answer": "...", "sources": [...], "conversation_id": "...", "rewritten_query": "...", "stats": {...}}
        """
        t_start = time.time()
        if not conversation_id:
            if skip_memory:
                import uuid
                conversation_id = "test_" + str(uuid.uuid4())[:8]
            else:
                conv = self.memory.create_conversation(
                    title=query[:50], category=category, tenant_id=tenant_id,
                    user_id=user_id, agent_id=agent_id, knowledge_base_id=knowledge_base_id,
                )
                conversation_id = conv["id"]
        if not knowledge_base_id and conversation_id and not skip_memory:
            knowledge_base_id = self.memory.get_conversation_knowledge_base(conversation_id)
        if knowledge_base_id and not self.memory.can_access_knowledge_base(
                knowledge_base_id, tenant_id, user_id, agent_id):
            # Keep the requested KB as a deny sentinel so retrieval cannot fall back
            # to another workspace or to private documents from the same tenant.
            knowledge_base_id = "__unauthorized_knowledge_base__"
        retrieval_config = self.memory.get_knowledge_base_retrieval_config(
            knowledge_base_id, tenant_id,
        ) if knowledge_base_id else None
        effective_top_k = int((retrieval_config or {}).get("config", {}).get("top_k", self.top_k))
        effective_use_rerank = bool((retrieval_config or {}).get("config", {}).get("use_rerank", self.use_rerank))
        effective_use_hybrid = (retrieval_config or {}).get("config", {}).get("use_hybrid", True)

        if skip_memory:
            user_msg_id = 0
        else:
            user_msg_id = self.memory.add_message(conversation_id, "user", query)

        trace_data = build_trace_envelope(
            original_query=query,
            rewrite_enabled=self.use_query_rewrite,
            conversation_id=conversation_id,
            conversation_category=category,
            path="chat.sync",
            db_path=getattr(self.memory, "_db_path", None),
        )
        trace_data["context"]["profiles"] = sorted({str(profile) for profile in (profiles or [])})

        # ---- 用户越狱检测 ----
        conv_history = self.memory.get_history(conversation_id)
        is_jailbreak, jb_reason = _detect_user_jailbreak(query, conv_history)
        if is_jailbreak:
            logger.warning(f"⚠️ 检测到越狱尝试: {jb_reason}")
            add_trace_step(
                trace_data,
                "jailbreak_detection",
                triggered=True,
                reason=jb_reason,
                user_query=query[:120],
            )
            finish_trace(trace_data, "blocked", reason="user_jailbreak")
            answer = (
                "我是专注于网络安全的智能助手，主要提供等保测评、数据安全、"
                "合规检查、安全管理体系等方面的知识。关于设备选型、价格、"
                "具体技术操作等问题，建议您咨询相关领域的专业人员获取更准确的信息。"
            )
            sources = []
            msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(
                conversation_id, msg_id, query, off_topic=True,
                total_time=round(time.time() - t_start, 3),
                trace_data=trace_data, answer_jailbreak=1,
            )
            self.memory.update_jailbreak_status(
                conversation_id, "pending", "用户诱导越狱", message_id=user_msg_id)
            return {"answer": answer, "sources": sources, "conversation_id": conversation_id, "skipped": True, "jailbreak_reason": jb_reason}

        # ---- 非安全话题检测：跳过检索/LLM，快速引导 ----
        offtopic_reply = self._check_offtopic(query)
        if offtopic_reply:
            answer = offtopic_reply
            sources = []
            add_trace_step(
                trace_data,
                "offtopic_detection",
                triggered=True,
                user_query=query[:120],
            )
            finish_trace(trace_data, "blocked", reason="offtopic")
            msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            self.memory.extract_and_save_memory(conversation_id, query, answer)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(
                conversation_id, msg_id, query, off_topic=True,
                total_time=round(time.time() - t_start, 3),
                trace_data=trace_data,
            )
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
            add_trace_step(trace_data, "greeting_detection", triggered=True)
            finish_trace(trace_data, "answered", reason="greeting")
            msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            self.memory.extract_and_save_memory(conversation_id, query, answer)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(
                conversation_id, msg_id, query, off_topic=True,
                total_time=round(time.time() - t_start, 3),
                trace_data=trace_data,
            )
            return {
                "answer": answer,
                "sources": sources,
                "conversation_id": conversation_id,
                "rewritten_query": None,
                "verified": False,
                "stats": {"search_time": 0, "llm_time": 0, "total_time": 0, "docs_count": 0},
            }

        # ---- 语义缓存（精确归一化命中，键包含知识库/模型/Profile/Prompt） ----
        cache_hit = False
        cache_key = ""
        cache_prompt_version = hashlib.sha256(SystemPromptLoader.get().encode("utf-8")).hexdigest()[:16]
        if not skip_memory:
            normalized_query = self.memory.normalize_cache_query(query)
            profile_scope = ",".join(sorted({str(profile) for profile in (profiles or [])}))
            cache_material = "|".join([
                normalized_query, str(knowledge_base_id or ""), str(getattr(self.llm, "model", "")),
                profile_scope, cache_prompt_version,
            ])
            cache_key = hashlib.sha256(cache_material.encode("utf-8")).hexdigest()
            cached = self.memory.get_semantic_cache(cache_key)
            if cached:
                cache_hit = True
                answer = cached["answer"]
                sources = cached.get("sources") or []
                add_trace_step(trace_data, "semantic_cache", hit=True, cache_key=cache_key[:12])
                finish_trace(trace_data, "cache_hit", returned_count=len(sources))
                msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=sources)
                self.memory.log_usage(
                    conversation_id, msg_id, query, total_time=round(time.time() - t_start, 3),
                    returned_count=len(sources), documents=sources, trace_data=trace_data,
                )
                return {
                    "answer": answer, "sources": sources, "conversation_id": conversation_id,
                    "rewritten_query": None, "verified": True, "cache_hit": True,
                    "stats": {"search_time": 0, "llm_time": 0,
                               "total_time": round(time.time() - t_start, 2),
                               "docs_count": len(sources)},
                }

        # ---- Query 改写 / 多路检索计划 ----
        t0_rw = time.time()
        query_plan = self._build_query_rewrite(query, conv_history)
        rewrite_time = time.time() - t0_rw
        search_queries = enrich_queries_for_compliance_decision(
            query, query_plan.retrieval_queries(), query_plan.query_type
        )
        add_trace_step(
            trace_data,
            "query_rewrite",
            enabled=self.use_query_rewrite,
            duration_s=round(rewrite_time, 3),
            time_s=round(rewrite_time, 3),
            original=query,
            rewritten=query_plan.semantic_query if query_plan.semantic_query != query else "",
            result=query_plan.to_dict(),
            effective_retrieval_queries=search_queries,
        )

        t0 = time.time()
        if retrieved_docs_override is None:
            docs = self.retriever.search_multi(
                search_queries,
                top_k=effective_top_k,
                use_rerank=effective_use_rerank,
                use_hybrid=effective_use_hybrid,
                metadata_filter=query_plan.metadata_filter(),
                rerank_query=query_plan.standalone_query or query,
                use_chroma_where=True,
                profiles=profiles,
                access_scope={"tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id, "knowledge_base_id": knowledge_base_id},
            )
        else:
            docs = list(retrieved_docs_override)
            trace_data["retrieval_override"] = "evaluation_shared_context"
        search_time = time.time() - t0
        logger.info(f"检索完成: {len(docs)} 条 ({search_time:.2f}s)")
        trace_data["query_rewrite"] = query_plan.to_dict()
        trace_data["effective_retrieval_queries"] = search_queries
        trace_data["retrieval"] = getattr(self.retriever, "last_trace", {})
        add_trace_step(
            trace_data,
            "retrieval",
            duration_s=round(search_time, 3),
            time_s=round(search_time, 3),
            returned_count=len(docs),
            total_results=len(docs),
            top_sources=[
                {"file_name": d.get("file_name", ""), "section": d.get("section", "")}
                for d in docs[:5]
            ],
            trace=trace_data["retrieval"],
        )

        # ---- 空结果预检 + 降级处理 ----
        if not docs:
            # 尝试用通用 taxonomy 配置里的宽泛查询做二次检索。
            for fq in fallback_queries("default"):
                fallback_docs = self.retriever.search(
                    fq, top_k=5, use_rerank=False, profiles=profiles,
                    access_scope={"tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id, "knowledge_base_id": knowledge_base_id},
                )
                if fallback_docs:
                    logger.info(f"降级检索成功: 「{fq}」→ {len(fallback_docs)} 条")
                    docs = fallback_docs
                    trace_data["fallback_query"] = fq
                    add_trace_step(
                        trace_data,
                        "fallback_retrieval",
                        query=fq,
                        returned_count=len(fallback_docs),
                    )
                    break

        external_docs = self._maybe_external_evidence(
            query, trace_data, conversation_id, tenant_id, user_id, docs,
        )
        if external_docs:
            docs.extend(external_docs)

        if not docs:
            record_event(
                self.memory, tenant_id, "retrieval_empty", "P2", "retrieval",
                {"query_length": len(query), "knowledge_base_id_present": bool(knowledge_base_id)},
                trace_data.get("trace_id", ""), "本轮授权检索未返回知识库文档；未保存查询正文。",
            )
            answer = "该问题超出我的知识范围，知识库中暂无相关文件覆盖。"
            sources = []
            finish_trace(trace_data, "no_retrieval_result")
            msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            self.memory.extract_and_save_memory(conversation_id, query, answer)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            counts = retrieval_counts(trace_data)
            self.memory.log_usage(
                conversation_id, msg_id, query,
                rewrite_time=round(rewrite_time, 3),
                faiss_time=round(search_time, 3),
                total_time=round(time.time() - t_start, 3),
                faiss_count=counts["faiss"],
                chroma_count=counts["chroma"],
                bm25_count=counts["bm25"],
                final_count=counts["final"],
                returned_count=0,
                trace_data=trace_data,
            )
            return {
                "answer": answer,
                "sources": sources,
                "conversation_id": conversation_id,
                "rewritten_query": query_plan.semantic_query if query_plan.semantic_query != query else None,
                "verified": False,
                "stats": {
                    "rewrite_time": round(rewrite_time, 2),
                    "search_time": round(search_time, 2),
                    "llm_time": 0,
                    "total_time": round(rewrite_time + search_time, 2),
                    "docs_count": 0,
                    "trace": trace_data,
                },
            }

        sources = [
            {
                "file_name": d["file_name"],
                "display_name": source_display_name(d),
                "category": d["category"],
                "section": d["section"],
                "content": d.get("content", ""),
                **compute_confidence(d),
                **({
                    "external": True,
                    "external_unverified": True,
                    "source_url": d.get("source_url", ""),
                    "source_id": d.get("source_id", ""),
                    "content_hash": d.get("content_hash", ""),
                    "confidence": d.get("confidence", 0.35),
                    "label": "外部待核验",
                } if d.get("external") else {}),
            }
            for d in docs
        ]

        graph_evidence = collect_graph_evidence(
            self.memory, tenant_id, knowledge_base_id, query, retrieved_docs=docs,
        )
        trace_data["graph_evidence"] = graph_evidence
        add_trace_step(
            trace_data, "graph_evidence", status=graph_evidence.get("status"),
            relation_count=len(graph_evidence.get("relations", [])),
            conflict_count=len(graph_evidence.get("conflicts", [])),
            source_document_ids=graph_evidence.get("source_document_ids", []),
        )

        guarded_answer = compliance_decision_guard_answer(query, sources, query_plan.query_type)
        if guarded_answer:
            add_trace_step(
                trace_data,
                "compliance_decision_guard",
                triggered=True,
                reason="no_authoritative_compliance_source",
            )
            finish_trace(trace_data, "guarded", reason="no_authoritative_compliance_source")
            msg_id = self.memory.add_message(conversation_id, "assistant", guarded_answer, sources=sources)
            self.memory.extract_and_save_memory(conversation_id, query, guarded_answer)
            first_msg = self.memory.get_history(conversation_id)
            if len([m for m in first_msg if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            trace_data["compliance_decision_guard"] = "no_authoritative_compliance_source"
            counts = retrieval_counts(trace_data)
            self.memory.log_usage(
                conversation_id, msg_id, query,
                rewrite_time=round(rewrite_time, 3),
                faiss_time=round(search_time, 3),
                total_time=round(time.time() - t_start, 3),
                faiss_count=counts["faiss"],
                chroma_count=counts["chroma"],
                bm25_count=counts["bm25"],
                final_count=counts["final"],
                returned_count=len(docs),
                documents=sources,
                trace_data=trace_data,
            )
            return {
                "answer": guarded_answer,
                "sources": sources,
                "conversation_id": conversation_id,
                "rewritten_query": query_plan.semantic_query if query_plan.semantic_query != query else None,
                "verified": False,
                "stats": {
                    "rewrite_time": round(rewrite_time, 2),
                    "search_time": round(search_time, 2),
                    "llm_time": 0,
                    "total_time": round(rewrite_time + search_time, 2),
                    "docs_count": len(docs),
                    "trace": trace_data,
                },
            }

        # ---- 追问/澄清检测：已禁用（首回答即完整） ----
        # ---- 历史对话（滑动窗口+压缩） ----
        history = None
        if self.use_history:
            history = self.memory.get_compressed_history(
                conversation_id,
                llm_provider=self.llm,
                keep_rounds=5,
            )

        # ---- 会话摘要 + 已治理的长期记忆 ----
        session_memory = self.memory.get_session_memory(conversation_id)
        session_context = ""
        if session_memory.get("user_role"):
            session_context += f"用户身份：{session_memory['user_role']}。\n"
        if session_memory.get("mentioned_standards"):
            stds = session_memory["mentioned_standards"][-5:]
            session_context += f"用户提到的标准/法规：{', '.join(stds)}。\n"
        long_term_memories = []
        if self.memory.long_term_memory_enabled(tenant_id, user_id, agent_id):
            long_term_memories = self.memory.get_long_term_memories(
                tenant_id, user_id, agent_id, query=query, limit=5,
            )
        if long_term_memories:
            session_context += "已确认的长期记忆：\n"
            session_context += "\n".join(
                f"- [{item['memory_type']}] {item['content']}"
                for item in long_term_memories
            ) + "\n"

        messages, truncation_info = build_prompt_messages(
            query=query,
            docs=docs,
            history=history[:-1] if history else None,
            include_example=self.include_example,
            session_context=session_context if session_context else None,
            system_prompt_override=system_prompt_override,
            response_language=response_language,
            graph_evidence=graph_evidence,
        )

        t1 = time.time()
        try:
            llm_result = self.llm.chat(messages, temperature=temperature)
        except Exception as exc:
            record_event(
                self.memory, tenant_id, "llm_generation_failed", "P1", "chat",
                {"model": getattr(self.llm, "model", ""), "error_type": type(exc).__name__},
                trace_data.get("trace_id", ""), "主 Agent 模型调用失败；错误正文不写入监控事件。",
            )
            breaker = getattr(getattr(self.llm, "circuit_breaker", None), "state", "")
            if breaker == "OPEN":
                record_event(
                    self.memory, tenant_id, "llm_circuit_breaker_open", "P1", "chat",
                    {"model": getattr(self.llm, "model", ""), "state": breaker},
                    trace_data.get("trace_id", ""), "主 Agent 熔断器已开启；请求将按现有降级策略处理。",
                )
            add_trace_step(trace_data, "llm_generation", failed=True, model=getattr(self.llm, "model", ""), error_type=type(exc).__name__)
            raise
        answer = llm_result.get("content", "")
        reasoning = llm_result.get("reasoning_content")
        llm_time = time.time() - t1
        add_trace_step(
            trace_data,
            "llm_generation",
            duration_s=round(llm_time, 3),
            time_s=round(llm_time, 3),
            total_time_s=round(time.time() - t_start, 3),
            model=getattr(self.llm, "model", ""),
            prompt_messages=len(messages),
            response_length=len(answer or ""),
            reasoning_length=len(reasoning or ""),
            was_truncated=bool(truncation_info.get("was_truncated") if isinstance(truncation_info, dict) else False),
        )

        # ---- 自检 ----
        was_verified = False
        verified = self._verify_answer(answer, sources)
        if verified != answer:
            logger.info(f"自检对回答进行了修正: {len(answer)} → {len(verified)} 字符")
            answer = verified
            was_verified = True
        add_trace_step(trace_data, "self_verify", prompt_version=getattr(self, "_last_verify_prompt_version", 0),
                       corrected=was_verified, enabled=bool(self.use_verification and sources))

        reflection_cfg = get_llm_config_card("reflection")
        reflection_llm = None
        if reflection_cfg.get("model") and reflection_cfg.get("base_url"):
            try:
                reflection_llm = LLMProvider(
                    base_url=reflection_cfg["base_url"], api_key=reflection_cfg.get("api_key", ""),
                    model=reflection_cfg["model"], use_ollama_fallback=False,
                )
            except Exception as exc:
                logger.warning("反思模型初始化失败: %s", exc)
        reflection = reflect_answer(
            self.memory, reflection_llm, query, answer, sources, "chat",
            usage_sink=lambda result, model: self.memory.record_llm_usage_event(
                tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                conversation_id=conversation_id, module="reflection",
                model=(result or {}).get("model") or model,
                prompt_tokens=((result or {}).get("usage") or {}).get("prompt_tokens", 0),
                completion_tokens=((result or {}).get("usage") or {}).get("completion_tokens", 0),
            ),
        )
        answer = reflection["answer"]
        add_trace_step(trace_data, "reflection", decision=reflection["decision"],
                       duration_ms=reflection.get("duration_ms", 0), rounds=reflection.get("rounds", 0),
                       reason=reflection.get("reason", ""), rule_version=reflection.get("rule_version", 0),
                       model=reflection_cfg.get("model", ""))

        # ---- 来源核验：删除引用知识库中不存在文件的句子（如编造"宪法""电信条例"等） ----
        answer = self._check_sources_origin(answer, sources)

        if is_compliance_decision_query(query, query_plan.query_type):
            unbacked_refs = answer_mentions_unbacked_legal_references(answer, sources)
            if unbacked_refs:
                logger.warning(f"合规依据护栏：回答提及未召回法规/标准名 {unbacked_refs}，改为低覆盖兜底")
                answer = COMPLIANCE_DECISION_FALLBACK_ANSWER
                was_verified = True

        # ---- 输出越界过滤（防御纵深） ----
        answer = _filter_output_forbidden(answer)
        # ---- 来源标注校验 ----
        answer = _validate_annotations(answer)
        add_trace_step(
            trace_data,
            "post_processing",
            verified=was_verified,
            source_check_corrected=was_verified,
            actions=["verify_answer"] if was_verified else [],
            sources_count=len(sources),
            answer_chars=len(answer or ""),
        )
        finish_trace(trace_data, "answered", returned_count=len(docs))

        msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=sources)
        self.memory.record_reflection_run(tenant_id, user_id, agent_id, conversation_id, {
            **reflection, "message_id": msg_id, "mode": "chat", "model": reflection_cfg.get("model", ""),
            "input_summary": query[:500], "output_summary": answer[:500],
        })

        # ---- 抽取并保存跨会话记忆（角色、标准等） ----
        self.memory.extract_and_save_memory(conversation_id, query, answer)

        first_msg = self.memory.get_history(conversation_id)
        if len([m for m in first_msg if m["role"] == "user"]) == 1:
            title = query[:50]
            self.memory.update_title(conversation_id, title)

        counts = retrieval_counts(trace_data)
        self.memory.log_usage(
            conversation_id=conversation_id,
            message_id=msg_id,
            query=query,
            rewrite_time=round(rewrite_time, 3),
            faiss_time=round(search_time, 3),
            llm_time=round(llm_time, 3),
            total_time=round(time.time() - t_start, 3),
            faiss_count=counts["faiss"],
            chroma_count=counts["chroma"],
            bm25_count=counts["bm25"],
            final_count=counts["final"],
            returned_count=len(docs),
            was_truncated=bool(truncation_info.get("was_truncated") if isinstance(truncation_info, dict) else False),
            documents=sources,
            trace_data=trace_data,
        )
        if cache_key and answer and sources:
            self.memory.put_semantic_cache(
                cache_key, self.memory.normalize_cache_query(query), answer, sources,
                getattr(self.llm, "model", ""), knowledge_base_id,
                ",".join(sorted({str(profile) for profile in (profiles or [])})),
                cache_prompt_version,
            )

        return {
            "answer": answer,
            "reasoning_content": reasoning,
            "sources": sources,
            "conversation_id": conversation_id,
            "rewritten_query": query_plan.semantic_query if query_plan.semantic_query != query else None,
            "verified": was_verified,
            "cache_hit": False,
            "stats": {
                "rewrite_time": round(rewrite_time, 2),
                "search_time": round(search_time, 2),
                "llm_time": round(llm_time, 2),
                "total_time": round(rewrite_time + search_time + llm_time, 2),
                "docs_count": len(docs),
                "truncation": truncation_info,
                "usage": llm_result.get("usage") or {},
                "model": llm_result.get("model") or getattr(self.llm, "model", ""),
                "trace": trace_data,
            },
        }

    async def ask_stream(
        self,
        query: str,
        conversation_id: Optional[str] = None,
        temperature: float = 0.1,
        category: str = "user",
        profiles: Optional[set[str] | list[str] | tuple[str, ...]] = None,
        tenant_id: str = "local-default",
        user_id: str = "local-owner",
        agent_id: str = "default-agent",
        knowledge_base_id: str = "",
        response_language: str = "zh-CN",
    ):
        """流式问答 — 异步生成器，分阶段 yield 事件

        事件类型：
          {"type": "status",    "stage": "retrieving"|"generating", "message": "..."}
          {"type": "token",     "content": "..."}        ← LLM 逐个 token
          {"type": "done",      "sources": [...], "conversation_id": "..."}
        """
        if not conversation_id:
            conv = self.memory.create_conversation(
                title=query[:50], category=category, tenant_id=tenant_id,
                user_id=user_id, agent_id=agent_id, knowledge_base_id=knowledge_base_id,
            )
            conversation_id = conv["id"]
        if not knowledge_base_id and conversation_id:
            knowledge_base_id = self.memory.get_conversation_knowledge_base(conversation_id)
        if knowledge_base_id and not self.memory.can_access_knowledge_base(
                knowledge_base_id, tenant_id, user_id, agent_id):
            knowledge_base_id = "__unauthorized_knowledge_base__"
        retrieval_config = self.memory.get_knowledge_base_retrieval_config(
            knowledge_base_id, tenant_id,
        ) if knowledge_base_id else None
        effective_top_k = int((retrieval_config or {}).get("config", {}).get("top_k", self.top_k))
        effective_use_rerank = bool((retrieval_config or {}).get("config", {}).get("use_rerank", self.use_rerank))
        effective_use_hybrid = (retrieval_config or {}).get("config", {}).get("use_hybrid", True)

        user_msg_id = self.memory.add_message(conversation_id, "user", query)

        reasoning_list = []
        t_start = time.time()
        t_rewrite = 0.0
        t_search = 0.0
        t_llm = 0.0
        was_truncated = False
        llm_success = True
        was_circuit_break = False
        circuit_provider = None

        # ---- 用户越狱检测（含渐进式越狱） ----
        conv_history = self.memory.get_history(conversation_id)
        is_jailbreak, jb_reason = _detect_user_jailbreak(query, conv_history)
        if is_jailbreak:
            logger.warning(f"⚠️ 检测到越狱尝试: {jb_reason}")
            td = build_trace_envelope(
                original_query=query,
                rewrite_enabled=self.use_query_rewrite,
                conversation_id=conversation_id,
                conversation_category=category,
                path="chat.stream",
                db_path=getattr(self.memory, "_db_path", None),
            )
            add_trace_step(td, "jailbreak_detection", triggered=True, reason=jb_reason, user_query=query[:80])
            finish_trace(td, "blocked", reason="user_jailbreak")
            jailbreak_reply = (
                "我是专注于网络安全的智能助手，主要提供等保测评、数据安全、"
                "合规检查、安全管理体系等方面的知识。关于设备选型、价格、"
                "具体技术操作等问题，建议您咨询相关领域的专业人员获取更准确的信息。"
            )
            msg_id = self.memory.add_message(
                conversation_id, "assistant", jailbreak_reply, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(conversation_id, msg_id, query, off_topic=True, total_time=round(
                time.time()-t_start, 3), trace_data=td, answer_jailbreak=1)
            self.memory.update_jailbreak_status(
                conversation_id, "pending", "用户诱导越狱", message_id=user_msg_id)
            self.memory.flag_jailbreak_message(msg_id)  # 永久标记回复消息
            yield {"type": "token", "content": jailbreak_reply}
            yield {"type": "done", "sources": [], "conversation_id": conversation_id, "message_id": msg_id}
            return

        # ---- 非安全话题检测（流式路径） ----
        offtopic_reply = self._check_offtopic(query)
        if offtopic_reply:
            td = build_trace_envelope(
                original_query=query,
                rewrite_enabled=self.use_query_rewrite,
                conversation_id=conversation_id,
                conversation_category=category,
                path="chat.stream",
                db_path=getattr(self.memory, "_db_path", None),
            )
            add_trace_step(td, "offtopic_detection", triggered=True, user_query=query[:80])
            finish_trace(td, "blocked", reason="offtopic")
            msg_id = self.memory.add_message(
                conversation_id, "assistant", offtopic_reply, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(conversation_id, msg_id, query, off_topic=True, total_time=round(
                time.time()-t_start, 3), trace_data=td, answer_jailbreak=1)
            self.memory.update_jailbreak_status(
                conversation_id, "pending", "用户诱导越狱", message_id=user_msg_id)
            self.memory.flag_jailbreak_message(msg_id)
            yield {"type": "token", "content": offtopic_reply}
            yield {"type": "done", "sources": [], "conversation_id": conversation_id, "message_id": msg_id}
            return

        # ---- 社交寒暄检测（流式路径） ----
        greeting_reply = self._check_greeting(query)
        if greeting_reply:
            td = build_trace_envelope(
                original_query=query,
                rewrite_enabled=self.use_query_rewrite,
                conversation_id=conversation_id,
                conversation_category=category,
                path="chat.stream",
                db_path=getattr(self.memory, "_db_path", None),
            )
            add_trace_step(td, "greeting_detection", triggered=True)
            finish_trace(td, "answered", reason="greeting")
            msg_id = self.memory.add_message(
                conversation_id, "assistant", greeting_reply, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            self.memory.log_usage(conversation_id, msg_id, query, off_topic=True, total_time=round(
                time.time()-t_start, 3), trace_data=td, answer_jailbreak=1)
            self.memory.flag_jailbreak_message(user_msg_id)
            self.memory.flag_jailbreak_message(msg_id)
            yield {"type": "token", "content": greeting_reply}
            yield {"type": "done", "sources": [], "conversation_id": conversation_id, "message_id": msg_id}
            return

        # Stage 1: 检索知识库
        yield {"type": "status", "stage": "retrieving", "message": "正在检索知识库..."}

        t0_rw = time.time()
        query_plan = self._build_query_rewrite(query, conv_history)
        search_queries = enrich_queries_for_compliance_decision(
            query, query_plan.retrieval_queries(), query_plan.query_type
        )
        t_rewrite = time.time() - t0_rw
        trace_data = build_trace_envelope(
            original_query=query,
            rewrite_enabled=self.use_query_rewrite,
            conversation_id=conversation_id,
            conversation_category=category,
            path="chat.stream",
            db_path=getattr(self.memory, "_db_path", None),
        )
        trace_data["context"]["profiles"] = sorted({str(profile) for profile in (profiles or [])})
        add_trace_step(
            trace_data,
            "query_rewrite",
            enabled=self.use_query_rewrite,
            duration_s=round(t_rewrite, 3),
            time_s=round(t_rewrite, 3),
            original=query,
            rewritten=query_plan.semantic_query if query_plan.semantic_query != query else "",
            result=query_plan.to_dict(),
            effective_retrieval_queries=search_queries,
        )

        loop = asyncio.get_event_loop()
        t0_sr = time.time()
        docs = await loop.run_in_executor(
            None,
            lambda: self.retriever.search_multi(
                search_queries,
                top_k=effective_top_k,
                use_rerank=effective_use_rerank,
                use_hybrid=effective_use_hybrid,
                metadata_filter=query_plan.metadata_filter(),
                rerank_query=query_plan.standalone_query or query,
                use_chroma_where=True,
                profiles=profiles,
                access_scope={"tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id, "knowledge_base_id": knowledge_base_id},
            ),
        )
        t_search = time.time() - t0_sr
        logger.info(f"流式检索完成: {len(docs)} 条")
        trace_data["query_rewrite"] = query_plan.to_dict()
        trace_data["effective_retrieval_queries"] = search_queries
        trace_data["retrieval"] = getattr(self.retriever, "last_trace", {})
        add_trace_step(
            trace_data,
            "retrieval",
            duration_s=round(t_search, 3),
            time_s=round(t_search, 3),
            returned_count=len(docs),
            total_results=len(docs),
            top_sources=[
                {"file_name": d.get("file_name", ""), "section": d.get("section", "")}
                for d in docs[:5]
            ],
            trace=trace_data["retrieval"],
        )

        # ---- 空结果预检 + 降级处理 ----
        if not docs:
            for fq in fallback_queries("default"):
                t0_fb = time.time()
                fallback_docs = await loop.run_in_executor(
                    None,
                    lambda q=fq: self.retriever.search(
                        q, top_k=5, use_rerank=False, profiles=profiles,
                        access_scope={"tenant_id": tenant_id, "user_id": user_id, "agent_id": agent_id, "knowledge_base_id": knowledge_base_id},
                    ),
                )
                t_search += time.time() - t0_fb
                if fallback_docs:
                    logger.info(f"流式降级检索成功: 「{fq}」→ {len(fallback_docs)} 条")
                    docs = fallback_docs
                    trace_data["fallback_query"] = fq
                    add_trace_step(
                        trace_data,
                        "fallback_retrieval",
                        query=fq,
                        returned_count=len(fallback_docs),
                    )
                    break

        external_docs = self._maybe_external_evidence(
            query, trace_data, conversation_id, tenant_id, user_id, docs,
        )
        if external_docs:
            docs.extend(external_docs)

        if not docs:
            record_event(
                self.memory, tenant_id, "retrieval_empty", "P2", "retrieval",
                {"query_length": len(query), "knowledge_base_id_present": bool(knowledge_base_id)},
                trace_data.get("trace_id", ""), "本轮授权检索未返回知识库文档；未保存查询正文。",
            )
            answer = "该问题超出我的知识范围，知识库中暂无相关文件覆盖。"
            sources = []
            finish_trace(trace_data, "no_retrieval_result")
            total_time = round(time.time() - t_start, 3)
            msg_id = self.memory.add_message(conversation_id, "assistant", answer, sources=[])
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            counts = retrieval_counts(trace_data)
            self.memory.log_usage(conversation_id, msg_id, query, rewrite_time=round(
                t_rewrite, 3), faiss_time=round(t_search, 3), total_time=total_time,
                faiss_count=counts["faiss"], chroma_count=counts["chroma"],
                bm25_count=counts["bm25"], final_count=counts["final"],
                returned_count=0, trace_data=trace_data)
            yield {"type": "token", "content": answer}
            yield {"type": "done", "sources": sources, "conversation_id": conversation_id, "message_id": msg_id}
            return

        sources = [
            {
                "file_name": d["file_name"],
                "display_name": source_display_name(d),
                "category": d["category"],
                "section": d["section"],
                "content": d.get("content", ""),
                **compute_confidence(d),
                **({
                    "external": True,
                    "external_unverified": True,
                    "source_url": d.get("source_url", ""),
                    "source_id": d.get("source_id", ""),
                    "content_hash": d.get("content_hash", ""),
                    "confidence": d.get("confidence", 0.35),
                    "label": "外部待核验",
                } if d.get("external") else {}),
            }
            for d in docs
        ]

        graph_evidence = collect_graph_evidence(
            self.memory, tenant_id, knowledge_base_id, query, retrieved_docs=docs,
        )
        trace_data["graph_evidence"] = graph_evidence
        add_trace_step(
            trace_data, "graph_evidence", status=graph_evidence.get("status"),
            relation_count=len(graph_evidence.get("relations", [])),
            conflict_count=len(graph_evidence.get("conflicts", [])),
            source_document_ids=graph_evidence.get("source_document_ids", []),
        )

        msg_id = self.memory.add_message(conversation_id, "assistant", "", sources=sources)
        yield {"type": "sources", "sources": sources, "conversation_id": conversation_id, "message_id": msg_id}

        guarded_answer = compliance_decision_guard_answer(query, sources, query_plan.query_type)
        if guarded_answer:
            trace_data["compliance_decision_guard"] = "no_authoritative_compliance_source"
            add_trace_step(
                trace_data,
                "compliance_decision_guard",
                triggered=True,
                reason="no_authoritative_compliance_source",
            )
            finish_trace(trace_data, "guarded", reason="no_authoritative_compliance_source")
            total_time = round(time.time() - t_start, 3)
            self.memory._update_last_message(conversation_id, guarded_answer, sources=sources)
            first_msgs = self.memory.get_history(conversation_id)
            if len([m for m in first_msgs if m["role"] == "user"]) == 1:
                self.memory.update_title(conversation_id, query[:50])
            counts = retrieval_counts(trace_data)
            self.memory.log_usage(conversation_id, msg_id, query, rewrite_time=round(
                t_rewrite, 3), faiss_time=round(t_search, 3), total_time=total_time,
                faiss_count=counts["faiss"], chroma_count=counts["chroma"],
                bm25_count=counts["bm25"], final_count=counts["final"],
                returned_count=len(docs), documents=sources, trace_data=trace_data)
            yield {"type": "token", "content": guarded_answer}
            yield {"type": "done", "sources": sources, "conversation_id": conversation_id, "message_id": msg_id}
            return

        # ---- 追问/澄清检测：已禁用（首回答即完整） ----

        # Stage 2: 生成回答
        yield {"type": "status", "stage": "generating", "message": "正在生成回答..."}

        history = None
        if self.use_history:
            history = self.memory.get_compressed_history(
                conversation_id, self.llm, keep_rounds=self.max_history
            )

        session_memory = self.memory.get_session_memory(conversation_id)
        session_context = ""
        if session_memory.get("user_role"):
            session_context += f"用户身份：{session_memory['user_role']}。\n"
        if session_memory.get("mentioned_standards"):
            stds = session_memory["mentioned_standards"][-5:]
            session_context += f"用户提到的标准/法规：{', '.join(stds)}。\n"
        long_term_memories = []
        if self.memory.long_term_memory_enabled(tenant_id, user_id, agent_id):
            long_term_memories = self.memory.get_long_term_memories(
                tenant_id, user_id, agent_id, query=query, limit=5,
            )
        if long_term_memories:
            session_context += "已确认的长期记忆：\n"
            session_context += "\n".join(
                f"- [{item['memory_type']}] {item['content']}"
                for item in long_term_memories
            ) + "\n"

        messages = build_prompt_messages(
            query=query,
            docs=docs,
            history=history[:-1] if history else None,
            include_example=self.include_example,
            session_context=session_context if session_context else None,
            response_language=response_language,
            graph_evidence=graph_evidence,
        )[0]  # 流式路径

        t0_llm = time.time()
        full_content = ""
        # A published final-review rule means the user must only see the reviewed answer.
        # Without one, retain the normal token-by-token experience and explicit skipped trace.
        reflection_required = any(
            "chat" in (rule.get("capability_modes") or [])
            for rule in self.memory.list_reflection_rules("published")
        )
        try:
            async for chunk in self.llm.chat_stream(messages, temperature=temperature):
                if isinstance(chunk, dict):
                    chunk_type = chunk.get("type", "content")
                    chunk_text = chunk.get("text", "")
                    if chunk_type == "reasoning":
                        reasoning_list.append(chunk_text)
                        yield {"type": "reasoning", "content": chunk_text}
                    else:
                        full_content += chunk_text
                        if not reflection_required:
                            yield {"type": "token", "content": chunk_text}
                else:
                    full_content += chunk
                    if not reflection_required:
                        yield {"type": "token", "content": chunk}
        except Exception as exc:
            record_event(
                self.memory, tenant_id, "llm_generation_failed", "P1", "chat.stream",
                {"model": getattr(self.llm, "model", ""), "error_type": type(exc).__name__},
                trace_data.get("trace_id", ""), "流式主 Agent 模型调用失败；错误正文不写入监控事件。",
            )
            breaker = getattr(getattr(self.llm, "circuit_breaker", None), "state", "")
            if breaker == "OPEN":
                record_event(
                    self.memory, tenant_id, "llm_circuit_breaker_open", "P1", "chat.stream",
                    {"model": getattr(self.llm, "model", ""), "state": breaker},
                    trace_data.get("trace_id", ""), "流式主 Agent 熔断器已开启；请求将按现有降级策略处理。",
                )
            add_trace_step(trace_data, "llm_generation", failed=True, model=getattr(self.llm, "model", ""), error_type=type(exc).__name__)
            raise
        t_llm = time.time() - t0_llm
        stream_usage = getattr(self.llm, "last_usage", {}) or {}
        self.memory.record_llm_usage_event(
            tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
            conversation_id=conversation_id, module="chat",
            provider=getattr(self.llm, "_provider_name", ""),
            model=getattr(self.llm, "model", ""),
            prompt_tokens=stream_usage.get("prompt_tokens", 0),
            completion_tokens=stream_usage.get("completion_tokens", 0),
        )
        total_time = round(time.time() - t_start, 3)
        add_trace_step(
            trace_data,
            "llm_generation",
            duration_s=round(t_llm, 3),
            time_s=round(t_llm, 3),
            total_time_s=total_time,
            model=getattr(self.llm, "model", ""),
            prompt_messages=len(messages),
            response_length=len(full_content or ""),
            reasoning_length=len("".join(reasoning_list)) if reasoning_list else 0,
            usage=stream_usage,
        )

        # 流式草稿已经输出；终审可能返回替换文本，前端以 answer_replace 事件呈现最终版本。
        yield {"type": "status", "stage": "reviewing", "message": "正在进行安全复核..."}
        reflection_cfg = get_llm_config_card("reflection")
        reflection_llm = None
        if reflection_cfg.get("model") and reflection_cfg.get("base_url"):
            try:
                reflection_llm = LLMProvider(
                    base_url=reflection_cfg["base_url"], api_key=reflection_cfg.get("api_key", ""),
                    model=reflection_cfg["model"], use_ollama_fallback=False,
                )
            except Exception as exc:
                logger.warning("流式反思模型初始化失败: %s", exc)
        reflection = reflect_answer(
            self.memory, reflection_llm, query, full_content, sources, "chat",
            usage_sink=lambda result, model: self.memory.record_llm_usage_event(
                tenant_id=tenant_id, user_id=user_id, agent_id=agent_id,
                conversation_id=conversation_id, module="reflection",
                model=(result or {}).get("model") or model,
                prompt_tokens=((result or {}).get("usage") or {}).get("prompt_tokens", 0),
                completion_tokens=((result or {}).get("usage") or {}).get("completion_tokens", 0),
            ),
        )
        reviewed_content = reflection["answer"]
        add_trace_step(trace_data, "reflection", decision=reflection["decision"],
                       duration_ms=reflection.get("duration_ms", 0), rounds=reflection.get("rounds", 0),
                       reason=reflection.get("reason", ""), rule_version=reflection.get("rule_version", 0),
                       model=reflection_cfg.get("model", ""))
        if reflection_required:
            full_content = reviewed_content
            yield {"type": "token", "content": full_content}
        elif reviewed_content != full_content:
            full_content = reviewed_content
            yield {"type": "answer_replace", "content": full_content,
                   "decision": reflection["decision"], "reason": reflection.get("reason", "")}

        # 保存到记忆
        self.memory._update_last_message(conversation_id, full_content, sources=sources)

        # ---- 来源核验：流式路径同样做后处理 ----
        corrected = self._check_sources_origin(full_content, sources)
        if corrected != full_content:
            logger.warning(f"流式路径来源核验：删除了编造引用内容，已修正记忆中的版本")
            self.memory._update_last_message(conversation_id, corrected, sources=sources)
            full_content = corrected

        if is_compliance_decision_query(query, query_plan.query_type):
            unbacked_refs = answer_mentions_unbacked_legal_references(full_content, sources)
            if unbacked_refs:
                logger.warning(f"流式路径合规依据护栏：回答提及未召回法规/标准名 {unbacked_refs}，改为低覆盖兜底")
                full_content = COMPLIANCE_DECISION_FALLBACK_ANSWER
                self.memory._update_last_message(conversation_id, full_content, sources=sources)

        # ---- 输出越界过滤（防御纵深） ----
        filtered = _filter_output_forbidden(full_content)
        if filtered != full_content:
            logger.warning(f"流式路径输出过滤：拦截了越界内容")
            self.memory._update_last_message(conversation_id, filtered, sources=sources)
            full_content = filtered

        # ---- 来源标注校验 ----
        annotated = _validate_annotations(full_content)
        if annotated != full_content:
            self.memory._update_last_message(conversation_id, annotated, sources=sources)
            full_content = annotated
        add_trace_step(
            trace_data,
            "post_processing",
            source_check_corrected=False,
            actions=[],
            sources_count=len(sources),
            answer_chars=len(full_content or ""),
        )
        finish_trace(trace_data, "answered", returned_count=len(docs))

        self.memory.record_reflection_run(tenant_id, user_id, agent_id, conversation_id, {
            **reflection, "message_id": msg_id, "mode": "chat", "model": reflection_cfg.get("model", ""),
            "input_summary": query[:500], "output_summary": full_content[:500],
        })

        first_msgs = self.memory.get_history(conversation_id)
        if len([m for m in first_msgs if m["role"] == "user"]) == 1:
            self.memory.update_title(conversation_id, query[:50])

        counts = retrieval_counts(trace_data)
        self.memory.log_usage(
            conversation_id=conversation_id,
            message_id=msg_id,
            query=query,
            rewrite_time=round(t_rewrite, 3),
            faiss_time=round(t_search, 3),
            llm_time=round(t_llm, 3),
            total_time=total_time,
            faiss_count=counts["faiss"],
            chroma_count=counts["chroma"],
            bm25_count=counts["bm25"],
            final_count=counts["final"],
            returned_count=len(docs),
            was_truncated=was_truncated,
            documents=sources,
            trace_data=trace_data,
        )

        yield {
            "type": "answer_done",
            "sources": sources,
            "conversation_id": conversation_id,
            "message_id": msg_id,
            "reasoning_content": "".join(reasoning_list) if reasoning_list else None,
        }
        yield {"type": "done", "sources": sources, "conversation_id": conversation_id, "message_id": msg_id, "reasoning_content": "".join(reasoning_list) if reasoning_list else None}

    def infer_semantic_rating_for_message(self, message_id: int) -> dict:
        """用户未评分时的后台语义评分，不参与本轮回答链路。"""
        item = self.memory.get_assistant_message_for_rating(message_id)
        if not item:
            return {"ok": False, "reason": "message_not_found"}
        if item.get("user_rating") is not None:
            return {"ok": True, "skipped": True, "reason": "user_already_rated"}
        if item.get("semantic_rating") is not None:
            return {"ok": True, "skipped": True, "reason": "semantic_already_rated"}

        self.memory.ensure_prompt_assets([{
            "slot": "semantic_scoring", "name": "语义评分", "model_role": "scoring",
            "description": "评估回答质量和用户对上一轮回答的满意度。",
            "template": SEMANTIC_SCORING_PROMPT, "variables": ["task", "query", "answer"],
        }])
        scoring_asset = self.memory.get_active_prompt_asset("semantic_scoring", SEMANTIC_SCORING_PROMPT)
        prompt = scoring_asset["template"].format(
            task="请评估 AI 助手回答质量，只输出一个数字（1-5）。",
            query=item.get("query", "")[:500], answer=item.get("content", "")[:1200],
        )

        rating = None
        used_fallback = False

        scoring_cfg = get_llm_config_card('scoring')
        scoring_url = (scoring_cfg.get('base_url') or '').rstrip('/') + \
            '/chat/completions' if scoring_cfg.get('base_url') else ''
        scoring_key = scoring_cfg.get('api_key', '')
        scoring_model = scoring_cfg.get('model', '')

        if scoring_url and scoring_model:
            try:
                headers = {"Content-Type": "application/json"}
                if scoring_key:
                    headers["Authorization"] = f"Bearer {scoring_key}"
                resp = httpx.post(
                    scoring_url,
                    headers=headers,
                    json={"model": scoring_model, "messages": [
                        {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 5},
                    timeout=15,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    usage = payload.get("usage") or {}
                    self.memory.record_llm_usage_event(
                        tenant_id="local-default", module="semantic_scoring",
                        model=payload.get("model") or scoring_model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                    )
                    text = payload["choices"][0]["message"]["content"].strip()
                    rating = int("".join(c for c in text if c.isdigit())[:1])
            except Exception as e:
                logger.debug(f"超时语义评分失败 (scoring card): {e}")

        if rating is None:
            used_fallback = True
            fallback_cfg = get_llm_config_card('fallback')
            fb_url = (fallback_cfg.get('base_url')
                      or 'http://localhost:11434/v1').rstrip('/') + '/chat/completions'
            fb_model = fallback_cfg.get('model') or 'qwen2.5:7b'
            try:
                resp = httpx.post(
                    fb_url,
                    json={"model": fb_model, "messages": [
                        {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 5},
                    timeout=15,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    usage = payload.get("usage") or {}
                    self.memory.record_llm_usage_event(
                        tenant_id="local-default", module="semantic_scoring_fallback",
                        model=payload.get("model") or fb_model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                    )
                    text = payload["choices"][0]["message"]["content"].strip()
                    rating = int("".join(c for c in text if c.isdigit())[:1])
            except Exception as e:
                logger.debug(f"超时语义评分失败 (fallback): {e}")

        if rating and 1 <= rating <= 5:
            updated = self.memory.update_semantic_rating_if_unrated(message_id, rating)
            logger.info(
                f"超时语义评分: msg_id={message_id} rating={rating} updated={updated} "
                f"(source={'fallback' if used_fallback else 'scoring_card'})"
            )
            return {"ok": True, "rating": rating, "used_ollama": used_fallback, "updated": updated,
                    "prompt_version": scoring_asset.get("version", 0)}
        return {"ok": False, "reason": "rating_failed", "used_ollama": used_fallback,
                "prompt_version": scoring_asset.get("version", 0)}

    def _infer_rating(self, conversation_id: str, user_query: str) -> dict:
        """语义兜底评分：分析用户对上一轮回答的态度

        从 DB 读取 scoring / jailbreak 卡片配置，替代硬编码 OpenRouter。
        返回：
            {"used_ollama": True}  — 使用本地兜底
            {"used_ollama": False} — 正常
            {}                      — 未评分（无上一条消息）
        """
        prev = self.memory.get_unrated_assistant_message(conversation_id)
        if not prev:
            return {}

        prev_answer = prev["content"][:500]
        self.memory.ensure_prompt_assets([{
            "slot": "semantic_scoring", "name": "语义评分", "model_role": "scoring",
            "description": "评估回答质量和用户对上一轮回答的满意度。",
            "template": SEMANTIC_SCORING_PROMPT, "variables": ["task", "query", "answer"],
        }])
        scoring_asset = self.memory.get_active_prompt_asset("semantic_scoring", SEMANTIC_SCORING_PROMPT)
        prompt = scoring_asset["template"].format(
            task="分析用户对上一轮回答的态度，只输出一个数字（1-5）。",
            query=user_query[:500], answer=prev_answer,
        )

        rating = None
        used_fallback = False

        # 从 DB 读取语义评分卡片配置
        scoring_cfg = get_llm_config_card('scoring')
        scoring_url = (scoring_cfg.get('base_url') or '').rstrip('/') + \
            '/chat/completions' if scoring_cfg.get('base_url') else ''
        scoring_key = scoring_cfg.get('api_key', '')
        scoring_model = scoring_cfg.get('model', '')

        if scoring_url and scoring_model:
            try:
                headers = {"Content-Type": "application/json"}
                if scoring_key:
                    headers["Authorization"] = f"Bearer {scoring_key}"
                resp = httpx.post(
                    scoring_url,
                    headers=headers,
                    json={"model": scoring_model, "messages": [
                        {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 5},
                    timeout=15,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    usage = payload.get("usage") or {}
                    self.memory.record_llm_usage_event(
                        tenant_id="local-default", module="semantic_scoring",
                        model=payload.get("model") or scoring_model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                    )
                    text = payload["choices"][0]["message"]["content"].strip()
                    rating = int("".join(c for c in text if c.isdigit())[:1])
            except Exception as e:
                logger.debug(f"语义评分失败 (scoring card): {e}")

        # 评分卡片失败 → 先试 fallback 卡片，再试硬编码 Ollama
        if rating is None:
            used_fallback = True
            fallback_cfg = get_llm_config_card('fallback')
            fb_url = (fallback_cfg.get('base_url')
                      or 'http://localhost:11434/v1').rstrip('/') + '/chat/completions'
            fb_model = fallback_cfg.get('model') or 'qwen2.5:7b'
            try:
                resp = httpx.post(
                    fb_url,
                    json={"model": fb_model, "messages": [
                        {"role": "user", "content": prompt}], "temperature": 0.1, "max_tokens": 5},
                    timeout=15,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    usage = payload.get("usage") or {}
                    self.memory.record_llm_usage_event(
                        tenant_id="local-default", module="semantic_scoring_fallback",
                        model=payload.get("model") or fb_model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                    )
                    text = payload["choices"][0]["message"]["content"].strip()
                    rating = int("".join(c for c in text if c.isdigit())[:1])
            except Exception as e:
                logger.debug(f"语义评分失败 (fallback): {e}")

        if rating and 1 <= rating <= 5:
            self.memory.update_rating(prev["id"], rating, semantic=True)
            logger.info(
                f"语义评分: msg_id={prev['id']} rating={rating} (source={'fallback' if used_fallback else 'scoring_card'})")
        scoring_prompt_version = scoring_asset.get("version", 0)

        # ---- 越狱检测：模型回答是否违规 ----
        try:
            self.memory.ensure_prompt_assets([{
                "slot": "jailbreak_detect", "name": "越狱检测", "model_role": "jailbreak",
                "description": "检测模型回答是否越权、危险或泄露内部信息。",
                "template": JAILBREAK_DETECT_PROMPT, "variables": ["answer", "query"],
            }])
            jb_asset = self.memory.get_active_prompt_asset("jailbreak_detect", JAILBREAK_DETECT_PROMPT)
            jailbreak_prompt = jb_asset["template"].format(answer=prev_answer[:800], query=user_query[:300])

            jb_detected = False
            jb_cfg = get_llm_config_card('jailbreak')
            jb_url = (jb_cfg.get('base_url') or '').rstrip('/') + \
                '/chat/completions' if jb_cfg.get('base_url') else ''
            jb_key = jb_cfg.get('api_key', '')
            jb_model = jb_cfg.get('model', '')

            if jb_url and jb_model:
                headers = {"Content-Type": "application/json"}
                if jb_key:
                    headers["Authorization"] = f"Bearer {jb_key}"
                resp = httpx.post(
                    jb_url,
                    headers=headers,
                    json={"model": jb_model, "messages": [
                        {"role": "user", "content": jailbreak_prompt}], "temperature": 0.1, "max_tokens": 10},
                    timeout=15,
                )
                if resp.status_code == 200:
                    payload = resp.json()
                    usage = payload.get("usage") or {}
                    self.memory.record_llm_usage_event(
                        tenant_id="local-default", module="jailbreak_detect",
                        model=payload.get("model") or jb_model,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0),
                    )
                    jb_text = payload["choices"][0]["message"]["content"].strip().lower()
                    jb_detected = jb_text.startswith("yes")

            if jb_detected:
                self.memory.update_rating(prev["id"], 1, semantic=True)  # 越狱回答强制最低分
                self.memory.update_jailbreak_status(
                    conversation_id, "pending", "模型回答含违规内容", message_id=prev["id"])
                logger.warning(f"越狱检测: msg_id={prev['id']} conv={conversation_id} 模型回答违规")
            else:
                # 如已有用户越狱(off_topic)标记，也显示
                pass
        except Exception as e:
            logger.debug(f"越狱检测失败: {e}")

        result = {"used_ollama": used_fallback} if used_fallback else {}
        result["semantic_scoring_prompt_version"] = scoring_prompt_version
        result["jailbreak_prompt_version"] = locals().get("jb_asset", {}).get("version", 0)
        return result

    def refresh_retriever(self):
        self.retriever.refresh_faiss()

    def stats(self) -> dict:
        retriever_stats = self.retriever.stats()
        # 统计已清洗文档数
        try:
            cleaned_dir = Path(_PROJECT_ROOT) / "RAG_DATA" / "03_cleaned"
            cleaned_docs = sum(1 for f in cleaned_dir.rglob("*.md") if f.is_file())
        except Exception:
            cleaned_docs = 0
        return {
            **retriever_stats,
            "llm": self.llm.to_config(),
            "top_k": self.top_k,
            "use_rerank": self.use_rerank,
            "use_history": self.use_history,
            "conversations": len(self.memory.get_conversations()),
            "cleaned_docs": cleaned_docs,
        }

    def _count_cleaned_docs(self) -> int:
        try:
            from pathlib import Path
            cleaned_dir = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "03_cleaned"
            return sum(1 for f in cleaned_dir.rglob("*.md") if f.is_file())
        except Exception:
            return 0


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
            cat_icon = category_emoji(s.get("category", ""))
            print(f"  {cat_icon} {s['file_name']} / {s['section']} ({s['score']})")
        print(f"\n⏱ {result['stats']}")
