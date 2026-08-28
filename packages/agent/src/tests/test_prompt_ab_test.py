from prompt_ab_test import run_prompt_ab_test


class FakeRetriever:
    def __init__(self):
        self.calls = 0

    def search_multi(self, *_args, **_kwargs):
        self.calls += 1
        return [{"file_name": "evidence.md", "category": "通用", "section": "1", "content": "控制措施"}]


class FakeAgent:
    def __init__(self):
        self.retriever = FakeRetriever()
        self.top_k = 5
        self.use_rerank = True
        self.calls = []

    def ask(self, **kwargs):
        self.calls.append(kwargs)
        prompt = kwargs["system_prompt_override"]
        answer = "包含控制措施" if prompt == "prompt-a" else "没有关键点"
        return {"answer": answer}


def test_prompt_ab_test_uses_same_retrieval_snapshot_and_only_changes_prompt():
    agent = FakeAgent()
    items = [{
        "id": 1, "query": "如何实施控制措施？", "category": "知识准确",
        "difficulty": "medium", "expected": {"required_all": ["控制措施"]},
    }]

    report = run_prompt_ab_test(
        agent,
        {"id": 1, "name": "A", "system_prompt": "prompt-a"},
        {"id": 2, "name": "B", "system_prompt": "prompt-b"},
        items,
    )

    assert agent.retriever.calls == 1
    assert len(agent.calls) == 2
    assert agent.calls[0]["retrieved_docs_override"] is agent.calls[1]["retrieved_docs_override"]
    assert agent.calls[0]["system_prompt_override"] == "prompt-a"
    assert agent.calls[1]["system_prompt_override"] == "prompt-b"
    assert report["winner"] == "A"
    assert report["paired_results"][0]["winner"] == "A"
