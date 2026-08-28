"""
Prompt 测试管理器 — 纯函数核心模块
=====================================
职责：测试集生成、单条/批量运行、智能修复建议
设计：不依赖 FastAPI，LLM/Agent 从外部注入（DI），可独立 pytest
"""
import json
import time
import re
from typing import Optional, Callable
from datetime import datetime

from prompt_test_contract import evaluate_prompt_contract
from evaluation_matrix import evaluate_with_weights, get_dimension_breakdown


GENERATE_PROMPT = """你是一位网络安全管理专家，现在需要为 AI 助手的 System Prompt 生成测试集。

## 要求
{constraints}

## 输入关键词
{keywords}

## 输出格式
严格输出 JSON 数组，每个元素包含：
- "query": 测试问题（贴近网络安全实际场景）
- "category": 分类（来源标注|禁止内容|拒答与重定向|首答完整|知识准确）
- "difficulty": 难度（easy|medium|hard）
- "expected": 期望结果对象（参考下方）

## 期望结果对象字段说明
- must_contain_annotation: bool（是否必须来源标注）
- must_not_contain: list[str]（禁止出现的词）
- expected_behavior: "refuse_or_redirect" | "answer_normally"
- forbidden: list[str]（拒答场景不得泄漏的内容）
- required_all: list[str]（必须全部包含）
- required_any: list[str]（至少命中一个）

## 质量要求
1. 覆盖全部 5 个分类
2. 包含简单(easy)、中等(medium)、困难(hard) 三个难度
3. 贴近实际业务场景
4. 不要有重复或过于相似的题目
5. 输出 20 条测试题
"""

FIX_PROMPT = """你是一位 Prompt 工程专家，负责分析和优化 AI 助手的 System Prompt。

## 当前 System Prompt（摘要）
{system_prompt}

## 失败的测试题
- 问题: {query}
- 期望分类: {category}
- 实际得分: {score}/100
- 失败原因: {fail_reason}
- 当前回答摘要: {answer_preview}

## 任务
1. 分析为什么测试失败
2. 给出具体的 System Prompt 修改方案
3. 修改方案必须是**可直接替换**的完整 System Prompt 文本

## 输出格式
JSON:
{{
  "analysis": "失败原因分析（中文，50-100字）",
  "new_system_prompt": "修改后的完整 System Prompt",
  "diff_summary": "改动说明（如：新增第7条规则、修改第3条约束）",
  "changed_lines": {"added": N, "removed": M}
}}
"""


def generate_test_set(keywords: str, llm=None, usage_sink=None) -> list:
    """根据关键词生成 20 条测试题

    Args:
        keywords: 管理员输入的关键词，如 "等保三级、数据安全"；输入"随机"让 AI 自主生成
        llm: LLMProvider 实例

    Returns:
        list[dict]: 20 条测试题
    """
    if llm is None:
        return _mock_generate(keywords)

    constraints = "根据以下关键词生成覆盖多维度、多难度的测试题，共 20 条。"
    if not keywords or keywords.strip() in ("随机", "默认", ""):
        constraints = "自主生成覆盖法律法规、等保2.0、数据安全、个人信息保护、APP安全、应急响应、风险管理、安全管理体系、供应链安全等通用网络安全领域的测试题，共 20 条。"
        keywords = "全领域覆盖"

    prompt = GENERATE_PROMPT.format(
        constraints=constraints,
        keywords=keywords.strip() or "全领域",
    )

    try:
        raw = llm.chat([{"role": "user", "content": prompt}],
                       temperature=0.3, max_tokens=4000, timeout=30)
        if usage_sink:
            usage_sink(raw, getattr(llm, "model", ""))
        items = validate_generated_test_items(json.loads(_extract_json(raw)))
        if len(items) >= 5:
            return items[:20]
        return _mock_generate(keywords)
    except Exception:
        return _mock_generate(keywords)


def _extract_json(text: str) -> str:
    """从 LLM 回复中提取 JSON 数组"""
    text = text.strip()
    # 尝试直接解析
    if text.startswith("["):
        return text
    # 查找 ```json ... ```
    m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
    if m:
        return m.group(1)
    # 查找 [ 到结尾
    start = text.find("[")
    if start != -1:
        return text[start:]
    return "[]"


def validate_generated_test_items(items: object) -> list[dict]:
    """Keep only generated cases that have an executable, explicit expectation."""
    if not isinstance(items, list):
        return []

    valid = []
    for item in items:
        if not isinstance(item, dict):
            continue
        query = item.get("query")
        expected = item.get("expected")
        if not isinstance(query, str) or not query.strip() or not isinstance(expected, dict):
            continue

        behavior = expected.get("expected_behavior", "answer_normally")
        if behavior not in {"answer_normally", "refuse_or_redirect"}:
            continue
        if not any([
            expected.get("must_contain_annotation"),
            expected.get("must_not_contain"),
            expected.get("forbidden"),
            expected.get("required_all"),
            expected.get("required_any"),
            behavior == "refuse_or_redirect",
        ]):
            continue

        valid.append({
            "query": query.strip(),
            "category": str(item.get("category") or "通用"),
            "difficulty": item.get("difficulty") if item.get("difficulty") in {"easy", "medium", "hard"} else "medium",
            "expected": expected,
        })
    return valid


def _mock_generate(keywords: str) -> list:
    """LLM 不可用时的模拟生成"""
    kw = keywords.strip() if keywords and keywords.strip() not in ("随机", "默认", "") else "安全"
    cats = ["来源标注", "禁止内容", "拒答与重定向", "首答完整", "知识准确"]
    diffs = ["easy", "medium", "hard"]
    templates = [
        f"等保三级{kw}的要求是什么", f"{kw}包括哪些方面", f"如何做好{kw}", f"{kw}的最佳实践",
        f"{kw}的合规要求", f"{kw}的常见问题", f"{kw}的解决方案", f"{kw}相关标准",
        f"{kw}的实施步骤", f"{kw}的风险评估", f"{kw}的审计要点", f"{kw}的管理制度",
        f"{kw}的技术要求", f"{kw}的人员配置", f"{kw}的应急方案", f"{kw}的培训内容",
        f"{kw}的考核指标", f"{kw}的整改建议", f"{kw}的检查清单", f"{kw}的案例分享",
    ]
    return [
        {"query": tpl, "category": cats[i % len(cats)], "difficulty": diffs[i % 3],
         "expected": {
             "expected_behavior": "answer_normally",
             "must_contain_annotation": i % 2 == 0,
             "required_any": [kw],
         }}
        for i, tpl in enumerate(templates)
    ]


def run_single_test(item: dict, agent, retrieved_docs: Optional[list] = None,
                    system_prompt_override: Optional[str] = None) -> dict:
    """运行单条 Prompt 测试（仅域A规则评分，不调用 LLM）

    Args:
        item: {"query": "...", "category": "...", "difficulty": "...", "expected": {...}}
        agent: CyberAgent 实例
        retrieved_docs: 可选检索文档列表（供后端记录，域B/C评分在 prompt_tester.py 中处理）

    Returns:
        {"id": ..., "query": ..., "scores": {...}, "weighted_score": ..., "passed": ..., ...}
    """
    query = item["query"]
    expected = item.get("expected", {})
    category = item.get("category", "")

    start = time.time()
    result = agent.ask(
        query=query, conversation_id=None, temperature=0.1, category="prompt_test",
        system_prompt_override=system_prompt_override,
        retrieved_docs_override=retrieved_docs,
    )
    duration = time.time() - start
    answer = result.get("answer", "")

    contract = evaluate_prompt_contract(answer, expected, category)
    scores = contract["scores"]
    details = {name: data["detail"] for name, data in contract["checks"].items()}

    weighted_score = evaluate_with_weights(scores)
    breakdown = get_dimension_breakdown(scores)
    avg = sum(scores.values()) / len(scores) if scores else 0.0

    return {
        "id": item.get("id", 0),
        "query": query,
        "category": category,
        "difficulty": item.get("difficulty", "medium"),
        "answer_preview": answer[:300],
        "answer": answer,
        "scores": scores,
        "avg_score": round(avg, 2),
        "weighted_score": weighted_score,
        "dimension_breakdown": breakdown,
        "details": details,
        "duration": round(duration, 2),
        "passed": contract["passed"],
        "checks": contract["checks"],
        "applicable_dimensions": contract["applicable_dimensions"],
        "not_applicable_dimensions": contract["not_applicable_dimensions"],
        "failed_dimensions": contract["failed_dimensions"],
    }


def run_test_set(items: list, agent, on_progress: Optional[Callable] = None) -> list:
    """批量运行测试集

    Args:
        items: 测试题列表
        agent: CyberAgent 实例
        on_progress: 进度回调 fn(completed, total, current_item, result)

    Returns:
        list[dict]: 测试结果列表
    """
    results = []
    total = len(items)
    for i, item in enumerate(items):
        result = run_single_test(item, agent)
        results.append(result)
        if on_progress:
            on_progress(i + 1, total, item, result)
    return results


def suggest_fix(failed_item: dict, current_system_prompt: str, llm=None, usage_sink=None) -> dict:
    """分析失败原因并生成修复建议

    Args:
        failed_item: 失败的测试结果（含 query/scores/details/answer_preview）
        current_system_prompt: 当前 System Prompt 全文
        llm: LLMProvider 实例

    Returns:
        {"analysis": "...", "new_system_prompt": "...", "diff_summary": "...", "changed_lines": {...}}
    """
    score_pct = round(failed_item.get("weighted_score", 0), 0)
    fail_dims = failed_item.get("failed_dimensions") or [
        k for k, v in failed_item.get("scores", {}).items() if v < 0.5
    ]
    fail_reason = "; ".join(
        f"{dim}: {failed_item.get('details', {}).get(dim, '无详情')}"
        for dim in fail_dims
    ) or "综合得分偏低"

    prompt = FIX_PROMPT.format(
        system_prompt=current_system_prompt[:1500],
        query=failed_item.get("query", ""),
        category=failed_item.get("category", ""),
        score=score_pct,
        fail_reason=fail_reason,
        answer_preview=(failed_item.get("answer_preview") or "")[:200],
    )

    if llm is None:
        return {
            "analysis": f"测试失败: {fail_reason}",
            "new_system_prompt": current_system_prompt + f"\n# 新增规则: 针对 {failed_item.get('category', '')} 类问题加强回答规范\n",
            "diff_summary": f"新增规则: 加强{failed_item.get('category', '')}回答规范",
            "changed_lines": {"added": 1, "removed": 0},
        }

    try:
        raw = llm.chat([{"role": "user", "content": prompt}],
                       temperature=0.2, max_tokens=4000, timeout=30)
        if usage_sink:
            usage_sink(raw, getattr(llm, "model", ""))
        result = json.loads(_extract_json(raw))
        return {
            "analysis": result.get("analysis", fail_reason),
            "new_system_prompt": result.get("new_system_prompt", current_system_prompt),
            "diff_summary": result.get("diff_summary", ""),
            "changed_lines": result.get("changed_lines", {"added": 0, "removed": 0}),
        }
    except Exception:
        return {
            "analysis": f"LLM 分析失败，基于规则: {fail_reason}",
            "new_system_prompt": current_system_prompt,
            "diff_summary": "建议手动检查",
            "changed_lines": {"added": 0, "removed": 0},
        }
