import json

import pytest

import eval_e2e


def item(metric):
    scores = {key: {"score": 0.8} for key in (
        "context_precision", "context_recall", "faithfulness", "relevancy", "hallucination")}
    if metric is not None:
        scores["context_relevance"] = metric
    return {"id": "E1", "query": "synthetic query", "answer": "synthetic answer",
            "domain": "test", "difficulty": "基础", "scores": scores, "sources": []}


def test_relevance_report_displays_summary_detail_and_scored_count(tmp_path, monkeypatch):
    monkeypatch.setattr(eval_e2e, "_VERSION_FILE", tmp_path / "no-versions.json")
    path = tmp_path / "report.html"
    eval_e2e.generate_html([
        item({"score": 0.75, "status": "scored"}),
        item({"score": None, "status": "judge_error"}),
    ], path)
    report = path.read_text(encoding="utf-8")
    assert '<th title="Context Relevance">B-CX</th>' in report
    assert "Context\nRelevance" in report
    assert report.count(">0.750<") == 2  # One summary card and one per-question cell.
    assert "已评分 1/2" in report
    assert "judge_error: 1" in report
    assert 'title="judge_error"' in report


@pytest.mark.parametrize("metric", [None, {"score": None, "status": "unscored_empty_retrieval"}])
def test_legacy_and_unscored_reports_show_missing_instead_of_zero(tmp_path, monkeypatch, metric):
    monkeypatch.setattr(eval_e2e, "_VERSION_FILE", tmp_path / "no-versions.json")
    path = tmp_path / "report.html"
    eval_e2e.generate_html([item(metric)], path)
    report = path.read_text(encoding="utf-8")
    assert "Context\nRelevance" in report
    assert "未评分" in report
    assert "已评分 0/1" in report
    assert ">0.000<" not in report


def test_relevance_compare_supports_new_and_legacy_results(tmp_path, monkeypatch):
    previous_path = tmp_path / "previous.json"
    versions_path = tmp_path / "versions.json"
    versions_path.write_text(json.dumps([
        {"version": "previous", "results_file": "previous.json"}, {"version": "current"},
    ]), encoding="utf-8")
    monkeypatch.setattr(eval_e2e, "_EVAL_DIR", tmp_path)
    monkeypatch.setattr(eval_e2e, "_VERSION_FILE", versions_path)
    previous_path.write_text(json.dumps([item({"score": 0.5})]), encoding="utf-8")
    report = eval_e2e._generate_compare_html({"context_relevance": 0.75})
    block = report.split('<div class="label">Context Relevance</div>')[1].split('compare-item')[0]
    assert "0.750" in block and "上次 0.500" in block
    assert "+0.250" in block
    previous_path.write_text(json.dumps([item(None)]), encoding="utf-8")
    legacy = eval_e2e._generate_compare_html({"context_relevance": 0.75})
    block = legacy.split('<div class="label">Context Relevance</div>')[1].split('compare-item')[0]
    assert "上次 未评分" in block
    assert "diff-up" not in block and "diff-down" not in block
