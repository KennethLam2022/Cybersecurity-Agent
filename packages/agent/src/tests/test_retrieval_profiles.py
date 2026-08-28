from pathlib import Path

from retriever import CyberRetriever, _filter_by_access_scope, _filter_by_enabled_profiles
from incremental_index import _access_metadata
from access_migration import _normalize


def test_profile_filter_keeps_general_and_excludes_legacy_telecom_by_default():
    docs = [
        {"file_name": "law", "category": "01-国家法律", "content": "general"},
        {"file_name": "telecom", "category": "04-通信行业", "content": "industry"},
        {"file_name": "upload", "category": "上传文档", "content": "unknown"},
    ]

    filtered = _filter_by_enabled_profiles(docs, {"general"})

    assert [doc["file_name"] for doc in filtered] == ["law"]
    assert filtered[0]["profile"] == "general"


def test_profile_filter_allows_explicit_industry_extension():
    docs = [{"file_name": "telecom", "category": "04-通信行业", "content": "industry"}]

    filtered = _filter_by_enabled_profiles(docs, {"general", "industry/telecom"})

    assert filtered[0]["profile"] == "industry/telecom"


def test_access_filter_runs_before_profile_filter_in_retrieval_pipeline():
    source = (Path(__file__).parents[3] / "preprocessor" / "src" / "retriever.py").read_text(encoding="utf-8")
    access_marker = "docs = _filter_by_access_scope(docs, access_scope)"
    profile_marker = "docs = _filter_by_enabled_profiles(docs, enabled_profiles)"
    assert source.index(access_marker, source.index("Access control is the first boundary")) < source.index(profile_marker, source.index("Access control is the first boundary"))


def test_access_scope_keeps_public_and_rejects_other_private_documents():
    docs = [
        {"file_name": "public", "visibility": "public"},
        {"file_name": "mine", "visibility": "private", "tenant_id": "t1", "owner_user_id": "u1", "agent_id": "a1"},
        {"file_name": "other", "visibility": "private", "tenant_id": "t2", "owner_user_id": "u2", "agent_id": "a2"},
    ]
    result = _filter_by_access_scope(docs, {"tenant_id": "t1", "user_id": "u1", "agent_id": "a1"})
    assert [item["file_name"] for item in result] == ["public", "mine"]


def test_access_scope_filters_selected_knowledge_base():
    docs = [
        {"file_name": "kb-a", "visibility": "tenant", "tenant_id": "t1", "knowledge_base_id": "kb-a"},
        {"file_name": "kb-b", "visibility": "tenant", "tenant_id": "t1", "knowledge_base_id": "kb-b"},
    ]
    result = _filter_by_access_scope(docs, {
        "tenant_id": "t1", "user_id": "u1", "agent_id": "a1", "knowledge_base_id": "kb-a",
    })
    assert [item["file_name"] for item in result] == ["kb-a"]


def test_legacy_or_incomplete_access_metadata_falls_back_to_public():
    assert _access_metadata({}) == {
        "visibility": "public", "tenant_id": "", "owner_user_id": "",
        "agent_id": "", "document_id": "",
    }
    assert _access_metadata({"visibility": "private", "tenant_id": "t1"})["visibility"] == "public"
    private = _access_metadata({
        "visibility": "private", "tenant_id": "t1", "owner_user_id": "u1",
        "agent_id": "a1", "document_id": "doc-1",
    })
    assert private["visibility"] == "private"
    assert private["document_id"] == "doc-1"


def test_access_migration_normalizes_legacy_and_private_scope():
    assert _normalize({})["visibility"] == "public"
    assert _normalize({"visibility": "tenant"})["visibility"] == "public"
    assert _normalize({
        "visibility": "private", "tenant_id": "t1", "owner_user_id": "u1",
    })["visibility"] == "private"


def test_parent_injection_carries_access_metadata_without_leaking_private_docs():
    retriever = object.__new__(CyberRetriever)
    retriever._parent_index = {
        "doc-private__s0": {
            "text": "2692 私有资料正文", "file_name": "YD2692", "category": "上传文档", "section": "范围",
            "visibility": "private", "tenant_id": "t1", "owner_user_id": "u1", "agent_id": "a1",
            "document_id": "doc-private",
        },
    }
    injected = retriever._inject_parent_sections([], ["2692"])
    assert injected[0]["visibility"] == "private"
    assert _filter_by_access_scope(injected, {"tenant_id": "t2", "user_id": "u2", "agent_id": "a2"}) == []
