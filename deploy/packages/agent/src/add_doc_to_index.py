"""增量索引：将新增的md文件切片后追加到 FAISS + Chroma

用法：python add_doc_to_index.py

说明：每次只处理1个文件，增量追加到现有索引库，无需全量重建
"""
import os, sys, json, time, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_SRC = os.path.dirname(os.path.abspath(__file__))
_PREPROC = os.path.join(_SRC, "..", "..", "preprocessor", "src")
for p in [_SRC, _PREPROC]:
    if p not in sys.path:
        sys.path.insert(0, p)

from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings

BASE = Path(_SRC).parent.parent.parent  # 项目根目录
MD_DIR = BASE / "packages" / "MD_Cleaned"
_FAISS_DIR = str(BASE / "vector_store" / "faiss_index")

embeddings = OllamaEmbeddings(
    model="quentinz/bge-small-zh-v1.5",
    base_url="http://localhost:11434",
)


def chunk_document(file_path: str, cat: str, stem: str) -> list[dict]:
    """与 index_batch.py 一致的切片逻辑"""
    text = Path(file_path).read_text(encoding="utf-8")

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

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=300, chunk_overlap=50,
        separators=["\n\n", "\n", "。", "，", " ", ""]
    )

    chunks = []
    for idx, (sec_title, sec_text) in enumerate(sections):
        if len(sec_text) > 256:
            sub_chunks = splitter.split_text(sec_text)
            for i, sub in enumerate(sub_chunks):
                chunks.append({
                    "content": sub,
                    "file_name": stem,
                    "category": cat,
                    "section": sec_title,
                    "chunk_id": f"{stem}__s{idx}__{i}",
                    "parent_id": f"{stem}__s{idx}",
                })
        else:
            chunks.append({
                "content": sec_text,
                "file_name": stem,
                "category": cat,
                "section": sec_title,
                "chunk_id": f"{stem}__s{idx}",
                "parent_id": f"{stem}__s{idx}",
            })
    return chunks


def add_to_faiss(chunks: list[dict]):
    """增量追加到 FAISS"""
    texts = [c["content"] for c in chunks]
    metadatas = [{
        "file_name": c["file_name"],
        "category": c["category"],
        "section": c["section"],
        "chunk_id": c["chunk_id"],
        "parent_id": c["parent_id"],
    } for c in chunks]
    ids = [c["chunk_id"] for c in chunks]

    t0 = time.time()
    import tempfile, shutil
    _tmp_faiss = os.path.join(tempfile.gettempdir(), "_cyber_faiss_tmp")
    # faiss C 层不支持中文路径，先复制到临时目录再加载
    _tmp_load = os.path.join(tempfile.gettempdir(), "_cyber_faiss_load")

    if os.path.exists(os.path.join(_FAISS_DIR, "index.faiss")):
        if os.path.exists(_tmp_load):
            shutil.rmtree(_tmp_load)
        shutil.copytree(_FAISS_DIR, _tmp_load)
        try:
            db = FAISS.load_local(_tmp_load, embeddings, allow_dangerous_deserialization=True)
        finally:
            shutil.rmtree(_tmp_load, ignore_errors=True)
        old_count = db.index.ntotal
        existing_ids = set()
        idx_to_id = db.index_to_docstore_id
        if idx_to_id:
            existing_ids = set(idx_to_id.values())

        new_texts, new_metadatas, new_ids = [], [], []
        for i, cid in enumerate(ids):
            if cid not in existing_ids:
                new_texts.append(texts[i])
                new_metadatas.append(metadatas[i])
                new_ids.append(cid)

        if new_ids:
            db.add_texts(texts=new_texts, metadatas=new_metadatas, ids=new_ids)
            added = len(new_ids)
        else:
            added = 0

        if os.path.exists(_tmp_faiss):
            shutil.rmtree(_tmp_faiss)
        db.save_local(_tmp_faiss)
        if os.path.exists(_FAISS_DIR):
            shutil.rmtree(_FAISS_DIR)
        shutil.copytree(_tmp_faiss, _FAISS_DIR)
        shutil.rmtree(_tmp_faiss)
        logger.info(f"FAISS: {old_count} → {db.index.ntotal} (新加 {added}, 跳过 {len(ids)-added}, {time.time()-t0:.2f}s)")
    else:
        db = FAISS.from_texts(texts=texts, metadatas=metadatas, ids=ids, embedding=embeddings)
        if os.path.exists(_tmp_faiss):
            shutil.rmtree(_tmp_faiss)
        db.save_local(_tmp_faiss)
        if os.path.exists(_FAISS_DIR):
            shutil.rmtree(_FAISS_DIR)
        shutil.copytree(_tmp_faiss, _FAISS_DIR)
        shutil.rmtree(_tmp_faiss)
        logger.info(f"FAISS 新建: {len(chunks)} chunks ({time.time()-t0:.2f}s)")


def add_to_chroma(chunks: list[dict]):
    """增量追加到 Chroma"""
    import chromadb
    from chromadb.config import Settings

    _CHROMA_DIR = str(BASE / "vector_store" / "chroma_db")

    t0 = time.time()
    client = chromadb.PersistentClient(
        path=_CHROMA_DIR,
        settings=Settings(anonymized_telemetry=False),
    )

    try:
        collection = client.get_collection("cyber_security")
        old_count = collection.count()
    except Exception as e:
        if "already exists" not in str(e):
            logger.warning(f"Chroma 获取collection出错: {e}，跳过Chroma")
            return
        collection = client.create_collection("cyber_security")
        old_count = 0

    ids = [c["chunk_id"] for c in chunks]
    texts = [c["content"] for c in chunks]
    metadatas = [{
        "file_name": c["file_name"],
        "category": c["category"],
        "section": c["section"],
        "chunk_id": c["chunk_id"],
        "parent_id": c["parent_id"],
    } for c in chunks]

    # 去重 Chroma
    existing_ids = set(collection.get(ids=ids, include=[])["ids"]) if old_count > 0 else set()
    new_ids, new_texts, new_metadatas = [], [], []
    for i, cid in enumerate(ids):
        if cid not in existing_ids:
            new_ids.append(cid)
            new_texts.append(texts[i])
            new_metadatas.append(metadatas[i])

    if new_ids:
        collection.add(ids=new_ids, documents=new_texts, metadatas=new_metadatas)
    new_count = collection.count()
    logger.info(f"Chroma: {old_count} → {new_count} (新加 {len(new_ids)}, 跳过 {len(ids)-len(new_ids)}, {time.time()-t0:.2f}s)")


if __name__ == "__main__":
    # 指定要追加的文件
    TARGET_FILES = [
        str(MD_DIR / "03-CII关基" / "等保与关基关系.md"),
    ]

    for fp in TARGET_FILES:
        p = Path(fp)
        if not p.exists():
            logger.warning(f"文件不存在: {fp}")
            continue

        cat = p.parent.name
        stem = p.stem
        logger.info(f"处理: {stem} ({cat})")

        chunks = chunk_document(fp, cat, stem)
        logger.info(f"切片: {len(chunks)} chunks")

        add_to_faiss(chunks)
        add_to_chroma(chunks)

        logger.info(f"✅ {stem} 索引完成")

    logger.info(f"\n全部完成！共处理 {len(TARGET_FILES)} 份文件")