import json

import pytest

import eval_e2e
from _eval_context import eval_context_recall


REFERENCES = [{"evidence_id": f"e{i}", "text": f"requirement {i}"} for i in range(1, 4)]
DOCS = [{"file_name": "security-policy", "content": "requirement 1; requirement 2"}]


class Judge:
    model = "synthetic-judge"

    def chat(self, messages, **kwargs):
        assert "SECRET ANSWER" not in messages[0]["content"]
        return {"content": json.dumps({"judgments": [
            {"evidence_id": "e1", "covered": True},
            {"evidence_id": "e2", "covered": True},
            {"evidence_id": "e3", "covered": False},
        ]})}


def test_recall_counts_reference_coverage_independently_of_answer_and_noise():
    options = {"reference_evidence": REFERENCES, "llm": Judge()}
    result = eval_context_recall("query", DOCS, "SECRET ANSWER", **options)
    noisy = eval_context_recall("query", DOCS + [{"content": "office supplies"}], "other answer", **options)
    assert result["score"] == 0.6667
    assert noisy == result
    assert result["details"]["reference_count"] == 3
    assert result["details"]["covered_count"] == 2
    assert result["evaluator"]["model_id"] == "synthetic-judge"


@pytest.mark.parametrize("references", [None, [], [{"text": "missing id"}], REFERENCES + [REFERENCES[0]]])
def test_recall_missing_or_invalid_reference_is_unscored(references):
    result = eval_context_recall("query", DOCS, "answer", llm=Judge(), reference_evidence=references)
    assert result["score"] is None
    assert result["status"] == "unscored_missing_reference"
    assert result["evaluator"]["method"] == "not_run"


def test_recall_valid_references_with_empty_retrieval_are_deterministic_zero():
    result = eval_context_recall("query", [], "", reference_evidence=REFERENCES, llm=Judge())
    assert result["score"] == 0
    assert result["status"] == "scored"
    assert result["evaluator"]["method"] == "deterministic"
    assert all(not item["covered"] for item in result["details"]["judgments"])


@pytest.mark.parametrize("labels, expected", [([True, True, True], 1), ([False, False, False], 0)])
def test_recall_accepts_reviewed_labels(labels, expected):
    result = eval_context_recall("query", DOCS, reference_evidence=REFERENCES,
        coverage_annotations=labels, annotation_source="gold-v1")
    assert result["score"] == expected
    assert result["details"]["annotation_source"] == "gold-v1"


def test_recall_missing_labels_or_judge_are_unscored():
    result = eval_context_recall("query", DOCS, reference_evidence=REFERENCES)
    assert result["status"] == "unscored_missing_annotation"
    assert result["score"] is None


def test_recall_judge_failure_and_context_truncation_do_not_fabricate_scores():
    class TimeoutJudge:
        model = "synthetic-judge"

        def chat(self, messages, **kwargs):
            raise TimeoutError("unavailable")

    result = eval_context_recall("query", DOCS, "answer", TimeoutJudge(),
                                 reference_evidence=REFERENCES)
    assert result["score"] is None
    assert result["details"]["failure_category"] == "call_failed"
    truncated = eval_context_recall("query", [{"content": "x" * 801}],
                                    llm=TimeoutJudge(), reference_evidence=REFERENCES)
    assert truncated["score"] is None
    assert truncated["details"]["failure_category"] == "invalid_judgment"


@pytest.mark.parametrize("response, category", [
    ('{"judgments": []}', "invalid_judgment"),
    ("not-json", "invalid_response"),
])
def test_recall_rejects_incomplete_judge_output(response, category):
    class InvalidJudge:
        model = "synthetic-judge"

        def chat(self, messages, **kwargs):
            return {"content": response}

    result = eval_context_recall("query", DOCS, reference_evidence=REFERENCES, llm=InvalidJudge())
    assert result["score"] is None
    assert result["details"]["failure_category"] == category


def test_e2e_reference_recall_is_saved_and_missing_references_do_not_enter_mean(tmp_path, monkeypatch):
    class Agent:
        llm = Judge()

        def ask(self, query, profiles=None):
            return {"answer": "answer", "sources": DOCS, "stats": {}}

    for name in ("eval_context_precision", "eval_faithfulness", "eval_relevancy", "eval_hallucination"):
        monkeypatch.setattr(eval_e2e, name, lambda *args, **kwargs: {"score": 0.8})
    questions = [{"id": "E1", "query": "query", "domain": "test", "difficulty": "基础"}]
    questions.append({**questions[0], "id": "E2", "reference_evidence": REFERENCES})
    path = tmp_path / "results.json"
    results = eval_e2e.run_evaluation(Agent(), questions, use_llm=True, output_file=path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved[0]["scores"]["context_recall"]["status"] == "unscored_missing_reference"
    assert saved[1]["scores"]["context_recall"]["score"] == 0.6667
    assert eval_e2e._compute_stats(results)["avg_scores"]["context_recall"] == 0.6667
