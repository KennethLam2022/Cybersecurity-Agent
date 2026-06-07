"""E2E 评测公共工具模块

共享：统计计算、HTML 页面模板、单题评分管道
"""
import json
import html as html_lib
from pathlib import Path
from datetime import datetime
from typing import Optional


def compute_avg_stats(results: list) -> dict:
    """计算所有维度的平均分（两套脚本通用）"""
    dims = {
        "faithfulness": [],
        "relevancy": [],
        "hallucination": [],
        "context_precision": [],
        "context_recall": [],
    }
    for r in results:
        for k in dims:
            v = r.get(k, r.get("scores", {}).get(k))
            if v is not None:
                dims[k].append(v)

    avg = {}
    for k, vals in dims.items():
        if vals:
            avg[k] = round(sum(vals) / len(vals), 4)
        else:
            avg[k] = 0.0
    avg["overall"] = round(sum(avg.values()) / len(avg), 4) if avg else 0.0
    return avg


def score_single_question(query: str, retrieved_docs: list, answer: str, eval_llm) -> dict:
    """单题评分管道：调用 5 个 LLM 评分函数"""
    from _eval_context import eval_context_precision, eval_context_recall
    from _eval_generation import eval_faithfulness, eval_relevancy, eval_hallucination

    ctx_precision = eval_context_precision(query, retrieved_docs, answer, llm=eval_llm)
    ctx_recall = eval_context_recall(query, retrieved_docs, answer, llm=eval_llm)
    faithfulness = eval_faithfulness(query, retrieved_docs, answer, llm=eval_llm)
    relevancy = eval_relevancy(query, answer, retrieved_docs, llm=eval_llm)
    hallucination = eval_hallucination(query, retrieved_docs, answer, llm=eval_llm)

    return {
        "context_precision": ctx_precision.get("score", 0),
        "context_recall": ctx_recall.get("score", 0),
        "faithfulness": faithfulness.get("score", 0),
        "relevancy": relevancy.get("score", 0),
        "hallucination": hallucination.get("score", 0),
        "details": {
            "context_precision": ctx_precision,
            "context_recall": ctx_recall,
            "faithfulness": faithfulness,
            "relevancy": relevancy,
            "hallucination": hallucination,
        },
    }


_DEFAULT_CSS = """
body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 40px; background: #f8f9fa; color: #333; }
h1 { color: #1a1a2e; border-bottom: 3px solid #4361ee; padding-bottom: 12px; }
h2 { color: #2d3436; margin-top: 32px; }
table { border-collapse: collapse; width: 100%; margin: 16px 0 32px; background: #fff; box-shadow: 0 2px 8px rgba(0,0,0,0.08); border-radius: 8px; overflow: hidden; }
th, td { padding: 10px 14px; text-align: left; border-bottom: 1px solid #eee; }
th { background: #4361ee; color: #fff; font-weight: 600; }
tr:hover { background: #f1f3ff; }
.score { font-weight: 700; }
.score-high { color: #00b894; }
.score-mid { color: #fdcb6e; }
.score-low { color: #e17055; }
.summary { background: #dfe6e9; padding: 20px; border-radius: 8px; margin: 16px 0; }
.footer { margin-top: 40px; font-size: 12px; color: #888; text-align: center; }
.meta { color: #636e72; font-size: 14px; }
"""


def render_html_page(title: str, body_html: str, output_file: Path, extra_css: str = "") -> str:
    """生成标准 HTML 报告页面"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{html_lib.escape(title)}</title>
<style>
{_DEFAULT_CSS}
{extra_css}
</style>
</head>
<body>
<h1>{html_lib.escape(title)}</h1>
<div class="meta">生成时间: {now}</div>
{body_html}
<div class="footer">网络安全智能 Agent · 评测系统 · 自动生成</div>
</body>
</html>"""
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html, encoding="utf-8")
    return html
