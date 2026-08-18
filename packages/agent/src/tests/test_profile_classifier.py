from profile_classifier import profile_options, suggest_document_profile


def test_profile_registry_exposes_general_and_industry_profiles():
    profiles = {p["profile"]: p for p in profile_options()}

    assert "general" in profiles
    assert profiles["general"]["scope"] == "general"
    assert "industry/telecom" in profiles
    assert "industry/finance" in profiles


def test_finance_document_is_suggested_as_finance_profile():
    suggestion = suggest_document_profile(filename="银行业数据安全风险评估指南.pdf")

    assert suggestion["profile"] == "industry/finance"
    assert suggestion["scope"] == "industry"
    assert suggestion["industry"] == "finance"
    assert suggestion["review_required"] is False


def test_telecom_document_is_industry_extension_not_default():
    suggestion = suggest_document_profile(filename="5G核心网安全防护要求.pdf")

    assert suggestion["profile"] == "industry/telecom"
    assert suggestion["scope"] == "industry"
    assert suggestion["profile"] != "general"


def test_unknown_document_falls_back_to_general_with_manual_review():
    suggestion = suggest_document_profile(filename="漏洞响应处置流程.md")

    assert suggestion["profile"] == "general"
    assert suggestion["scope"] == "general"
    assert suggestion["review_required"] is True
