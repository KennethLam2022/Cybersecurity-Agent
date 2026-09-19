import importlib.util
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src"
_SPEC = importlib.util.spec_from_file_location("legal_chunking_under_test", SRC / "legal_chunking.py")
_MODULE = importlib.util.module_from_spec(_SPEC)
assert _SPEC and _SPEC.loader
_SPEC.loader.exec_module(_MODULE)
chunk_markdown_document = _MODULE.chunk_markdown_document


def test_legal_document_is_split_by_clause_and_preserves_context():
    text = """# 中华人民共和国网络安全法

## 第七章 附则

**第七十八条** 其他内容。

**第七十九条** 本法自2017年6月1日起施行。
"""

    chunks = chunk_markdown_document(text, "网络安全法")

    assert [chunk["clause"] for chunk in chunks] == ["第七十八条", "第七十九条"]
    assert chunks[1]["section"] == "第七章 附则"
    assert "中华人民共和国网络安全法" in chunks[1]["content"]
    assert "2017年6月1日起施行" in chunks[1]["content"]
    assert chunks[1]["chunk_type"] == "clause"


def test_non_clause_sections_still_produce_stable_chunks():
    text = """# 文档标题

## 范围

这是一个没有条款编号的说明章节，内容应保留。
"""

    chunks = chunk_markdown_document(text, "文档")

    assert len(chunks) == 1
    assert chunks[0]["chunk_type"] == "section"
    assert chunks[0]["section"] == "范围"
