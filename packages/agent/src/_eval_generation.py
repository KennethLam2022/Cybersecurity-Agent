"""域C：生成质量评估 — Faithfulness + Relevancy + Hallucination

评估回答的忠实度、相关性、幻觉检测。
使用 LLM-as-Judge 进行评分，有回退到启发式方法。

用法:
    from _eval_generation import eval_faithfulness, eval_relevancy, eval_hallucination

    result = eval_faithfulness(query, retrieved_docs, answer, llm=llm)
    # => {"score": 0.85, "explanation": "...", "details": {...}}

    python _eval_generation.py    # 独立运行演示
"""
import os
import sys
import json
import logging
import re
from typing import Optional

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────
#  Faithfulness — 回答是否忠实于检索文档
# ──────────────────────────────────────────────

_FAITHFULNESS_PROMPT = """你是一位严格的 RAG 事实一致性评估专家。你的任务：评估"回答"是否忠实于"检索文档"，没有编造或歪曲信息。

评分标准（0-1 分）：
1. 逐句检查回答中的每个事实陈述
2. 判断每个陈述是否与检索文档中的信息一致（不要求逐字，要求意思一致）
3. 计算一致率：consistent_statements / total_statements

规则：
- 完全忠实，所有陈述都有文档支持 → 高分（0.9-1.0）
- 大部分忠实，个别细节有偏差或过度推断 → 中分（0.5-0.8）
- 存在明显的矛盾或编造 → 低分（0.2-0.4）
- 大量编造内容 → 0.0-0.1
- 注意：回答中的概括性总结如果可以从文档中合理推断出，视为一致
- 如果回答拒绝回答（如"无法回答"），而文档确实包含了相关信息，扣分

输出必须是 JSON 格式（不要多余文字）：
{{"score": 0.0-1.0, "total_statements": N, "consistent_statements": N, "contradictions": ["具体矛盾1", ...], "explanation": "简要分析理由"}}

--- 用户问题 ---
{query}

--- 检索文档 ---
{context}

--- 回答 ---
{answer}
"""


def eval_faithfulness(
    query: str,
    retrieved_docs: list[dict],
    answer: str,
    llm=None,
    usage_sink=None,
) -> dict:
    """评估回答是否忠实于检索文档（无编造、无矛盾）

    Args:
        query: 用户问题
        retrieved_docs: 检索到的文档列表
        answer: 模型回答
        llm: LLMProvider 实例（可选）

    Returns:
        {"score": float (0-1), "explanation": str, "details": {...}}
    """
    if not answer or not answer.strip():
        return {"score": 0.0, "explanation": "回答为空", "details": {}}

    # 清洗引用标记，避免 [来源N: ...] 干扰评分
    answer_clean = _clean_answer(answer)

    context_text = _format_docs(retrieved_docs)

    if llm is not None:
        try:
            prompt = _FAITHFULNESS_PROMPT.format(
                query=query, context=context_text, answer=answer_clean
            )
            result = _llm_judge(llm, prompt, usage_sink=usage_sink)
            score = max(0.0, min(1.0, float(result.get("score", 0))))
            return {
                "score": round(score, 4),
                "explanation": result.get("explanation", ""),
                "details": {
                    "total_statements": result.get("total_statements", 0),
                    "consistent_statements": result.get("consistent_statements", 0),
                    "contradictions": result.get("contradictions", []),
                },
            }
        except Exception as e:
            logger.warning(f"LLM faithfulness 评估失败: {e}")

    # ── 启发式回退 ──
    return _heuristic_faithfulness(answer, context_text)


def _heuristic_faithfulness(answer: str, context_text: str) -> dict:
    """基于 n-gram 重叠的启发式 faithfulness 评估"""
    sentences = _split_sentences(answer)
    if not sentences:
        return {"score": 0.0, "explanation": "无法分解句子", "details": {}}

    context_lower = context_text.lower()
    # 使用 2-gram（更适合中文）和 3-gram
    context_ngrams_2 = _get_ngrams(context_lower, n=2)
    context_ngrams_3 = _get_ngrams(context_lower, n=3)
    context_words = set(re.findall(r'[\w\u4e00-\u9fff]+', context_lower))

    consistent = 0
    contradictions_found = []

    for sent in sentences:
        sent_lower = sent.lower()
        sent_words = set(re.findall(r'[\w\u4e00-\u9fff]+', sent_lower))
        if not sent_words:
            consistent += 1
            continue

        # 词重叠率
        word_overlap = len(sent_words & context_words) / len(sent_words) if sent_words else 0

        # 2-gram/3-gram 重叠率
        sent_ngrams_2 = _get_ngrams(sent_lower, n=2)
        sent_ngrams_3 = _get_ngrams(sent_lower, n=3)
        ngram_2_overlap = len(sent_ngrams_2 & context_ngrams_2) / max(len(sent_ngrams_2), 1)
        ngram_3_overlap = len(sent_ngrams_3 & context_ngrams_3) / max(len(sent_ngrams_3), 1)

        # 综合评分：词重叠或 n-gram 重叠有一个高即可
        overlap_score = max(word_overlap, ngram_2_overlap, ngram_3_overlap)
        if overlap_score >= 0.15:
            consistent += 1
        else:
            contradictions_found.append(sent[:60])

    score = consistent / len(sentences) if sentences else 0
    return {
        "score": round(score, 4),
        "explanation": f"启发式评估：{consistent}/{len(sentences)} 个陈述与文档信息一致",
        "details": {
            "total_statements": len(sentences),
            "consistent_statements": consistent,
            "contradictions": contradictions_found[:5],
            "method": "heuristic",
        },
    }


# ──────────────────────────────────────────────
#  Relevancy — 回答是否针对问题
# ──────────────────────────────────────────────

_RELEVANCY_PROMPT = """你是一位严格的 RAG 回答相关性评估专家。你的任务：评估"回答"是否针对"用户问题"。

评分标准（0-1 分）：
1. 判断回答是否直接回答了用户的问题
2. 判断回答是否解决了用户的深层需求

规则：
- 回答完全切题、直接解决问题 → 高分（0.9-1.0）
- 回答与问题相关但存在部分偏移或多余内容 → 中分（0.4-0.8）
- 回答部分相关，但偏离了问题核心 → 低分（0.1-0.3）
- 答非所问，完全无关 → 0.0
- 如果回答拒绝回答（因安全原因合理拒绝除外）→ 低分
- 即使回答是正确的，如果不是针对问题给的 → 低分

输出必须是 JSON 格式（不要多余文字）：
{{"score": 0.0-1.0, "explanation": "简要分析理由"}}

--- 用户问题 ---
{query}

--- 回答 ---
{answer}
"""


def eval_relevancy(
    query: str,
    answer: str,
    retrieved_docs: Optional[list[dict]] = None,
    llm=None,
    usage_sink=None,
) -> dict:
    """评估回答是否针对问题

    Args:
        query: 用户问题
        answer: 模型回答
        retrieved_docs: 可选的检索文档（用于启发式回退）
        llm: LLMProvider 实例（可选）

    Returns:
        {"score": float (0-1), "explanation": str, "details": {...}}
    """
    if not answer or not answer.strip():
        return {"score": 0.0, "explanation": "回答为空", "details": {}}
    if not query or not query.strip():
        return {"score": 0.5, "explanation": "问题为空，无法判断相关性", "details": {}}

    # 清洗引用标记，避免 [来源N: ...] 干扰评分
    answer_clean = _clean_answer(answer)

    if llm is not None:
        try:
            prompt = _RELEVANCY_PROMPT.format(query=query, answer=answer_clean)
            result = _llm_judge(llm, prompt, usage_sink=usage_sink)
            score = max(0.0, min(1.0, float(result.get("score", 0))))
            return {
                "score": round(score, 4),
                "explanation": result.get("explanation", ""),
                "details": {"method": "llm"},
            }
        except Exception as e:
            logger.warning(f"LLM relevancy 评估失败: {e}")

    # ── 启发式回退 ──
    return _heuristic_relevancy(query, answer)


def _heuristic_relevancy(query: str, answer: str) -> dict:
    """基于中文字符匹配的启发式 relevancy 评估"""
    query_lower = query.lower()
    answer_lower = answer.lower()

    # 中文按字匹配（每个汉字独立）
    query_chars = set(re.findall(r'[\u4e00-\u9fff]', query_lower))
    answer_chars = set(re.findall(r'[\u4e00-\u9fff]', answer_lower))

    # 英文按词匹配
    query_words_en = set(re.findall(r'[a-z][a-z0-9]*', query_lower))
    answer_words_en = set(re.findall(r'[a-z][a-z0-9]*', answer_lower))

    if not query_chars and not query_words_en:
        return {"score": 0.5, "explanation": "问题中无可匹配的字符", "details": {"method": "heuristic"}}
    if not answer_chars and not answer_words_en:
        return {"score": 0.0, "explanation": "回答中无可匹配的字符", "details": {"method": "heuristic"}}

    # 中文字符覆盖率
    char_coverage = len(query_chars & answer_chars) / \
        max(len(query_chars), 1) if query_chars else 1.0
    # 英文词覆盖率
    word_coverage = len(query_words_en & answer_words_en) / \
        max(len(query_words_en), 1) if query_words_en else 1.0

    # 综合：中文占主导，英文作为补充
    if query_chars and query_words_en:
        query_coverage = 0.7 * char_coverage + 0.3 * word_coverage
    elif query_chars:
        query_coverage = char_coverage
    else:
        query_coverage = word_coverage

    # 回答长度与问题长度的比例（太短可能不完整，太长可能跑题）
    len_ratio = len(answer) / max(len(query), 1)
    len_ratio_normalized = min(1.0, len_ratio / 10)

    score = 0.7 * query_coverage + 0.3 * len_ratio_normalized
    score = min(1.0, max(0.0, score))

    return {
        "score": round(score, 4),
        "explanation": f"启发式评估：问题字符覆盖率为 {char_coverage:.0%}，英文词覆盖率为 {word_coverage:.0%}，长度比例为 {len_ratio:.1f}x",
        "details": {
            "char_coverage": round(char_coverage, 4),
            "word_coverage": round(word_coverage, 4),
            "len_ratio": round(len_ratio, 2),
            "method": "heuristic",
        },
    }


# ──────────────────────────────────────────────
#  Hallucination — 幻觉检测
# ──────────────────────────────────────────────

_HALLUCINATION_PROMPT = """你是一位严格的 RAG 幻觉检测专家。你的任务：检测"回答"中是否存在"检索文档"不支持的信息（幻觉）。

评分标准（0-1 分，越高表示越少幻觉）：
1. 列出回答中的所有事实性陈述
2. 逐条判断每个陈述是否能在检索文档中找到支持
3. 无幻觉率 = 有支持的陈述 / 总陈述数

规则：
- 完全没有幻觉，所有陈述均有文档支持 → 1.0（无幻觉）
- 少量轻微幻觉（细节稍有偏差、过度概括）→ 0.6-0.9
- 中等幻觉（部分重要信息没有文档支持）→ 0.3-0.5
- 严重幻觉（大量编造信息）→ 0.0-0.2
- 对检索文档信息的合理重述和总结不视为幻觉
- 注意：明确标注为"推测""可能""建议"的内容不视为幻觉
- 列出具体的幻觉陈述，方便定位问题

输出必须是 JSON 格式（不要多余文字）：
{{"score": 0.0-1.0, "total_statements": N, "hallucinated_statements": N, "hallucination_list": ["具体幻觉1", ...], "explanation": "简要分析理由"}}

--- 用户问题 ---
{query}

--- 检索文档 ---
{context}

--- 回答 ---
{answer}
"""


def eval_hallucination(
    query: str,
    retrieved_docs: list[dict],
    answer: str,
    llm=None,
    usage_sink=None,
) -> dict:
    """检测回答中的幻觉（信息编造）

    Args:
        query: 用户问题
        retrieved_docs: 检索到的文档列表
        answer: 模型回答
        llm: LLMProvider 实例（可选）

    Returns:
        {"score": float (0-1), "explanation": str, "details": {...}}
        注意：score 越高表示幻觉越少（越忠实）
    """
    if not answer or not answer.strip():
        return {"score": 0.0, "explanation": "回答为空", "details": {}}

    # 清洗引用标记，避免 [来源N: ...] 干扰评分
    answer_clean = _clean_answer(answer)

    context_text = _format_docs(retrieved_docs)

    if llm is not None:
        try:
            prompt = _HALLUCINATION_PROMPT.format(
                query=query, context=context_text, answer=answer_clean
            )
            result = _llm_judge(llm, prompt, usage_sink=usage_sink)
            score = max(0.0, min(1.0, float(result.get("score", 0))))
            # score 越高 = 幻觉越少
            return {
                "score": round(score, 4),
                "explanation": result.get("explanation", ""),
                "details": {
                    "total_statements": result.get("total_statements", 0),
                    "hallucinated_statements": result.get("hallucinated_statements", 0),
                    "hallucination_list": result.get("hallucination_list", []),
                    "no_hallucination_rate": round(score, 4),
                },
            }
        except Exception as e:
            logger.warning(f"LLM hallucination 评估失败: {e}")

    # ── 启发式回退 ──
    return _heuristic_hallucination(answer, context_text)


def _heuristic_hallucination(answer: str, context_text: str) -> dict:
    """基于字符重叠的启发式幻觉检测"""
    sentences = _split_sentences(answer)
    if not sentences:
        return {"score": 1.0, "explanation": "无句子可分析", "details": {}}

    context_lower = context_text.lower()
    context_chars = set(re.findall(r'[\u4e00-\u9fff]', context_lower))
    context_ngrams = _get_ngrams(context_lower, n=2)

    hallucinated = 0
    hallucination_list = []

    for sent in sentences:
        sent_lower = sent.lower()
        sent_chars = set(re.findall(r'[\u4e00-\u9fff]', sent_lower))
        sent_ngrams = _get_ngrams(sent_lower, n=2)

        if not sent_chars:
            continue

        # 字符重叠率
        char_overlap = len(sent_chars & context_chars) / max(len(sent_chars), 1)
        # 二元组重叠
        ngram_overlap = len(sent_ngrams & context_ngrams) / max(len(sent_ngrams), 1)

        # 如果字符重叠率和二元组重叠率都很低，可能是幻觉
        if char_overlap < 0.15 and ngram_overlap == 0:
            hallucinated += 1
            hallucination_list.append(sent[:80])

    total = len([s for s in sentences if re.findall(r'[\u4e00-\u9fff]', s)])
    total = max(total, 1)
    score = 1.0 - (hallucinated / total)
    score = max(0.0, min(1.0, score))

    return {
        "score": round(score, 4),
        "explanation": f"启发式评估：{hallucinated}/{total} 个句子被标记为疑似幻觉",
        "details": {
            "total_statements": total,
            "hallucinated_statements": hallucinated,
            "hallucination_list": hallucination_list[:5],
            "no_hallucination_rate": round(score, 4),
            "method": "heuristic",
        },
    }


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────


def _format_docs(docs: list[dict], max_chars: int = 4000) -> str:
    """将检索文档列表格式化为评估上下文文本"""
    if not docs:
        return ""
    parts = []
    total = 0
    for i, d in enumerate(docs, 1):
        content = d.get("content", "") or ""
        source = d.get("file_name", f"doc_{i}")
        snippet = f"[{i}] 来源：{source}\n{content.strip()[:800]}"
        if total + len(snippet) > max_chars:
            remaining = max_chars - total
            if remaining > 200:
                parts.append(snippet[:remaining])
            break
        parts.append(snippet)
        total += len(snippet)
    return "\n\n".join(parts)


def _split_sentences(text: str) -> list[str]:
    """将文本分割为句子列表

    支持中文标点、英文标点、编号列表（如 1. 2. 3.）的分割
    """
    # 先规范化：将换行+编号格式转为带句号的形式
    text = re.sub(r'\n\s*\d+[\.\、\．]\s*', r'。\g<0>', text)
    # 分割
    parts = re.split(r'(?<=[。！？.!?])\s*', text)
    return [p.strip() for p in parts if p.strip() and len(p.strip()) > 1]


def _get_ngrams(text: str, n: int = 3) -> set:
    """从文本中提取 n-gram 集合

    对中文按字切分（每个汉字视为一个 token），对英文按词切分。
    """
    # 分离中文字符和非中文字词
    chinese_chars = re.findall(r'[\u4e00-\u9fff]', text.lower())
    non_chinese = re.findall(r'[a-z][a-z0-9]*', text.lower())
    all_tokens = chinese_chars + non_chinese

    ngrams = set()
    for i in range(len(all_tokens) - n + 1):
        ngrams.add(" ".join(all_tokens[i: i + n]))
    return ngrams


def _get_char_set(text: str) -> set:
    """提取文本中的中文字符集合，用于粗略的相似度匹配"""
    return set(re.findall(r'[\u4e00-\u9fff]', text.lower()))


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取 JSON 部分"""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n", 1)
        text = lines[1] if len(lines) > 1 else lines[0]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start: end + 1]
    return text


def _llm_judge(llm, prompt: str, timeout: int = 300, usage_sink=None) -> dict:
    """调用 LLM-as-Judge，统一处理调用参数 + JSON 解析

    Args:
        llm: LLMProvider 实例
        prompt: 完整的评分 prompt
        timeout: 超时秒数

    Returns:
        解析后的 JSON dict

    Raises:
        ValueError: LLM 返回的内容无法解析为 JSON
    """
    resp = llm.chat(
        [{"role": "user", "content": prompt}],
        temperature=0.01,   # 低温度保证 JSON 格式确定性
        timeout=timeout,
    )
    if usage_sink:
        usage_sink(resp, getattr(llm, "model", ""))
    raw = resp.get("content", "")
    text = _extract_json(raw)

    if not text.startswith("{"):
        logger.warning(f"LLM 评分响应未包含 JSON | 原始响应前200字: {raw[:200]!r}")
        raise ValueError(f"LLM 返回非 JSON: {raw[:80]!r}")

    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        logger.warning(f"JSON 解析失败: {e} | 提取文本前200字: {text[:200]!r}")
        raise ValueError(f"JSON 解析失败: {e}")


def _clean_answer(answer: str) -> str:
    """清洗回答中的引用标记，保留纯内容供评分

    只影响评分函数内部的 answer 副本，前台显示的原始 answer 不变。
    """
    if not answer:
        return answer

    # 1. 移除 【思考过程】...--- 整块
    answer = re.sub(
        r'【思考过程】.*?(?:---|\Z)',
        '',
        answer,
        flags=re.DOTALL,
    )

    # 2. 移除行内 [来源N: xxx] 或 [来源N]
    answer = re.sub(r'\s*\[来源\d+[^\]]*\]', '', answer)

    # 3. 移除 [注：xxx]（通常末尾）
    answer = re.sub(r'\s*\[注：[^\]]*\]', '', answer)

    # 4. 清理多余空行
    answer = re.sub(r'\n{3,}', '\n\n', answer)
    answer = answer.strip()

    return answer


# ──────────────────────────────────────────────
#  独立运行：演示
# ──────────────────────────────────────────────


def _demo():
    """演示用测试数据"""
    query = "等保三级对数据存储加密有什么要求？"
    docs = [
        {
            "file_name": "GB-T 22239-2019.md",
            "content": (
                "GB/T 22239-2019 三级安全要求中的'数据安全保护'部分规定："
                "应采用加密技术对重要数据进行存储加密保护，包括鉴别信息、"
                "重要业务数据和重要个人信息。加密算法应使用国家密码管理"
                "部门认可的密码算法。"
            ),
        },
        {
            "file_name": "等保三级技术要求.md",
            "content": (
                "等保三级数据安全要求：\n"
                "a) 应采用加密或其他有效措施实现系统管理数据、鉴别信息和"
                "重要业务数据的存储保密性；\n"
                "b) 应采用加密技术对重要数据进行传输加密。"
            ),
        },
    ]
    answer_faithful = (
        "等保三级对数据存储加密的要求：\n"
        "1. 重要数据（包括鉴别信息、重要业务数据、重要个人信息）应使用加密技术进行存储保护\n"
        "2. 加密算法必须使用国家密码管理部门认可的算法\n"
        "3. 同时要求对传输过程中的重要数据进行加密保护"
    )
    answer_hallucinated = (
        "等保三级对数据存储加密的要求：\n"
        "1. 应使用 AES-256 加密算法对数据进行加密\n"
        "2. 密钥需每 30 天轮换一次\n"
        "3. 加密后的数据需存储在 HSM 硬件安全模块中\n"
        "4. 同时要求对重要数据进行传输加密"
    )
    return query, docs, answer_faithful, answer_hallucinated


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    query, docs, answer_faithful, answer_hallucinated = _demo()

    logger.info("=" * 60)
    logger.info("域C：生成质量评估（回退模式 — 无 LLM）")
    logger.info("=" * 60)
    logger.info(f"问题: {query}\n")

    # ── Faithfulness（忠实回答）──
    logger.info("-" * 40)
    logger.info("测试1：忠实回答 → Faithfulness 应该高分")
    result = eval_faithfulness(query, docs, answer_faithful, llm=None)
    logger.info(f"  Score: {result['score']:.4f}")
    logger.info(f"  说明: {result['explanation'][:80]}")
    logger.info(f"  详情: {result['details']}\n")

    # ── Faithfulness（幻觉回答）──
    logger.info("-" * 40)
    logger.info("测试2：含幻觉回答 → Faithfulness 应该低分")
    result = eval_faithfulness(query, docs, answer_hallucinated, llm=None)
    logger.info(f"  Score: {result['score']:.4f}")
    logger.info(f"  说明: {result['explanation'][:80]}")
    logger.info(f"  详情: {result['details']}\n")

    # ── Relevancy（相关回答）──
    logger.info("-" * 40)
    logger.info("测试3：回答相关性 → Relevancy 应该高分")
    result = eval_relevancy(query, answer_faithful, docs, llm=None)
    logger.info(f"  Score: {result['score']:.4f}")
    logger.info(f"  说明: {result['explanation'][:80]}\n")

    # ── Relevancy（无关回答）──
    logger.info("-" * 40)
    irrelevant_answer = "今天天气不错，适合出去走走。"
    logger.info("测试4：无关回答 → Relevancy 应该低分")
    result = eval_relevancy(query, irrelevant_answer, docs, llm=None)
    logger.info(f"  Score: {result['score']:.4f}")
    logger.info(f"  说明: {result['explanation'][:80]}\n")

    # ── Hallucination（忠实回答）──
    logger.info("-" * 40)
    logger.info("测试5：忠实回答 → Hallucination 应该高分（幻觉少）")
    result = eval_hallucination(query, docs, answer_faithful, llm=None)
    logger.info(f"  Score: {result['score']:.4f}（越高越无幻觉）")
    logger.info(f"  说明: {result['explanation'][:80]}")
    logger.info(f"  详情: {result['details']}\n")

    # ── Hallucination（幻觉回答）──
    logger.info("-" * 40)
    logger.info("测试6：含幻觉回答 → Hallucination 应该低分（幻觉多）")
    result = eval_hallucination(query, docs, answer_hallucinated, llm=None)
    logger.info(f"  Score: {result['score']:.4f}（越高越无幻觉）")
    logger.info(f"  说明: {result['explanation'][:80]}")
    logger.info(f"  详情: {result['details']}\n")

    logger.info("=" * 60)
    logger.info("[OK] 域C评估完成")


if __name__ == "__main__":
    main()
