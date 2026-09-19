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


def test_retrieval_eval_uses_each_item_profile_for_search():
    module = _load("eval_retrieval_profile_execution", "_eval_retrieval.py")

    class FakeRetriever:
        def __init__(self):
            self.profiles = []

        def search(self, *args, **kwargs):
            self.profiles.append(kwargs.get("profiles"))
            return []

    retriever = FakeRetriever()
    module._eval_mode(
        [
            {"query": "通用", "expected": "x"},
            {"query": "通信", "expected": "x", "profile": "industry/telecom"},
        ],
        retriever,
        use_rerank=False,
        use_hybrid=True,
        sources=("faiss",),
    )

    assert retriever.profiles == [{"general"}, {"industry/telecom"}]


def test_retrieval_eval_result_marks_mixed_profiles(monkeypatch):
    module = _load("eval_retrieval_profile_result", "_eval_retrieval.py")

    class FakeRetriever:
        def __init__(self, **kwargs):
            self._faiss_db = None
            self._chroma_collection = None

        def search(self, *args, **kwargs):
            return []

    class FakeMemory:
        def save_retrieval_eval(self, **kwargs):
            return None

    monkeypatch.setitem(__import__("sys").modules["retriever"].__dict__, "CyberRetriever", FakeRetriever)
    result = module.evaluate_with_items(
        [
            {"query": "通用", "expected": "x"},
            {"query": "通信", "expected": "x", "profile": "industry/telecom"},
        ],
        FakeMemory(),
    )

    assert result["evaluation_profile"] == "mixed"
    assert result["profiles"] == ["general", "industry/telecom"]


def test_retrieval_failure_diagnosis_distinguishes_miss_and_low_rank():
    module = _load("eval_retrieval_diagnosis", "_eval_retrieval.py")

    miss = module.classify_retrieval_result(
        [{"file_name": "other.md", "content": "无关内容"}],
        "目标标准", 0, 0, None,
    )
    assert miss["failure_class"] == "retrieval_miss"
    assert miss["matched_count"] == 0

    low_rank_docs = [
        {"file_name": "noise.md", "content": "噪声"},
        {"file_name": "target.md", "content": "目标标准"},
    ]
    low_rank = module.classify_retrieval_result(low_rank_docs, "目标标准", 1, 1, 2)
    assert low_rank["failure_class"] in {"pass", "retrieval_noise"}
