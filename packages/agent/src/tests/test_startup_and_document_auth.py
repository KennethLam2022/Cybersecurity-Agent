from pathlib import Path


def test_document_page_requires_token_before_profile_request():
    content = (Path(__file__).parents[1] / "static" / "data_preview.html").read_text(encoding="utf-8")

    assert "if (!documentToken()) return;" in content
    assert "showDocumentTokenModal('');" in content
    assert "showDocumentTokenModal('Token 无效或已过期');" in content


def test_retriever_does_not_build_bm25_during_constructor():
    content = (Path(__file__).parents[3] / "preprocessor" / "src" / "retriever.py").read_text(encoding="utf-8")

    constructor = content[content.index("def __init__", content.index("class CyberRetriever")):]
    constructor = constructor[:constructor.index("def _detect_negation")]
    assert "self._build_bm25_index()" not in constructor
