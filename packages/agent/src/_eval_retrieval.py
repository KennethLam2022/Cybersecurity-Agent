"""检索质量评估脚本 —— Recall@K + MRR 自动跑分

用法:
    python packages/agent/src/_eval_retrieval.py

运行结果存入 retrieval_eval 表，通过 GET /api/stats/retrieval-eval 查询。
"""
from memory import ConversationMemory
from retriever import CyberRetriever
import sys
import os
import json
import time
import logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent / "preprocessor" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
logger = logging.getLogger("eval_retrieval")


GENERAL_TEST_SET = [
    {"query": "等保三级对访问控制有什么要求", "expected": "等保|等级保护", "category": "等保"},
    {"query": "个人信息保护法中用户同意机制如何实现", "expected": "个人信息保护|个保法", "category": "数据安全"},
    {"query": "关键信息基础设施安全保护条例的适用范围", "expected": "关键信息基础|CII", "category": "CII"},
    {"query": "数据出境安全评估的流程是什么", "expected": "数据出境|数据安全", "category": "数据安全"},
    {"query": "移动互联网APP个人信息保护要求", "expected": "APP|个人信息", "category": "APP安全"},
    {"query": "网络安全事件应急响应流程", "expected": "应急响应|事件", "category": "应急"},
    {"query": "数据分类分级的方法和标准", "expected": "数据分类|数据分级", "category": "数据安全"},
    {"query": "GB/T 22239-2019 中的安全要求", "expected": "22239|等保", "category": "等保"},
    {"query": "网络数据安全管理条例的核心内容", "expected": "网络数据|数据安全", "category": "数据安全"},
    {"query": "国产密码算法在等保中的应用", "expected": "密码|国密|GM/T", "category": "密码"},
    {"query": "供应链安全管理要求有哪些", "expected": "供应链", "category": "CII"},
    {"query": "云计算服务安全评估办法", "expected": "云计算|云服务", "category": "等保"},
    {"query": "网络安全等级保护定级指南", "expected": "定级|等级保护", "category": "等保"},
    {"query": "数据安全风险评估方法", "expected": "风险评估|20984", "category": "数据安全"},
    {"query": "网络安全事件分类分级", "expected": "事件分类|20986", "category": "应急"},
    {"query": "物联网安全接入技术要求", "expected": "物联网|IoT", "category": "等保"},
    {"query": "个人信息安全影响评估方法", "expected": "个人信息|安全影响", "category": "数据安全"},
    {"query": "网络安全法的核心义务", "expected": "网络安全法", "category": "合规"},
    {"query": "密码法对商用密码的管理要求", "expected": "密码法|商用密码", "category": "密码"},
    {"query": "数据安全技术 数据脱敏规范", "expected": "脱敏|数据安全", "category": "数据安全"},
    {"query": "个人信息去标识化技术规范", "expected": "去标识化|37964", "category": "数据安全"},
    {"query": "Intel TDX 安全技术原理", "expected": "TDX|可信", "category": "技术"},
    {"query": "GB/T 20984 信息安全风险评估方法", "expected": "20984|风险评估", "category": "等保"},
    {"query": "上海市网络安全事件应急预案", "expected": "上海|应急预案", "category": "应急"},
    {"query": "软件供应链中的开源组件风险如何识别和处置", "expected": "供应链|开源", "category": "供应链"},
    {"query": "云环境中的身份权限最小化如何落地", "expected": "云|身份|权限", "category": "云安全"},
    {"query": "漏洞修复优先级应依据哪些风险因素确定", "expected": "漏洞|风险", "category": "漏洞管理"},
    {"query": "安全运营中心如何建立告警分级和闭环流程", "expected": "安全运营|告警|闭环", "category": "安全运营"},
    {"query": "第三方供应商接入前应开展哪些安全评估", "expected": "供应商|安全评估", "category": "供应链"},
]

# 通信行业资料仍可用于专项回归，但不参与通用网络安全主干评分。
INDUSTRY_TELECOM_TEST_SET = [
    {"query": "5G核心网的安全技术要求有哪些", "expected": "5G|核心网", "category": "通信行业", "profile": "industry/telecom"},
    {"query": "工信部网络安全考核指标有哪些", "expected": "工信部|网络安全考核", "category": "通信行业", "profile": "industry/telecom"},
    {"query": "通信网络安全防护管理办法", "expected": "通信网络|工信部", "category": "通信行业", "profile": "industry/telecom"},
    {"query": "移动通信网网元功能安全要求", "expected": "网元|移动通信", "category": "通信行业", "profile": "industry/telecom"},
    {"query": "中国移动大数据安全保护体系", "expected": "大数据|中国移动", "category": "通信行业", "profile": "industry/telecom"},
    {"query": "工信部公共互联网网络安全应急预案", "expected": "应急预案|公共互联网", "category": "通信行业", "profile": "industry/telecom"},
]

TEST_SET = GENERAL_TEST_SET


def _item_profile(item: dict, default: str = "general") -> str:
    profile = str(item.get("profile") or default).strip()
    return "industry/telecom" if profile == "telecom" else (profile or "general")


def get_test_set(profile: str = "general") -> list[dict]:
    """返回评测集；行业专项必须显式指定 profile。"""
    if profile in ("industry/telecom", "telecom"):
        return list(INDUSTRY_TELECOM_TEST_SET)
    return list(GENERAL_TEST_SET)


def evaluate(profile: str = "general"):
    memory = ConversationMemory()
    retriever = CyberRetriever(use_hybrid=True)
    test_set = get_test_set(profile)
    evaluation_profile = _item_profile({"profile": profile})

    results_summary = {
        "evaluation_profile": evaluation_profile,
        "total": len(test_set), "pass": 0, "fail": 0, "items": []
    }
    all_recall_5, all_recall_10, all_mrr = [], [], []

    for item in test_set:
        query = item["query"]
        expected = item["expected"]

        docs = retriever.search(query, top_k=10, use_rerank=True, profiles={evaluation_profile})

        recall_5, recall_10 = 0, 0
        mrr = 0.0
        top1_match_after_rerank = 0

        first_rank = None

        for rank, d in enumerate(docs[:10], 1):
            content = (d.get("file_name", "") + " " + d.get("content", "")).lower()
            expected_lower = expected.lower()
            matched = any(kw.strip().lower() in content for kw in expected_lower.split("|"))

            if matched:
                if rank <= 5:
                    recall_5 = 1
                    recall_10 = 1
                elif rank <= 10:
                    recall_10 = 1
                if first_rank is None:
                    first_rank = rank
                    mrr = 1.0 / rank
                if rank == 1:
                    top1_match_after_rerank = 1

        all_recall_5.append(recall_5)
        all_recall_10.append(recall_10)
        all_mrr.append(mrr)

        memory.save_retrieval_eval(
            query=query,
            expected_source=expected,
            recall_5=recall_5,
            recall_10=recall_10,
            mrr=mrr,
            faiss_count=len(retriever._faiss_db.index_to_docstore_id) if retriever._faiss_db else 0,
            chroma_count=len(retriever._chroma_collection.get(include=[])[
                             "ids"]) if retriever._chroma_collection else 0,
            rerank_top1_match=top1_match_after_rerank,
            profile=evaluation_profile,
        )

        results_summary["items"].append({
            "query": query,
            "expected": expected,
            "profile": evaluation_profile,
            "recall_5": recall_5,
            "recall_10": recall_10,
            "mrr": round(mrr, 3),
            "top5_files": [d.get("file_name", "") for d in docs[:5]],
        })
        status = "PASS" if recall_5 == 1 else "FAIL"
        if status == "PASS":
            results_summary["pass"] += 1
        else:
            results_summary["fail"] += 1

        logger.info(f"  [{status}] {query[:40]:40s} R@5={recall_5} R@10={recall_10} MRR={mrr:.3f}")

    avg_recall_5 = sum(all_recall_5) / len(all_recall_5) * 100
    avg_recall_10 = sum(all_recall_10) / len(all_recall_10) * 100
    avg_mrr = sum(all_mrr) / len(all_mrr)

    logger.info("")
    logger.info("=" * 50)
    logger.info(f"检索质量跑分完成")
    logger.info(f"  profile: {evaluation_profile}")
    logger.info(f"  测试集: {len(test_set)} 条")
    logger.info(
        f"  通过率: {results_summary['pass']}/{results_summary['total']} ({results_summary['pass']/results_summary['total']*100:.0f}%)")
    logger.info(f"  平均 Recall@5:  {avg_recall_5:.1f}%")
    logger.info(f"  平均 Recall@10: {avg_recall_10:.1f}%")
    logger.info(f"  平均 MRR:       {avg_mrr:.3f}")
    logger.info("=" * 50)

    return results_summary


def generate_test_set_from_keywords(keywords: str, llm=None) -> list:
    """用 LLM 根据关键词生成检索测试集"""
    prompt = f"""你是一个 RAG 检索质量评估专家。根据以下关键词，生成 30 条检索质量测试查询。

关键词：{keywords}

要求：
1. 每条包含 query（查询语句）、expected（期望匹配的关键词，用 | 分隔）、category（分类）
2. 覆盖不同角度和难度
3. 必须围绕关键词展开，不要忽略关键词
4. query 用中文，长度不超过 60 字

只输出 JSON 数组，不要多余文字，格式：
[
  {{"query": "等保三级对访问控制有什么要求", "expected": "等保|等级保护", "category": "等保"}},
  ...
]"""

    if llm is not None:
        try:
            resp = llm.chat([{"role": "user", "content": prompt}])
            text = resp.get("content", "")
            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            text = text.strip()
            items = json.loads(text)
            if isinstance(items, list) and len(items) > 0:
                return items
        except Exception as e:
            logger.warning(f"LLM 生成测试集失败，使用默认集: {e}")
    return TEST_SET


def evaluate_with_items(items: list, memory) -> dict:
    """对给定测试集执行检索质量评估"""
    from retriever import CyberRetriever

    retriever = CyberRetriever(use_hybrid=True)
    profiles = sorted({_item_profile(item) for item in items})
    results_summary = {
        "evaluation_profile": profiles[0] if len(profiles) == 1 else "mixed",
        "profiles": profiles,
        "total": len(items), "pass": 0, "fail": 0, "items": []
    }
    all_recall_5, all_recall_10, all_mrr = [], [], []

    for item in items:
        query = item["query"]
        expected = item.get("expected", "")
        item_profile = _item_profile(item)
        docs = retriever.search(query, top_k=10, use_rerank=True, profiles={item_profile})

        recall_5, recall_10 = 0, 0
        mrr = 0.0
        top1_match = 0
        first_rank = None

        for rank, d in enumerate(docs[:10], 1):
            content = (d.get("file_name", "") + " " + d.get("content", "")).lower()
            matched = any(kw.strip().lower() in content for kw in expected.lower().split("|"))
            if matched:
                if rank <= 5:
                    recall_5 = 1
                if rank <= 10:
                    recall_10 = 1
                if first_rank is None:
                    first_rank = rank
                    mrr = 1.0 / rank
                if rank == 1:
                    top1_match = 1

        all_recall_5.append(recall_5)
        all_recall_10.append(recall_10)
        all_mrr.append(mrr)

        memory.save_retrieval_eval(
            query=query, expected_source=expected,
            recall_5=recall_5, recall_10=recall_10, mrr=mrr,
            faiss_count=len(retriever._faiss_db.index_to_docstore_id) if retriever._faiss_db else 0,
            chroma_count=len(retriever._chroma_collection.get(include=[])[
                             "ids"]) if retriever._chroma_collection else 0,
            rerank_top1_match=top1_match,
            profile=item_profile,
        )

        status = "PASS" if recall_5 == 1 else "FAIL"
        if status == "PASS":
            results_summary["pass"] += 1
        else:
            results_summary["fail"] += 1

        results_summary["items"].append({
            "query": query,
            "expected": expected,
            "profile": item_profile,
            "recall_5": recall_5,
            "recall_10": recall_10,
            "mrr": round(mrr, 4),
        })

    avg_recall_5 = sum(all_recall_5) / len(all_recall_5) * 100
    avg_recall_10 = sum(all_recall_10) / len(all_recall_10) * 100
    avg_mrr = sum(all_mrr) / len(all_mrr)

    results_summary["avg_recall_5"] = round(avg_recall_5, 1)
    results_summary["avg_recall_10"] = round(avg_recall_10, 1)
    results_summary["avg_mrr"] = round(avg_mrr, 3)
    logger.info(
        f"evaluate_with_items: {results_summary['pass']}/{results_summary['total']} R@5={avg_recall_5:.0f}% MRR={avg_mrr:.3f}")
    return results_summary


def evaluate_single_query(agent, query: str, expected: str, profile: str = "general") -> dict:
    """单条检索质量跑分（复用 agent 现有的 retriever）"""
    from datetime import datetime
    retriever = agent.retriever
    item_profile = _item_profile({"profile": profile})
    docs = retriever.search(query, top_k=10, use_rerank=True, profiles={item_profile})

    recall_5, recall_10 = 0, 0
    mrr = 0.0
    top1_match = 0
    first_rank = None

    for rank, d in enumerate(docs[:10], 1):
        content = (d.get("file_name", "") + " " + d.get("content", "")).lower()
        matched = any(kw.strip().lower() in content for kw in expected.lower().split("|"))
        if matched:
            if rank <= 5:
                recall_5 = 1
            if rank <= 10:
                recall_10 = 1
            if first_rank is None:
                first_rank = rank
                mrr = 1.0 / rank
            if rank == 1:
                top1_match = 1

    faiss_count = len(retriever._faiss_db.index_to_docstore_id) if retriever._faiss_db else 0
    chroma_count = len(retriever._chroma_collection.get(include=[])[
                       "ids"]) if retriever._chroma_collection else 0

    agent.memory.save_retrieval_eval(
        query=query, expected_source=expected,
        recall_5=recall_5, recall_10=recall_10, mrr=mrr,
        faiss_count=faiss_count, chroma_count=chroma_count,
        rerank_top1_match=top1_match,
        profile=item_profile,
    )

    return {
        "query": query,
        "expected": expected,
        "profile": item_profile,
        "recall_5": recall_5,
        "recall_10": recall_10,
        "mrr": mrr,
        "top1_match": top1_match,
    }


def _eval_mode(items, retriever, use_rerank: bool, use_hybrid: bool,
               sources: tuple) -> dict:
    """对给定测试集用指定模式跑分，返回 recall_5 和 mrr 列表"""
    recall_5s, mrrs = [], []
    for item in items:
        query = item["query"]
        expected = item.get("expected", "")
        item_profile = _item_profile(item)
        docs = retriever.search(query, top_k=10, use_rerank=use_rerank,
                                use_hybrid=use_hybrid, sources=sources,
                                profiles={item_profile})
        recall_5, mrr = 0, 0.0
        first_rank = None
        for rank, d in enumerate(docs[:10], 1):
            content = (d.get("file_name", "") + " " + d.get("content", "")).lower()
            if any(kw.strip().lower() in content for kw in expected.lower().split("|")):
                if rank <= 5:
                    recall_5 = 1
                if first_rank is None:
                    first_rank = rank
                    mrr = 1.0 / rank
        recall_5s.append(recall_5)
        mrrs.append(mrr)
    return {"recall_5s": recall_5s, "mrrs": mrrs}


def _eval_mode_bm25_only(items: list, retriever, top_k=10) -> dict:
    """对给定测试集用 BM25-only 模式跑分（直接调 _bm25_search，不走 search() 兜底路径）

    参数：
      items:     测试集 [{"query", "expected", ...}]
      retriever: CyberRetriever 实例（需已初始化 BM25 索引）
    """
    recall_5s, mrrs = [], []
    for item in items:
        query = item["query"]
        expected = item.get("expected", "")
        docs = retriever._bm25_search(query, top_k * 2) or []
        from retriever import _filter_by_enabled_profiles
        docs = _filter_by_enabled_profiles(docs, {_item_profile(item)})

        recall_5, mrr = 0, 0.0
        first_rank = None
        for rank, d in enumerate(docs[:10], 1):
            content = (d.get("file_name", "") + " " + d.get("content", "")).lower()
            if any(kw.strip().lower() in content for kw in expected.lower().split("|")):
                if rank <= 5:
                    recall_5 = 1
                if first_rank is None:
                    first_rank = rank
                    mrr = 1.0 / rank
        recall_5s.append(recall_5)
        mrrs.append(mrr)
    return {"recall_5s": recall_5s, "mrrs": mrrs}


def evaluate_with_items_compare(items: list, memory) -> dict:
    """对同一测试集跑4种检索模式对比：FAISS-only / BM25-only / Hybrid no rerank / Hybrid+rerank"""
    from retriever import CyberRetriever

    retriever_faiss = CyberRetriever(use_hybrid=False)
    retriever_hybrid = CyberRetriever(use_hybrid=True)

    all_results = {}

    # 3 种模式走 _eval_mode（通过 search 方法）
    modes = [
        ("faiss_only", retriever_faiss, False, False, ("faiss",)),
        ("hybrid_no_rerank", retriever_hybrid, False, True, ("faiss", "chroma")),
        ("hybrid_rerank", retriever_hybrid, True, True, ("faiss", "chroma")),
    ]
    for name, ret, rerank, hybrid, sources in modes:
        logger.info(f"  跑分模式: {name}")
        r = _eval_mode(items, ret, rerank, hybrid, sources)
        all_results[name] = r
        avg_r5 = sum(r["recall_5s"]) / len(r["recall_5s"]) * 100
        avg_mrr = sum(r["mrrs"]) / len(r["mrrs"])
        logger.info(f"    {name}: R@5={avg_r5:.0f}% MRR={avg_mrr:.3f}")

    # BM25-only：直接调 _bm25_search，不走 search() 的兜底路径
    logger.info("  跑分模式: bm25_only")
    all_results["bm25_only"] = _eval_mode_bm25_only(items, retriever_faiss)
    avg_r5 = sum(all_results["bm25_only"]["recall_5s"]) / len(items) * 100
    avg_mrr = sum(all_results["bm25_only"]["mrrs"]) / len(items)
    logger.info(f"    bm25_only: R@5={avg_r5:.0f}% MRR={avg_mrr:.3f}")

    for i, item in enumerate(items):
        memory.save_eval_comparison(
            query=item["query"],
            expected_source=item.get("expected", ""),
            faiss_only_recall_5=all_results["faiss_only"]["recall_5s"][i],
            faiss_only_mrr=all_results["faiss_only"]["mrrs"][i],
            bm25_only_recall_5=all_results["bm25_only"]["recall_5s"][i],
            bm25_only_mrr=all_results["bm25_only"]["mrrs"][i],
            hybrid_no_rerank_recall_5=all_results["hybrid_no_rerank"]["recall_5s"][i],
            hybrid_no_rerank_mrr=all_results["hybrid_no_rerank"]["mrrs"][i],
            hybrid_rerank_recall_5=all_results["hybrid_rerank"]["recall_5s"][i],
            hybrid_rerank_mrr=all_results["hybrid_rerank"]["mrrs"][i],
            profile=_item_profile(item),
        )

    base_r5 = sum(all_results["faiss_only"]["recall_5s"]) / len(items)
    hybrid_r5 = sum(all_results["hybrid_no_rerank"]["recall_5s"]) / len(items)
    rerank_r5 = sum(all_results["hybrid_rerank"]["recall_5s"]) / len(items)
    bm25_r5 = sum(all_results["bm25_only"]["recall_5s"]) / len(items)
    base_mrr = sum(all_results["faiss_only"]["mrrs"]) / len(items)
    hybrid_mrr = sum(all_results["hybrid_no_rerank"]["mrrs"]) / len(items)
    rerank_mrr = sum(all_results["hybrid_rerank"]["mrrs"]) / len(items)
    bm25_mrr = sum(all_results["bm25_only"]["mrrs"]) / len(items)

    profiles = sorted({_item_profile(item) for item in items})
    summary = {
        "count": len(items),
        "evaluation_profile": profiles[0] if len(profiles) == 1 else "mixed",
        "profiles": profiles,
        "faiss_only_recall_5": round(base_r5, 3),
        "faiss_only_mrr": round(base_mrr, 3),
        "bm25_only_recall_5": round(bm25_r5, 3),
        "bm25_only_mrr": round(bm25_mrr, 3),
        "hybrid_no_rerank_recall_5": round(hybrid_r5, 3),
        "hybrid_no_rerank_mrr": round(hybrid_mrr, 3),
        "hybrid_rerank_recall_5": round(rerank_r5, 3),
        "hybrid_rerank_mrr": round(rerank_mrr, 3),
        "hybrid_gain_recall_5": round(hybrid_r5 - base_r5, 3) if base_r5 > 0 else 0,
        "rerank_gain_recall_5": round(rerank_r5 - hybrid_r5, 3) if hybrid_r5 > 0 else 0,
        "hybrid_gain_mrr": round(hybrid_mrr - base_mrr, 3) if base_mrr > 0 else 0,
        "rerank_gain_mrr": round(rerank_mrr - hybrid_mrr, 3) if hybrid_mrr > 0 else 0,
        "items": [],
    }

    # 添加每题各模式的详细得分
    for i, item in enumerate(items):
        summary["items"].append({
            "query": item["query"],
            "expected": item.get("expected", ""),
            "profile": _item_profile(item),
            "faiss_only_recall_5": all_results["faiss_only"]["recall_5s"][i],
            "faiss_only_mrr": all_results["faiss_only"]["mrrs"][i],
            "bm25_only_recall_5": all_results["bm25_only"]["recall_5s"][i],
            "bm25_only_mrr": all_results["bm25_only"]["mrrs"][i],
            "hybrid_no_rerank_recall_5": all_results["hybrid_no_rerank"]["recall_5s"][i],
            "hybrid_no_rerank_mrr": all_results["hybrid_no_rerank"]["mrrs"][i],
            "hybrid_rerank_recall_5": all_results["hybrid_rerank"]["recall_5s"][i],
            "hybrid_rerank_mrr": all_results["hybrid_rerank"]["mrrs"][i],
        })

    logger.info("")
    logger.info("=" * 60)
    logger.info("检索模式对比完成")
    logger.info(f"  FAISS-only:        R@5={base_r5*100:.0f}%  MRR={base_mrr:.3f}")
    logger.info(f"  BM25-only:         R@5={bm25_r5*100:.0f}%  MRR={bm25_mrr:.3f}")
    logger.info(f"  Hybrid no rerank:  R@5={hybrid_r5*100:.0f}%  MRR={hybrid_mrr:.3f}")
    logger.info(f"  Hybrid+rerank:     R@5={rerank_r5*100:.0f}%  MRR={rerank_mrr:.3f}")
    logger.info(
        f"  ── Hybrid增益:     R@5={summary['hybrid_gain_recall_5']*100:+.0f}pp  MRR={summary['hybrid_gain_mrr']:+.3f}")
    logger.info(
        f"  ── Rerank增益:     R@5={summary['rerank_gain_recall_5']*100:+.0f}pp  MRR={summary['rerank_gain_mrr']:+.3f}")
    logger.info("=" * 60)

    return summary


if __name__ == "__main__":
    evaluate()
