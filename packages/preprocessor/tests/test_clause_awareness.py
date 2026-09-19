import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from clause_awareness import apply_clause_awareness, extract_clause_signals


def test_extract_clause_signals_detects_article_and_date_intent():
    signals = extract_clause_signals("网络安全法第七十九条什么时候施行")
    assert signals["articles"] == ["第七十九条"]
    assert signals["date_query"] is True


def test_exact_clause_is_boosted_over_other_clause():
    docs = [
        {"clause": "第七十八条", "section": "第七章 附则", "content": "其他内容", "chunk_type": "clause", "score": 0.2},
        {"clause": "第七十九条", "section": "第七章 附则", "content": "本法自2017年6月1日起施行", "chunk_type": "clause", "score": 0.3},
    ]
    ranked = sorted(apply_clause_awareness(docs, "网络安全法第七十九条什么时候施行"), key=lambda item: item["score"])
    assert ranked[0]["clause"] == "第七十九条"
    assert ranked[0]["clause_awareness"] > ranked[1]["clause_awareness"]


def test_general_query_does_not_change_documents():
    docs = [{"content": "普通安全内容", "score": 0.4}]
    assert apply_clause_awareness(docs, "网络安全管理体系怎么建设") == docs
