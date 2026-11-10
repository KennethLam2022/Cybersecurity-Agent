import pytest

from _eval_context import eval_context_precision
import eval_e2e


DOCS = [
    {"file_name": f"doc-{index}", "content": f"context {index}"}
    for index in range(1, 4)
]


@pytest.mark.parametrize(
    ("labels", "expected"),
    [
        ([True, False, True], 5 / 6),
        ([False, True, True], 7 / 12),
        ([False, False, False], 0),
    ],
)
def test_context_precision_uses_retrieval_rank_not_answer(labels, expected):
    result = eval_context_precision(
        "query",
        DOCS,
        answer="answer A",
        relevance_annotations=labels,
        annotation_source="gold-set-v1",
    )
    other_answer = eval_context_precision(
        "query",
        DOCS,
        answer="unrelated answer",
        relevance_annotations=labels,
        annotation_source="gold-set-v1",
    )

    assert result["score"] == pytest.approx(round(expected, 4))
    assert result["status"] == "scored"
    assert result["details"]["judgments"] == [
        {"rank": rank, "relevant": relevant}
        for rank, relevant in enumerate(labels, start=1)
    ]
    assert result["details"]["annotation_source"] == "gold-set-v1"
    assert other_answer == result


def test_context_precision_marks_empty_retrieval_unscored():
    result = eval_context_precision("query", [], llm=None)

    assert result["score"] is None
    assert result["status"] == "unscored_empty_retrieval"
    assert result["evaluator"]["method"] == "not_run"


def test_e2e_keeps_empty_retrieval_as_unscored_instead_of_runtime_error(
    tmp_path, monkeypatch
):
    class Agent:
        llm = None

        def ask(self, _query, profiles=None):
            return {"answer": "", "sources": [], "retrieved_docs": [], "stats": {}}

    def scored(*_args, **_kwargs):
        return {"score": 0.8, "explanation": "test", "details": {}}

    for metric in ("eval_context_recall", "eval_faithfulness", "eval_relevancy", "eval_hallucination"):
        monkeypatch.setattr(eval_e2e, metric, scored)

    result = eval_e2e.run_evaluation(
        Agent(),
        [{"id": "E01", "domain": "test", "difficulty": "基础", "query": "query"}],
        use_llm=False,
        output_file=tmp_path / "e2e.json",
    )[0]

    assert result["auto_status"] == "无来源" and "error" not in result
    assert result["scores"]["context_precision"]["status"] == "unscored_empty_retrieval"


def test_context_precision_requires_one_boolean_label_per_document():
    result = eval_context_precision(
        "query", DOCS, relevance_annotations=[True, False]
    )

    assert result["score"] is None
    assert result["status"] == "unscored_missing_annotation"
    assert result["details"]["missing_inputs"] == ["relevance_annotations"]


def test_context_precision_does_not_invent_annotation_source():
    result = eval_context_precision(
        "query", DOCS, relevance_annotations=[True, False, True]
    )

    assert result["score"] is None
    assert result["status"] == "unscored_missing_annotation"
    assert result["details"]["judgments"] == [
        {"rank": 1, "relevant": True},
        {"rank": 2, "relevant": False},
        {"rank": 3, "relevant": True},
    ]
    assert "annotation_source" not in result["details"]


def test_context_precision_rejects_unknown_options():
    with pytest.raises(TypeError, match="Unsupported precision options"):
        eval_context_precision("query", DOCS, unexpected=True)


def test_context_precision_uses_query_and_context_only_for_judging():
    class Judge:
        model = "test-judge"

        def chat(self, messages, **kwargs):
            prompt = messages[0]["content"]
            assert "query" in prompt
            assert "context 1" in prompt
            assert "SECRET ANSWER" not in prompt
            return {
                "content": (
                    '{"judgments": ['
                    '{"rank": 1, "relevant": true},'
                    '{"rank": 2, "relevant": false},'
                    '{"rank": 3, "relevant": true}]}'
                )
            }

    usage = []
    result = eval_context_precision(
        "query", DOCS, "SECRET ANSWER", Judge(), lambda *_: usage.append(True)
    )

    assert result["score"] == pytest.approx(round(5 / 6, 4))
    assert result["status"] == "scored"
    assert usage == [True]
    assert result["evaluator"] == {
        "method": "llm_judge",
        "model_id": "test-judge",
        "prompt_version": "context-precision-v1",
    }


def test_context_precision_does_not_turn_judge_failure_into_a_score():
    class BrokenJudge:
        model = "test-judge"

        def chat(self, *_args, **_kwargs):
            raise TimeoutError("judge unavailable")

    result = eval_context_precision("query", DOCS, llm=BrokenJudge())

    assert result["score"] is None
    assert result["status"] == "judge_error"
    assert result["details"]["failure_category"] == "call_failed"


@pytest.mark.parametrize(
    "docs",
    [
        [{"file_name": "long", "content": "x" * 801}],
        [
            {"file_name": f"doc-{index}", "content": "x" * 800}
            for index in range(1, 7)
        ],
    ],
)
def test_context_precision_rejects_truncated_judge_context(docs):
    class Judge:
        model = "test-judge"

        def chat(self, *_args, **_kwargs):
            pytest.fail("Judge must not score contexts it did not receive")

    result = eval_context_precision("query", docs, llm=Judge())

    assert result["score"] is None
    assert result["status"] == "judge_error"
    assert result["details"]["failure_category"] == "invalid_judgment"


def test_context_precision_rejects_incomplete_judge_labels():
    class IncompleteJudge:
        model = "test-judge"

        def chat(self, *_args, **_kwargs):
            return {"content": '{"judgments": [{"rank": 1, "relevant": true}]}'}

    result = eval_context_precision("query", DOCS, llm=IncompleteJudge())

    assert result["score"] is None
    assert result["status"] == "judge_error"
    assert result["details"]["failure_category"] == "invalid_judgment"
