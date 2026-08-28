"""Deterministic, review-first extraction for the SecureNexus knowledge graph."""
from __future__ import annotations

import re
import json
from pathlib import Path

_STANDARD = re.compile(r"\b(?:GB/T|GB/Z|GB|YD/T|YD|JR/T|GM/T|ISO/IEC|ISO)\s*[_ -]?\s*\d+(?:\.\d+)?(?:\s*[-_－]\s*\d{4})?", re.I)
_LEGAL = re.compile(r"《[^》\n]{2,80}(?:法|条例|办法|规定|规范|标准|指南)》")
_CLAUSE = re.compile(r"(?:第[一二三四五六七八九十百千万零\d]+条|\b\d+(?:\.\d+){1,4}\b)")
_PENALTY = re.compile(r"[^。；\n]{0,100}(?:罚款|没收|责令改正|暂停|吊销|处罚)[^。；\n]{0,100}")
_CLAUSE_CONFLICT_MARKERS = {
    "prohibit": re.compile(r"(?:不得|禁止|严禁|不应)"),
    "require": re.compile(r"(?:应当|必须|应|需要|可以)"),
}
_SEMANTIC_PREDICATES = {"references", "requires", "prohibits", "applies_to", "implements", "governs"}


def _read_document_text(document: dict) -> str:
    path = Path(str(document.get("cleaned_path") or ""))
    if not path.is_file():
        return str(document.get("source_name") or "")
    try:
        return path.read_text(encoding="utf-8", errors="ignore")[:300_000]
    except OSError:
        return str(document.get("source_name") or "")


def _unique(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for value in values:
        value = " ".join(value.strip().split())
        key = value.lower()
        if value and key not in seen:
            seen.add(key)
            output.append(value)
        if len(output) >= limit:
            break
    return output


def _clause_evidence(text: str, match: re.Match) -> tuple[str, str]:
    start = max(0, match.start() - 90)
    end = min(len(text), match.end() + 180)
    evidence = " ".join(text[start:end].replace("\r", " ").replace("\n", " ").split())
    marker = ""
    for name, pattern in _CLAUSE_CONFLICT_MARKERS.items():
        if pattern.search(evidence):
            marker = name
            break
    return evidence[:240], marker


def extract_document_graph(memory, document: dict, created_by: str = "admin") -> dict:
    """Extract deterministic candidates and leave every graph item pending review."""
    tenant_id = str(document.get("tenant_id") or "local-default")
    knowledge_base_id = str(document.get("knowledge_base_id") or "")
    document_id = str(document.get("id") or "")
    if not document_id:
        raise ValueError("文档缺少稳定 ID")
    run = memory.create_graph_extraction_run(tenant_id, knowledge_base_id, document_id, created_by)
    try:
        text = _read_document_text(document)
        doc_entity = memory.create_graph_entity(
            tenant_id, knowledge_base_id, "document", str(document.get("source_name") or document_id),
            {"profile": document.get("profile", "general"), "category": document.get("category", ""),
             "version": document.get("version", 1), "lifecycle_status": document.get("lifecycle_status", "")},
            document_id, created_by,
        )
        entities = [doc_entity]
        relations = []
        standards = _unique(_STANDARD.findall(text), 40)
        legal_docs = _unique(_LEGAL.findall(text), 40)
        clause_matches = list(_CLAUSE.finditer(text))
        clauses = _unique([match.group(0) for match in clause_matches], 80)
        penalties = _unique(_PENALTY.findall(text), 20)
        standard_entities = []
        legal_entities = []
        clause_entities = []
        for name in standards:
            entity = memory.create_graph_entity(tenant_id, knowledge_base_id, "standard", name,
                                                {"extraction": "standard_identifier"}, document_id, created_by)
            entities.append(entity)
            standard_entities.append(entity)
            relations.append(memory.create_graph_relation(tenant_id, knowledge_base_id, doc_entity["id"], "mentions", entity["id"],
                {"evidence": name}, document_id, 0.95, created_by))
        for name in legal_docs:
            entity = memory.create_graph_entity(tenant_id, knowledge_base_id, "document", name,
                                                {"extraction": "legal_reference"}, document_id, created_by)
            entities.append(entity)
            legal_entities.append(entity)
            relations.append(memory.create_graph_relation(tenant_id, knowledge_base_id, doc_entity["id"], "mentions", entity["id"],
                {"evidence": name}, document_id, 0.82, created_by))
        for name in clauses:
            match = next((item for item in clause_matches if item.group(0).strip() == name), None)
            evidence, marker = _clause_evidence(text, match) if match else (name, "")
            entity = memory.create_graph_entity(tenant_id, knowledge_base_id, "clause", name,
                                                {"extraction": "clause_identifier", "evidence": evidence,
                                                 "obligation_marker": marker}, document_id, created_by)
            entities.append(entity)
            clause_entities.append(entity)
            relations.append(memory.create_graph_relation(tenant_id, knowledge_base_id, doc_entity["id"], "contains_clause", entity["id"],
                {"evidence": evidence, "obligation_marker": marker}, document_id, 0.72, created_by))
        # Same-document links are reviewable co-occurrence candidates, not asserted truth.
        for standard in standard_entities:
            for clause in clause_entities[:20]:
                relations.append(memory.create_graph_relation(
                    tenant_id, knowledge_base_id, standard["id"], "has_clause", clause["id"],
                    {"evidence": "同一文档中共同出现，需人工确认标准与条款的正式归属。",
                     "extraction": "same_document_cooccurrence"}, document_id, 0.58, created_by,
                ))
            for legal in legal_entities[:20]:
                relations.append(memory.create_graph_relation(
                    tenant_id, knowledge_base_id, legal["id"], "references_standard", standard["id"],
                    {"evidence": "同一文档中共同出现，需人工确认法规引用关系。",
                     "extraction": "same_document_cooccurrence"}, document_id, 0.55, created_by,
                ))
        for name in penalties:
            entity = memory.create_graph_entity(tenant_id, knowledge_base_id, "penalty", name,
                                                {"extraction": "penalty_sentence"}, document_id, created_by)
            entities.append(entity)
            relations.append(memory.create_graph_relation(tenant_id, knowledge_base_id, doc_entity["id"], "mentions_penalty", entity["id"],
                {"evidence": name[:180]}, document_id, 0.65, created_by))
        industry = str((document.get("metadata") or {}).get("industry") or "").strip()
        if industry:
            entity = memory.create_graph_entity(tenant_id, knowledge_base_id, "industry", industry,
                                                {"extraction": "document_metadata"}, document_id, created_by)
            entities.append(entity)
            relations.append(memory.create_graph_relation(tenant_id, knowledge_base_id, doc_entity["id"], "applies_to", entity["id"],
                {"evidence": "document metadata"}, document_id, 0.9, created_by))
        memory.update_graph_extraction_run(run["id"], tenant_id, "pending_review", len({item["id"] for item in entities}), len({item["id"] for item in relations}))
        return memory.get_graph_extraction_run(run["id"], tenant_id) or run
    except Exception as exc:
        memory.update_graph_extraction_run(run["id"], tenant_id, "failed", error=str(exc))
        raise


def _json_object(text: object) -> dict:
    value = str(text or "")
    start, end = value.find("{"), value.rfind("}")
    if start < 0 or end <= start:
        return {}
    try:
        parsed = json.loads(value[start:end + 1])
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _normalized_evidence(value: str) -> str:
    return "".join(str(value or "").lower().split())


def extract_semantic_graph_candidates(memory, document: dict, llm, created_by: str = "admin",
                                      usage_sink=None) -> dict:
    """Ask a governed model for evidence-bound, review-only graph candidates."""
    tenant_id = str(document.get("tenant_id") or "local-default")
    knowledge_base_id = str(document.get("knowledge_base_id") or "")
    document_id = str(document.get("id") or "")
    if not document_id or not knowledge_base_id:
        raise ValueError("文档缺少图谱抽取范围")
    from reflection_engine import PROMPT_ASSET_DEFAULTS
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    asset = memory.get_active_prompt_asset("graph_relation_extraction")
    if llm is None or not asset.get("template"):
        return {"status": "skipped", "reason": "semantic_graph_model_unconfigured", "created": 0,
                "prompt_version": asset.get("version", 0)}
    entities = [item for item in memory.list_graph_entities(tenant_id, knowledge_base_id, limit=2000)
                if str(item.get("source_document_id") or "") == document_id]
    if len(entities) < 2:
        return {"status": "skipped", "reason": "deterministic_entities_required", "created": 0,
                "prompt_version": asset.get("version", 0)}
    text = _read_document_text(document)
    prompt = asset["template"].format(
        document_title=str(document.get("source_name") or document_id)[:300],
        entities=json.dumps([{"id": item["id"], "type": item["entity_type"], "name": item["name"]}
                             for item in entities], ensure_ascii=False),
        document_text=text[:50_000],
    )
    try:
        response = llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=1800, timeout=90)
        if usage_sink:
            usage_sink(response, getattr(llm, "model", ""))
    except Exception as exc:
        return {"status": "failed", "reason": str(exc)[:500], "created": 0, "prompt_version": asset.get("version", 0)}
    payload = _json_object(response.get("content", "") if isinstance(response, dict) else response)
    allowed_ids = {str(item["id"]) for item in entities}
    normalized_document = _normalized_evidence(text)
    created = []
    rejected = 0
    for candidate in list(payload.get("relations") or [])[:80]:
        if not isinstance(candidate, dict):
            rejected += 1; continue
        subject_id = str(candidate.get("subject_id") or "")
        object_id = str(candidate.get("object_id") or "")
        predicate = str(candidate.get("predicate") or "").strip().lower()
        evidence = " ".join(str(candidate.get("evidence") or "").split())[:240]
        if subject_id not in allowed_ids or object_id not in allowed_ids or subject_id == object_id or predicate not in _SEMANTIC_PREDICATES:
            rejected += 1; continue
        normalized = _normalized_evidence(evidence)
        if len(normalized) < 8 or normalized not in normalized_document:
            rejected += 1; continue
        qualifiers = candidate.get("qualifiers") if isinstance(candidate.get("qualifiers"), dict) else {}
        try:
            confidence = float(candidate.get("confidence", 0.5))
        except (TypeError, ValueError):
            confidence = 0.5
        created.append(memory.create_graph_relation(
            tenant_id, knowledge_base_id, subject_id, predicate, object_id,
            {"evidence": evidence, "qualifiers": qualifiers, "extraction": "llm_semantic_candidate",
             "prompt_version": asset.get("version", 0), "review_required": True},
            document_id, max(0.0, min(confidence, 0.85)), created_by, status="pending_review",
        ))
    memory.log_audit(tenant_id, created_by, str(document.get("agent_id") or ""), "graph.semantic.extract",
                     "document", document_id, {"created": len(created), "rejected": rejected,
                                                "prompt_version": asset.get("version", 0)})
    return {"status": "pending_review", "created": len(created), "rejected": rejected,
            "prompt_version": asset.get("version", 0), "relations": created}


def graph_impact(memory, tenant_id: str, entity_id: str, knowledge_base_id: str = "") -> dict:
    """Return approved direct relationships and deterministic version conflicts."""
    entity = memory.get_graph_entity(entity_id, tenant_id)
    if not entity or entity.get("status") != "approved":
        return {"entity": None, "relations": [], "affected_documents": [], "conflicts": []}
    relations = memory.list_graph_relations(tenant_id, knowledge_base_id, "approved", entity_id)
    document_ids = {item.get("source_document_id") for item in relations if item.get("source_document_id")}
    conflicts = []
    if entity.get("entity_type") == "standard":
        base = re.sub(r"\s*[-_－]\s*\d{4}$", "", entity["name"].lower()).strip()
        candidates = memory.list_graph_entities(tenant_id, knowledge_base_id, "approved", "standard")
        for candidate in candidates:
            candidate_base = re.sub(r"\s*[-_－]\s*\d{4}$", "", candidate["name"].lower()).strip()
            if candidate["id"] != entity_id and candidate_base == base and candidate["name"] != entity["name"]:
                conflicts.append({"type": "standard_version_conflict", "entity_id": candidate["id"],
                                  "name": candidate["name"], "message": "同一标准编号存在不同版本，需管理员确认适用版本。"})
    if entity.get("entity_type") == "clause":
        clause_relations = [item for item in relations if item.get("predicate") == "contains_clause"]
        markers = {}
        for relation in clause_relations:
            marker = str((relation.get("properties") or {}).get("obligation_marker") or "")
            source_id = str(relation.get("source_document_id") or "")
            if marker and source_id:
                markers.setdefault(marker, set()).add(source_id)
        if "prohibit" in markers and "require" in markers:
            conflicts.append({
                "type": "cross_clause_obligation_conflict",
                "entity_id": entity_id,
                "message": "同一条款候选证据出现禁止与要求两类义务标记，需人工比对文档版本、适用范围和原文。",
                "source_document_ids": sorted(markers["prohibit"] | markers["require"]),
                "status": "candidate_conflict",
                "review_required": True,
            })
    return {"entity": entity, "relations": relations, "affected_documents": sorted(document_ids), "conflicts": conflicts}


def scan_graph_conflicts(memory, tenant_id: str, knowledge_base_id: str = "", limit: int = 100) -> dict:
    """Scan approved graph evidence and return deduplicated review candidates.

    This is deliberately deterministic: it explains why a conflict was flagged,
    preserves source document IDs, and never changes graph status or answer data.
    """
    if not knowledge_base_id:
        return {"status": "empty", "items": [], "summary": {"total": 0}}
    entities = memory.list_graph_entities(tenant_id, knowledge_base_id, "approved", limit=2000)
    by_id = {str(item["id"]): item for item in entities}
    candidates: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for entity in entities:
        impact = graph_impact(memory, tenant_id, entity["id"], knowledge_base_id)
        for conflict in impact.get("conflicts", []):
            conflict_type = str(conflict.get("type") or "unknown")
            other_id = str(conflict.get("entity_id") or "")
            key = (conflict_type, str(entity["id"]), other_id)
            reverse = (conflict_type, other_id, str(entity["id"]))
            if key in seen or reverse in seen:
                continue
            seen.add(key)
            item = dict(conflict)
            item["entity_name"] = entity.get("name", "")
            item["entity_type"] = entity.get("entity_type", "")
            item["source_document_ids"] = sorted(set(item.get("source_document_ids") or impact.get("affected_documents") or []))
            if other_id and other_id in by_id:
                item["other_entity_name"] = by_id[other_id].get("name", "")
            item["review_required"] = True
            candidates.append(item)
    candidates.sort(key=lambda item: (item.get("type", ""), item.get("entity_name", ""), item.get("other_entity_name", "")))
    bounded = candidates[:max(1, min(int(limit), 500))]
    return {
        "status": "review_required" if bounded else "clear",
        "items": bounded,
        "summary": {"total": len(candidates), "returned": len(bounded), "review_required": len(candidates)},
        "message": "冲突仅为规则识别候选，必须结合原始文档、版本和适用范围人工复核。" if bounded else "当前已审核图谱未发现规则冲突候选。",
    }


def _graph_query_tokens(value: str) -> set[str]:
    """Return conservative normalized tokens for query/entity matching."""
    text = " ".join(str(value or "").lower().split())
    if not text:
        return set()
    compact = re.sub(r"[《》【】\[\]（）()“”\"'、，。；：:！？!?\s]+", "", text)
    tokens = {text, compact}
    tokens.update(item for item in re.split(r"[^a-z0-9/.-]+", text) if len(item) >= 2)
    if compact and len(compact) >= 2:
        tokens.add(compact)
    return tokens


def _graph_names_match(query: str, name: str) -> bool:
    query_tokens = _graph_query_tokens(query)
    name_tokens = _graph_query_tokens(name)
    if query_tokens.intersection(name_tokens):
        return True
    return any(len(token) >= 3 and (token in other or other in token)
               for token in query_tokens for other in name_tokens)


def collect_graph_evidence(memory, tenant_id: str, knowledge_base_id: str,
                           query: str, retrieved_docs: list[dict] | None = None,
                           limit: int = 12) -> dict:
    """Collect approved graph evidence without changing retrieval or access scope.

    A relation is eligible only when its source document is already present in the
    authorized retrieval result.  This makes the graph an explainable supplement,
    rather than a second retrieval path or a permission bypass.
    """
    retrieved_docs = list(retrieved_docs or [])
    authorized_ids = {
        str(doc.get("document_id") or doc.get("source_document_id") or "")
        for doc in retrieved_docs
        if not doc.get("external")
    }
    authorized_ids.discard("")
    empty = {
        "status": "empty", "entities": [], "relations": [], "conflicts": [],
        "source_document_ids": [], "message": "没有匹配到已审核且已授权的图谱证据。",
    }
    if not authorized_ids or not knowledge_base_id:
        return empty

    query_tokens = _graph_query_tokens(query)
    entities = memory.list_graph_entities(tenant_id, knowledge_base_id, "approved", limit=500)
    matched = [entity for entity in entities
               if _graph_names_match(query, entity.get("name", ""))]
    matched_ids = {str(entity["id"]) for entity in matched}
    if not matched_ids:
        return empty

    relations = memory.list_graph_relations(tenant_id, knowledge_base_id, "approved", limit=2000)
    selected = []
    source_ids = set()
    for relation in relations:
        source_id = str(relation.get("source_document_id") or "")
        if source_id not in authorized_ids:
            continue
        if str(relation.get("subject_id")) not in matched_ids and str(relation.get("object_id")) not in matched_ids:
            continue
        selected.append({
            "id": relation.get("id", ""),
            "subject": relation.get("subject_name", ""),
            "predicate": relation.get("predicate", ""),
            "object": relation.get("object_name", ""),
            "evidence": str((relation.get("properties") or {}).get("evidence") or "")[:240],
            "confidence": round(float(relation.get("confidence") or 0), 3),
            "source_document_id": source_id,
            "label": "已审核图谱证据",
        })
        source_ids.add(source_id)
        if len(selected) >= max(1, min(int(limit), 50)):
            break

    if not selected:
        return empty

    conflicts = []
    for entity in matched:
        impact = graph_impact(memory, tenant_id, entity["id"], knowledge_base_id)
        for conflict in impact.get("conflicts", []):
            conflicts.append({**conflict, "status": "candidate_conflict", "review_required": True})
    return {
        "status": "approved_evidence",
        "entities": [{"id": e["id"], "type": e["entity_type"], "name": e["name"]} for e in matched[:50]],
        "relations": selected,
        "conflicts": conflicts,
        "source_document_ids": sorted(source_ids),
        "message": "以下内容仅为已审核图谱补充证据，适用性和冲突仍需结合原始来源人工确认。",
    }
