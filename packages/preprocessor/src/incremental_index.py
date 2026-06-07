"""增量索引：将新增的 .md 文件追加到父文档索引 + FAISS + Chroma

用法：
  python incremental_index.py path/to/new_file.md [more_files.md...]

说明：
  - 父文档索引：从 parent_texts.json 追加新节，已有 ID 自动跳过
  - FAISS：加载现有索引，追加新 chunk，中文路径用临时目录中转
  - Chroma：获取现有 collection，追加新 document
  - BM25 在启动时自动从 parent_texts.json 重建，无需手动处理

与 build_parent_index.py / rebuild_faiss_only.py / rebuild_chroma.py 的区别：
  这些是全量重建脚本，incremental_index.py 只处理新增文件。
"""
import os
import sys
import json
import time
import logging
import shutil
import tempfile
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_RAG = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA"
_CLEANED_DIR = _RAG / "03_cleaned"
_STORE_DIR = _RAG / "04_vector_store"
_PARENT_FILE = _STORE_DIR / "parent_texts.json"
_FAISS_DIR = str(_STORE_DIR / "faiss_index")
_CHROMA_DIR = str(_STORE_DIR / "chroma_db")

EMBED_MODEL = "quentinz/bge-small-zh-v1.5"
OLLAMA_URL = "http://localhost:11434"


# ─── 父文档索引（增量） ───────────────────────────────

def _extract_sections(file_path: str) -> list[dict]:
    """按 ## / ### / # 标题切分文档（与 build_parent_index.py 一致）"""
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

    result = []
    for idx, (sec_title, sec_text) in enumerate(sections):
        text = sec_text
        if version_warning:
            text = f"{version_warning}\n\n{sec_text}"
        result.append({"section": sec_title, "text": text})
    return result


def _update_parent_index(md_paths: list[str]) -> int:
    """增量追加父文档索引，返回新加节数"""
    existing = {}
    if _PARENT_FILE.exists():
        existing = json.loads(_PARENT_FILE.read_text(encoding="utf-8"))

    added = 0
    for fp in md_paths:
        p = Path(fp)
        stem = p.stem
        category = p.parent.name
        sections = _extract_sections(fp)
        for idx, sec in enumerate(sections):
            parent_id = f"{stem}__s{idx}"
            if parent_id not in existing:
                existing[parent_id] = {
                    "text": sec["text"],
                    "file_name": stem,
                    "category": category,
                    "section": sec["section"],
                }
                added += 1

    _PARENT_FILE.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"父文档索引: 新加 {added} 节, 总计 {len(existing)} 节")
    return added


# ─── 切片（与 add_doc_to_index.py + index_batch.py 一致） ─

def _chunk_document(file_path: str, category: str, stem: str) -> list[dict]:
    """将 .md 文件按标题切分 → 递归切片 → 返回 chunks"""
    from langchain_text_splitters import RecursiveCharacterTextSplitter

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
                    "category": category,
                    "section": sec_title,
                    "chunk_id": f"{stem}__s{idx}__{i}",
                    "parent_id": f"{stem}__s{idx}",
                })
        else:
            chunks.append({
                "content": sec_text,
                "file_name": stem,
                "category": category,
                "section": sec_title,
                "chunk_id": f"{stem}__s{idx}",
                "parent_id": f"{stem}__s{idx}",
            })
    return chunks


# ─── FAISS 增量 ─────────────────────────────────────

def _add_to_faiss(chunks: list[dict]):
    """增量追加到 FAISS（用临时目录绕开中文路径问题）"""
    from langchain_community.vectorstores import FAISS
    from langchain_ollama import OllamaEmbeddings

    embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)
    texts = [c["content"] for c in chunks]
    metadatas = [{
        "file_name": c["file_name"], "category": c["category"],
        "section": c["section"], "chunk_id": c["chunk_id"],
        "parent_id": c["parent_id"],
    } for c in chunks]
    ids = [c["chunk_id"] for c in chunks]

    t0 = time.time()
    _tmp = os.path.join(tempfile.gettempdir(), "_cyber_faiss_tmp")
    _tmp_load = os.path.join(tempfile.gettempdir(), "_cyber_faiss_load")

    faiss_index_file = os.path.join(_FAISS_DIR, "index.faiss")
    if os.path.exists(faiss_index_file):
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

        if os.path.exists(_tmp):
            shutil.rmtree(_tmp)
        db.save_local(_tmp)
        if os.path.exists(_FAISS_DIR):
            shutil.rmtree(_FAISS_DIR)
        shutil.copytree(_tmp, _FAISS_DIR)
        shutil.rmtree(_tmp)
        logger.info(f"FAISS: {old_count} → {db.index.ntotal} (新加 {added}, {time.time()-t0:.2f}s)")
    else:
        logger.warning(f"FAISS 索引不存在: {faiss_index_file}，先跑全量重建")
        db = FAISS.from_texts(texts=texts, metadatas=metadatas, ids=ids, embedding=embeddings)
        if os.path.exists(_tmp):
            shutil.rmtree(_tmp)
        db.save_local(_tmp)
        if os.path.exists(_FAISS_DIR):
            shutil.rmtree(_FAISS_DIR)
        shutil.copytree(_tmp, _FAISS_DIR)
        shutil.rmtree(_tmp)
        logger.info(f"FAISS 新建: {len(chunks)} chunks ({time.time()-t0:.2f}s)")


# ─── Chroma 增量 ────────────────────────────────────

def _add_to_chroma(chunks: list[dict]):
    """增量追加到 Chroma（嵌入与 FAISS 同模型，确保维度一致）"""
    import chromadb
    from chromadb.config import Settings
    from langchain_ollama import OllamaEmbeddings

    embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)

    t0 = time.time()
    client = chromadb.PersistentClient(
        path=_CHROMA_DIR, settings=Settings(anonymized_telemetry=False))

    try:
        collection = client.get_collection("cyber_security")
    except Exception:
        collection = client.create_collection("cyber_security")

    old_count = collection.count()

    ids = [c["chunk_id"] for c in chunks]
    texts = [c["content"] for c in chunks]
    metadatas = [{
        "file_name": c["file_name"], "category": c["category"],
        "section": c["section"], "chunk_id": c["chunk_id"],
        "parent_id": c["parent_id"],
    } for c in chunks]

    existing_ids = set(collection.get(ids=ids, include=[])["ids"]) if old_count > 0 else set()
    new_ids, new_texts, new_metadatas = [], [], []
    for i, cid in enumerate(ids):
        if cid not in existing_ids:
            new_ids.append(cid)
            new_texts.append(texts[i])
            new_metadatas.append(metadatas[i])

    if new_ids:
        # 预计算 embeddings（与 FAISS 同一模型，保证 512 维一致）
        emb_list = embeddings.embed_documents(new_texts)
        collection.add(ids=new_ids, documents=new_texts,
                       metadatas=new_metadatas, embeddings=emb_list)

    new_count = collection.count()
    logger.info(f"Chroma: {old_count} → {new_count} (新加 {len(new_ids)}, {time.time()-t0:.2f}s)")


# ─── 后置校验 ──────────────────────────────────────

def _validate_consistency():
    """对比 FAISS 和 Chroma 数量，不一致时自动补全（不重新 embedding）"""
    from langchain_community.vectorstores import FAISS
    from langchain_ollama import OllamaEmbeddings
    import chromadb
    from chromadb.config import Settings
    import tempfile

    embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)
    faiss_path = os.path.join(_FAISS_DIR, "index.faiss")
    if not os.path.exists(faiss_path):
        return

    # 加载 FAISS
    _tmp = os.path.join(tempfile.gettempdir(), "_cyber_faiss_val")
    try:
        if os.path.exists(_tmp):
            shutil.rmtree(_tmp)
        shutil.copytree(_FAISS_DIR, _tmp)
        db = FAISS.load_local(_tmp, embeddings, allow_dangerous_deserialization=True)
        faiss_count = db.index.ntotal
    except Exception as e:
        logger.warning(f"后置校验: FAISS 加载失败: {e}")
        return
    finally:
        shutil.rmtree(_tmp, ignore_errors=True)

    # 加载 Chroma
    client = chromadb.PersistentClient(
        path=_CHROMA_DIR, settings=Settings(anonymized_telemetry=False))
    try:
        collection = client.get_collection("cyber_security")
        chroma_count = collection.count()
    except Exception:
        chroma_count = 0

    if faiss_count == chroma_count:
        logger.info(f"后置校验 ✅ FAISS={faiss_count} Chroma={chroma_count}")
        return

    logger.warning(f"后置校验 ⚠️ FAISS={faiss_count} ≠ Chroma={chroma_count}，正在自动补全...")

    if chroma_count < faiss_count:
        # Chroma 缺了：从 FAISS 读向量直接补入 Chroma
        chroma_existing = set(collection.get(include=[])["ids"])
        missing_ids, missing_texts, missing_metadatas, missing_embs = [], [], [], []
        for idx_id, doc_id in db.index_to_docstore_id.items():
            if doc_id not in chroma_existing:
                doc = db.docstore.search(doc_id)
                emb = db.index.reconstruct(int(idx_id))
                missing_ids.append(doc_id)
                missing_texts.append(doc.page_content)
                missing_metadatas.append(doc.metadata)
                missing_embs.append(emb.tolist())
        if missing_ids:
            collection.add(ids=missing_ids, documents=missing_texts,
                           metadatas=missing_metadatas, embeddings=missing_embs)
            logger.info(f"  自动补全 Chroma: +{len(missing_ids)} 条 (从 FAISS 复制向量)")

    else:
        # FAISS 缺了：从 Chroma 读向量直接补入 FAISS
        chroma_all = collection.get(include=["documents", "metadatas", "embeddings"])
        chroma_ids_set = set(chroma_all["ids"])
        faiss_ids_set = set(db.index_to_docstore_id.values())
        missing_docs, missing_metadatas, missing_ids, missing_embs = [], [], [], []
        for i, cid in enumerate(chroma_all["ids"]):
            if cid not in faiss_ids_set:
                missing_ids.append(cid)
                missing_docs.append(chroma_all["documents"][i])
                missing_metadatas.append(chroma_all["metadatas"][i])
                missing_embs.append(chroma_all["embeddings"][i])
        if missing_ids:
            text_embeddings = list(zip(missing_docs, missing_embs))
            db.add_embeddings(text_embeddings=text_embeddings,
                              metadatas=missing_metadatas, ids=missing_ids)
            _tmp_save = os.path.join(tempfile.gettempdir(), "_cyber_faiss_tmp")
            if os.path.exists(_tmp_save):
                shutil.rmtree(_tmp_save)
            db.save_local(_tmp_save)
            if os.path.exists(_FAISS_DIR):
                shutil.rmtree(_FAISS_DIR)
            shutil.copytree(_tmp_save, _FAISS_DIR)
            shutil.rmtree(_tmp_save)
            logger.info(f"  自动补全 FAISS: +{len(missing_ids)} 条 (从 Chroma 复制向量)")

    # 最终对比
    try:
        final_faiss = db.index.ntotal
    except Exception:
        final_faiss = 0
    try:
        final_chroma = collection.count()
    except Exception:
        final_chroma = 0
    if final_faiss == final_chroma:
        logger.info(f"  自动补全后一致 ✅ FAISS={final_faiss} Chroma={final_chroma}")
    else:
        logger.error(f"  自动补全后仍不一致 ❌ FAISS={final_faiss} Chroma={final_chroma}（需要人工介入）")


# ─── 主入口 ──────────────────────────────────────────

def incremental_index(md_paths: list[str]):
    """增量索引入口：父文档 → FAISS → Chroma"""
    if not md_paths:
        logger.info("没有新文件，跳过增量索引")
        return {"parent_added": 0, "faiss_added": 0, "chroma_added": 0}

    logger.info(f"增量索引: {len(md_paths)} 个文件:")
    for p in md_paths:
        logger.info(f"  {p}")

    # 1. 父文档索引
    parent_added = _update_parent_index(md_paths)

    # 2. 收集所有 chunks
    all_chunks = []
    for fp in md_paths:
        p = Path(fp)
        stem = p.stem
        category = p.parent.name
        chunks = _chunk_document(fp, category, stem)
        logger.info(f"  切片 {stem}: {len(chunks)} chunks")
        all_chunks.extend(chunks)

    # 2b. Layer 3: 向量语义去重（合并相似度 > 0.95 的 chunk）
    try:
        from deduplicator import semantic_deduplicate
        before = len(all_chunks)
        all_chunks = semantic_deduplicate(all_chunks, threshold=0.95)
        deduped = before - len(all_chunks)
        if deduped:
            logger.info(f"  Layer 3 语义去重: 移除 {deduped} 个重复 chunk ({before} → {len(all_chunks)})")
    except Exception as e:
        logger.warning(f"  Layer 3 跳过: {e}")

    # 3. Chroma 先写（如果 Chroma 失败，FAISS 还没写，不会出现不一致）
    _add_to_chroma(all_chunks)

    # 4. FAISS 后写
    _add_to_faiss(all_chunks)

    # 5. 后置校验：对比 FAISS 和 Chroma 数量，不一致自动补全
    _validate_consistency()

    logger.info(f"✅ 增量索引完成: {parent_added} 父节, {len(all_chunks)} chunks")
    return {"parent_added": parent_added, "faiss_added": len(all_chunks)}


def _remove_from_chroma(stems: list[str]):
    """从 Chroma 中删除指定 stem 的所有 chunk"""
    import chromadb
    from chromadb.config import Settings
    t0 = time.time()
    client = chromadb.PersistentClient(
        path=_CHROMA_DIR, settings=Settings(anonymized_telemetry=False))
    try:
        collection = client.get_collection("cyber_security")
    except Exception:
        logger.warning("Chroma collection 不存在，跳过删除")
        return
    old_count = collection.count()
    for stem in stems:
        collection.delete(where={"file_name": stem})
    new_count = collection.count()
    logger.info(f"Chroma 清理: 移除 {old_count - new_count} 条 ({stems}), {time.time()-t0:.2f}s")


def _rebuild_faiss_from_parents():
    """从 parent_texts.json 全量重建 FAISS 索引（用于删除旧 stem 后的重建）"""
    from langchain_community.vectorstores import FAISS
    from langchain_ollama import OllamaEmbeddings
    import tempfile
    import shutil

    if not _PARENT_FILE.exists():
        logger.warning("parent_texts.json 不存在，跳过 FAISS 重建")
        return

    parent_data = json.loads(_PARENT_FILE.read_text(encoding="utf-8"))
    texts = [v["text"] for v in parent_data.values()]
    metadatas = [{
        "file_name": v["file_name"], "category": v.get("category", ""),
        "section": v.get("section", ""), "chunk_id": k, "parent_id": k,
    } for k, v in parent_data.items()]
    ids = list(parent_data.keys())

    from langchain_text_splitters import RecursiveCharacterTextSplitter
    splitter = RecursiveCharacterTextSplitter(chunk_size=300, chunk_overlap=50, separators=[
                                              "\n\n", "\n", "。", "，", " ", ""])
    all_texts, all_metadatas, all_ids = [], [], []
    for i, t in enumerate(texts):
        if len(t) > 256:
            sub = splitter.split_text(t)
            for j, s in enumerate(sub):
                all_texts.append(s)
                all_metadatas.append(metadatas[i])
                all_ids.append(f"{ids[i]}__{j}")
        else:
            all_texts.append(t)
            all_metadatas.append(metadatas[i])
            all_ids.append(ids[i])

    t0 = time.time()
    embeddings = OllamaEmbeddings(model=EMBED_MODEL, base_url=OLLAMA_URL)
    db = FAISS.from_texts(texts=all_texts, metadatas=all_metadatas,
                          ids=all_ids, embedding=embeddings)
    _tmp = os.path.join(tempfile.gettempdir(), "_cyber_faiss_tmp")
    if os.path.exists(_tmp):
        shutil.rmtree(_tmp)
    db.save_local(_tmp)
    if os.path.exists(_FAISS_DIR):
        shutil.rmtree(_FAISS_DIR)
    shutil.copytree(_tmp, _FAISS_DIR)
    shutil.rmtree(_tmp)
    logger.info(f"FAISS 全量重建: {len(all_ids)} chunks ({time.time()-t0:.2f}s)")


def remove_stems(stems: list[str]):
    """删除旧版 stem 在 Chroma + FAISS 中的全部数据"""
    logger.info(f"🗑️ 清理旧版: {stems}")
    _remove_from_chroma(stems)
    _rebuild_faiss_from_parents()
    logger.info(f"✅ 清理完成: {stems}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args:
        logger.info("用法:")
        logger.info("  python incremental_index.py path/to/file1.md [file2.md ...]")
        logger.info("  python incremental_index.py --remove-stems stem1 stem2 ...")
        sys.exit(1)

    if args[0] == "--remove-stems":
        stems = args[1:]
        if stems:
            remove_stems(stems)
        else:
            logger.warning("--remove-stems 后未提供 stem 名称")
        sys.exit(0)

    # 普通增量索引：逐个处理 .md 文件
    md_files = []
    for arg in args:
        p = Path(arg)
        if p.exists() and p.suffix == ".md":
            md_files.append(str(p.resolve()))
        elif not p.exists():
            logger.warning(f"文件不存在，跳过: {arg}")

    if md_files:
        incremental_index(md_files)
    else:
        logger.info("没有有效的 .md 文件")
