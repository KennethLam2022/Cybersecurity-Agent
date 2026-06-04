"""从 MD_Cleaned 重建 FAISS + Chroma 向量索引

用法：python rebuild_chroma.py

改进点：
  - 用 ollama.embed() 批量 API 替代 LangChain 逐条 embedding（快 10x+）
  - FAISS 和 Chroma 复用同一批预计算 embedding
"""
import os, shutil, logging, json, time
from pathlib import Path
import chromadb
from chromadb.config import Settings
import ollama
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "03_cleaned"
STORE_DIR = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA" / "04_vector_store"
_FAISS_DIR = str(STORE_DIR / "faiss_index")
EMBED_MODEL = "quentinz/bge-small-zh-v1.5"
OLLAMA_URL = "http://localhost:11434"

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
    version_warning = ""
    for line in text.split("\n")[:5]:
        if "⚠️" in line:
            version_warning = line.strip()
            break

    lines = text.split("\n")
    current_section = "前言"
    current_texts = []
    sections = []
    for line in lines:
        if line.startswith("## "):
            if current_texts:
                sections.append((current_section, "\n".join(current_texts)))
            current_section = line.lstrip("# ").strip()
            current_texts = [line]
        elif line.startswith("### ") or line.startswith("# "):
            if current_texts:
                sections.append((current_section, "\n".join(current_texts)))
            current_section = line.lstrip("# ").strip()
            current_texts = [line]
        else:
            current_texts.append(line)
    if current_texts:
        sections.append((current_section, "\n".join(current_texts)))

    CHUNK_SIZE = 384
    splitter = RecursiveCharacterTextSplitter(
        chunk_size=CHUNK_SIZE, chunk_overlap=50,
        separators=["\n\n", "\n", "。", "，", " ", ""]
    )

    chunks = []
    for idx, (sec_title, sec_text) in enumerate(sections):
        if len(sec_text) > CHUNK_SIZE:
            sub_chunks = splitter.split_text(sec_text)
            for i, sub in enumerate(sub_chunks):
                content = sub
                if version_warning:
                    content = f"{version_warning}\n\n{sub}"
                chunks.append({"content": content, "file_name": stem, "category": cat,
                                "section": sec_title, "chunk_id": f"{stem}__s{idx}__{i}",
                                "parent_id": f"{stem}__s{idx}"})
        else:
            content = sec_text
            if version_warning:
                content = f"{version_warning}\n\n{sec_text}"
            chunks.append({"content": content, "file_name": stem, "category": cat,
                            "section": sec_title, "chunk_id": f"{stem}__s{idx}",
                            "parent_id": f"{stem}__s{idx}"})
    return chunks

all_chunks = []
for fp, cat, stem in files:
    chunks = chunk_document(fp, cat, stem)
    all_chunks.extend(chunks)
logger.info(f"切片完成：{len(all_chunks)} chunks")

texts = [c["content"] for c in all_chunks]
metadatas = [{"file_name": c["file_name"], "category": c["category"],
              "section": c["section"], "chunk_id": c["chunk_id"],
              "parent_id": c["parent_id"]} for c in all_chunks]
ids = [c["chunk_id"] for c in all_chunks]

# 3. 用 ollama.embed() 批量计算 embedding（比 LangChain 逐条快很多）
logger.info(f"批量计算 {len(texts)} 个 embeddings (Ollama batch API)...")
t0 = time.time()

BATCH_SIZE = 16
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
        "hnsw:space": "cosine",
        "hnsw:sync_threshold": 100000,
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

# 5. 重建 FAISS（复用预计算 embedding）
faiss_dir = str(STORE_DIR / "faiss_index")
if os.path.exists(faiss_dir):
    shutil.rmtree(faiss_dir)

# faiss C 层不支持中文路径，先保存到临时目录再复制
import tempfile
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

# 6. 验证
print(f"\n{'='*60}")
print(f"  重建完成")
print(f"{'='*60}")
print(f"  文档: {len(files)} 份")
print(f"  Chunks: {len(all_chunks)}")
print(f"  Chroma: {chroma_dir}")
print(f"  FAISS:  {faiss_dir}")

try:
    c = chromadb.PersistentClient(path=chroma_dir, settings=Settings(anonymized_telemetry=False))
    col = c.get_collection("cyber_security")
    print(f"  Chroma 验证: {col.count()} chunks ✅")
except Exception as e:
    print(f"  Chroma 验证失败: {e}")

try:
    db = FAISS.load_local(faiss_dir, lc_embeddings, allow_dangerous_deserialization=True)
    print(f"  FAISS 验证: {db.index.ntotal} vectors ✅")
except Exception as e:
    print(f"  FAISS 验证失败: {e}")
