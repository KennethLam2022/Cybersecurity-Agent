from pathlib import Path


def test_retrieval_summary_kpi_values_do_not_break_or_clip():
    html = (Path(__file__).parents[1] / "templates" / "admin.html").read_text(encoding="utf-8")

    assert (
        "#retrievalSummary{display:grid;"
        "grid-template-columns:repeat(auto-fill,minmax(min(100%,130px),1fr))}"
    ) in html
    assert "#retrievalSummary .stat-card{min-width:130px}" in html
    assert (
        "#retrievalSummary .stat-card .num"
        "{white-space:nowrap;overflow-wrap:normal;word-break:normal}"
    ) in html
