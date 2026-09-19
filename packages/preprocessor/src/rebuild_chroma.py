"""从 MD_Cleaned 重建 FAISS + Chroma 向量索引

用法：python rebuild_chroma.py

改进点：
  - 用 ollama.embed() 批量 API 替代 LangChain 逐条 embedding（快 10x+）
  - FAISS 和 Chroma 复用同一批预计算 embedding
"""
import tempfile
import os
import shutil
import logging
import json
import time
from pathlib import Path
import chromadb
from chromadb.config import Settings
import ollama
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings
from legal_chunking import chunk_markdown_document
from index_contract import normalize_metadata, write_manifest, index_config, record_index_stage

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BATCH_SIZE = 16

BASE = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "03_cleaned"
STORE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "04_vector_store"
_FAISS_DIR = str(STORE_DIR / "faiss_index")
_INDEX_CONFIG = index_config()
EMBED_MODEL = _INDEX_CONFIG["embedding_model"]
OLLAMA_URL = _INDEX_CONFIG["embedding_base_url"]

# LangChain 包装器（FAISS 需要 embedding 对象做查询）
lc_embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)

# 1. 收集所有 MD 文件
files = []
for cat_dir in sorted(BASE.iterdir()):
    if not cat_dir.is_dir() or cat_dir.name in ("test_output", "_deprecated", "_archive"):
        continue
    for md_file in sorted(cat_dir.glob("*.md")):
        files.append((str(md_file), cat_dir.name, md_file.stem))

logger.info(f"共 {len(files)} 份文档")

# 2. 切片


def chunk_document(file_path, cat, stem):
    text = Path(file_path).read_text(encoding="utf-8")
    sidecar = Path(file_path).with_suffix(".meta.json")
    file_metadata = {}
    if sidecar.exists():
        try:
            file_metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("读取文档元数据失败: %s", sidecar)
    version_warning = next((line.strip() for line in text.splitlines()[:5] if "⚠️" in line), "")
    chunks = []
    for index, item in enumerate(chunk_markdown_document(text, stem, chunk_size=800)):
        content = item["content"]
        if version_warning:
            content = f"{version_warning}\n\n{content}"
        suffix = item.get("clause") or f"p{item.get('piece_index', index)}"
        chunks.append({
            "content": content, "file_name": stem, "category": cat,
            "section": item["section"], "clause": item.get("clause", ""),
            "chunk_type": item.get("chunk_type", "section"),
            "chunk_id": f"{stem}__s{item['section_index']}__{suffix}",
            "parent_id": f"{stem}__s{item['section_index']}", **file_metadata})
    return chunks


all_chunks = []
for fp, cat, stem in files:
    chunks = chunk_document(fp, cat, stem)
    all_chunks.extend(chunks)
logger.info(f"切片完成：{len(all_chunks)} chunks")

texts = [c["content"] for c in all_chunks]
metadatas = [normalize_metadata(c) for c in all_chunks]
ids = [c["chunk_id"] for c in all_chunks]

# 3. 用 ollama.embed() 批量计算 embedding（比 LangChain 逐条快很多）
logger.info(f"批量计算 {len(texts)} 个 embeddings (Ollama batch API)...")
record_index_stage(STORE_DIR, "embedding", "started", batch_size=BATCH_SIZE, item_count=len(texts), model=EMBED_MODEL)
t0 = time.time()

all_embeddings = []
for i in range(0, len(texts), BATCH_SIZE):
    batch = texts[i:i+BATCH_SIZE]
    resp = ollama.embed(model=EMBED_MODEL, input=batch)
    all_embeddings.extend(resp.embeddings)
    pct = min(100, round((i + len(batch)) / len(texts) * 100))
    elapsed = time.time() - t0
    logger.info(f"  embedding: {i+len(batch)}/{len(texts)} ({pct}%, {elapsed:.0f}s)")

elapsed = time.time() - t0
logger.info(f"embedding 完成，维度={len(all_embeddings[0])}，耗时={elapsed:.0f}s")
record_index_stage(STORE_DIR, "embedding", "completed", item_count=len(texts), vector_dimension=len(all_embeddings[0]) if all_embeddings else 0, duration_s=round(elapsed, 3))

# 4. 重建 Chroma
chroma_dir = os.path.join(str(STORE_DIR), "chroma_db")
if os.path.exists(chroma_dir):
    shutil.rmtree(chroma_dir)
os.makedirs(chroma_dir, exist_ok=True)

chroma_client = chromadb.PersistentClient(
    path=chroma_dir,
    settings=Settings(anonymized_telemetry=False),
)
collection = chroma_client.create_collection(
    name="cyber_security",
    metadata={
        "hnsw:space": _INDEX_CONFIG["chroma_space"],
        "hnsw:sync_threshold": 100000,
        "index_contract_version": _INDEX_CONFIG["contract_version"],
        "embedding_model": EMBED_MODEL,
    },
)

BATCH = 500
for i in range(0, len(ids), BATCH):
    end = min(i + BATCH, len(ids))
    collection.add(
        ids=ids[i:end],
        embeddings=all_embeddings[i:end],
        documents=texts[i:end],
        metadatas=metadatas[i:end],
    )
logger.info(f"Chroma 重建完成：{collection.count()} chunks")
record_index_stage(STORE_DIR, "chroma_index", "completed", item_count=collection.count(), space=_INDEX_CONFIG["chroma_space"])

# 5. 重建 FAISS（复用预计算 embedding）
faiss_dir = str(STORE_DIR / "faiss_index")
if os.path.exists(faiss_dir):
    shutil.rmtree(faiss_dir)

# faiss C 层不支持中文路径，先保存到临时目录再复制
_tmp_faiss = os.path.join(tempfile.gettempdir(), "_cyber_faiss_tmp")
if os.path.exists(_tmp_faiss):
    shutil.rmtree(_tmp_faiss)

text_embeddings = list(zip(texts, all_embeddings))
faiss_db = FAISS.from_embeddings(
    text_embeddings=text_embeddings,
    embedding=lc_embeddings,
    metadatas=metadatas,
    ids=ids,
)
faiss_db.save_local(_tmp_faiss)
shutil.copytree(_tmp_faiss, faiss_dir)
shutil.rmtree(_tmp_faiss)
logger.info(f"FAISS 重建完成：{faiss_db.index.ntotal} vectors")
record_index_stage(STORE_DIR, "faiss_index", "completed", item_count=faiss_db.index.ntotal)
write_manifest(STORE_DIR / "index_manifest.json", all_chunks,
               len(all_embeddings[0]) if all_embeddings else 0)

# 6. 验证
logger.info(f"\n{'='*60}")
logger.info(f"  重建完成")
logger.info(f"{'='*60}")
logger.info(f"  文档: {len(files)} 份")
logger.info(f"  Chunks: {len(all_chunks)}")
logger.info(f"  Chroma: {chroma_dir}")
logger.info(f"  FAISS:  {faiss_dir}")

try:
    c = chromadb.PersistentClient(path=chroma_dir, settings=Settings(anonymized_telemetry=False))
    col = c.get_collection("cyber_security")
    logger.info(f"  Chroma 验证: {col.count()} chunks ✅")
except Exception as e:
    logger.info(f"  Chroma 验证失败: {e}")

try:
    db = FAISS.load_local(faiss_dir, lc_embeddings, allow_dangerous_deserialization=True)
    logger.info(f"  FAISS 验证: {db.index.ntotal} vectors ✅")
except Exception as e:
    logger.info(f"  FAISS 验证失败: {e}")
