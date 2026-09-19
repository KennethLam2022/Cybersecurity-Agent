"""Build a side-by-side clause-aware RAG index without replacing the active index.

Examples:
  python rebuild_clause_index.py --dry-run
  python rebuild_clause_index.py --build
  python rebuild_clause_index.py --build --limit 20
"""
from __future__ import annotations

import argparse
import json
import logging
import shutil
from pathlib import Path

import chromadb
from chromadb.config import Settings
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings

from legal_chunking import chunk_markdown_document
from index_contract import index_config


logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[3]
RAG = ROOT / "RAG_DATA"
BASE = RAG / "03_cleaned"
DEFAULT_TARGET = RAG / "04_vector_store_clause"


def collect_files() -> list[Path]:
    files: list[Path] = []
    for category in sorted(BASE.iterdir()):
        if not category.is_dir() or category.name in {"test_output", "_deprecated"}:
            continue
        files.extend(sorted(category.glob("*.md")))
    return files


def sidecar(path: Path) -> dict:
    meta_path = path.with_suffix(".meta.json")
    if not meta_path.exists():
        return {}
    try:
        data = json.loads(meta_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def collect_chunks(files: list[Path]) -> tuple[list[dict], dict]:
    chunks: list[dict] = []
    stats = {"documents": len(files), "chunks": 0, "clause_chunks": 0, "section_chunks": 0}
    for path in files:
        metadata = sidecar(path)
        document_id = str(metadata.get("document_id") or path.stem)
        source_name = str(metadata.get("file_name") or path.stem)
        category = str(metadata.get("category") or path.parent.name)
        for index, item in enumerate(chunk_markdown_document(path.read_text(encoding="utf-8"), source_name, 800)):
            suffix = item.get("clause") or f"p{item.get('piece_index', index)}"
            chunk = {
                "content": item["content"],
                "file_name": source_name,
                "category": category,
                "section": item["section"],
                "clause": item.get("clause", ""),
                "chunk_type": item.get("chunk_type", "section"),
                "chunk_id": f"{document_id}__s{item['section_index']}__{suffix}",
                "parent_id": f"{document_id}__s{item['section_index']}",
                "document_id": document_id,
                "profile": str(metadata.get("profile", "")),
                "scope": str(metadata.get("scope", "")),
                "industry": str(metadata.get("industry", "")),
                "profile_confidence": metadata.get("profile_confidence", 0),
                "visibility": str(metadata.get("visibility") or "public"),
                "tenant_id": str(metadata.get("tenant_id", "")),
                "owner_user_id": str(metadata.get("owner_user_id", "")),
                "agent_id": str(metadata.get("agent_id", "")),
                "knowledge_base_id": str(metadata.get("knowledge_base_id", "")),
            }
            chunks.append(chunk)
            stats["chunks"] += 1
            stats[f"{item.get('chunk_type', 'section')}_chunks"] += 1
    return chunks, stats


def build_index(chunks: list[dict], target: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    config = index_config()
    embeddings = OllamaEmbeddings(model=config["embedding_model"], base_url=config["embedding_base_url"])
    texts = [item["content"] for item in chunks]
    ids = [item["chunk_id"] for item in chunks]
    metadatas = [{key: value for key, value in item.items() if key not in {"content"}} for item in chunks]
    vectors = embeddings.embed_documents(texts)

    parents: dict[str, dict] = {}
    for item in chunks:
        parent_id = item["parent_id"]
        parent = parents.setdefault(parent_id, {
            "text": "",
            "file_name": item["file_name"],
            "document_id": item.get("document_id", ""),
            "category": item["category"],
            "section": item["section"],
            "profile": item.get("profile", ""),
            "scope": item.get("scope", ""),
            "industry": item.get("industry", ""),
            "visibility": item.get("visibility", "public"),
            "tenant_id": item.get("tenant_id", ""),
            "owner_user_id": item.get("owner_user_id", ""),
            "agent_id": item.get("agent_id", ""),
            "knowledge_base_id": item.get("knowledge_base_id", ""),
        })
        body = item["content"]
        if body not in parent["text"]:
            parent["text"] = f"{parent['text']}\n\n{body}".strip()
    (target / "parent_texts.json").write_text(json.dumps(parents, ensure_ascii=False, indent=2), encoding="utf-8")

    chroma_dir = target / "chroma_db"
    client = chromadb.PersistentClient(path=str(chroma_dir), settings=Settings(anonymized_telemetry=False))
    collection = client.create_collection("cyber_security_clause", metadata={"hnsw:space": config["chroma_space"], "index_contract_version": config["contract_version"], "embedding_model": config["embedding_model"]})
    for start in range(0, len(ids), 500):
        end = min(start + 500, len(ids))
        collection.add(ids=ids[start:end], embeddings=vectors[start:end], documents=texts[start:end], metadatas=metadatas[start:end])

    faiss_dir = target / "faiss_index"
    db = FAISS.from_texts(texts=texts, embedding=embeddings, metadatas=metadatas, ids=ids)
    db.save_local(str(faiss_dir))
    (target / "manifest.json").write_text(json.dumps({"chunk_strategy": "clause-aware-v1", "stats": {"chunks": len(chunks)}}, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="重建条款级候选索引，不覆盖当前索引")
    parser.add_argument("--dry-run", action="store_true", help="只统计文档和切片，不生成向量")
    parser.add_argument("--build", action="store_true", help="生成独立候选 Chroma/FAISS 索引")
    parser.add_argument("--limit", type=int, default=0, help="只处理前 N 份文档，用于试运行")
    parser.add_argument("--target-dir", default=str(DEFAULT_TARGET), help="候选索引输出目录")
    args = parser.parse_args()
    files = collect_files()
    if args.limit > 0:
        files = files[:args.limit]
    chunks, stats = collect_chunks(files)
    logger.info("文档=%s, chunks=%s, 条款chunks=%s, 章节chunks=%s", stats["documents"], stats["chunks"], stats["clause_chunks"], stats["section_chunks"])
    if args.build:
        build_index(chunks, Path(args.target_dir))
        logger.info("候选索引已生成: %s", args.target_dir)
    elif not args.dry_run:
        parser.error("请指定 --dry-run 或 --build")


if __name__ == "__main__":
    main()
