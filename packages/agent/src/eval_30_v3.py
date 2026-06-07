"""30题评估v3 — 人话版问题（不给文档编号，15题带角色 + 15题不带角色）

用法：
  python eval_30_v3.py

问题设计：
  - 带角色（👤）："我是运维工程师/安全管理员/法务..."
  - 不带角色（💬）：纯自然语言提问
  每个领域两种类型各占一半。
"""
import os
import sys
import json
import time
import csv
import http.server
import socketserver
import webbrowser
import threading
import argparse
import html as html_lib
import logging
from pathlib import Path
from datetime import datetime

_SRC = os.path.dirname(os.path.abspath(__file__))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

logger = logging.getLogger(__name__)


EVAL_DIR = Path(_SRC).parent.parent.parent / "eval_results"
EVAL_DIR.mkdir(parents=True, exist_ok=True)

VERSION = "v3_human_tone"

# 标记：👤 = 带用户角色，💬 = 纯自然语言
QUESTIONS = [
    # ── 02-等保国标（5题）──
    {"id": "H01", "domain": "02-等保国标", "difficulty": "中等",
     "query": "我是运维工程师，我们系统要做等保三级，安全审计方面有啥要求？", "style": "role"},
    {"id": "H02", "domain": "02-等保国标", "difficulty": "困难",
     "query": "我是安全主管，系统等保测评没过，常见的整改项有哪些？先改什么后改什么？", "style": "role"},
    {"id": "H03", "domain": "02-等保国标", "difficulty": "困难",
     "query": "等保四级和三级到底差在哪？安全要求上有什么不一样？", "style": "plain"},
    {"id": "H04", "domain": "02-等保国标", "difficulty": "中等",
     "query": "安全区域边界的访问控制，等保三级有什么具体要求？", "style": "plain"},
    {"id": "H05", "domain": "02-等保国标", "difficulty": "中等",
     "query": "我是安全运维，等保三级对安全管理中心有啥要求？日志集中那些。", "style": "role"},

    # ── 01-国家法律（5题）──
    {"id": "H06", "domain": "01-国家法律", "difficulty": "基础",
     "query": "处理用户个人信息要满足什么条件才算合法？", "style": "plain"},
    {"id": "H07", "domain": "01-国家法律", "difficulty": "困难",
     "query": "我是法务，公司有海外业务，数据能出海吗？出海要满足什么条件？", "style": "role"},
    {"id": "H08", "domain": "01-国家法律", "difficulty": "中等",
     "query": "我是产品经理，收集用户信息时什么是最小必要原则？怎么把握尺度？", "style": "role"},
    {"id": "H09", "domain": "01-国家法律", "difficulty": "中等",
     "query": "哪些信息算敏感个人信息？处理规则有什么特殊要求？", "style": "plain"},
    {"id": "H10", "domain": "01-国家法律", "difficulty": "困难",
     "query": "我是合规经理，网络安全法和等级保护制度是什么关系？我们该怎么合规？", "style": "role"},

    # ── 03-CII关基（4题）──
    {"id": "H11", "domain": "03-CII关基", "difficulty": "困难",
     "query": "等保和CII到底啥关系？一个系统既是等保三级又是CII，怎么管？", "style": "plain"},
    {"id": "H12", "domain": "03-CII关基", "difficulty": "中等",
     "query": "CII每年都要做安全检测评估吗？都查什么？报告怎么写？", "style": "plain"},
    {"id": "H13", "domain": "03-CII关基", "difficulty": "基础",
     "query": "什么样的系统会被认定为CII？认定流程是怎样的？", "style": "role"},
    {"id": "H14", "domain": "03-CII关基", "difficulty": "中等",
     "query": "我是安全值班的，CII出安全事件了，上报流程是怎样的？多长时间内要报？", "style": "role"},

    # ── 04-通信行业（3题）──
    {"id": "H15", "domain": "04-通信行业", "difficulty": "基础",
     "query": "电信网和互联网安全防护的定级备案流程是怎样的？", "style": "plain"},
    {"id": "H16", "domain": "04-通信行业", "difficulty": "中等",
     "query": "工信部网络信息安全考核都考什么？评分标准有哪些？", "style": "role"},
    {"id": "H17", "domain": "04-通信行业", "difficulty": "中等",
     "query": "用户个人信息保护有什么技术要求？该怎么落地？", "style": "plain"},

    # ── 05-跨领域综合（3题）──
    {"id": "H18", "domain": "05-跨领域综合", "difficulty": "困难",
     "query": "我是安全总监，既要满足等保三级又要满足CII要求，管理制度怎么整合？", "style": "role"},
    {"id": "H19", "domain": "05-跨领域综合", "difficulty": "困难",
     "query": "用户的手机号、位置信息、通话记录属于什么级别的数据？需要什么保护措施？", "style": "plain"},
    {"id": "H20", "domain": "05-跨领域综合", "difficulty": "困难",
     "query": "采购核心网设备时供应链安全要关注哪些风险点？", "style": "role"},

    # ── 07-应急响应（3题 新增）──
    {"id": "E01", "domain": "07-应急响应", "difficulty": "中等",
     "query": "应急响应预案应该包含哪些核心内容？事件分级怎么定？", "style": "plain"},
    {"id": "E02", "domain": "07-应急响应", "difficulty": "困难",
     "query": "我是安全值班员，发现服务器被勒索病毒攻击了，第一步应该做什么？完整的应急处置流程是怎样的？", "style": "role"},
    {"id": "E03", "domain": "07-应急响应", "difficulty": "基础",
     "query": "应急演练有哪些类型？桌面推演和实战演练分别适合什么场景？", "style": "plain"},

    # ── 08-风险评估（3题 新增）──
    {"id": "R01", "domain": "08-风险评估", "difficulty": "中等",
     "query": "信息安全风险评估的流程是怎样的？有哪些常用的评估方法？", "style": "plain"},
    {"id": "R02", "domain": "08-风险评估", "difficulty": "困难",
     "query": "我是安全工程师，公司要做信息安全风险评估，具体怎么开展？有哪些关键步骤和产出？", "style": "role"},
    {"id": "R03", "domain": "08-风险评估", "difficulty": "基础",
     "query": "风险评估和等保测评是什么关系？做了等保还要不要做风险评估？", "style": "plain"},

    # ── 09-灾难恢复（2题 新增）──
    {"id": "D01", "domain": "09-灾难恢复", "difficulty": "中等",
     "query": "灾难恢复计划（DRP）应该包含哪些核心要素？RTO和RPO是什么意思？怎么确定指标？", "style": "plain"},
    {"id": "D02", "domain": "09-灾难恢复", "difficulty": "中等",
     "query": "我是运维主管，公司要制定灾备方案，同城灾备和异地灾备有什么区别？怎么选？", "style": "role"},

    # ── 10-业务连续性（2题 新增）──
    {"id": "B01", "domain": "10-业务连续性", "difficulty": "中等",
     "query": "业务连续性管理（BCM）和灾难恢复（DR）有什么区别？怎么建立业务连续性管理体系？", "style": "plain"},
    {"id": "B02", "domain": "10-业务连续性", "difficulty": "困难",
     "query": "我是安全经理，公司要做业务连续性管理体系建设，从哪里开始？关键步骤是什么？", "style": "role"},
]


def run_evaluation(agent, output_file):
    results = []
    total = len(QUESTIONS)

    role_count = sum(1 for q in QUESTIONS if q.get("style") == "role")
    plain_count = total - role_count

    logger.info(f"\n{'='*70}")
    logger.info(f"  30题评估v3 — 人话版（不带文档编号）")
    logger.info(f"  带角色: {role_count}题 | 纯自然语言: {plain_count}题")
    logger.info(f"  {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    logger.info(f"{'='*70}\n")

    for i, q in enumerate(QUESTIONS, 1):
        logger.info(f"[{i}/{total}] [{q['domain'][:6]}] [{q['difficulty']}] {q['query']}")
        t0 = time.time()
        try:
            result = agent.ask(q["query"])
            elapsed = time.time() - t0

            entry = {
                "id": q["id"],
                "domain": q["domain"],
                "difficulty": q["difficulty"],
                "query": q["query"],
                "rewritten_query": result.get("rewritten_query"),
                "answer": result["answer"],
                "sources": result.get("sources", []),
                "stats": result.get("stats", {}),
                "timestamp": datetime.now().isoformat(),
                "score": None,
                "note": "",
                "auto_status": f"有来源({len(result.get('sources', []))}条)",
            }

            status_ok = entry['auto_status'] != '运行错误'
            logger.info(
                f"  {'[OK]' if status_ok else '[ERR]'} {elapsed:.1f}s | 来源: {len(result.get('sources', []))}条")

            # 打印top-3来源
            for j, s in enumerate(result.get('sources', [])[:3], 1):
                logger.info(f"    [{j}] {s['file_name'][:35]} | {s['section'][:30]}")
            if len(result.get('sources', [])) > 3:
                logger.info(f"    ... 还有 {len(result.get('sources', [])) - 3} 条")

        except Exception as e:
            elapsed = time.time() - t0
            entry = {
                "id": q["id"],
                "domain": q["domain"],
                "difficulty": q["difficulty"],
                "query": q["query"],
                "rewritten_query": None,
                "answer": "",
                "sources": [],
                "stats": {},
                "timestamp": datetime.now().isoformat(),
                "score": None,
                "note": str(e),
                "auto_status": "运行错误",
            }
            logger.info(f"  [ERR] {elapsed:.1f}s | 错误: {str(e)[:60]}")

        results.append(entry)

        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

    # 摘要
    logger.info(f"\n{'='*70}")
    logger.info(f"  30题评估v3 摘要")
    logger.info(f"{'='*70}")
    has_src = sum(1 for r in results if r["sources"])
    errors = sum(1 for r in results if r["auto_status"] == "运行错误")
    total_src = sum(len(r["sources"]) for r in results)
    logger.info(f"  总题数: {len(results)}")
    logger.info(f"  有来源: {has_src}/{len(results)}")
    logger.info(f"  总来源数: {total_src}")
    logger.info(f"  错误:   {errors}")

    from collections import Counter
    domain_stats = Counter(r["domain"] for r in results)
    domain_ok = Counter(r["domain"] for r in results if r["sources"])
    logger.info(f"\n  按领域：")
    for d in sorted(domain_stats):
        logger.info(f"    {d}: {domain_stats[d]}题 | 有来源 {domain_ok[d]}")

    logger.info(f"\n  ✅ 已保存: {output_file}")
    return results


def generate_html(results, output_file):
    """生成评分页 HTML（含检索质量仪表盘）

    每道题卡新增检索质量行：
      📊 置信度: ■高×N ■中×N ■低×N | 📂 N个不同文档 | ✅/⚠️ 领域命中
    顶部新增KPI：高置信率
    """
    # ── 全局检索质量指标 ──
    total_sources = sum(len(r.get("sources", [])) for r in results if r.get("sources"))
    high_total = sum(
        1 for r in results if r.get("sources")
        for s in r["sources"] if s.get("label") == "高"
    )
    high_ratio = round(high_total / total_sources * 100) if total_sources else 0

    rows_html = ""
    opts = '<option value=""></option>' + \
        ''.join(f'<option value="{v}">{v}</option>' for v in [5, 4, 3, 2, 1])

    for i, r in enumerate(results):
        answer = (r.get("answer") or "").strip()
        answer_safe = html_lib.escape(answer)
        sources = r.get("sources") or []
        status = r.get("auto_status", "")
        status_safe = html_lib.escape(status)

        qid_safe = html_lib.escape(str(r.get("id", "")))
        domain_safe = html_lib.escape(str(r.get("domain", "")))
        diff_safe = html_lib.escape(str(r.get("difficulty", "")))
        query_safe = html_lib.escape(str(r.get("query", "")))

        # ── 检索质量指标 ──
        if sources:
            labels = [s.get("label", "低") for s in sources]
            high_n = labels.count("高")
            mid_n = labels.count("中")
            low_n = labels.count("低")
            # 条形可视化
            total_n = max(high_n + mid_n + low_n, 1)
            high_pct = round(high_n / total_n * 100)
            mid_pct = round(mid_n / total_n * 100)
            low_pct = 100 - high_pct - mid_pct

            unique_docs = len(set(s.get("file_name", "") for s in sources))
            top_cat = sources[0].get("category", "")
            q_domain = r.get("domain", "")
            # 领域匹配：比较前两位编号（如 "02-等保国标" → "02"）
            domain_match = top_cat[:2] == q_domain[:2] if top_cat and q_domain else False
            match_icon = "✅" if domain_match else "⚠️"

            metrics_html = f"""
      <div class="q-met">
        <span class="met-bar"><span class="met-h" style="width:{high_pct}%"></span><span class="met-m" style="width:{mid_pct}%"></span><span class="met-l" style="width:{low_pct}%"></span></span>
        <span class="met-lbl">高{high_n}中{mid_n}低{low_n}</span>
        <span class="met-doc">📂 {unique_docs}个文档</span>
        <span class="met-cat">{match_icon} {top_cat[:15]}</span>
      </div>"""
        else:
            metrics_html = '<div class="q-met"><span class="met-lbl" style="color:var(--t2)">无来源</span></div>'

        # 来源列表
        src_items = []
        for s in sources:
            conf_label = s.get("label", "中")
            conf_val = s.get("confidence", 0)
            src_items.append(f"{s.get('file_name', '?')}({conf_label}{conf_val})")
        source_str = "; ".join(src_items) if src_items else "无"
        source_str_safe = html_lib.escape(source_str)

        rows_html += f"""
    <div class="q-card" data-idx="{i}">
      <div class="q-hdr">
        <span class="q-id">{qid_safe}</span>
        <span class="q-dmn">{domain_safe}</span>
        <span class="q-dff {'h' if r['difficulty'] == '困难' else 'm' if r['difficulty'] == '中等' else 'e'}">{diff_safe}</span>
        <span class="q-st">{status_safe}</span>
      </div>
      <div class="q-txt">{query_safe}</div>
      <div class="q-ans">{answer_safe}</div>
      <div class="q-src">📎 {source_str_safe}</div>
      {metrics_html}
      <div class="q-sc">
        <div class="sg"><label>准确性</label><select class="ss" data-d="acc" onchange="u(this)">{opts}</select></div>
        <div class="sg"><label>完整性</label><select class="ss" data-d="cmp" onchange="u(this)">{opts}</select></div>
        <div class="sg"><label>引用</label><select class="ss" data-d="cit" onchange="u(this)">{opts}</select></div>
        <div class="st"><span>总分 </span><span class="tv">/15</span></div>
        <div class="ng"><input class="ni" type="text" placeholder="备注" /></div>
      </div>
    </div>"""

    src_label = f"{high_ratio}%"

    html = f"""<!DOCTYPE html><html lang="zh-CN">
<head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Agent 30题评估 — 人话版（含检索质量仪表盘）</title>
<style>
:root{{--bg:#f5f5f7;--c:#fff;--t:#1d1d1f;--t2:#6e6e73;--b:#e5e5ea;--a:#0071e3;--g:#34c759;--y:#ff9500;--r:#ff3b30;--f:-apple-system,BlinkMacSystemFont,"SF Pro SC","PingFang SC","Noto Sans SC","Microsoft YaHei",sans-serif}}
*{{margin:0;padding:0;box-sizing:border-box}}
body{{font-family:var(--f);background:var(--bg);color:var(--t);padding:24px;line-height:1.6}}
.hdr{{max-width:960px;margin:0 auto 32px}}
.hdr h1{{font-size:28px;font-weight:700}}
.hdr .meta{{color:var(--t2);font-size:14px}}
.hdr .vb{{display:inline-block;background:var(--a);color:#fff;font-size:12px;font-weight:600;padding:4px 12px;border-radius:20px}}
.sb{{max-width:960px;margin:0 auto 24px;display:flex;gap:16px;flex-wrap:wrap}}
.si{{background:var(--c);border-radius:12px;padding:12px 20px;border:1px solid var(--b);flex:1;min-width:120px;text-align:center}}
.si .n{{font-size:28px;font-weight:700;display:block}}
.si .l{{font-size:12px;color:var(--t2)}}
.si.bl .n{{color:var(--a)}}.si.gn .n{{color:var(--g)}}.si.or .n{{color:var(--y)}}.si.pk .n{{color:#af52de}}
.q-card{{max-width:960px;margin:0 auto 16px;background:var(--c);border-radius:16px;padding:20px 24px;border:1px solid var(--b)}}
.q-card:hover{{box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.q-hdr{{display:flex;gap:8px;align-items:center;margin-bottom:8px;flex-wrap:wrap}}
.q-id{{font-size:12px;font-weight:600;color:var(--a);background:#e8f0fe;padding:2px 8px;border-radius:6px}}
.q-dmn{{font-size:12px;color:var(--t2)}}
.q-dff{{font-size:11px;padding:2px 8px;border-radius:4px;font-weight:500}}
.q-dff.e{{background:#e8f5e9;color:#2e7d32}}.q-dff.m{{background:#fff3e0;color:#e65100}}.q-dff.h{{background:#fce4ec;color:#c62828}}
.q-st{{font-size:11px;color:var(--t2);margin-left:auto}}
.q-txt{{font-size:16px;font-weight:600;margin-bottom:12px}}
.q-ans{{font-size:14px;background:#fafafa;border-radius:8px;padding:12px 16px;border:1px solid var(--b);margin-bottom:8px;white-space:pre-wrap;max-height:400px;overflow-y:auto}}
.q-src{{font-size:12px;color:var(--t2);margin-bottom:8px;line-height:1.5;max-height:60px;overflow-y:auto}}
.q-met{{font-size:12px;display:flex;align-items:center;gap:12px;margin-bottom:12px;flex-wrap:wrap}}
.met-bar{{display:inline-flex;height:8px;border-radius:4px;overflow:hidden;width:60px;background:var(--b)}}
.met-h{{background:var(--g)}}.met-m{{background:var(--y)}}.met-l{{background:var(--r)}}
.met-lbl{{color:var(--t2);white-space:nowrap}}
.met-doc{{color:var(--t2);white-space:nowrap}}
.met-cat{{font-weight:500;white-space:nowrap}}
.q-sc{{display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap}}
.sg{{display:flex;flex-direction:column;gap:4px}}
.sg label{{font-size:12px;color:var(--t2);font-weight:500}}
.ss{{font-family:var(--f);font-size:13px;padding:6px 10px;border:1px solid var(--b);border-radius:8px;background:var(--c);cursor:pointer;min-width:110px}}
.ss:focus{{outline:none;border-color:var(--a)}}
.st{{font-size:24px;font-weight:700;padding:4px 0;min-width:80px;text-align:center}}
.st .tv{{color:var(--a)}}
.ng input{{font-family:var(--f);font-size:13px;padding:6px 10px;border:1px solid var(--b);border-radius:8px;width:140px}}
.ng input:focus{{outline:none;border-color:var(--a)}}
.ab{{max-width:960px;margin:32px auto 0;display:flex;gap:12px;justify-content:center;flex-wrap:wrap}}
.btn{{font-family:var(--f);font-size:14px;font-weight:500;padding:10px 24px;border-radius:12px;border:none;cursor:pointer;transition:all .2s}}
.btn-p{{background:var(--a);color:#fff}}.btn-p:hover{{background:#0077ed}}
.btn-s{{background:var(--b);color:var(--t)}}.btn-s:hover{{background:#d0d0d5}}
.btn-d{{background:var(--r);color:#fff}}.btn-d:hover{{opacity:.9}}
@media(max-width:640px){{.q-sc{{flex-direction:column;align-items:stretch}}.sg,.ss,.ng input{{width:100%}}.st{{text-align:left}}}}</style>
</head><body>
<div class="hdr">
  <h1>🛡️ Agent 30题评估 — 人话版</h1>
  <div class="meta">{datetime.now().strftime('%Y-%m-%d %H:%M')} | <span class="vb">含检索质量仪表盘</span></div>
</div>
<div class="sb">
  <div class="si bl"><span class="n">{len(results)}</span><span class="l">总题数</span></div>
  <div class="si gn"><span class="n">{sum(1 for r in results if r.get('sources'))}</span><span class="l">有来源</span></div>
  <div class="si pk"><span class="n">{src_label}</span><span class="l">高置信率</span></div>
  <div class="si or"><span class="n" id="sc">0</span><span class="l">已评分</span></div>
  <div class="si gn"><span class="n" id="avg">-</span><span class="l">平均分</span></div>
  <div class="si bl"><span class="n" id="full">0</span><span class="l">满分(15)</span></div>
</div>
{rows_html}
<div class="ab">
  <button class="btn btn-p" onclick="exp()">📋 导出CSV</button>
  <button class="btn btn-d" onclick="clr()">🗑️ 清空评分</button>
</div>
<script>
const K='agent_eval_{VERSION}';
function ls(k,d){{try{{return JSON.parse(localStorage.getItem(k))||d}}catch(e){{return d}}}}
function ss(k,v){{localStorage.setItem(k,JSON.stringify(v))}}
function u(el){{var c=el.closest('.q-card'),a=parseFloat(c.querySelector('[data-d=acc]').value)||0,b=parseFloat(c.querySelector('[data-d=cmp]').value)||0,cc=parseFloat(c.querySelector('[data-d=cit]').value)||0,t=a+b+cc;c.querySelector('.tv').textContent=t+'/15';c.querySelector('.tv').style.color=t>=13?'var(--g)':t>=9?'var(--y)':'var(--r)';sv()}}
function sv(){{var s={{}};document.querySelectorAll('.q-card').forEach(function(c){{s[c.dataset.idx]={{acc:c.querySelector('[data-d=acc]').value,cmp:c.querySelector('[data-d=cmp]').value,cit:c.querySelector('[data-d=cit]').value,note:c.querySelector('.ni').value}}}});ss(K,s);up()}}
function up(){{var s=ls(K,{{}}),n=document.querySelectorAll('.q-card').length,sc=0,su=0,f=0;Object.values(s).forEach(function(v){{var a=parseFloat(v.acc)||0,b=parseFloat(v.cmp)||0,c=parseFloat(v.cit)||0;if(a&&b&&c){{sc++;su+=a+b+c;if(a+b+c>=15)f++}}}});document.getElementById('sc').textContent=sc;document.getElementById('avg').textContent=sc?(su/sc).toFixed(1):'-';document.getElementById('full').textContent=f}}
function rs(){{var s=ls(K,{{}});Object.entries(s).forEach(function(e){{var c=document.querySelector('.q-card[data-idx="'+e[0]+'"]');if(!c)return;if(e[1].acc)c.querySelector('[data-d=acc]').value=e[1].acc;if(e[1].cmp)c.querySelector('[data-d=cmp]').value=e[1].cmp;if(e[1].cit)c.querySelector('[data-d=cit]').value=e[1].cit;if(e[1].note)c.querySelector('.ni').value=e[1].note;u(c.querySelector('.ss'))}})}}function clr(){{if(!confirm('清空所有评分？'))return;localStorage.removeItem(K);location.reload()}}
function exp(){{var s=ls(K,{{}}),d=new Date().toISOString().slice(0,10),l=['ID,领域,难度,问题,准确性,完整性,引用质量,总分,备注'];document.querySelectorAll('.q-card').forEach(function(c){{var id=c.querySelector('.q-id').textContent.trim(),dm=c.querySelector('.q-dmn').textContent.trim(),df=c.querySelector('.q-dff').textContent.trim(),qt=c.querySelector('.q-txt').textContent.trim(),x=s[c.dataset.idx]||{{}},a=parseFloat(x.acc)||0,b=parseFloat(x.cmp)||0,cc=parseFloat(x.cit)||0,t=a+b+cc||'',n=(x.note||'').replace(/"/g,'""');l.push(id+','+dm+','+df+',"'+qt+'",'+a+','+b+','+cc+','+t+',"'+n+'"')}});var b=new Blob(['\uFEFF'+l.join('\\n')],{{type:'text/csv;charset=utf-8'}}),a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='eval30_human_scores_'+d+'.csv';a.click()}}
rs();
</script>
</body></html>"""

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(html)
    logger.info(f"  ✅ HTML: {output_file}")


def main():
    from agent import CyberAgent
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-run", action="store_true", help="仅生成HTML")
    parser.add_argument("--serve", action="store_true", help="启动HTTP服务")
    parser.add_argument("--rerun-failed", action="store_true", help="重跑失败题（运行错误）并生成新JSON+HTML")
    parser.add_argument("--input", type=str, default="", help="指定结果JSON路径（默认使用最新）")
    args = parser.parse_args()

    if args.rerun_failed:
        if args.input:
            input_path = Path(args.input)
        else:
            jsons = sorted(EVAL_DIR.glob("eval30_v3_results_*.json"))
            if not jsons:
                logger.info("没有找到结果文件")
                return
            input_path = jsons[-1]

        logger.info(f"加载: {input_path.name}")
        with open(input_path, encoding="utf-8") as f:
            results = json.load(f)

        failed = [r for r in results if r.get("auto_status") == "运行错误"]
        logger.info(f"失败题数: {len(failed)}")
        if not failed:
            html_path = EVAL_DIR / \
                f"scorecard_30_{VERSION}_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
            generate_html(results, html_path)
            if args.serve:
                port = 8762
                os.chdir(EVAL_DIR)
                handler = http.server.SimpleHTTPRequestHandler
                socketserver.TCPServer.allow_reuse_address = True
                httpd = socketserver.TCPServer(("", port), handler)
                url = f"http://localhost:{port}/{html_path.name}"
                logger.info(f"\n  🌐 http://localhost:{port}/{html_path.name}")
                webbrowser.open(url)
                httpd.serve_forever()
            return

        agent = CyberAgent(include_example=True)
        for r in failed:
            logger.info(f"\n重跑: {r.get('id')} | {r.get('query')}")
            t0 = time.time()
            try:
                res = agent.ask(r["query"])
                elapsed = time.time() - t0
                r["answer"] = res.get("answer", "")
                r["sources"] = res.get("sources", [])
                r["stats"] = res.get("stats", {})
                r["timestamp"] = datetime.now().isoformat()
                r["note"] = "重跑成功"
                r["auto_status"] = f"有来源({len(r['sources'])}条)" if r["sources"] else "无来源"
                logger.info(f"  ✓ {elapsed:.1f}s | 来源: {len(r['sources'])}条")
            except Exception as e:
                elapsed = time.time() - t0
                r["timestamp"] = datetime.now().isoformat()
                r["note"] = f"重跑失败: {str(e)}"
                r["auto_status"] = "运行错误"
                logger.info(f"  ✗ {elapsed:.1f}s | 错误: {str(e)[:80]}")

        out_ts = datetime.now().strftime("%Y%m%d_%H%M")
        out_json = EVAL_DIR / f"eval30_v3_results_{out_ts}_rerun.json"
        with open(out_json, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=2)

        html_path = EVAL_DIR / f"scorecard_30_{VERSION}_{out_ts}_rerun.html"
        generate_html(results, html_path)
        logger.info(f"\n  ✅ 已保存: {out_json}")
        logger.info(f"  ✅ HTML:  {html_path}")

        if args.serve:
            port = 8762
            os.chdir(EVAL_DIR)
            handler = http.server.SimpleHTTPRequestHandler
            socketserver.TCPServer.allow_reuse_address = True
            httpd = socketserver.TCPServer(("", port), handler)
            url = f"http://localhost:{port}/{html_path.name}"
            logger.info(f"\n  🌐 http://localhost:{port}/{html_path.name}")
            webbrowser.open(url)
            httpd.serve_forever()
        return

    run = not args.no_run

    if run:
        agent = CyberAgent(include_example=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M")
        json_path = EVAL_DIR / f"eval30_v3_results_{ts}.json"
        results = run_evaluation(agent, json_path)
    else:
        jsons = sorted(EVAL_DIR.glob("eval30_v3_results_*.json"))
        if not jsons:
            logger.info("没有找到结果文件")
            return
        latest = jsons[-1]
        logger.info(f"加载: {latest.name}")
        with open(latest, encoding="utf-8") as f:
            results = json.load(f)

    html_path = EVAL_DIR / f"scorecard_30_{VERSION}_{datetime.now().strftime('%Y%m%d_%H%M')}.html"
    generate_html(results, html_path)

    if args.serve:
        port = 8762
        os.chdir(EVAL_DIR)
        handler = http.server.SimpleHTTPRequestHandler
        socketserver.TCPServer.allow_reuse_address = True
        httpd = socketserver.TCPServer(("", port), handler)
        url = f"http://localhost:{port}/{html_path.name}"
        logger.info(f"\n  🌐 http://localhost:{port}/{html_path.name}")
        webbrowser.open(url)
        httpd.serve_forever()


if __name__ == "__main__":
    main()
