"""从已有的 Chroma DB 独立重建 FAISS 索引"""
import os, shutil, time, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

BASE = Path(__file__).parent.parent.parent
STORE_DIR = BASE / "vector_store"
FAISS_DIR = STORE_DIR / "faiss_index"

from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings

lc_embeddings = OllamaEmbeddings(model="quentinz/bge-small-zh-v1.5", base_url="http://localhost:11434")

# 从 Chroma 读出所有数据
import chromadb
from chromadb.config import Settings

chroma_dir = str(STORE_DIR / "chroma_db")
client = chromadb.PersistentClient(path=chroma_dir, settings=Settings(anonymized_telemetry=False))
collection = client.get_collection("cyber_security")
count = collection.count()
logger.info(f"Chroma 共有 {count} 条记录")

all_data = collection.get(include=["documents", "metadatas", "embeddings"])
texts = all_data["documents"]
metadatas = all_data["metadatas"]
embeddings = all_data["embeddings"]
ids = all_data["ids"]

logger.info(f"读取 {len(texts)} 个文档，{len(embeddings)} 个 embedding")

# 重建 FAISS
if os.path.exists(str(FAISS_DIR)):
    shutil.rmtree(str(FAISS_DIR))
time.sleep(0.5)

# 先创建空目录
os.makedirs(str(FAISS_DIR), exist_ok=True)

# 使用短路径名尝试
import tempfile
tmp_dir = os.path.join(tempfile.gettempdir(), "cyber_faiss_rebuild")
if os.path.exists(tmp_dir):
    shutil.rmtree(tmp_dir)
os.makedirs(tmp_dir, exist_ok=True)

logger.info(f"构建 FAISS 索引中...")
text_embeddings = list(zip(texts, embeddings))
faiss_db = FAISS.from_embeddings(
    text_embeddings=text_embeddings,
    embedding=lc_embeddings,
    metadatas=metadatas,
    ids=ids,
)

# 先保存到临时目录（短路径）
faiss_db.save_local(tmp_dir)
logger.info(f"FAISS 临时保存成功: {tmp_dir}")

# 复制到目标目录
if os.path.exists(str(FAISS_DIR)):
    shutil.rmtree(str(FAISS_DIR))
shutil.copytree(tmp_dir, str(FAISS_DIR))
shutil.rmtree(tmp_dir)

logger.info(f"FAISS 重建完成：{faiss_db.index.ntotal} vectors → {FAISS_DIR}")
print(f"\n{'='*60}")
print(f"  FAISS 重建完成")
print(f"  目录: {FAISS_DIR}")
print(f"  向量数: {faiss_db.index.ntotal}")
print(f"{'='*60}")