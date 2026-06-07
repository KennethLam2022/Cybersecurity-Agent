"""综合质量评测（Standalone 版本）

一键跑 30 题，自动计算 域B（Context Precision/Recall）+ 域C（Faithfulness/Relevancy/Hallucination），
输出 JSON + HTML 报告，支持版本对比。

用法:
    # 跑评估
    python eval_e2e.py

    # 仅从已有 JSON 生成 HTML（不重跑）
    python eval_e2e.py --no-run

    # 跑完显示版本对比
    python eval_e2e.py --compare

    # 跑完自动打开浏览器
    python eval_e2e.py --open

    # 指定测试题数（方便调试）
    python eval_e2e.py --limit 5
"""
from _eval_generation import eval_faithfulness, eval_relevancy, eval_hallucination
from _eval_context import eval_context_precision, eval_context_recall
import os
import sys
import json
import time
import webbrowser
import logging
import argparse
import re
from pathlib import Path
from datetime import datetime
from typing import Optional
import html as html_lib

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

_SRC = Path(__file__).parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

# ── 项目路径 ──
_PROJECT_ROOT = _SRC.parent.parent.parent
_EVAL_DIR = _PROJECT_ROOT / "eval_results"
_EVAL_DIR.mkdir(parents=True, exist_ok=True)
_VERSION_DIR = _EVAL_DIR / "versions"
_VERSION_DIR.mkdir(exist_ok=True)
_HTML_DIR = _EVAL_DIR / "html"
_HTML_DIR.mkdir(exist_ok=True)
_VERSION_FILE = _VERSION_DIR / "eval_versions.json"

# ── 评分函数 ──


# ============================================================
#  测试题（同 eval_30_v3.py 保持一致）
# ============================================================
QUESTIONS = [
    # ── 等保合规（5题：从原10题精选）──
    {"id": "H01", "domain": "等保合规", "difficulty": "中等",
     "query": "我是运维工程师，我们系统要做等保三级，安全审计方面有啥要求？", "style": "role"},
    {"id": "H02", "domain": "等保合规", "difficulty": "困难",
     "query": "我是安全主管，系统等保测评没过，常见的整改项有哪些？先改什么后改什么？", "style": "role"},
    {"id": "H03", "domain": "等保合规", "difficulty": "中等",
     "query": "我负责网络这一块，安全通信网络有啥技术要求？加密、隔离之类的。", "style": "role"},
    {"id": "H04", "domain": "等保合规", "difficulty": "困难",
     "query": "等保四级和三级到底差在哪？安全要求上有什么不一样？", "style": "plain"},
    {"id": "H05", "domain": "等保合规", "difficulty": "困难",
     "query": "我是咨询顾问，客户问我等保三级和CII的关系，他们系统既是三级又是CII，安全要求怎么叠加？", "style": "role"},

    # ── 数据安全（5题）──
    {"id": "H06", "domain": "数据安全", "difficulty": "中等",
     "query": "我是数据安全负责人，用户要求删除个人信息，我们怎么响应？流程是什么？", "style": "role"},
    {"id": "H07", "domain": "数据安全", "difficulty": "基础",
     "query": "数据分类分级应该怎么做？用户个人信息属于哪一级？", "style": "plain"},
    {"id": "H08", "domain": "数据安全", "difficulty": "困难",
     "query": "我是法务，跨境数据传输要怎么做才合规？法律法规有哪些具体要求？", "style": "role"},
    {"id": "H09", "domain": "数据安全", "difficulty": "中等",
     "query": "数据脱敏有哪些方案？客服查询场景下推荐哪种？", "style": "plain"},
    {"id": "H10", "domain": "数据安全", "difficulty": "中等",
     "query": "我是业务部门负责人，数据安全法要求的数据安全风险评估怎么做？我们部门要配合什么？", "style": "role"},

    # ── 安全运营（4题）──
    {"id": "H11", "domain": "安全运营", "difficulty": "中等",
     "query": "SOC安全运营中心建设需要多少人？三班倒怎么排？", "style": "plain"},
    {"id": "H12", "domain": "安全运营", "difficulty": "困难",
     "query": "我是安全总监，公司要做红蓝演练，怎么规划？一年几次合适？", "style": "role"},
    {"id": "H13", "domain": "安全运营", "difficulty": "困难",
     "query": "我是运维负责人，第三方供应商要远程接入我们的网络做维护，安全上怎么管控？", "style": "role"},
    {"id": "H14", "domain": "安全运营", "difficulty": "基础",
     "query": "安全意识培训怎么做才有效？全员培训和定向培训分别怎么安排？", "style": "plain"},

    # ── 管理体系（4题）──
    {"id": "H15", "domain": "管理体系", "difficulty": "中等",
     "query": "网络安全管理制度体系分几级？每级包含什么内容？", "style": "plain"},
    {"id": "H16", "domain": "管理体系", "difficulty": "中等",
     "query": "我是综合部新来的，公司安全组织架构怎么设？安全领导小组管什么？", "style": "role"},
    {"id": "H17", "domain": "管理体系", "difficulty": "困难",
     "query": "我是合规主管，公司要做ISO 27001认证，和等保的关系是什么？可以一起做吗？", "style": "role"},
    {"id": "H18", "domain": "管理体系", "difficulty": "中等",
     "query": "供应商安全管理有哪些要求？合作前、合作中、合作后分别要做什么？", "style": "plain"},

    # ── 基础设施/CII（4题）──
    {"id": "H19", "domain": "基础设施安全", "difficulty": "中等",
     "query": "关键信息基础设施怎么识别？哪些系统属于CII？", "style": "plain"},
    {"id": "H20", "domain": "基础设施安全", "difficulty": "困难",
     "query": "我是CII安全负责人，CII每年要做安全检测评估，具体怎么做？范围和频次？", "style": "role"},
    {"id": "H21", "domain": "基础设施安全", "difficulty": "中等",
     "query": "CII供应链安全有什么特殊要求？设备采购有什么额外管控？", "style": "plain"},
    {"id": "H22", "domain": "基础设施安全", "difficulty": "中等",
     "query": "我是安全管理员，重大安全事件的上报流程是什么？向谁报？多长时间内？", "style": "role"},

    # ── 应急响应（3题 新增）──
    {"id": "E01", "domain": "应急响应", "difficulty": "中等",
     "query": "应急响应预案应该包含哪些核心内容？事件分级怎么定？", "style": "plain"},
    {"id": "E02", "domain": "应急响应", "difficulty": "困难",
     "query": "我是安全值班员，发现服务器被勒索病毒攻击了，第一步应该做什么？完整的应急处置流程是怎样的？", "style": "role"},
    {"id": "E03", "domain": "应急响应", "difficulty": "基础",
     "query": "应急演练有哪些类型？桌面推演和实战演练分别适合什么场景？一年几次合适？", "style": "plain"},

    # ── 风险评估（3题 新增）──
    {"id": "R01", "domain": "风险评估", "difficulty": "中等",
     "query": "信息安全风险评估的流程是怎样的？有哪些常用的评估方法？", "style": "plain"},
    {"id": "R02", "domain": "风险评估", "difficulty": "困难",
     "query": "我是安全部新来的，公司要做信息安全风险评估，具体怎么开展？有哪些关键步骤和产出？", "style": "role"},
    {"id": "R03", "domain": "风险评估", "difficulty": "基础",
     "query": "风险评估和等保测评是什么关系？做了等保还要不要做风险评估？", "style": "plain"},

    # ── 灾难恢复（1题 新增）──
    {"id": "D01", "domain": "灾难恢复", "difficulty": "中等",
     "query": "灾难恢复计划（DRP）应该包含哪些核心要素？RTO和RPO是什么意思？怎么确定指标？", "style": "plain"},

    # ── 业务连续性（1题 新增）──
    {"id": "B01", "domain": "业务连续性", "difficulty": "中等",
     "query": "业务连续性管理（BCM）和灾难恢复（DR）有什么区别？怎么建立业务连续性管理体系？", "style": "plain"},
]


# ============================================================
#  核心：跑评估
# ============================================================

def _get_retrieved_docs(result: dict) -> list[dict]:
    """从 agent 返回结果中提取检索文档列表（用于评分函数）"""
    sources = result.get("sources", [])
    docs = []
    for s in sources:
        docs.append({
            "file_name": s.get("file_name", ""),
            "content": s.get("content", s.get("snippet", "")),
            "section": s.get("section", ""),
            "confidence": s.get("confidence", 0),
            "label": s.get("label", "中"),
        })
    return docs


def run_evaluation(
    agent,
    questions: list[dict],
    use_llm: bool = True,
    output_file: Optional[Path] = None,
    eval_llm=None,
    answer_llm=None,
) -> list[dict]:
    """跑评估：提问 → 评分（域B + 域C）

    Args:
        agent: CyberAgent 实例
        questions: 测试题列表
        use_llm: 是否使用 LLM 做评分（False 则用启发式回退）
        output_file: JSON 输出路径
        eval_llm: 独立的评测 LLM（如不提供则用 agent.llm）
        answer_llm: 回答用的 LLM（如提供则临时替换 agent.llm，避免直连 DeepSeek 超时）

    Returns:
        带评分的结果列表
    """
    if output_file is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_file = _EVAL_DIR / f"eval_e2e_results_{ts}.json"

    llm = eval_llm or (agent.llm if use_llm else None)
    total = len(questions)

    # 统计数据
    role_count = sum(1 for q in questions if q.get("style") == "role")
    plain_count = total - role_count
    domains = list(dict.fromkeys(q["domain"] for q in questions))

    logger.info(f"\n{'='*70}")
    logger.info(f"  综合质量评测")
    logger.info(f"  题数: {total}（角色{role_count} + 自然语言{plain_count}）")
    logger.info(f"  领域: {', '.join(domains)}")
    logger.info(f"  评分模式: {'LLM-as-Judge' if use_llm else '启发式回退'}")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"{'='*70}\n")

    results = []
    for i, q in enumerate(questions, 1):
        query = q["query"]
        logger.info(f"[{i}/{total}] [{q['domain'][:6]}] [{q['difficulty']}] {query[:50]}...")
        t0 = time.time()

        try:
            # 1. 调用 agent（如提供了 answer_llm，临时替换 agent.llm 避免直连超时）
            if answer_llm:
                original_llm = getattr(agent, 'llm', None)
                agent.llm = answer_llm
                try:
                    result = agent.ask(query)
                finally:
                    agent.llm = original_llm
            else:
                result = agent.ask(query)
            elapsed = time.time() - t0

            answer = result.get("answer", "")
            retrieved_docs = _get_retrieved_docs(result)

            # 2. 域B + 域C 评分
            score_cp = eval_context_precision(query, retrieved_docs, answer, llm=llm)
            score_cr = eval_context_recall(query, retrieved_docs, answer, llm=llm)
            score_ft = eval_faithfulness(query, retrieved_docs, answer, llm=llm)
            score_rl = eval_relevancy(query, answer, retrieved_docs, llm=llm)
            score_hc = eval_hallucination(query, retrieved_docs, answer, llm=llm)

            entry = {
                "id": q["id"],
                "domain": q["domain"],
                "difficulty": q["difficulty"],
                "query": query,
                "style": q.get("style", "plain"),
                "answer": answer,
                "sources": result.get("sources", []),
                "stats": result.get("stats", {}),
                "timestamp": datetime.now().isoformat(),
                "elapsed": round(elapsed, 2),
                "scores": {
                    "context_precision": score_cp,
                    "context_recall": score_cr,
                    "faithfulness": score_ft,
                    "relevancy": score_rl,
                    "hallucination": score_hc,
                },
                "auto_status": f"有来源({len(retrieved_docs)}条)" if retrieved_docs else "无来源",
                "truncation": result.get("stats", {}).get("truncation", {}),
            }

            status_ok = len(retrieved_docs) > 0
            trunc = entry["truncation"]
            trunc_str = f"截断 {trunc.get('truncated_count', 0)}条" if trunc.get(
                "truncated_count", 0) > 0 else "未截断"
            avg_score = (score_cp["score"] + score_cr["score"] + score_ft["score"]
                         + score_rl["score"] + score_hc["score"]) / 5
            logger.info(f"  {'[OK]' if status_ok else '[WARN]'} {elapsed:.1f}s | "
                        f"来源: {len(retrieved_docs)}条 | {trunc_str} | "
                        f"平均分: {avg_score:.3f}")

        except Exception as e:
            elapsed = time.time() - t0
            entry = {
                "id": q["id"],
                "domain": q["domain"],
                "difficulty": q["difficulty"],
                "query": query,
                "style": q.get("style", "plain"),
                "answer": "",
                "sources": [],
                "stats": {},
                "timestamp": datetime.now().isoformat(),
                "elapsed": round(elapsed, 2),
                "scores": {},
                "auto_status": "运行错误",
                "error": str(e),
            }
            logger.info(f"  [ERR] {elapsed:.1f}s | {str(e)[:80]}")

        results.append(entry)

        # 每跑完一题即时保存
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    # ── 汇总 ──
    logger.info(f"\n{'='*70}")
    logger.info(f"  评估完成")
    logger.info(f"{'='*70}")
    has_src = sum(1 for r in results if r.get("sources"))
    errors = sum(1 for r in results if r.get("auto_status") == "运行错误")
    scorable = [r for r in results if r.get("scores")]

    if scorable:
        avg_scores = {}
        for key in ["context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"]:
            vals = [r["scores"][key]["score"] for r in scorable if key in r.get("scores", {})]
            avg_scores[key] = round(sum(vals) / len(vals), 4) if vals else 0
        logger.info(f"  平均评分:")
        logger.info(f"    Context Precision: {avg_scores.get('context_precision', 0):.4f}")
        logger.info(f"    Context Recall:    {avg_scores.get('context_recall', 0):.4f}")
        logger.info(f"    Faithfulness:      {avg_scores.get('faithfulness', 0):.4f}")
        logger.info(f"    Relevancy:         {avg_scores.get('relevancy', 0):.4f}")
        logger.info(f"    Hallucination:     {avg_scores.get('hallucination', 0):.4f}")

        # C5 截断影响统计
        trunc_records = [r for r in scorable if r.get(
            "truncation", {}).get("truncated_count", 0) > 0]
        c5_rate = len(trunc_records) / len(scorable) * 100 if scorable else 0
        if trunc_records:
            orig_sum = sum(r["truncation"]["original_count"] for r in trunc_records)
            kept_sum = sum(r["truncation"]["kept_count"] for r in trunc_records)
            avg_trunc_rate = (orig_sum - kept_sum) / orig_sum * 100 if orig_sum else 0
            logger.info(f"    C5 截断影响率:   {c5_rate:.0f}% 的题目被截断")
            logger.info(
                f"    平均截断量: 原{orig_sum//len(trunc_records)}条 → 保留{kept_sum//len(trunc_records)}条")
        else:
            logger.info(f"    C5 截断影响率:   0%（未被截断）")

    logger.info(f"\n  总题数: {len(results)}")
    logger.info(f"  有来源: {has_src}/{len(results)}")
    logger.info(f"  错误:   {errors}")
    logger.info(f"  已保存: {output_file}")
    return results


# ============================================================
#  HTML 报告生成
# ============================================================

def generate_html(results: list[dict], output_file: Path):
    """生成综合质量评测 HTML 报告（含域B + 域C 评分）"""
    total = len(results)
    has_src = sum(1 for r in results if r.get("sources"))
    errors = sum(1 for r in results if r.get("auto_status") == "运行错误")
    scorable = [r for r in results if r.get("scores")]

    # 计算平均分
    avg_scores = {}
    if scorable:
        for key in ["context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"]:
            vals = [r["scores"][key]["score"] for r in scorable if key in r.get("scores", {})]
            avg_scores[key] = round(sum(vals) / len(vals), 4) if vals else 0

    # C5 截断影响率
    trunc_records = [r for r in scorable if r.get("truncation", {}).get("truncated_count", 0) > 0]
    c5_rate = len(trunc_records) / len(scorable) * 100 if scorable else 0

    def _score_color(v):
        if v >= 0.8:
            return "#34c759"
        if v >= 0.5:
            return "#ff9500"
        return "#ff3b30"

    def _bar(v):
        pct = int(v * 100)
        color = _score_color(v)
        return f'<div style="display:flex;align-items:center;gap:6px"><div style="flex:1;height:6px;background:#e5e5ea;border-radius:3px;overflow:hidden"><div style="width:{pct}%;height:100%;background:{color};border-radius:3px"></div></div><span style="font-size:12px;font-weight:600;color:{color};min-width:36px">{pct}%</span></div>'

    # ── 汇总卡片 ──
    score_cards = ""
    score_labels = {
        "context_precision": "Context\nPrecision",
        "context_recall": "Context\nRecall",
        "faithfulness": "Faithfulness\n忠实度",
        "relevancy": "Relevancy\n相关性",
        "hallucination": "Hallucination\n幻觉检测",
    }
    for key, label in score_labels.items():
        v = avg_scores.get(key, 0)
        score_cards += f"""
    <div class="sc" style="text-align:center;padding:16px">
      <div style="font-size:28px;font-weight:700;color:{_score_color(v)}">{v:.3f}</div>
      <div style="font-size:11px;color:#6e6e73;white-space:pre-line;margin-top:4px">{label}</div>
      <div style="margin-top:8px">{_bar(v)}</div>
    </div>"""

    # ── 每行明细 ──
    rows_html = ""
    for i, r in enumerate(results, 1):
        qid = html_lib.escape(str(r.get("id", "")))
        domain = html_lib.escape(str(r.get("domain", "")))
        diff = html_lib.escape(str(r.get("difficulty", "")))
        query = html_lib.escape(str(r.get("query", "")))
        answer = html_lib.escape((r.get("answer") or "")[:200])
        status = html_lib.escape(str(r.get("auto_status", "")))

        scores = r.get("scores", {})
        cells = ""
        for key in ["context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"]:
            if key in scores and scores[key]:
                v = scores[key]["score"]
                cells += f'<td style="text-align:center;color:{_score_color(v)};font-weight:600">{v:.3f}</td>'
            else:
                cells += '<td style="text-align:center;color:#6e6e73">-</td>'

        diff_class = {"困难": "h", "中等": "m", "基础": "e"}.get(diff, "e")

        trunc = r.get("truncation", {})
        trunc_count = trunc.get("truncated_count", 0)
        trunc_str = f"截断{trunc_count}条" if trunc_count > 0 else "无截断"
        trunc_color = "var(--y)" if trunc_count > 0 else "var(--g)"

        rows_html += f"""
    <tr>
      <td style="font-size:12px;font-weight:600;color:#0071e3">{qid}</td>
      <td style="font-size:11px;color:#6e6e73">{domain[:8]}</td>
      <td><span class="diff-badge {diff_class}">{diff}</span></td>
      <td style="font-size:13px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{query}">{query}</td>
      <td style="font-size:12px;color:#6e6e73;max-width:150px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="{answer}">{answer}</td>
      {cells}
      <td style="font-size:11px;color:{trunc_color};text-align:center;font-weight:600">{trunc_str}</td>
      <td style="font-size:11px;color:#6e6e73;text-align:center">{r.get('elapsed', 0):.1f}s</td>
      <td style="font-size:11px;text-align:center">{status}</td>
    </tr>"""

    # ── 版本对比 ──
    compare_html = _generate_compare_html(avg_scores, results)

    html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>综合质量评测报告</title>
<style>
:root{{--bg:#f5f5f7;--c:#fff;--t:#1d1d1f;--t2:#6e6e73;--b:#e5e5ea;--a:#0071e3;--g:#34c759;--y:#ff9500;--r:#ff3b30}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:-apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC","Noto Sans SC",sans-serif;background:var(--bg);color:var(--t);padding:24px;line-height:1.6}}
.container{{max-width:1200px;margin:0 auto}}
h1{{font-size:24px;font-weight:700;margin-bottom:4px}}
.meta{{font-size:13px;color:var(--t2);margin-bottom:24px}}
.kpi-row{{display:flex;gap:12px;margin-bottom:24px;flex-wrap:wrap}}
.kpi{{background:var(--c);border-radius:12px;padding:12px 20px;border:1px solid var(--b);flex:1;min-width:100px;text-align:center}}
.kpi .n{{font-size:26px;font-weight:700;display:block}}
.kpi .l{{font-size:11px;color:var(--t2)}}
.score-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px;margin-bottom:24px}}
.sc{{background:var(--c);border-radius:12px;padding:16px;border:1px solid var(--b)}}
.sc:hover{{box-shadow:0 2px 12px rgba(0,0,0,.08)}}
table{{width:100%;border-collapse:collapse;background:var(--c);border-radius:12px;overflow:hidden;border:1px solid var(--b);font-size:13px}}
th{{background:#fafafa;padding:10px 12px;text-align:left;font-weight:600;font-size:11px;color:var(--t2);white-space:nowrap;border-bottom:1px solid var(--b)}}
td{{padding:9px 12px;border-bottom:1px solid var(--b);vertical-align:middle}}
tr:last-child td{{border-bottom:none}}
tr:hover{{background:#fafafa}}
.diff-badge{{display:inline-block;font-size:11px;padding:2px 8px;border-radius:4px;font-weight:500}}
.diff-badge.e{{background:#e8f5e9;color:#2e7d32}}
.diff-badge.m{{background:#fff3e0;color:#e65100}}
.diff-badge.h{{background:#fce4ec;color:#c62828}}
.compare-section{{background:var(--c);border-radius:12px;padding:20px;border:1px solid var(--b);margin-bottom:24px}}
.compare-section h3{{font-size:15px;font-weight:600;margin-bottom:12px}}
.compare-grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
.compare-item{{padding:12px;border-radius:8px;border:1px solid var(--b)}}
.compare-item .label{{font-size:11px;color:var(--t2);margin-bottom:4px}}
.compare-item .row{{display:flex;justify-content:space-between;font-size:13px}}
.compare-item .curr{{font-weight:600}}
.compare-item .prev{{color:var(--t2)}}
.compare-item .diff-up{{color:var(--g)}}
.compare-item .diff-down{{color:var(--r)}}
.legend{{font-size:12px;color:var(--t2);margin:8px 0}}
@media(max-width:640px){{.score-grid{{grid-template-columns:1fr 1fr}}}}
</style>
</head>
<body>
<div class="container">
  <h1>综合质量评测</h1>
  <div class="meta">
    {datetime.now().strftime('%Y-%m-%d %H:%M')} |
    跑分模式: 含启发式回退 |
    <span style="color:var(--g)">域B: Context Precision/Recall</span> |
    <span style="color:var(--a)">域C: Faithfulness/Relevancy/Hallucination</span>
  </div>

  <!-- KPI -->
  <div class="kpi-row">
    <div class="kpi"><span class="n">{total}</span><span class="l">总题数</span></div>
    <div class="kpi"><span class="n" style="color:var(--g)">{has_src}</span><span class="l">有来源</span></div>
    <div class="kpi"><span class="n" style="color:var(--r)">{errors}</span><span class="l">错误</span></div>
    <div class="kpi"><span class="n" style="color:var(--a)">{len(scorable)}</span><span class="l">已评分</span></div>
    <div class="kpi"><span class="n" style="color:var(--y)">{c5_rate:.0f}%</span><span class="l">截断影响(C5)</span></div>
  </div>

  <!-- 域B + 域C 评分卡片 -->
  <div class="score-grid">
    {score_cards}
  </div>

  <!-- 版本对比 -->
  {compare_html}

  <!-- 明细表 -->
  <div class="legend">💡 评分范围 0.0–1.0，绿色≥0.8 橙色≥0.5 红色<0.5 | 域B：Context Precision/Recall | 域C：Faithfulness/Relevancy/Hallucination</div>
  <table>
    <thead>
      <tr>
        <th>#</th><th>领域</th><th>难度</th><th>问题</th><th>回答片段</th>
        <th title="Context Precision">B-CP</th>
        <th title="Context Recall">B-CR</th>
        <th title="Faithfulness">C-Faith</th>
        <th title="Relevancy">C-Rel</th>
        <th title="Hallucination">C-Halu</th>
        <th title="C5 截断">截断</th>
        <th>耗时</th><th>状态</th>
      </tr>
    </thead>
    <tbody>
      {rows_html}
    </tbody>
  </table>
  <div style="text-align:center;margin-top:24px;font-size:12px;color:var(--t2)">
    综合质量评测报告 · 自动生成
  </div>
</div>
</body>
</html>"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"  [OK] HTML: {output_file}")


def _generate_compare_html(current_avg: dict, current_results: list = None) -> str:
    """生成版本对比 HTML 块"""
    versions = _load_versions()
    if len(versions) < 2:
        return '<div class="compare-section"><h3>版本对比</h3><p style="color:var(--t2);font-size:13px">暂无历史记录，跑完两轮后自动对比</p></div>'

    prev = versions[-2]
    prev_path = _EVAL_DIR / prev.get("results_file", "")
    if not prev_path.exists():
        return '<div class="compare-section"><h3>版本对比</h3><p style="color:var(--t2);font-size:13px">历史结果文件不存在</p></div>'

    try:
        prev_results = json.loads(prev_path.read_text(encoding="utf-8"))
        prev_scorable = [r for r in prev_results if r.get("scores")]
        prev_avg = {}
        for key in ["context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"]:
            vals = [r["scores"][key]["score"] for r in prev_scorable if key in r.get("scores", {})]
            prev_avg[key] = round(sum(vals) / len(vals), 4) if vals else 0
        # C5
        prev_trunc = [r for r in prev_scorable if r.get(
            "truncation", {}).get("truncated_count", 0) > 0]
        prev_c5_rate = len(prev_trunc) / len(prev_scorable) * 100 if prev_scorable else 0
    except Exception:
        return '<div class="compare-section"><h3>版本对比</h3><p style="color:var(--t2);font-size:13px">历史数据读取失败</p></div>'

    # 当前 C5
    curr_scorable = [r for r in (current_results or []) if r.get("scores")]
    curr_trunc = [r for r in curr_scorable if r.get("truncation", {}).get("truncated_count", 0) > 0]
    curr_c5_rate = len(curr_trunc) / len(curr_scorable) * 100 if curr_scorable else 0

    labels = {
        "context_precision": "Context Precision",
        "context_recall": "Context Recall",
        "faithfulness": "Faithfulness",
        "relevancy": "Relevancy",
        "hallucination": "Hallucination",
    }

    items_html = ""
    for key, label in labels.items():
        curr = current_avg.get(key, 0)
        prev_val = prev_avg.get(key, 0)
        diff = curr - prev_val
        diff_str = f"+{diff:.3f}" if diff > 0 else f"{diff:.3f}"
        diff_class = "diff-up" if diff > 0 else ("diff-down" if diff < 0 else "")
        items_html += f"""
    <div class="compare-item">
      <div class="label">{label}</div>
      <div class="row">
        <span class="curr">{curr:.3f}</span>
        <span class="prev">上次 {prev_val:.3f}</span>
        <span class="{diff_class}">{diff_str}</span>
      </div>
    </div>"""

    # C5 对比
    c5_diff = curr_c5_rate - prev_c5_rate
    c5_class = "diff-up" if c5_diff < 0 else ("diff-down" if c5_diff > 0 else "")
    items_html += f"""
    <div class="compare-item">
      <div class="label">C5 截断影响率</div>
      <div class="row">
        <span class="curr">{curr_c5_rate:.0f}%</span>
        <span class="prev">上次 {prev_c5_rate:.0f}%</span>
        <span class="{c5_class}">{c5_diff:+.0f}pp</span>
      </div>
    </div>"""

    prev_ver = prev.get("version", "未知")
    return f"""
<div class="compare-section">
  <h3>版本对比 vs {prev_ver}</h3>
  <div class="compare-grid">
    {items_html}
  </div>
</div>"""


# ============================================================
#  版本管理
# ============================================================

def _load_versions() -> list:
    if _VERSION_FILE.exists():
        try:
            return json.loads(_VERSION_FILE.read_text(encoding="utf-8"))
        except Exception:
            return []
    return []


def _save_version(results_json: Path, html_path: Path, version_tag: str, stats: dict):
    versions = _load_versions()
    versions.append({
        "version": version_tag,
        "timestamp": datetime.now().isoformat(),
        "results_file": str(results_json.relative_to(_EVAL_DIR) if results_json.parent == _EVAL_DIR else results_json.name),
        "html_file": str(html_path.relative_to(_HTML_DIR) if html_path.parent == _HTML_DIR else html_path.name),
        "total": stats.get("total"),
        "errors": stats.get("errors"),
        "avg_scores": stats.get("avg_scores", {}),
    })
    _VERSION_FILE.write_text(
        json.dumps(versions, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _compute_stats(results: list) -> dict:
    scorable = [r for r in results if r.get("scores")]
    avg_scores = {}
    for key in ["context_precision", "context_recall", "faithfulness", "relevancy", "hallucination"]:
        vals = [r["scores"][key]["score"] for r in scorable if key in r.get("scores", {})]
        avg_scores[key] = round(sum(vals) / len(vals), 4) if vals else 0
    # C5 截断影响
    trunc_records = [r for r in scorable if r.get("truncation", {}).get("truncated_count", 0) > 0]
    c5_rate = len(trunc_records) / len(scorable) * 100 if scorable else 0
    return {
        "total": len(results),
        "errors": sum(1 for r in results if r.get("auto_status") == "运行错误"),
        "avg_scores": avg_scores,
        "c5_truncation_rate": round(c5_rate, 1),
        "c5_truncated_count": len(trunc_records),
    }


def show_comparison():
    """终端打印版本对比"""
    versions = _load_versions()
    if len(versions) < 2:
        logger.info("  [i] 只有一次记录，无法对比")
        return

    curr = versions[-1]
    prev = versions[-2]

    logger.info(f"\n  {'='*55}")
    logger.info(f"  版本对比: {prev['version']} → {curr['version']}")
    logger.info(f"  {'='*55}")

    curr_avg = curr.get("avg_scores", {})
    prev_avg = prev.get("avg_scores", {})

    labels = {
        "context_precision": "Context Precision",
        "context_recall": "Context Recall",
        "faithfulness": "Faithfulness",
        "relevancy": "Relevancy",
        "hallucination": "Hallucination",
    }

    logger.info(f"  {'指标':<22} {'当前':>8} {'上次':>8} {'变化':>8}")
    logger.info(f"  {'-'*50}")
    for key, label in labels.items():
        curr_v = curr_avg.get(key, 0)
        prev_v = prev_avg.get(key, 0)
        diff = curr_v - prev_v
        diff_str = f"+{diff:.3f}" if diff > 0 else str(diff)
        logger.info(f"  {label:<22} {curr_v:>8.3f} {prev_v:>8.3f} {diff_str:>8}")

    logger.info(f"\n  总题数: {curr.get('total', '?')} → {prev.get('total', '?')}")
    logger.info(f"  错误数: {curr.get('errors', '?')} → {prev.get('errors', '?')}")


# ============================================================
#  入口
# ============================================================

def find_latest_results() -> tuple[Optional[Path], Optional[Path]]:
    """查找最新的结果 JSON 和 HTML"""
    json_files = sorted(_EVAL_DIR.glob("eval_e2e_results_*.json"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    html_files = sorted(_HTML_DIR.glob("eval_e2e_report_*.html"),
                        key=lambda p: p.stat().st_mtime, reverse=True)
    latest_json = json_files[0] if json_files else None
    latest_html = html_files[0] if html_files else None
    return latest_json, latest_html


def load_questions_from_db(db_path: Optional[str] = None) -> list[dict]:
    """从 SQLite e2e_eval_items 表加载测试题"""
    if db_path is None:
        db_path = str(_PROJECT_ROOT / "agent_data" / "conversations.db")
    if not db_path or not os.path.exists(db_path):
        logger.info("  [WARN] DB 不存在，回退硬编码测试集")
        return QUESTIONS
    import sqlite3


def _db(db_path: str):
    c = sqlite3.connect(db_path)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    return c


    try:
        with _db(db_path) as conn:
            rows = conn.execute(
                "SELECT query, domain, difficulty, style FROM e2e_eval_items WHERE is_active=1 ORDER BY id ASC"
            ).fetchall()
        if not rows:
            logger.info("  [WARN] DB 中无测试题，回退硬编码测试集")
            return QUESTIONS
        questions = []
        for i, (query, domain, difficulty, style) in enumerate(rows, 1):
            prefix = domain[:2] if len(domain) >= 2 else "ZZ"
            questions.append({
                "id": f"E{prefix.upper()}{i:02d}",
                "domain": domain or "通用",
                "difficulty": difficulty or "中等",
                "query": query,
                "style": style or "plain",
            })
        return questions
    except Exception as e:
        logger.info(f"  [WARN] 读取 DB 失败: {e}，回退硬编码测试集")
        return QUESTIONS


def main():
    parser = argparse.ArgumentParser(description="综合质量评测")
    parser.add_argument("--no-run", action="store_true", help="不跑评估，仅从已有 JSON 生成 HTML")
    parser.add_argument("--compare", action="store_true", help="跑完对比上次结果")
    parser.add_argument("--open", action="store_true", help="完成后自动打开浏览器")
    parser.add_argument("--limit", type=int, default=0, help="限定跑 N 题（调试用）")
    parser.add_argument("--heuristic", action="store_true", help="强制使用启发式回退评分（不用 LLM）")
    parser.add_argument("--from-db", action="store_true", help="从 SQLite 数据库读取测试题")
    args = parser.parse_args()

    version_tag = datetime.now().strftime("%Y%m%d_%H%M%S")

    questions = QUESTIONS
    if args.from_db:
        questions = load_questions_from_db()
        logger.info(f"  [i] 从数据库加载 {len(questions)} 题")
    elif args.limit > 0:
        questions = questions[:args.limit]
        logger.info(f"  [i] 限定 {args.limit} 题（调试模式）")

    results = []
    if not args.no_run:
        # 初始化 agent
        logger.info(f"\n  [*] 初始化 CyberAgent ...")
        from agent import CyberAgent
        agent = CyberAgent()
        logger.info(
            f"  [OK] Agent 就绪 (LLM: {agent.llm.model if hasattr(agent.llm, 'model') else '?'})\n")

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_json = _EVAL_DIR / f"eval_e2e_results_{ts}.json"

        results = run_evaluation(
            agent=agent,
            questions=questions,
            use_llm=not args.heuristic,
            output_file=output_json,
        )
    else:
        # 从已有 JSON 加载
        latest_json, _ = find_latest_results()
        if not latest_json:
            logger.info("  [ERR] 未找到历史结果 JSON，请先不带 --no-run 跑一次")
            sys.exit(1)
        results = json.loads(latest_json.read_text(encoding="utf-8"))
        logger.info(f"  [i] 从 {latest_json.name} 加载 {len(results)} 条结果")

    # 生成 HTML
    if results:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_html = _HTML_DIR / f"eval_e2e_report_{ts}.html"
        generate_html(results, output_html)

        # 保存版本记录
        stats = _compute_stats(results)
        _save_version(
            results_json=output_json if not args.no_run else (
                latest_json or _EVAL_DIR / "unknown.json"),
            html_path=output_html,
            version_tag=version_tag,
            stats=stats,
        )

        # 版本对比
        if args.compare:
            show_comparison()

        # 自动打开
        if args.open:
            webbrowser.open(f"file:///{output_html.resolve().as_posix()}")
            logger.info(f"  [WWW] 已打开浏览器")

        logger.info(f"\n  [OK] 完成。查看报告: {output_html}")
    else:
        logger.info("  [ERR] 无结果，无法生成报告")


if __name__ == "__main__":
    main()
