"""评估检索上下文质量，包括 Context Precision 和 Context Recall。

用法:
    # 作为模块导入
    from _eval_context import eval_context_precision, eval_context_recall

    result = eval_context_precision(query, retrieved_docs, llm=llm)

    # 独立运行
    python _eval_context.py
"""
import os
import sys
import json
import logging
import re
from typing import Optional
from _eval_recall import eval_context_recall
from _eval_relevance import eval_context_relevance

logger = logging.getLogger(__name__)
_EVAL_DOC_CHAR_LIMIT = 800
_EVAL_CONTEXT_CHAR_LIMIT = 4000

# ──────────────────────────────────────────────
#  Context Precision — 排序检索结果的相关性精度
# ──────────────────────────────────────────────

_CONTEXT_PRECISION_PROMPT = """你是严格的 RAG 检索评估专家。只根据用户问题，判断每个检索片段是否与回答该问题直接相关。不要评价回答，也不要使用外部知识补充判断。

--- 用户问题 ---
{query}

--- 检索文档 ---
{context}

按原始排名逐条返回判断，不得遗漏或重排。只输出 JSON：
{{"judgments":[{{"rank":1,"relevant":true}},{{"rank":2,"relevant":false}}]}}
"""


def eval_context_precision(
    query: str,
    retrieved_docs: list[dict],
    answer: str = "",
    *legacy_args,
    **options,
) -> dict:
    """Calculate ranked context precision from labels or a Judge.

    Args:
        query: User query.
        retrieved_docs: Retrieved documents in their original rank order.
        answer: Legacy positional argument; ignored for this metric.
        legacy_args: Legacy positional `llm` and `usage_sink` values.
        **options: Optional `llm`, `usage_sink`, `relevance_annotations`, and
            `annotation_source` values.

    Returns:
        A metric result with score, status, explanation, details, and evaluator.

    Raises:
        TypeError: If an unsupported option is supplied.

    Example:
        `eval_context_precision(query, docs, relevance_annotations=[True, False], annotation_source="gold-set-v1")`
    """
    if len(legacy_args) > 2 or any(
        name in options for name in ("llm", "usage_sink")[:len(legacy_args)]
    ):
        raise TypeError("Invalid legacy positional options")
    options.update(zip(("llm", "usage_sink"), legacy_args))
    del answer
    allowed = {"llm", "usage_sink", "relevance_annotations", "annotation_source"}
    unknown = sorted(set(options) - allowed)
    if unknown:
        raise TypeError(f"Unsupported precision options: {', '.join(unknown)}")
    return _evaluate_context_precision(query, retrieved_docs, options)


def _evaluate_context_precision(query, retrieved_docs, options) -> dict:
    llm = options.get("llm")
    usage_sink = options.get("usage_sink")
    relevance_annotations = options.get("relevance_annotations")
    annotation_source = options.get("annotation_source")
    if not retrieved_docs:
        return _precision_unscored(
            "unscored_empty_retrieval", "没有检索片段。", ["retrieved_contexts"]
        )

    judgments = _precision_annotations(relevance_annotations, len(retrieved_docs))
    if judgments is not None:
        source = annotation_source.strip() if isinstance(annotation_source, str) else ""
        if source:
            return _precision_scored(judgments, "annotation", source)
        if llm is None:
            return _precision_unscored(
                "unscored_missing_annotation",
                "相关性标签缺少可审查的来源标识。",
                ["relevance_annotations"],
                judgments=judgments,
            )
    elif relevance_annotations is not None:
        return _precision_unscored(
            "unscored_missing_annotation",
            "相关性标签必须为与检索片段等长的布尔值列表。",
            ["relevance_annotations"],
            judgments=_partial_precision_annotations(relevance_annotations, len(retrieved_docs)),
        )
    if llm is None:
        return _precision_unscored(
            "unscored_missing_annotation",
            "没有提供检索片段相关性标签或 Judge。",
            ["relevance_annotations"],
        )
    try:
        judged = _judge_context_precision(query, retrieved_docs, llm, usage_sink)
        return _precision_scored(judged, "llm_judge", None, llm=llm)
    except _InvalidContextJudgment as exc:
        return _precision_judge_error(llm, "invalid_judgment", str(exc))
    except ValueError as exc:
        return _precision_judge_error(llm, "invalid_response", str(exc))
    except Exception as exc:
        logger.warning("LLM context_precision 评估失败: %s", exc)
        return _precision_judge_error(llm, "call_failed", "Judge 调用失败。")


def _precision_annotations(labels, doc_count: int) -> list[dict] | None:
    if not isinstance(labels, list) or len(labels) != doc_count:
        return None
    if any(not isinstance(label, bool) for label in labels):
        return None
    return [{"rank": rank, "relevant": label} for rank, label in enumerate(labels, 1)]


def _partial_precision_annotations(labels, doc_count: int) -> list[dict]:
    if not isinstance(labels, list):
        return []
    return [{"rank": rank, "relevant": label} for rank, label in enumerate(labels[:doc_count], 1) if isinstance(label, bool)]


def _judge_context_precision(query: str, docs: list[dict], llm, usage_sink) -> list[dict]:
    if _precision_context_is_truncated(docs):
        raise _InvalidContextJudgment("Judge 可见的检索上下文不完整。")
    prompt = _CONTEXT_PRECISION_PROMPT.format(
        query=query, context=_format_docs(docs)
    )
    result = _llm_judge(llm, prompt, usage_sink=usage_sink)
    raw_judgments = result.get("judgments")
    if not isinstance(raw_judgments, list) or len(raw_judgments) != len(docs):
        raise _InvalidContextJudgment("Judge 未为每个检索片段返回判断。")

    judgments = []
    for rank, item in enumerate(raw_judgments, start=1):
        if (
            not isinstance(item, dict)
            or type(item.get("rank")) is not int
            or item["rank"] != rank
            or type(item.get("relevant")) is not bool
        ):
            raise _InvalidContextJudgment("Judge 返回了缺失、重复或无效的排名标签。")
        judgments.append({"rank": rank, "relevant": item["relevant"]})
    return judgments


def _precision_context_is_truncated(docs: list[dict]) -> bool:
    total_chars = 0
    for rank, doc in enumerate(docs, start=1):
        content = (doc.get("content", "") or "").strip()
        if len(content) > _EVAL_DOC_CHAR_LIMIT:
            return True
        source = doc.get("file_name", f"doc_{rank}")
        total_chars += len(f"[{rank}] 来源：{source}\n{content}")
        total_chars += 2 if rank > 1 else 0
        if total_chars > _EVAL_CONTEXT_CHAR_LIMIT:
            return True
    return False


class _InvalidContextJudgment(ValueError):
    pass


def _precision_score(judgments: list[dict]) -> tuple[float, int]:
    relevant_count = sum(item["relevant"] for item in judgments)
    if relevant_count == 0:
        return 0.0, 0
    relevant_seen = 0
    numerator = 0.0
    for item in judgments:
        relevant_seen += int(item["relevant"])
        if item["relevant"]:
            numerator += relevant_seen / item["rank"]
    return round(numerator / relevant_count, 4), round(numerator, 4)


def _precision_scored(judgments, method, source, llm=None) -> dict:
    score, numerator = _precision_score(judgments)
    evaluator = _precision_evaluator(method, llm)
    return {
        "score": score,
        "status": "scored",
        "explanation": "按检索排名和相关性标签计算 Average Precision。",
        "details": {
            "judgments": judgments,
            "ap_numerator": numerator,
            "relevant_count": sum(item["relevant"] for item in judgments),
            **({"annotation_source": source} if method == "annotation" else {}),
        },
        "evaluator": evaluator,
    }


def _precision_unscored(status, explanation, missing_inputs, judgments=None) -> dict:
    details = {"missing_inputs": missing_inputs}
    if judgments:
        details["judgments"] = judgments
    return {
        "score": None,
        "status": status,
        "explanation": explanation,
        "details": details,
        "evaluator": _precision_evaluator("not_run"),
    }


def _precision_judge_error(llm, category, explanation) -> dict:
    return {
        "score": None,
        "status": "judge_error",
        "explanation": explanation,
        "details": {
            "failure_category": category,
            "fallback_attempted": False,
        },
        "evaluator": _precision_evaluator("llm_judge", llm),
    }


def _precision_evaluator(method, llm=None) -> dict:
    is_judge = method == "llm_judge"
    return {
        "method": method,
        "model_id": getattr(llm, "model", None) if is_judge else None,
        "prompt_version": "context-precision-v1" if is_judge else None,
    }


# ──────────────────────────────────────────────
#  辅助函数
# ──────────────────────────────────────────────


def _format_docs(docs: list[dict], max_chars: int = _EVAL_CONTEXT_CHAR_LIMIT) -> str:
    """将检索文档列表格式化为评评估上下文文本"""
    if not docs:
        return ""
    parts = []
    total = 0
    for i, d in enumerate(docs, 1):
        content = d.get("content", "") or ""
        source = d.get("file_name", f"doc_{i}")
        snippet = f"[{i}] 来源：{source}\n{content.strip()[:_EVAL_DOC_CHAR_LIMIT]}"
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

    logger.info("=" * 60)
    logger.info("域B：上下文质量评估（回退模式 — 无 LLM）")
    logger.info("=" * 60)
    logger.info(f"问题: {query}\n")

    # Context Precision
    result_p = eval_context_precision(query, docs, answer, llm=None)
    precision_score = result_p["score"]
    precision_label = f"{precision_score:.4f}" if precision_score is not None else "未评分"
    logger.info(f"[Context Precision]  {precision_label}")
    logger.info(f"  说明: {result_p['explanation'][:80]}")
    logger.info(f"  详情: {result_p['details']}\n")

    # Context Recall
    result_r = eval_context_recall(query, docs, answer, llm=None)
    recall_label = f"{result_r['score']:.4f}" if result_r["score"] is not None else "未评分"
    logger.info(f"[Context Recall]     {recall_label}")
    logger.info(f"  说明: {result_r['explanation'][:80]}")
    logger.info(f"  详情: {result_r['details']}\n")

    # 汇总
    logger.info(f"\n[OK] 域B评估完成")
    logger.info(f"  Context Precision: {precision_label}")
    logger.info(f"  Context Recall:    {recall_label}")


if __name__ == "__main__":
    main()
