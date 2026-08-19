from profile_classifier import (
    available_profiles,
    enabled_retrieval_profiles,
    profile_for_metadata,
    profile_options,
    profile_version_snapshot,
    suggest_document_profile,
)


def test_profile_registry_exposes_general_and_industry_profiles():
    profiles = {p["profile"]: p for p in profile_options()}

    assert "general" in profiles
    assert profiles["general"]["scope"] == "general"
    assert "industry/telecom" in profiles
    assert "industry/finance" in profiles


def test_profile_classifier_aliases_are_registry_configured():
    profiles = {p["profile"]: p for p in available_profiles()}
    assert "5g核心网" in profiles["industry/telecom"].get("classifier_aliases", [])
    assert "电网安全" in profiles["industry/energy"].get("classifier_aliases", [])


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


def test_legacy_categories_resolve_to_configured_profiles():
    assert profile_for_metadata(category="01-国家法律") == "general"
    assert profile_for_metadata(category="04-通信行业") == "industry/telecom"
    assert profile_for_metadata(category="上传文档") == "pending"


def test_retrieval_profiles_default_to_general(monkeypatch):
    monkeypatch.delenv("CYBER_AGENT_RETRIEVAL_PROFILES", raising=False)
    monkeypatch.delenv("CYBER_AGENT_SOURCE_PROFILES", raising=False)
    assert enabled_retrieval_profiles() == {"general"}


def test_retrieval_profiles_can_enable_industry_extensions(monkeypatch):
    monkeypatch.setenv("CYBER_AGENT_RETRIEVAL_PROFILES", "general,industry/telecom")
    assert enabled_retrieval_profiles() == {"general", "industry/telecom"}


def test_profile_version_snapshot_tracks_registry_and_definition():
    snapshot = profile_version_snapshot("industry/finance")

    assert snapshot["profile"] == "industry/finance"
    assert snapshot["registry_version"]
    assert snapshot["profile_version"].startswith(snapshot["registry_version"] + ":")
    assert len(snapshot["definition_hash"]) == 16
    assert snapshot["status"] == "known"


def test_unknown_profile_snapshot_is_explicitly_not_comparable():
    snapshot = profile_version_snapshot("industry/retired")

    assert snapshot["profile_version"] == "unknown"
    assert snapshot["status"] == "unknown"
