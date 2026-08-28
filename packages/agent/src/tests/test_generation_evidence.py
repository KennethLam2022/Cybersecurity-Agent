from generation_evidence import (
    append_mcp_fetch_evidence,
    append_mcp_search_evidence,
    build_generation_evidence_plan,
    collect_generation_references,
    generate_outline_from_evidence,
)
from memory import ConversationMemory


class FakeRetriever:
    def __init__(self, results=None, error=None):
        self.results = results or []
        self.error = error
        self.calls = []

    def search_multi(self, queries, **kwargs):
        self.calls.append({"queries": queries, **kwargs})
        if self.error:
            raise self.error
        return self.results


def test_generation_evidence_uses_profile_and_access_scope_without_content_copying():
    retriever = FakeRetriever([
        {"document_id": "doc-1", "file_name": "制度.md", "section": "第 1 章",
         "profile": "finance", "visibility": "tenant", "score": 0.8, "content": "private body"},
        {"document_id": "doc-1", "file_name": "制度.md", "section": "第 2 章", "score": 0.7},
    ])
    result = collect_generation_references(
        retriever, "起草网络安全制度", {"profile": "finance"}, "tenant-1", "user-1", "agent-1",
    )

    assert result["status"] == "retrieved"
    assert result["references"] == [{
        "document_id": "doc-1", "source_name": "制度.md", "section": "第 1 章",
        "profile": "finance", "visibility": "tenant", "score": 0.8,
    }]
    assert retriever.calls[0]["profiles"] == {"finance"}
    assert retriever.calls[0]["access_scope"] == {
        "tenant_id": "tenant-1", "user_id": "user-1", "agent_id": "agent-1",
    }


def test_generation_evidence_degrades_without_sources_when_retrieval_fails():
    result = collect_generation_references(
        FakeRetriever(error=RuntimeError("index unavailable")), "制作网络安全PPT", {}, "t", "u", "a",
    )
    assert result["status"] == "error"
    assert result["references"] == []
    assert "index unavailable" in result["error"]


def test_generation_evidence_plan_skips_external_fetch_when_rag_is_sufficient():
    evidence = {
        "status": "retrieved",
        "references": [{"document_id": "1"}, {"document_id": "2"}],
        "context_blocks": [{"content": "授权资料 1"}, {"content": "授权资料 2"}],
    }
    plan = build_generation_evidence_plan(
        "制作网络安全 PPT https://example.com/article",
        "presentation",
        {"allow_external_research": True},
        evidence,
    )
    assert plan["rag_sufficient"] is True
    assert plan["next_step"] == "generate_from_rag"
    assert plan["external_allowed"] is True


def test_generation_evidence_plan_uses_search_when_external_research_has_no_url():
    evidence = {"status": "empty", "references": [], "context_blocks": []}
    plan = build_generation_evidence_plan(
        "制作关于关基安全的 PPT",
        "presentation",
        {"allow_external_research": True},
        evidence,
    )
    assert plan["next_step"] == "search_bing"
    assert plan["external_allowed"] is True


def test_generation_evidence_plan_uses_bing_search_only_when_external_research_is_allowed():
    plan = build_generation_evidence_plan(
        "制作关于关基安全的 PPT", "presentation",
        {"allow_external_research": True},
        {"status": "empty", "references": [], "context_blocks": []},
    )
    assert plan["next_step"] == "search_bing"
    assert plan["search_requested"] is True


def test_mcp_search_evidence_is_marked_external_and_unverified():
    result = append_mcp_search_evidence(
        {"status": "empty", "profile": "general", "references": [], "context_blocks": []},
        "关基安全 法律法规",
        lambda query, count: {"output": {"content": [{"type": "text", "text": "搜索结果"}]}},
    )
    assert result["mcp_calls"] == [{"tool": "bing_search", "status": "succeeded"}]
    assert result["references"][0]["external_unverified"] is True
    assert result["context_blocks"][0]["source_type"] == "mcp_search_unverified"
    plan = build_generation_evidence_plan(
        "制作关于关基安全的 PPT", "presentation", {"allow_external_research": True}, result,
    )
    assert plan["generation_allowed"] is True
    assert plan["external_evidence_present"] is True


def test_mcp_fetch_evidence_is_marked_external_and_unverified():
    calls = []

    def execute(url, max_length):
        calls.append((url, max_length))
        return {"output": {"content": [{"type": "text", "text": "网页资料内容"}]}}

    result = append_mcp_fetch_evidence(
        {"status": "empty", "profile": "general", "references": [], "context_blocks": []},
        ["https://example.com/security"],
        execute,
    )
    assert calls == [("https://example.com/security", 3500)]
    assert result["mcp_calls"] == [{"url": "https://example.com/security", "status": "succeeded"}]
    assert result["references"][0]["external_unverified"] is True
    assert result["context_blocks"][0]["source_type"] == "mcp_external_unverified"


class _GenerationLlm:
    model = "test-generation"

    def chat(self, messages, **_kwargs):
        assert "已授权 RAG 与外部待核验资料" in messages[0]["content"]
        return {
            "content": '{"title":"关基安全建设","mode":"presentation",'
            '"pages":['
            '{"page":1,"title":"背景","points":["依据 [资料1] 识别保护范围"]},'
            '{"page":2,"title":"风险","points":["依据 [资料2] 识别主要风险"]},'
            '{"page":3,"title":"控制","points":["依据 [资料1] 建立控制措施"]},'
            '{"page":4,"title":"实施","points":["依据 [资料2] 制定实施路线"]},'
            '{"page":5,"title":"结论","points":["待核验组织现状"]}],'
            '"evidence_gaps":["组织现状待核验"]}',
        }


def test_generation_outline_uses_bounded_evidence_and_strict_page_count(tmp_path):
    memory = ConversationMemory(str(tmp_path / "generation-prompt.db"))
    outline, meta = generate_outline_from_evidence(
        memory,
        _GenerationLlm(),
        "presentation",
        "制作关基安全 PPT",
        {"page_count": "5", "scope": "网络安全通用场景"},
        {
            "context_blocks": [
                {"source_name": "制度 A", "section": "第 1 章", "content": "资料 A"},
                {"source_name": "制度 B", "section": "第 2 章", "content": "资料 B"},
            ],
        },
    )
    assert len(outline["pages"]) == 5
    assert outline["fields"]["scope"] == "网络安全通用场景"
    assert meta["status"] == "generated"
