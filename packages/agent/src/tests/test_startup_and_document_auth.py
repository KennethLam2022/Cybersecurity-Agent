from pathlib import Path


def test_document_page_uses_logged_in_user_session_for_management_requests():
    content = (Path(__file__).parents[1] / "static" / "data_preview.html").read_text(encoding="utf-8")

    assert "credentials = 'same-origin'" in content
    assert "Authorization" not in content
    assert "if (resp.status === 401) window.location.href = '/';" in content
    assert "documentToken" not in content


def test_retriever_does_not_build_bm25_during_constructor():
    content = (Path(__file__).parents[3] / "preprocessor" / "src" / "retriever.py").read_text(encoding="utf-8")

    constructor = content[content.index("def __init__", content.index("class CyberRetriever")):]
    constructor = constructor[:constructor.index("def _detect_negation")]
    assert "self._build_bm25_index()" not in constructor
