from agent_eval.judge_calibration import build_judge_calibration_report


def test_calibration_report_uses_only_human_reviewed_scores():
    report = build_judge_calibration_report([
        {
            "result_key": "r1", "judge_scores": {"faithfulness": 0.9, "relevancy": 0.7},
            "human_scores": {"faithfulness": 0.8, "relevancy": 0.3}, "reviewer": "reviewer-a",
        },
        {
            "result_key": "r2", "judge_scores": {"faithfulness": 0.1},
            "human_scores": {},
        },
    ])

    assert report["reviewed_count"] == 1
    assert report["compared_score_count"] == 2
    assert report["agreement_rate"] == 0.5
    assert report["mean_absolute_error"] == 0.25
    assert report["per_metric"]["faithfulness"]["mean_absolute_error"] == 0.1
    assert report["disagreements"][0]["result_key"] == "r1"
    assert report["disagreements"][0]["differences"][0]["metric"] == "relevancy"


def test_calibration_report_keeps_missing_scores_out_of_denominator():
    report = build_judge_calibration_report([
        {"result_key": "r1", "judge_scores": {"faithfulness": 0.9}, "human_scores": {"faithfulness": 0.9}},
    ])

    assert report["compared_score_count"] == 1
    assert report["per_metric"]["relevancy"]["agreement_rate"] is None
