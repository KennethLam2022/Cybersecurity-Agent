from agent_eval.builtin_cases import load_builtin_cases


def test_builtin_agent_eval_cases_are_general_security_core():
    cases = load_builtin_cases()

    assert len(cases) == 30
    assert {case["profile"] for case in cases} == {"general"}
    assert {case["case_type"] for case in cases} == {
        "router", "retrieval", "answer_quality", "safety", "conversation"
    }
    assert not any("运营商" in case["query"] if isinstance(case["query"], str) else False for case in cases)
