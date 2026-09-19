"""Tests for structured query rewrite helpers and metadata filtering."""

import os
import sys

_AGENT_SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PROJECT_ROOT = os.path.abspath(os.path.join(_AGENT_SRC, "..", "..", ".."))
_PREPROCESSOR_SRC = os.path.join(_PROJECT_ROOT, "preprocessor", "src")

for path in (_AGENT_SRC, _PREPROCESSOR_SRC):
    if path not in sys.path:
        sys.path.insert(0, path)

from agent import (
    COMPLIANCE_DECISION_FALLBACK_ANSWER,
    QueryRewriteResult,
    infer_query_type_from_text,
    _extract_json_object,
    answer_mentions_unbacked_legal_references,
    compliance_decision_guard_answer,
    enrich_queries_for_compliance_decision,
    enrich_queries_for_date_fact,
    build_semantic_cache_key,
    can_use_generic_fallback,
    enforce_reflection_safety,
    evidence_coverage,
    has_authoritative_compliance_sources,
    source_display_name,
    SystemPromptLoader,
)
from metadata_filter import (
    apply_metadata_filter,
    boost_by_metadata,
    build_chroma_where,
    infer_metadata_filter_from_query,
    merge_filter_specs,
)
from security_taxonomy import category_aliases, category_emoji, category_icon, infer_categories_from_text, normalize_category


def test_extract_json_object_from_markdown_fence():
    data = _extract_json_object('```json\n{"query_type":"article_lookup"}\n```')
    assert data == {"query_type": "article_lookup"}


def test_query_rewrite_result_keeps_original_and_dedupes_queries():
    result = QueryRewriteResult.from_dict(
        "等保三级访问控制要求",
        {
            "standalone_query": "GB/T 22239-2019 等保三级访问控制要求",
            "semantic_query": "网络安全等级保护三级访问控制要求",
            "keyword_query": "GB/T 22239 访问控制 三级",
            "sub_queries": ["等保三级访问控制要求", "等保三级访问控制要求"],
            "entities": {"doc_ids": ["22239"], "article_numbers": ["8.1.4"]},
        },
    )

    queries = result.retrieval_queries()
    assert queries[0] == "等保三级访问控制要求"
    assert len(queries) == len(set(queries))
    assert result.metadata_filter()["doc_ids"] == ["22239"]
    assert result.metadata_filter()["hard_filter"] is True


def test_infer_query_type_covers_common_security_routing_cases():
    assert infer_query_type_from_text("等保三级测评前，安全审计需要准备哪些材料？") == "compliance_decision"
    assert infer_query_type_from_text("网络安全法对网络运营者的安全保护义务有哪些规定？") == "article_lookup"
    assert infer_query_type_from_text("ISO 27001和等保有什么区别？") == "comparison"
    assert infer_query_type_from_text("数据分类分级通常包括哪些步骤？") == "general"


def test_query_rewrite_result_uses_original_query_when_model_fields_are_empty():
    result = QueryRewriteResult.from_dict(
        "数据分类分级通常包括哪些步骤？",
        {"standalone_query": "", "semantic_query": "", "query_type": ""},
    )

    assert result.semantic_query == "数据分类分级通常包括哪些步骤？"
    assert result.retrieval_queries()


def test_query_rewrite_restores_identifiers_omitted_by_model():
    result = QueryRewriteResult.from_dict(
        "请问 GB/T 22239-2019 第8.1.4条要求是什么？",
        {
            "standalone_query": "网络安全等级保护访问控制要求",
            "semantic_query": "等级保护访问控制要求",
            "keyword_query": "访问控制",
            "entities": {},
        },
    )

    assert "22239" in result.entities["doc_ids"]
    assert "8.1.4" in result.entities["article_numbers"]
    assert any("GB/T22239-2019" in query for query in result.retrieval_queries())
    assert any("第8.1.4条" in query for query in result.retrieval_queries())


def test_hard_reference_queries_do_not_use_generic_fallback():
    article_plan = QueryRewriteResult.from_dict(
        "GB/T 22239-2019 第8.1.4条要求是什么？",
        {"query_type": "article_lookup", "entities": {"doc_ids": ["22239"], "article_numbers": ["8.1.4"]}},
    )
    standard_plan = QueryRewriteResult.from_dict(
        "GB/T 22239-2019有哪些要求？",
        {"query_type": "standard_lookup", "entities": {"doc_ids": ["22239"]}},
    )

    assert can_use_generic_fallback(article_plan) is False
    assert can_use_generic_fallback(standard_plan) is False


def test_general_query_can_use_generic_fallback():
    plan = QueryRewriteResult.from_dict(
        "访问控制有哪些常见做法？",
        {"query_type": "general", "entities": {}},
    )

    assert can_use_generic_fallback(plan) is True


def test_high_risk_reflection_degradation_blocks_original_answer():
    answer, guarded = enforce_reflection_safety(
        "网络安全法规定的法律责任是什么？",
        "article_lookup",
        "可能需要承担相应责任。",
        {"decision": "degraded"},
    )

    assert guarded is True
    assert answer == COMPLIANCE_DECISION_FALLBACK_ANSWER


def test_normal_reflection_degradation_does_not_block_general_answer():
    answer, guarded = enforce_reflection_safety(
        "访问控制有哪些常见做法？",
        "general",
        "请结合组织实际情况设计访问控制。",
        {"decision": "degraded"},
    )

    assert guarded is False
    assert answer.startswith("请结合")


def test_evidence_coverage_is_traceable():
    result = evidence_coverage([{"content": "依据"}, {"content": ""}], "回答")

    assert result == {"source_count": 2, "usable_source_count": 1,
                      "answer_characters": 2, "covered": True,
                      "coverage_ratio": 0.5}


def test_semantic_cache_key_isolated_by_access_and_retrieval_scope():
    base = dict(
        normalized_query="访问控制",
        tenant_id="tenant-a",
        user_id="user-a",
        agent_id="agent-a",
        knowledge_base_id="kb-a",
        model="model-a",
        profiles=["general"],
        prompt_version="prompt-a",
        top_k=10,
        use_rerank=True,
        use_hybrid=True,
    )

    key = build_semantic_cache_key(**base)
    assert key != build_semantic_cache_key(**{**base, "tenant_id": "tenant-b"})
    assert key != build_semantic_cache_key(**{**base, "user_id": "user-b"})
    assert key != build_semantic_cache_key(**{**base, "agent_id": "agent-b"})
    assert key != build_semantic_cache_key(**{**base, "knowledge_base_id": "kb-b"})
    assert key != build_semantic_cache_key(**{**base, "use_rerank": False})


def test_legitimate_single_turn_procedure_question_is_not_jailbreak():
    from agent import _detect_user_jailbreak

    blocked, _ = _detect_user_jailbreak("数据分类分级的主要依据和实施步骤是什么？", [])

    assert blocked is False


def test_infer_metadata_filter_from_standard_and_article():
    spec = infer_metadata_filter_from_query("GB/T 22239-2019 第8.1.4条 等保三级访问控制要求")

    assert "22239" in spec.doc_ids
    assert "8.1.4" in spec.article_numbers
    assert "02-等保国标" in spec.categories
    assert spec.hard_filter is True


def test_infer_metadata_filter_recognizes_iso_iec_identifiers():
    spec = infer_metadata_filter_from_query("ISO/IEC 27001:2022 控制措施")

    assert "ISO/IEC27001:2022" in spec.file_name_contains
    assert "ISO/IEC27001:2022" in spec.doc_ids
    assert spec.hard_filter is True


def test_apply_metadata_filter_matches_existing_fields():
    docs = [
        {
            "file_name": "GB-T 22239-2019.md",
            "category": "02-等保国标",
            "section": "8.1.4 访问控制",
            "content": "三级系统应具备访问控制能力。",
            "score": 0.5,
        },
        {
            "file_name": "其他文件.md",
            "category": "01-国家法律",
            "section": "第二十一条",
            "content": "其他内容",
            "score": 0.8,
        },
    ]
    spec = infer_metadata_filter_from_query("GB/T 22239-2019 8.1.4 访问控制")

    filtered = apply_metadata_filter(docs, spec)
    assert len(filtered) == 1
    assert filtered[0]["file_name"] == "GB-T 22239-2019.md"


def test_boost_by_metadata_improves_matching_distance_score():
    docs = [{
        "file_name": "GB-T 22239-2019.md",
        "category": "02-等保国标",
        "section": "8.1.4 访问控制",
        "content": "三级访问控制要求",
        "score": 0.7,
    }]
    spec = infer_metadata_filter_from_query("GB/T 22239-2019 8.1.4 等保三级访问控制")

    boosted = boost_by_metadata(docs, spec)
    assert boosted[0]["score"] < 0.7
    assert boosted[0]["_metadata_boost"] > 0


def test_build_chroma_where_only_for_single_exact_category():
    spec = merge_filter_specs({"categories": ["等保"]})
    assert build_chroma_where(spec) == {"category": "02-等保国标"}

    spec_multi = merge_filter_specs({"categories": ["等保", "关基"]})
    assert build_chroma_where(spec_multi) is None


def test_taxonomy_normalizes_general_and_industry_aliases():
    aliases = category_aliases()
    assert aliases["数据安全"] == "05-数据安全"
    assert normalize_category("等保") == "02-等保国标"
    assert normalize_category("通信") == "04-通信行业"
    assert category_icon("02-等保国标") == "shield"
    assert category_emoji("04-通信行业") == "📡"


def test_taxonomy_infers_multiple_configured_categories():
    hits = infer_categories_from_text("APP 调用通讯录权限时涉及个人信息保护和隐私合规")
    assert "07-APP安全" in hits
    assert "11-个人信息保护" in hits


def test_source_display_name_combines_standard_id_and_title():
    doc = {
        "file_name": "GB-T 22239-2019",
        "section": "8.1.4 访问控制",
        "content": "# 信息安全技术 网络安全等级保护基本要求\n\n8.1.4 访问控制要求",
    }

    assert source_display_name(doc) == "GB-T 22239-2019《信息安全技术 网络安全等级保护基本要求》"


def test_compliance_decision_query_enrichment_when_rewrite_fallback():
    queries = enrich_queries_for_compliance_decision(
        "我能独立对国内的网站进行渗透测试吗",
        ["我能独立对国内的网站进行渗透测试吗"],
    )

    joined = "\n".join(queries)
    assert "法律依据" in joined
    assert "授权" in joined
    assert "法律责任" in joined
    assert "边界" in joined
    assert len(queries) == len(set(queries))


def test_date_fact_query_enrichment_targets_legal_dates_and_effective_clause():
    queries = enrich_queries_for_date_fact(
        "中国的网络安全法什么时候颁布",
        ["中国的网络安全法什么时候颁布"],
    )

    joined = "\n".join(queries)
    assert "通过日期" in joined
    assert "施行日期" in joined
    assert "附则" in joined
    assert "第七十九条" not in joined
    assert "网络安全法" in joined
    assert len(queries) == len(set(queries))


def test_date_fact_query_enrichment_does_not_hardcode_one_law_for_other_documents():
    queries = enrich_queries_for_date_fact(
        "GB/T 22239-2019 什么时候发布",
        ["GB/T 22239-2019 什么时候发布"],
    )

    joined = "\n".join(queries)
    assert "发布日期" in joined
    assert "网络安全法" not in joined


def test_date_fact_query_enrichment_keeps_other_law_name_without_network_security_terms():
    queries = enrich_queries_for_date_fact(
        "中华人民共和国数据安全法什么时候施行",
        ["中华人民共和国数据安全法什么时候施行"],
    )

    joined = "\n".join(queries)
    assert "中华人民共和国数据安全法" in joined
    assert "网络安全法" not in joined
    assert "第七十九条" not in joined


def test_runtime_prompt_is_general_and_does_not_allow_training_knowledge_fallback():
    prompt = SystemPromptLoader.get()

    assert "禁止用自己的训练知识补充任何内容" in prompt
    assert "运营商系统分类标准" not in prompt
    assert "核心网 | 5GC/EPC/IMS/HLR/HSS" not in prompt


def test_compliance_decision_guard_blocks_low_confidence_operational_sources():
    sources = [
        {
            "file_name": "GB-T 36627-2018",
            "display_name": "GB-T 36627-2018《信息安全技术 网络安全等级保护测试评估技术指南》",
            "category": "02-等保国标",
            "section": "B.3 渗透测试方案",
            "content": "渗透测试方案、测试工具和测试过程。",
            "confidence": 0.23,
        }
    ]

    assert not has_authoritative_compliance_sources(sources)
    assert compliance_decision_guard_answer("我能独立对国内的网站进行渗透测试吗", sources) == COMPLIANCE_DECISION_FALLBACK_ANSWER


def test_compliance_decision_guard_allows_supported_compliance_source():
    sources = [
        {
            "file_name": "安全合规管理制度",
            "display_name": "安全合规管理制度",
            "category": "01-制度规范",
            "section": "授权要求",
            "content": "开展测试前应当取得系统责任方授权，并明确测试范围、时间窗口和责任边界。",
            "confidence": 0.82,
        }
    ]

    assert has_authoritative_compliance_sources(sources)
    assert compliance_decision_guard_answer("我能独立对国内的网站进行渗透测试吗", sources) is None


def test_answer_mentions_unbacked_legal_references_detects_reference_not_in_sources():
    sources = [
        {
            "file_name": "网络安全法",
            "display_name": "网络安全法",
            "category": "01-国家法律",
            "section": "第二十七条",
            "content": "非法侵入他人网络。",
            "confidence": 0.82,
        }
    ]

    unbacked = answer_mentions_unbacked_legal_references("依据《网络安全法》和《数据安全法》，不能这样做。", sources)
    assert unbacked == ["数据安全法"]
