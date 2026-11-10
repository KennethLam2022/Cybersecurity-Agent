import json

import pytest

from _eval_context import eval_context_relevance


DOCS = [{"file_name": "security-guidance", "content": "Report time. Report impact. Report actions. Office supplies."}]
UNITS = ["Report time.", "Report impact.", "Report actions.", "Office supplies."]


class Judge:
    model = "synthetic-judge"

    def __init__(self, response):
        self.response = response

    def chat(self, messages, **kwargs):
        assert "SECRET ANSWER" not in messages[0]["content"]
        return {"content": json.dumps(self.response)}


def judgments(labels):
    return {"documents": [{"rank": 1, "units": [
        {"text": text, "relevant": label} for text, label in zip(UNITS, labels)
    ]}]}


@pytest.mark.parametrize("labels, expected", [
    ([True, True, True, True], 1), ([True, True, True, False], 0.75),
    ([False, False, False, False], 0),
])
def test_relevance_scores_information_ratio_without_using_answer(labels, expected):
    judge = Judge(judgments(labels))
    result = eval_context_relevance("What must an incident report contain?", DOCS, llm=judge)
    second = eval_context_relevance("What must an incident report contain?", DOCS, "SECRET ANSWER", llm=judge)
    assert result == second
    assert result["score"] == expected
    assert result["details"]["unit_count"] == 4
    assert result["evaluator"]["model_id"] == "synthetic-judge"
    assert len({unit["unit_id"] for unit in result["details"]["judgments"]}) == 4
    assert "Office supplies" not in json.dumps(result)


def test_relevance_deduplicates_identical_units_across_documents():
    response = {"documents": [
        {"rank": 1, "units": [{"text": "Report time.", "relevant": True}]},
        {"rank": 2, "units": [{"text": "Report time.", "relevant": True}]},
    ]}
    result = eval_context_relevance("query", [DOCS[0], DOCS[0]], llm=Judge(response))
    assert result["score"] == 1
    assert result["details"]["unit_count"] == 1


@pytest.mark.parametrize("docs, response, expected_status", [
    ([], {}, "unscored_empty_retrieval"),
    ([{"content": "# Heading"}], {"documents": [{"rank": 1, "units": []}]}, "unscored_no_evaluable_units"),
    (DOCS, {"documents": [{"rank": 1, "units": [{"text": UNITS[0]}]}]}, "unscored_missing_annotation"),
    (DOCS, {"documents": []}, "judge_error"),
    (DOCS, {"documents": [{"rank": 1, "units": [{"text": "invented fact", "relevant": True}]}]}, "judge_error"),
])
def test_relevance_distinguishes_empty_unlabeled_and_invalid_output(docs, response, expected_status):
    result = eval_context_relevance("query", docs, llm=Judge(response))
    assert result["score"] is None
    assert result["status"] == expected_status


def test_relevance_judge_timeout_and_missing_judge_do_not_invent_zero():
    class TimeoutJudge:
        model = "synthetic-judge"

        def chat(self, *args, **kwargs):
            raise TimeoutError("unavailable")

    result = eval_context_relevance("query", DOCS, llm=TimeoutJudge())
    assert result["score"] is None
    assert result["details"]["failure_category"] == "call_failed"
    assert eval_context_relevance("query", DOCS)["status"] == "unscored_missing_annotation"


def test_relevance_rejects_truncated_input_before_judge():
    result = eval_context_relevance("query", [{"content": "x" * 801}], llm=Judge({}))
    assert result["score"] is None
    assert result["details"]["failure_category"] == "invalid_judgment"


def test_relevance_rejects_conflicting_duplicates():
    response = {"documents": [{"rank": 1, "units": [
        {"text": "Report time.", "relevant": True},
        {"text": "Report time.", "relevant": False},
    ]}]}
    result = eval_context_relevance("query", DOCS, llm=Judge(response))
    assert result["score"] is None
    assert result["details"]["failure_category"] == "invalid_judgment"


def test_relevance_reports_invalid_json_and_records_judge_usage():
    class InvalidJudge:
        model = "synthetic-judge"

        def chat(self, *args, **kwargs):
            return {"content": "not-json", "usage": {"total_tokens": 20}}

    usage = []
    result = eval_context_relevance("query", DOCS, llm=InvalidJudge(),
                                   usage_sink=lambda response, model: usage.append(response["usage"]))
    assert usage == [{"total_tokens": 20}]
    assert result["score"] is None
    assert result["details"]["failure_category"] == "invalid_response"
