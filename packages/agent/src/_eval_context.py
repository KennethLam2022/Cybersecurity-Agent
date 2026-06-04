"""域B：上下文质量评估 — Context Precision + Context Recall

评估回答引用检索文档的准确率，以及检索文档关键信息被引用的比例。
使用 LLM-as-Judge 进行评分，有回退到启发式方法。

用法:
    # 作为模块导入
    from _eval_context import eval_context_precision, eval_context_recall

    result = eval_context_precision(query, retrieved_docs, answer, llm=llm)
    # => {"score": 0.85, "explanation": "...", "details": {...}}

    # 独立运行
    python _eval_context.py
"""
import os, sys, json, logging, re
from typing import Optional

logger = logging.getLogger(__name__)

# ──────────────────────────────────────────────
#  Context Precision — 回答引用检索文档的准确率
# ──────────────────────────────────────────────

_CONTEXT_PRECISION_PROMPT = """你是一位严格的 RAG 评估专家。你的任务：评估"回答"中的每一个陈述是否被"检索文档"所支持。

评分标准（0-1 分）：
1. 逐句分析回答，列出每个陈述
2. 判断每个陈述是否可以从检索文档中找到支持
3. 计算支持率：supported_claims / total_claims

规则：
- 如果回答完全基于检索文档、没有编造 → 高分（0.9-1.0）
- 如果回答部分内容在文档中找不到支持 → 中分（0.4-0.8）
- 如果回答大量内容与文档无关或编造 → 低分（0.0-0.3）
- 不要求逐字匹配，只要意思能在文档中找到支持即可
- 如果回答拒绝回答或说"没有相关信息"而文档确实有相关内容，扣分

输出必须是 JSON 格式（不要多余文字）：
{"score": 0.0-1.0, "total_claims": N, "supported_claims": N, "explanation": "简要分析理由"}

--- 用户问题 ---
{query}

--- 检索文档 ---
{context}

--- 回答 ---
{answer}
"""


def eval_context_precision(
    query: str,
    retrieved_docs: list[dict],
    answer: str,
    llm=None,
) -> dict:
    """评估回答引用检索文档的准确率

    Args:
        query: 用户问题
        retrieved_docs: 检索到的文档列表 [{"file_name":..., "content":..., ...}, ...]
        answer: 模型回答
        llm: LLMProvider 实例（可选），若为 None 则使用启发式回退

    Returns:
        {"score": float (0-1), "explanation": str, "details": {...}}
    """
    if not answer or not answer.strip():
        return {"score": 0.0, "explanation": "回答为空", "details": {}}

    context_text = _format_docs(retrieved_docs)

    if llm is not None:
        try:
            prompt = _CONTEXT_PRECISION_PROMPT.format(
                query=query, context=context_text, answer=answer
            )
            resp = llm.chat([{"role": "user", "content": prompt}], timeout=120)
            text = resp.get("content", "")
            text = _extract_json(text)
            result = json.loads(text)
            score = max(0.0, min(1.0, float(result.get("score", 0))))
            return {
                "score": round(score, 4),
                "explanation": result.get("explanation", ""),
                "details": {
                    "total_claims": result.get("total_claims", 0),
                    "supported_claims": result.get("supported_claims", 0),
                },
            }
        except Exception as e:
            logger.warning(f"LLM context_precision 评估失败，使用回退: {e}")

    # ── 启发式回退：基于关键词重叠的近似评估 ──
    return _heuristic_context_precision(answer, context_text)


def _heuristic_context_precision(answer: str, context_text: str) -> dict:
    """基于关键词重叠的启发式 context precision 评估"""
    sentences = _split_sentences(answer)
    if not sentences:
        return {"score": 0.0, "explanation": "无法分解句子", "details": {}}

    context_lower = context_text.lower()
    context_chars = set(re.findall(r'[\u4e00-\u9fff]', context_lower))
    context_words_en = set(re.findall(r'[a-z][a-z0-9]*', context_lower))

    supported = 0
    for sent in sentences:
        sent_lower = sent.lower()
        sent_chars = set(re.findall(r'[\u4e00-\u9fff]', sent_lower))
        sent_words_en = set(re.findall(r'[a-z][a-z0-9]*', sent_lower))

        if not sent_chars and not sent_words_en:
            supported += 1
            continue

        # 中文字符重叠率
        char_overlap = len(sent_chars & context_chars) / max(len(sent_chars), 1) if sent_chars else 1.0
        # 英文词重叠率
        word_overlap = len(sent_words_en & context_words_en) / max(len(sent_words_en), 1) if sent_words_en else 1.0

        if char_overlap >= 0.3 or word_overlap >= 0.3:
            supported += 1

    score = supported / len(sentences) if sentences else 0
    return {
        "score": round(score, 4),
        "explanation": f"启发式评估：{supported}/{len(sentences)} 个句子与检索文档有显著关键词重叠",
        "details": {
            "total_claims": len(sentences),
            "supported_claims": supported,
            "method": "heuristic",
        },
    }


# ──────────────────────────────────────────────
#  Context Recall — 检索文档关键信息被引用的比例
# ──────────────────────────────────────────────

_CONTEXT_RECALL_PROMPT = """你是一位严格的 RAG 评估专家。你的任务：评估"回答"覆盖了多少"检索文档"中的关键信息。

评分标准（0-1 分）：
1. 阅读检索文档，提取其中与用户问题相关的关键信息点
2. 判断回答覆盖了其中多少个关键信息点
3. 计算覆盖率：covered_key_points / total_key_points

规则：
- 如果回答覆盖了文档中所有关键信息 → 高分（0.9-1.0）
- 如果回答只覆盖了部分关键信息 → 中分（0.3-0.8）
- 如果回答几乎没用到文档中的信息 → 低分（0.0-0.2）
- 如果回答完全没有引用文档内容 → 0分
- 注意：只计文档中有的、与问题相关的信息；超出文档的内容不计入

输出必须是 JSON 格式（不要多余文字）：
{"score": 0.0-1.0, "total_key_points": N, "covered_key_points": N, "explanation": "简要分析理由"}

--- 用户问题 ---
{query}

--- 检索文档 ---
{context}

--- 回答 ---
{answer}
"""


def eval_context_recall(
    query: str,
    retrieved_docs: list[dict],
    answer: str,
    llm=None,
) -> dict:
    """评估检索文档关键信息被回答引用的比例

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

    context_text = _format_docs(retrieved_docs)
    if not context_text.strip():
        return {"score": 0.0, "explanation": "没有检索到文档", "details": {}}

    if llm is not None:
        try:
            prompt = _CONTEXT_RECALL_PROMPT.format(
                query=query, context=context_text, answer=answer
            )
            resp = llm.chat([{"role": "user", "content": prompt}], timeout=120)
            text = resp.get("content", "")
            text = _extract_json(text)
            result = json.loads(text)
            score = max(0.0, min(1.0, float(result.get("score", 0))))
            return {
                "score": round(score, 4),
                "explanation": result.get("explanation", ""),
                "details": {
                    "total_key_points": result.get("total_key_points", 0),
                    "covered_key_points": result.get("covered_key_points", 0),
                },
            }
        except Exception as e:
            logger.warning(f"LLM context_recall 评估失败，使用回退: {e}")

    # ── 启发式回退 ──
    return _heuristic_context_recall(answer, context_text)


def _heuristic_context_recall(answer: str, context_text: str) -> dict:
    """基于字符覆盖的启发式 context recall 评估"""
    answer_lower = answer.lower()
    answer_chars = set(re.findall(r'[\u4e00-\u9fff]', answer_lower))

    context_chars = set(re.findall(r'[\u4e00-\u9fff]', context_text.lower()))

    if len(context_chars) == 0:
        return {"score": 0.0, "explanation": "文档中无可提取的关键词", "details": {}}

    overlap = len(answer_chars & context_chars)
    # 使用 sqrt 归一化，避免文档过长导致的稀疏问题
    score = min(1.0, overlap / max(len(context_chars) ** 0.5, 1) * 0.15)

    return {
        "score": round(score, 4),
        "explanation": f"启发式评估：回答含 {len(answer_chars)} 个不同中文字符，文档含 {len(context_chars)} 个，重叠 {overlap} 个",
        "details": {
            "total_key_points": len(context_chars),
            "covered_key_points": overlap,
            "method": "heuristic",
        },
    }


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────


def _format_docs(docs: list[dict], max_chars: int = 4000) -> str:
    """将检索文档列表格式化为评评估上下文文本"""
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
    """将文本分割为句子列表"""
    # 支持中文标点（。！？）和英文标点（.!?）
    parts = re.split(r'(?<=[。！？.!?])\s*', text)
    return [p.strip() for p in parts if p.strip()]


def _extract_json(text: str) -> str:
    """从 LLM 输出中提取 JSON 部分"""
    text = text.strip()
    # 移除 markdown 代码块标记
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
    if text.endswith("```"):
        text = text.rsplit("```", 1)[0]
    text = text.strip()
    # 找到第一个 { 和最后一个 }
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        text = text[start : end + 1]
    return text


# ──────────────────────────────────────────────
#  独立运行：简单自测
# ──────────────────────────────────────────────


def _demo():
    """演示用测试数据"""
    query = "等保三级对访问控制有什么要求？"
    docs = [
        {
            "file_name": "等保三级技术要求.md",
            "content": (
                "等保三级对访问控制的要求包括："
                "1) 应对登录的用户进行身份标识和鉴别；"
                "2) 应提供访问控制机制，限制用户对资源的访问权限；"
                "3) 应实现对重要数据和敏感信息的访问控制；"
                "4) 应提供对访问行为的审计功能。"
            ),
        },
        {
            "file_name": "GB-T 22239-2019.md",
            "content": (
                "GB/T 22239-2019 三级安全要求中，"
                "访问控制属于安全计算环境的一部分，"
                "要求实施最小权限原则，对用户、进程和服务进行权限管理。"
            ),
        },
    ]
    answer = (
        "等保三级对访问控制的要求主要包括四个方面：\n"
        "1. 身份标识和鉴别：对登录用户进行身份验证；\n"
        "2. 访问控制机制：限制用户对资源的访问权限；\n"
        "3. 重要数据和敏感信息的访问控制；\n"
        "4. 访问行为审计功能。\n"
        "此外，还需要实施最小权限原则。"
    )
    return query, docs, answer


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
    )

    query, docs, answer = _demo()

    print("=" * 60)
    print("域B：上下文质量评估（回退模式 — 无 LLM）")
    print("=" * 60)
    print(f"问题: {query}\n")

    # Context Precision
    result_p = eval_context_precision(query, docs, answer, llm=None)
    print(f"[Context Precision]  {result_p['score']:.4f}")
    print(f"  说明: {result_p['explanation'][:80]}")
    print(f"  详情: {result_p['details']}\n")

    # Context Recall
    result_r = eval_context_recall(query, docs, answer, llm=None)
    print(f"[Context Recall]     {result_r['score']:.4f}")
    print(f"  说明: {result_r['explanation'][:80]}")
    print(f"  详情: {result_r['details']}\n")

    # 汇总
    print(f"\n[OK] 域B评估完成")
    print(f"  Context Precision: {result_p['score']:.4f}")
    print(f"  Context Recall:    {result_r['score']:.4f}")


if __name__ == "__main__":
    main()