import json

from knowledge_graph import (collect_graph_evidence, extract_document_graph, extract_semantic_graph_candidates,
                             graph_impact, scan_graph_conflicts)
from memory import ConversationMemory


def _document(memory: ConversationMemory, tmp_path):
    user = memory.register_user("graph-owner@example.com", "Correct-Horse-30", "Graph Owner")
    kb = memory.create_knowledge_base(user["tenant_id"], "法规资料", profile="general")
    path = tmp_path / "gbt22239.md"
    path.write_text(
        "《网络安全法》\nGB/T 22239-2019\n8.1.4 访问控制\n违反本规定的，责令改正并处以罚款。",
        encoding="utf-8",
    )
    document_id = "doc-graph-001"
    memory.register_document(document_id, "GB/T 22239-2019 网络安全等级保护基本要求", "通用", "general",
                             "tenant", user["tenant_id"], user["id"], user["agent_id"], kb["id"])
    memory.mark_document_indexed(document_id, str(path))
    assert memory.update_document_lifecycle(document_id, "published", user["tenant_id"])
    return user, memory.get_document(document_id, user["tenant_id"])


def test_graph_extraction_is_review_first_and_auditable(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph.db"))
    user, document = _document(memory, tmp_path)
    run = extract_document_graph(memory, document, user["id"])
    assert run["status"] == "pending_review"
    assert run["entity_count"] >= 4
    assert run["relation_count"] >= 3

    entities = memory.list_graph_entities(user["tenant_id"], document["knowledge_base_id"], "pending_review")
    relations = memory.list_graph_relations(user["tenant_id"], document["knowledge_base_id"], "pending_review")
    assert any(item["predicate"] == "has_clause" for item in relations)
    assert any(item["predicate"] == "references_standard" for item in relations)
    standard = next(item for item in entities if item["entity_type"] == "standard")
    assert graph_impact(memory, user["tenant_id"], standard["id"], document["knowledge_base_id"])["entity"] is None

    assert memory.update_graph_entity_status(standard["id"], user["tenant_id"], "approved", user["id"])
    relation = next(item for item in relations if item["object_id"] == standard["id"])
    assert memory.update_graph_relation_status(relation["id"], user["tenant_id"], "approved", user["id"])
    impact = graph_impact(memory, user["tenant_id"], standard["id"], document["knowledge_base_id"])
    assert impact["entity"]["name"].lower().startswith("gb/t 22239")
    assert impact["affected_documents"] == [document["id"]]

    previous = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "standard",
                                          "GB/T 22239-2008", {}, document["id"], user["id"])
    memory.update_graph_entity_status(previous["id"], user["tenant_id"], "approved", user["id"])
    assert graph_impact(memory, user["tenant_id"], standard["id"], document["knowledge_base_id"])["conflicts"]


def test_graph_scope_prevents_cross_workspace_reads(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph.db"))
    user, document = _document(memory, tmp_path)
    extract_document_graph(memory, document, user["id"])
    other = memory.register_user("graph-other@example.com", "Correct-Horse-30", "Graph Other")
    assert memory.list_graph_entities(other["tenant_id"], document["knowledge_base_id"]) == []
    entity = memory.list_graph_entities(user["tenant_id"], document["knowledge_base_id"])[0]
    assert memory.get_graph_entity(entity["id"], other["tenant_id"]) is None


def test_graph_evidence_requires_approved_relation_and_authorized_retrieval_doc(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-evidence.db"))
    user, document = _document(memory, tmp_path)
    extract_document_graph(memory, document, user["id"])
    pending = collect_graph_evidence(
        memory, user["tenant_id"], document["knowledge_base_id"],
        "GB/T 22239-2019", [{"document_id": document["id"]}],
    )
    assert pending["status"] == "empty"

    entities = memory.list_graph_entities(user["tenant_id"], document["knowledge_base_id"], "pending_review")
    standard = next(item for item in entities if item["entity_type"] == "standard")
    relation = next(item for item in memory.list_graph_relations(
        user["tenant_id"], document["knowledge_base_id"], "pending_review"
    ) if item["object_id"] == standard["id"])
    memory.update_graph_entity_status(standard["id"], user["tenant_id"], "approved", user["id"])
    memory.update_graph_relation_status(relation["id"], user["tenant_id"], "approved", user["id"])

    docs = [{"document_id": document["id"], "file_name": "source.md"}]
    before = list(docs)
    evidence = collect_graph_evidence(
        memory, user["tenant_id"], document["knowledge_base_id"],
        "GB/T 22239-2019", docs,
    )
    assert evidence["status"] == "approved_evidence"
    assert evidence["source_document_ids"] == [document["id"]]
    assert evidence["relations"][0]["label"] == "已审核图谱证据"
    assert docs == before


def test_graph_evidence_matches_normalized_chinese_entity_name(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-cn.db"))
    user, document = _document(memory, tmp_path)
    entity = memory.create_graph_entity(
        user["tenant_id"], document["knowledge_base_id"], "document", "《网络安全法》", {}, document["id"], user["id"]
    )
    source = memory.create_graph_entity(
        user["tenant_id"], document["knowledge_base_id"], "document", "制度来源", {}, document["id"], user["id"]
    )
    relation = memory.create_graph_relation(
        user["tenant_id"], document["knowledge_base_id"], source["id"], "mentions", entity["id"],
        {"evidence": "同文档法规引用"}, document["id"], 0.8, user["id"]
    )
    for item_id in (entity["id"], source["id"]):
        memory.update_graph_entity_status(item_id, user["tenant_id"], "approved", user["id"])
    memory.update_graph_relation_status(relation["id"], user["tenant_id"], "approved", user["id"])
    result = collect_graph_evidence(
        memory, user["tenant_id"], document["knowledge_base_id"], "网络安全法",
        [{"document_id": document["id"]}],
    )
    assert result["status"] == "approved_evidence"


def test_graph_evidence_excludes_out_of_scope_sources(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-scope.db"))
    user, document = _document(memory, tmp_path)
    extract_document_graph(memory, document, user["id"])
    entities = memory.list_graph_entities(user["tenant_id"], document["knowledge_base_id"], "pending_review")
    standard = next(item for item in entities if item["entity_type"] == "standard")
    relation = next(item for item in memory.list_graph_relations(
        user["tenant_id"], document["knowledge_base_id"], "pending_review"
    ) if item["object_id"] == standard["id"])
    memory.update_graph_entity_status(standard["id"], user["tenant_id"], "approved", user["id"])
    memory.update_graph_relation_status(relation["id"], user["tenant_id"], "approved", user["id"])

    assert collect_graph_evidence(
        memory, user["tenant_id"], document["knowledge_base_id"],
        "GB/T 22239-2019", [{"document_id": "different-authorized-doc"}],
    )["status"] == "empty"


def test_graph_impact_marks_cross_clause_obligation_conflict_for_review(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-conflict.db"))
    user, document = _document(memory, tmp_path)
    second_id = "doc-graph-002"
    first_doc = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "document", "制度 A", {}, document["id"], user["id"])
    second_doc = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "document", "制度 B", {}, second_id, user["id"])
    clause = memory.create_graph_entity(
        user["tenant_id"], document["knowledge_base_id"], "clause", "第八条", {}, document["id"], user["id"]
    )
    r1 = memory.create_graph_relation(
        user["tenant_id"], document["knowledge_base_id"], first_doc["id"], "contains_clause", clause["id"],
        {"evidence": "第八条：应当建立访问控制制度", "obligation_marker": "require"}, document["id"], 0.8, user["id"]
    )
    r2 = memory.create_graph_relation(
        user["tenant_id"], document["knowledge_base_id"], second_doc["id"], "contains_clause", clause["id"],
        {"evidence": "第八条：不得建立该访问控制制度", "obligation_marker": "prohibit"}, second_id, 0.8, user["id"]
    )
    for entity_id in (first_doc["id"], second_doc["id"], clause["id"]):
        memory.update_graph_entity_status(entity_id, user["tenant_id"], "approved", user["id"])
    for relation_id in (r1["id"], r2["id"]):
        memory.update_graph_relation_status(relation_id, user["tenant_id"], "approved", user["id"])

    conflicts = graph_impact(memory, user["tenant_id"], clause["id"], document["knowledge_base_id"])["conflicts"]
    assert any(item["type"] == "cross_clause_obligation_conflict" and item["review_required"] for item in conflicts)


def test_graph_conflict_scan_is_deduplicated_and_review_only(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-conflict-scan.db"))
    user, document = _document(memory, tmp_path)
    first_doc = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "document", "制度 A", {}, document["id"], user["id"])
    second_doc = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "document", "制度 B", {}, "doc-graph-002", user["id"])
    clause = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "clause", "第九条", {}, document["id"], user["id"])
    relations = [
        memory.create_graph_relation(user["tenant_id"], document["knowledge_base_id"], first_doc["id"], "contains_clause", clause["id"], {"evidence": "应当建立制度", "obligation_marker": "require"}, document["id"], .8, user["id"]),
        memory.create_graph_relation(user["tenant_id"], document["knowledge_base_id"], second_doc["id"], "contains_clause", clause["id"], {"evidence": "不得建立制度", "obligation_marker": "prohibit"}, "doc-graph-002", .8, user["id"]),
    ]
    for entity_id in (first_doc["id"], second_doc["id"], clause["id"]):
        memory.update_graph_entity_status(entity_id, user["tenant_id"], "approved", user["id"])
    for relation in relations:
        memory.update_graph_relation_status(relation["id"], user["tenant_id"], "approved", user["id"])
    result = scan_graph_conflicts(memory, user["tenant_id"], document["knowledge_base_id"])
    assert result["status"] == "review_required"
    assert result["summary"]["total"] == len(result["items"])
    assert all(item["review_required"] for item in result["items"])
    assert sorted(result["items"][0]["source_document_ids"]) == sorted(["doc-graph-002", document["id"]])


def test_semantic_graph_candidates_require_known_entities_and_verifiable_evidence(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-semantic.db"))
    user, document = _document(memory, tmp_path)
    extract_document_graph(memory, document, user["id"])
    entities = [item for item in memory.list_graph_entities(user["tenant_id"], document["knowledge_base_id"])
                if item["source_document_id"] == document["id"]]
    doc_entity = next(item for item in entities if item["entity_type"] == "document")
    standard = next(item for item in entities if item["entity_type"] == "standard")

    class SemanticLlm:
        model = "semantic-test"

        def chat(self, *_args, **_kwargs):
            return {"content": json.dumps({"relations": [
                {"subject_id": doc_entity["id"], "predicate": "references", "object_id": standard["id"],
                 "evidence": "GB/T 22239-2019", "confidence": .96},
                {"subject_id": "unknown", "predicate": "requires", "object_id": standard["id"],
                 "evidence": "GB/T 22239-2019", "confidence": .8},
                {"subject_id": doc_entity["id"], "predicate": "requires", "object_id": standard["id"],
                 "evidence": "文档中不存在的虚构证据", "confidence": .8},
            ]}, ensure_ascii=False)}

    result = extract_semantic_graph_candidates(memory, document, SemanticLlm(), user["id"])
    assert result["status"] == "pending_review"
    assert result["created"] == 1
    assert result["rejected"] == 2
    relation = result["relations"][0]
    assert relation["status"] == "pending_review"
    assert relation["properties"]["extraction"] == "llm_semantic_candidate"


def test_graph_network_contains_only_relation_endpoints(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-network.db"))
    user, document = _document(memory, tmp_path)
    extract_document_graph(memory, document, user["id"])
    entities = memory.list_graph_entities(user["tenant_id"], document["knowledge_base_id"], "pending_review")
    relations = memory.list_graph_relations(user["tenant_id"], document["knowledge_base_id"], "pending_review")
    endpoint_ids = {item["subject_id"] for item in relations} | {item["object_id"] for item in relations}
    assert endpoint_ids
    assert endpoint_ids.issubset({item["id"] for item in entities})


def test_graph_review_recommendations_separate_high_confidence_and_noise(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-review.db"))
    user, document = _document(memory, tmp_path)
    source = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "document", "来源", {}, document["id"], user["id"])
    standard = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "standard", "GB/T 22239-2019", {"extraction": "standard_identifier"}, document["id"], user["id"])
    clause = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "clause", "8.1", {}, document["id"], user["id"])
    high = memory.create_graph_relation(user["tenant_id"], document["knowledge_base_id"], source["id"], "mentions", standard["id"], {"extraction": "standard_identifier"}, document["id"], .95, user["id"])
    noise = memory.create_graph_relation(user["tenant_id"], document["knowledge_base_id"], source["id"], "has_clause", clause["id"], {"extraction": "same_document_cooccurrence"}, document["id"], .58, user["id"])
    result = memory.graph_review_recommendations(user["tenant_id"], document["knowledge_base_id"])
    assert result["summary"]["high_confidence"] == 1
    assert result["summary"]["noise_or_low_confidence"] == 1
    assert result["high_confidence"][0]["id"] == high["id"]
    assert result["noise_or_low_confidence"][0]["id"] == noise["id"]


def test_batch_graph_review_requires_pending_in_scope_candidates(tmp_path):
    memory = ConversationMemory(str(tmp_path / "graph-batch.db"))
    user, document = _document(memory, tmp_path)
    source = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "document", "来源", {}, document["id"], user["id"])
    standard = memory.create_graph_entity(user["tenant_id"], document["knowledge_base_id"], "standard", "GB/T 22239-2019", {}, document["id"], user["id"])
    relation = memory.create_graph_relation(user["tenant_id"], document["knowledge_base_id"], source["id"], "mentions", standard["id"], {}, document["id"], .95, user["id"])
    result = memory.batch_update_graph_status(user["tenant_id"], document["knowledge_base_id"], "relation", [relation["id"], "outside"], "approved", user["id"])
    assert result["updated"] == 1
    assert result["skipped"] == 1
    assert memory.get_graph_relation(relation["id"], user["tenant_id"])["status"] == "approved"
