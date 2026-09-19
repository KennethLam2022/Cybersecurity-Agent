import os
import sys

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_SRC = os.path.join(_ROOT, "preprocessor", "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from retrieval_text import (
    build_bm25_text,
    extract_retrieval_identifiers,
    tokenize_for_retrieval,
)


def test_tokenizer_preserves_security_identifiers_and_variants():
    tokens = tokenize_for_retrieval("GB/T 22239-2019 第8.1.4条 5G SA")

    assert "GB/T 22239-2019" in tokens
    assert "GBT222392019" in tokens
    assert "第8.1.4条" in tokens
    assert "5G" in tokens
    assert "SA" in tokens


def test_bm25_text_contains_metadata_fields_before_body():
    text = build_bm25_text({
        "file_name": "网络安全技术 网络安全等级保护基本要求 GB-T-22239-2019.md",
        "title": "网络安全等级保护基本要求",
        "standard_name": "GB/T 22239-2019",
        "aliases": ["等保2.0"],
        "section": "第8章 访问控制",
        "clause": "第8.1.4条",
        "text": "应建立访问控制策略。",
    })

    assert text.index("GB/T 22239-2019") < text.index("应建立访问控制策略")
    assert "等保2.0" in text
    assert "第8.1.4条" in text


def test_bm25_field_text_does_not_replace_returned_source_content():
    metadata = {"title": "标题", "text": "正文证据"}
    assert build_bm25_text(metadata).endswith("正文证据")


def test_extract_identifiers_normalizes_standard_and_clause_values():
    identifiers = extract_retrieval_identifiers("请解释 GB/T22239—2019 第8.1.4条")

    assert identifiers["standards"] == ["GB/T22239-2019"]
    assert identifiers["clauses"] == ["第8.1.4条"]


def test_extract_identifiers_supports_iso_and_iec_variants():
    identifiers = extract_retrieval_identifiers("ISO/IEC 27001:2022 与 IEC 62443-3-3")

    assert "ISO/IEC27001:2022" in identifiers["standards"]
    assert "IEC62443-3-3" in identifiers["standards"]
