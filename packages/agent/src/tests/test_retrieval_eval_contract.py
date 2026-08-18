from retrieval_eval_contract import (
    is_negative_expectation,
    match_retrieval_expectation,
    normalize_retrieval_expectation,
    serialize_retrieval_expectation,
)


def test_legacy_pipe_expectation_is_supported():
    expected = normalize_retrieval_expectation("等保|等级保护")

    assert expected["relevant_terms"] == ["等保", "等级保护"]
    assert match_retrieval_expectation({"file_name": "等级保护要求.pdf", "content": "访问控制"}, expected)


def test_structured_expectation_prefers_doc_id_and_source_evidence():
    doc = {"id": "doc-42", "file_name": "policy.pdf", "content": "通用说明"}

    assert match_retrieval_expectation(doc, {"relevant_doc_ids": ["doc-42"]})
    assert match_retrieval_expectation(doc, {"relevant_sources": ["policy.pdf"]})
    assert not match_retrieval_expectation(doc, {"relevant_doc_ids": ["doc-99"]})


def test_expectation_can_be_persisted_as_json():
    stored = serialize_retrieval_expectation({"relevant_sources": ["policy.pdf"]})

    assert '"relevant_sources"' in stored
    assert normalize_retrieval_expectation(stored)["relevant_sources"] == ["policy.pdf"]


def test_negative_expectation_is_explicit_and_serializable():
    expected = {"expect_no_match": True, "relevant_sources": ["finance-policy.pdf"]}

    assert is_negative_expectation(expected)
    assert normalize_retrieval_expectation(serialize_retrieval_expectation(expected))["expect_no_match"]
