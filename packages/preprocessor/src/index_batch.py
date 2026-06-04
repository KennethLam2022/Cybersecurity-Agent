"""阶段二：批量切片+向量化入库（20份/批， Chroma + FAISS）

用法：
  python index_batch.py --batch 1    # 跑第1批（20份）
  python index_batch.py --batch 2    # 跑第2批
  python index_batch.py --status     # 查看进度
"""
import json, os, re, time, sys, logging, shutil
from pathlib import Path
import chromadb
from chromadb.config import Settings as ChromaSettings
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_RAG = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA"
BASE = _RAG / "03_cleaned"
STORE_DIR = _RAG / "04_vector_store"
STORE_DIR.mkdir(parents=True, exist_ok=True)
PROGRESS_FILE = STORE_DIR / ".index_progress.json"
BATCH_SIZE = 20
_FAISS_DIR = str(STORE_DIR / "faiss_index")

# --- 收集所有 MD 文件 ---
def collect_md_files():
    files = []
    for cat_dir in sorted(BASE.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name in ("test_output", "_deprecated"):
            continue
        for md_file in sorted(cat_dir.glob("*.md")):
            files.append((str(md_file), cat_dir.name, md_file.stem))
    return files

# --- 加载/保存进度 ---
def load_progress():
    if PROGRESS_FILE.exists():
        return json.loads(PROGRESS_FILE.read_text(encoding="utf-8"))
    return {"indexed": [], "batches_done": [], "stats": {"total_chunks": 0, "total_chars": 0}}

def save_progress(p):
    PROGRESS_FILE.write_text(json.dumps(p, ensure_ascii=False, indent=2), encoding="utf-8")

# --- 切片逻辑 ---
def chunk_document(file_path, cat, stem):
    text = Path(file_path).read_text(encoding="utf-8")
    # 检测版本警告（如果有，每段chunk都要带）
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

    splitter = RecursiveCharacterTextSplitter(
        chunk_size=300, chunk_overlap=50,
        separators=["\n\n", "\n", "。", "，", " ", ""]
    )

    chunks = []
    for idx, (sec_title, sec_text) in enumerate(sections):
        if len(sec_text) > 256:
            sub_chunks = splitter.split_text(sec_text)
            for i, sub in enumerate(sub_chunks):
                content = sub
                if version_warning:
                    content = f"{version_warning}\n\n{sub}"
                chunks.append({
                    "content": content,
                    "file_name": stem,
                    "category": cat,
                    "section": sec_title,
                    "chunk_id": f"{stem}__s{idx}__{i}",
                    "parent_id": f"{stem}__s{idx}",
                })
        else:
            content = sec_text
            if version_warning:
                content = f"{version_warning}\n\n{sec_text}"
            chunks.append({
                "content": content,
                "file_name": stem,
                "category": cat,
                "section": sec_title,
                "chunk_id": f"{stem}__s{idx}",
                "parent_id": f"{stem}__s{idx}",
            })
    return chunks, len(text)

# --- 为指定文件列表批量切片 ---
def chunk_files(file_list):
    all_chunks = []
    total_chars = 0
    for fp, cat, stem in file_list:
        chunks, n_chars = chunk_document(fp, cat, stem)
        all_chunks.extend(chunks)
        total_chars += n_chars
    return all_chunks, total_chars

# --- 分批运行 ---
def run_batch(batch_num):
    all_files = collect_md_files()
    progress = load_progress()
    indexed_set = set(progress["indexed"])
    to_index = [(fp, cat, stem) for fp, cat, stem in all_files if fp not in indexed_set]

    total_batches = (len(to_index) + BATCH_SIZE - 1) // BATCH_SIZE
    start = (batch_num - 1) * BATCH_SIZE
    # 确保 start 不超出 to_index 范围（支持断点续跑）
    if start >= len(to_index):
        start = 0
    batch_files = to_index[start:start + BATCH_SIZE]

    if not batch_files:
        logger.info(f"批次 {batch_num} 没有待处理文件，跳过")
        return

    logger.info(f"批次 {batch_num}/{total_batches + len(progress['batches_done'])} : {len(batch_files)} 份")

    # Embedding 模型
    embeddings = OllamaEmbeddings(
        model="quentinz/bge-small-zh-v1.5",
        base_url="http://localhost:11434",
    )

    # ----- Chroma：全量重建（避免 HNSW compaction bug）-----
    # 收集所有已有 + 新增文件，一起切片 + embed + 写入新集合
    chroma_dir = os.path.join(str(STORE_DIR), "chroma_db")
    all_indexed = [(fp, cat, stem) for fp, cat, stem in all_files if fp in indexed_set]
    all_for_chroma = all_indexed + batch_files

    logger.info(f"  Chroma 全量重建：{len(all_for_chroma)} 份文件（已有 {len(all_indexed)} + 新增 {len(batch_files)}）")

    # 切片
    chroma_chunks, _ = chunk_files(all_for_chroma)
    if chroma_chunks:
        chroma_texts = [c["content"] for c in chroma_chunks]
        chroma_metadatas = [{
            "file_name": c["file_name"],
            "category": c["category"],
            "section": c["section"],
            "chunk_id": c["chunk_id"],
            "parent_id": c["parent_id"],
        } for c in chroma_chunks]
        chroma_ids = [c["chunk_id"] for c in chroma_chunks]

        # 预计算 embeddings
        logger.info(f"  预计算 {len(chroma_chunks)} 个 Ollama embeddings ...")
        chroma_embedded = embeddings.embed_documents(chroma_texts)
        logger.info(f"  embedding 完成，维度={len(chroma_embedded[0]) if chroma_embedded else 0}")

        # 删旧库建新库
        if os.path.exists(chroma_dir):
            shutil.rmtree(chroma_dir)
        chroma_client = chromadb.PersistentClient(
            path=chroma_dir,
            settings=ChromaSettings(anonymized_telemetry=False),
        )
        collection = chroma_client.create_collection(
            name="cyber_security",
            metadata={
                "hnsw:space": "cosine",
                "hnsw:sync_threshold": 100000,
            },
        )
        # 批量添加（每次 500 chunks，避免单次提交过大）
        for i in range(0, len(chroma_ids), 500):
            end = min(i + 500, len(chroma_ids))
            collection.add(
                ids=chroma_ids[i:end],
                embeddings=chroma_embedded[i:end],
                documents=chroma_texts[i:end],
                metadatas=chroma_metadatas[i:end],
            )
        logger.info(f"  Chroma: {len(chroma_chunks)} chunks 写入完成")

    # 新增文件的 chunks（用于 FAISS 增量更新 + 进度统计）
    new_chunks, total_chars = chunk_files(batch_files)
    new_texts = [c["content"] for c in new_chunks]
    new_metadatas = [{
        "file_name": c["file_name"],
        "category": c["category"],
        "section": c["section"],
        "chunk_id": c["chunk_id"],
        "parent_id": c["parent_id"],
    } for c in new_chunks]
    new_ids = [c["chunk_id"] for c in new_chunks]

    for fp, cat, stem in batch_files:
        n_chunks = sum(1 for c in new_chunks if c["file_name"] == stem)
        logger.info(f"  ✅ {stem} ({n_chunks} chunks, ? 字)")

    # ----- FAISS：增量追加（FAISS 无 compaction 问题）-----
    if os.path.exists(os.path.join(_FAISS_DIR, "index.faiss")):
        logger.info("  加载已有 FAISS 库...")
        faiss_db = FAISS.load_local(
            _FAISS_DIR,
            embeddings,
            allow_dangerous_deserialization=True,
        )
        faiss_db.add_texts(texts=new_texts, metadatas=new_metadatas, ids=new_ids)
    else:
        logger.info("  新建 FAISS 库...")
        faiss_db = FAISS.from_texts(
            texts=new_texts, embedding=embeddings,
            metadatas=new_metadatas, ids=new_ids,
        )
    os.makedirs(_FAISS_DIR, exist_ok=True)
    faiss_db.save_local(_FAISS_DIR)
    logger.info(f"  FAISS: +{len(new_chunks)} chunks")

    # 更新进度
    for fp, _, _ in batch_files:
        progress["indexed"].append(fp)
    progress["batches_done"].append(batch_num)
    progress["stats"]["total_chunks"] += len(new_chunks)
    progress["stats"]["total_chars"] += total_chars
    save_progress(progress)

    remaining = len(to_index) - start - len(batch_files)
    logger.info(f"\n批次 {batch_num} 完成! 累计: {len(progress['indexed'])}/{len(all_files)} 份, {progress['stats']['total_chunks']} chunks, 剩余约 {remaining} 份")

# --- 查看状态 ---
def show_status():
    all_files = collect_md_files()
    progress = load_progress()
    indexed = len(progress["indexed"])
    print(f"\n{'='*50}")
    print(f"  向量化进度")
    print(f"{'='*50}")
    print(f"  总文档:    {len(all_files)} 份")
    print(f"  已索引:    {indexed} 份")
    print(f"  未索引:    {len(all_files) - indexed} 份")
    print(f"  总chunks:  {progress['stats']['total_chunks']}")
    print(f"  已完成批次:  {progress['batches_done']}")
    remaining = len(all_files) - indexed
    if remaining > 0:
        next_batch = max(progress["batches_done"]) + 1 if progress["batches_done"] else 1
        print(f"  下一批次:  --batch {next_batch}")
    # Chroma 状态
    chroma_dir = os.path.join(str(STORE_DIR), "chroma_db")
    if os.path.exists(chroma_dir):
        try:
            c = chromadb.PersistentClient(path=chroma_dir, settings=ChromaSettings(anonymized_telemetry=False))
            col = c.get_collection("cyber_security")
            print(f"  Chroma: {col.count()} chunks")
        except Exception as e:
            print(f"  Chroma: 读取失败 ({e})")
    # FAISS 状态
    if os.path.exists(os.path.join(_FAISS_DIR, "index.faiss")):
        print(f"  FAISS: 存在 ({_FAISS_DIR})")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="批量切片+向量化入库")
    parser.add_argument("--batch", type=int, help="批次号")
    parser.add_argument("--status", action="store_true", help="查看进度")
    parser.add_argument("--reset", action="store_true", help="重置进度")
    args = parser.parse_args()

    if args.reset:
        if STORE_DIR.exists():
            shutil.rmtree(STORE_DIR)
        STORE_DIR.mkdir(parents=True, exist_ok=True)
        if os.path.exists(_FAISS_DIR):
            shutil.rmtree(_FAISS_DIR)
        save_progress({"indexed": [], "batches_done": [], "stats": {"total_chunks": 0, "total_chars": 0}})
        print("进度已重置")
    elif args.status:
        show_status()
    elif args.batch:
        run_batch(args.batch)
    else:
        show_status()