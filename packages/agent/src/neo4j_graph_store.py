"""Optional Neo4j projection for SecureNexus knowledge graph.

SQLite remains the governance source of truth. Neo4j is a rebuildable
projection used for graph exploration and path queries.
"""
from __future__ import annotations

import os
from typing import Any

try:
    from neo4j import GraphDatabase
except ImportError:  # Optional for SQLite-only development and tests.
    GraphDatabase = None


class Neo4jGraphStore:
    def __init__(self) -> None:
        self.uri = os.environ.get("NEO4J_URI", "").strip()
        self.user = os.environ.get("NEO4J_USER", "neo4j").strip()
        self.password = os.environ.get("NEO4J_PASSWORD", "").strip()
        self.database = os.environ.get("NEO4J_DATABASE", "neo4j").strip() or "neo4j"
        self.enabled = os.environ.get("NEO4J_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
        self._driver = None
        self.last_error = ""

    @property
    def configured(self) -> bool:
        return bool(self.enabled and GraphDatabase and self.uri and self.user and self.password)

    def _get_driver(self):
        if not self.configured:
            return None
        if self._driver is None:
            self._driver = GraphDatabase.driver(self.uri, auth=(self.user, self.password))
        return self._driver

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def health(self) -> dict[str, Any]:
        if not self.enabled:
            return {"enabled": False, "available": False, "reason": "NEO4J_ENABLED 未开启"}
        if GraphDatabase is None:
            return {"enabled": True, "available": False, "reason": "未安装 neo4j Python 驱动"}
        if not self.uri or not self.password:
            return {"enabled": True, "available": False, "reason": "缺少 NEO4J_URI 或 NEO4J_PASSWORD"}
        try:
            self._get_driver().verify_connectivity()
            return {"enabled": True, "available": True, "uri": self.uri, "database": self.database}
        except Exception as exc:
            self.last_error = str(exc)[:500]
            return {"enabled": True, "available": False, "uri": self.uri, "database": self.database, "reason": self.last_error}

    def _write_rows(self, entities: list[dict], relations: list[dict]) -> dict[str, int]:
        driver = self._get_driver()
        if driver is None:
            raise RuntimeError("Neo4j 未配置或驱动未安装")
        entity_query = """
        UNWIND $items AS item
        MERGE (n:SecureNexusEntity {entity_id:item.entity_id})
        SET n.tenant_id=item.tenant_id, n.knowledge_base_id=item.knowledge_base_id,
            n.entity_type=item.entity_type, n.name=item.name, n.status=item.status,
            n.source_document_id=item.source_document_id
        """
        relation_query = """
        UNWIND $items AS item
        MATCH (s:SecureNexusEntity {entity_id:item.subject_id})
        MATCH (o:SecureNexusEntity {entity_id:item.object_id})
        MERGE (s)-[r:SECURENEXUS_RELATION {relation_id:item.relation_id}]->(o)
        SET r.tenant_id=item.tenant_id, r.knowledge_base_id=item.knowledge_base_id,
            r.predicate=item.predicate, r.status=item.status, r.confidence=item.confidence,
            r.source_document_id=item.source_document_id, r.extraction=item.extraction,
            r.evidence=item.evidence
        """
        with driver.session(database=self.database) as session:
            if entities:
                session.run(entity_query, items=entities).consume()
            if relations:
                session.run(relation_query, items=relations).consume()
        return {"entities": len(entities), "relations": len(relations)}

    def sync_from_memory(self, memory, tenant_id: str, knowledge_base_id: str, status: str = "") -> dict[str, Any]:
        entities = memory.list_graph_entities(tenant_id, knowledge_base_id, status, limit=2000)
        relations = memory.list_graph_relations(tenant_id, knowledge_base_id, status, limit=5000)
        entity_rows = [{"entity_id": e["id"], "tenant_id": tenant_id, "knowledge_base_id": knowledge_base_id,
                        "entity_type": e["entity_type"], "name": e["name"], "status": e["status"],
                        "source_document_id": e.get("source_document_id", "")} for e in entities]
        relation_rows = []
        for r in relations:
            props = r.get("properties") or {}
            relation_rows.append({"relation_id": r["id"], "tenant_id": tenant_id,
                                  "knowledge_base_id": knowledge_base_id, "subject_id": r["subject_id"],
                                  "object_id": r["object_id"], "predicate": r["predicate"], "status": r["status"],
                                  "confidence": float(r.get("confidence") or 0),
                                  "source_document_id": r.get("source_document_id", ""),
                                  "extraction": str(props.get("extraction") or ""),
                                  "evidence": str(props.get("evidence") or "")[:1000]})
        result = self._write_rows(entity_rows, relation_rows)
        return {"ok": True, "status": status or "all", **result}

    def network(self, tenant_id: str, knowledge_base_id: str, status: str = "approved", limit: int = 120) -> dict[str, Any] | None:
        if not self.configured:
            return None
        driver = self._get_driver()
        try:
            with driver.session(database=self.database) as session:
                rows = session.run("""
                    MATCH (s:SecureNexusEntity)-[r:SECURENEXUS_RELATION]->(o:SecureNexusEntity)
                    WHERE r.tenant_id=$tenant_id AND r.knowledge_base_id=$knowledge_base_id
                      AND ($status='' OR r.status=$status)
                    RETURN s.entity_id AS source, s.name AS source_label, s.entity_type AS source_type,
                           o.entity_id AS target, o.name AS target_label, o.entity_type AS target_type,
                           r.relation_id AS id, r.predicate AS label, r.confidence AS confidence,
                           r.status AS status ORDER BY r.confidence DESC LIMIT $limit
                """, tenant_id=tenant_id, knowledge_base_id=knowledge_base_id,
                    status=status, limit=max(1, min(int(limit), 500))).data()
            ids = {r["source"] for r in rows} | {r["target"] for r in rows}
            nodes = {}
            for row in rows:
                nodes[row["source"]] = {"id": row["source"], "label": row["source_label"], "type": row["source_type"]}
                nodes[row["target"]] = {"id": row["target"], "label": row["target_label"], "type": row["target_type"]}
            return {"nodes": list(nodes.values()), "edges": [{"id": r["id"], "source": r["source"], "target": r["target"],
                    "label": r["label"], "confidence": r["confidence"], "status": r["status"]} for r in rows],
                    "summary": {"node_count": len(ids), "edge_count": len(rows), "status": status, "backend": "neo4j"}}
        except Exception as exc:
            self.last_error = str(exc)[:500]
            return None


_STORE = Neo4jGraphStore()


def get_neo4j_graph_store() -> Neo4jGraphStore:
    return _STORE
