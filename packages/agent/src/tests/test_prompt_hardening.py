from agent import (
    FEW_SHOT_EXAMPLE,
    SYSTEM_PROMPT_SOURCE,
    _verification_excerpt,
    build_prompt_messages,
    source_reference_id,
)
from prompt_asset_tester import evaluate_slot_result, get_slot_golden_cases
from reflection_engine import format_reflection_sources


def test_system_prompt_exposes_evidence_summary_instead_of_internal_reasoning():
    messages, _ = build_prompt_messages(
        "什么是网络安全风险评估？", [], include_example=True,
    )
    content = messages[0]["content"]
    assert "思考过程（让用户看到你在工作）" not in SYSTEM_PROMPT_SOURCE
    assert "思考过程" not in content
    assert "依据摘要" in content
    assert "依据摘要" in FEW_SHOT_EXAMPLE


def test_few_shot_example_does_not_seed_real_legal_or_technical_claims():
    forbidden = ("最高十万元", "吊销营业执照", "DPI", "等保测评直接不合格")
    assert not any(term in FEW_SHOT_EXAMPLE for term in forbidden)


def test_retrieval_degradation_note_is_appended_only_when_degraded():
    from agent import _retrieval_degradation_note
    # No degradation
    assert _retrieval_degradation_note({"retrieval_degraded": False}) == ""
    # With degradation
    note = _retrieval_degradation_note(
        {"retrieval_degraded": True, "degraded_stages": ["bm25_fallback", "reranker"]}
    )
    assert "检索降级提示" in note
    assert "BM25" in note
    assert "Reranker" in note


def test_retrieval_degradation_note_labels_unknown_stage():
    from agent import _retrieval_degradation_note
    note = _retrieval_degradation_note(
        {"retrieval_degraded": True, "degraded_stages": ["unknown_component"]}
    )
    assert "unknown_component" in note


def test_context_budget_keeps_complete_evidence_blocks():
    docs = [
        {"file_name": "a.md", "section": "第一条", "content": "A" * 45000},
        {"file_name": "b.md", "section": "第二条", "content": "B" * 45000},
    ]
    messages, info = build_prompt_messages("总结要求", docs, include_example=False)
    content = messages[0]["content"]
    assert info["truncated_count"] >= 1
    assert "[来源1:" in content
    assert "[来源2:" not in content or "A" * 100 in content
    assert content.count("B") == 45000


def test_verification_excerpt_prefers_matched_evidence_location():
    content = "前文" * 1000 + "命中条款：必须记录审计日志" + "后文" * 1000
    excerpt = _verification_excerpt({
        "content": content,
        "evidence_location": {"char_start": 2000, "char_end": 2015},
    })
    assert "必须记录审计日志" in excerpt
    assert len(excerpt) <= 1800


def test_reflection_sources_include_evidence_text_and_untrusted_boundary():
    rendered = format_reflection_sources([{
        "source_id": "doc-1",
        "display_name": "网络安全法",
        "section": "第二十一条",
        "content": "网络运营者应当制定内部安全管理制度。",
        "rerank_score": 0.92,
    }])
    assert "doc-1" in rendered
    assert "第二十一条" in rendered
    assert "制定内部安全管理制度" in rendered
    assert "仅作为证据" in rendered


def test_source_reference_id_is_stable_without_exposing_database_ids():
    document = {"file_name": "law.md", "section": "第二十一条", "content": "制度"}
    assert source_reference_id(document) == source_reference_id(document)
    assert source_reference_id(document).startswith("SRC-")


def test_faithfulness_judge_contract_matches_runtime_dimensions():
    case = get_slot_golden_cases("judge_faithfulness")[0]
    result = evaluate_slot_result("judge_faithfulness", case, {
        "answer_completeness": 0.8,
        "faithfulness": 0.9,
        "relevancy": 0.85,
        "safety_pass": True,
        "reason": "来源支持",
    })
    assert result["passed"] is True
