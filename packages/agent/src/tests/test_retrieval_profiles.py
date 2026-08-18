from retriever import _filter_by_enabled_profiles


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
