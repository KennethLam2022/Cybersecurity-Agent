"""Self-contained HTML report for Agent Evaluation release review."""
from __future__ import annotations

import html
import json
from typing import Any


def render_agent_eval_report(run: dict[str, Any], results: list[dict[str, Any]], comparison: dict[str, Any] | None = None) -> str:
    summary = run.get("summary") or {}
    gate = run.get("gate") or {}
    rows = []
    for item in results:
        metrics = item.get("metrics") or {}
        status = "通过" if metrics.get("task_success") else "失败"
        failed = [key for key, value in metrics.items() if value is False]
        rows.append(
            "<tr>"
            f"<td>{html.escape(str(item.get('case_key', '')))}</td>"
            f"<td>{html.escape(str(item.get('query', ''))[:120])}</td>"
            f"<td>{html.escape(status)}</td>"
            f"<td>{html.escape('、'.join(failed) or '-')}</td>"
            f"<td>{int(item.get('elapsed_ms') or 0)}ms</td>"
            "</tr>"
        )
    comparison_html = ""
    if comparison:
        comparison_html = f"<h2>Baseline 对比</h2><pre>{html.escape(json.dumps(comparison, ensure_ascii=False, indent=2))}</pre>"
    gate_text = "通过" if gate.get("passed") else "阻塞"
    return f"""<!doctype html><html lang='zh-CN'><meta charset='utf-8'><title>Agent Evaluation Report</title>
<style>body{{font-family:Arial,sans-serif;max-width:1100px;margin:32px auto;color:#222}}h1{{margin-bottom:4px}}.meta{{color:#666;font-size:13px}}.gate{{padding:12px;margin:18px 0;border:1px solid #ddd;color:{'#16803c' if gate.get('passed') else '#c62828'}}}table{{width:100%;border-collapse:collapse}}th,td{{padding:8px;border-bottom:1px solid #eee;text-align:left;font-size:13px}}pre{{background:#f6f6f6;padding:12px;white-space:pre-wrap}}</style>
<h1>Agent Evaluation 发布评测报告</h1><div class='meta'>运行 ID：{html.escape(str(run.get('run_id', '')))}</div>
<div class='gate'><strong>发布门禁：{gate_text}</strong><br>{html.escape('；'.join(gate.get('reasons') or gate.get('blockers') or []))}</div>
<h2>汇总</h2><pre>{html.escape(json.dumps(summary, ensure_ascii=False, indent=2))}</pre>
<h2>用例明细</h2><table><thead><tr><th>用例</th><th>问题</th><th>结果</th><th>失败检查</th><th>耗时</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
{comparison_html}</html>"""
