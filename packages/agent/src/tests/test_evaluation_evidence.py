from agent_eval.evaluation_evidence import estimate_usage_cost, redact_evidence, redact_text


def test_redaction_removes_common_sensitive_evaluation_evidence():
    evidence = redact_text("联系 a@example.com，电话 13800138000，IP 10.0.0.1，Bearer abcdefghijk")

    assert "a@example.com" not in evidence
    assert "13800138000" not in evidence
    assert "10.0.0.1" not in evidence
    assert "abcdefghijk" not in evidence


def test_recursive_redaction_preserves_trace_shape():
    evidence = redact_evidence({"steps": [{"input": "api_key=super-secret-key"}]})

    assert evidence["steps"][0]["input"] == "[REDACTED_SECRET]"


def test_cost_estimate_uses_only_explicit_local_pricing():
    priced = estimate_usage_cost(
        {"prompt_tokens": 500_000, "completion_tokens": 250_000}, "model-a",
        {"model-a": {"input_per_million_usd": 2, "output_per_million_usd": 4}},
    )
    unknown = estimate_usage_cost({"prompt_tokens": 1}, "model-b", {})

    assert priced["estimated_cost_usd"] == 2.0
    assert priced["pricing_status"] == "estimated_from_local_configuration"
    assert unknown["estimated_cost_usd"] is None
    assert unknown["pricing_status"] == "unknown_model_or_rate"
