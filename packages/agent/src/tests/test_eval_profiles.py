import importlib.util
from pathlib import Path


SRC = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SRC / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_retrieval_default_set_is_general_and_telecom_is_explicit():
    module = _load("eval_retrieval_profiles", "_eval_retrieval.py")

    assert module.TEST_SET is module.GENERAL_TEST_SET
    assert not any("5G" in item["query"] or "中国移动" in item["query"] for item in module.TEST_SET)
    assert all(item["profile"] == "industry/telecom" for item in module.INDUSTRY_TELECOM_TEST_SET)


def test_agent_eval_default_questions_are_general():
    module = _load("eval_30_profiles", "eval_30_v3.py")

    assert module.QUESTIONS is module.GENERAL_QUESTIONS
    assert not any(
        "工信部" in item["query"] or "电信网" in item["query"] or "核心网" in item["query"]
        for item in module.QUESTIONS
    )
    assert all(item["profile"] == "industry/telecom" for item in module.INDUSTRY_TELECOM_QUESTIONS)
