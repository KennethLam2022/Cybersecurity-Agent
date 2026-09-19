"""正式检索管道 — Chroma + FAISS 双库召回 + 硅基流动 Reranker 重排序

用法：
  from retriever import CyberRetriever

  retriever = CyberRetriever()
  results = retriever.search("等保三级 访问控制要求", top_k=5)
  # [{"content": "...", "file_name": "...", "category": "...", "section": "...",
  #   "rerank_score": 0.98, "score": 0.42, "source": "chroma|faiss"}, ...]
"""
import os
import time
import logging
import requests
import json
import re
import random
import threading
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional
import chromadb
from chromadb.config import Settings

from langchain_community.vectorstores import FAISS
from langchain_ollama import OllamaEmbeddings

from rank_bm25 import BM25Okapi
from jieba_compat import load_jieba

from memory import get_llm_config_card
from metadata_filter import (
    MetadataFilterSpec,
    apply_metadata_filter,
    boost_by_metadata,
    build_chroma_where,
    infer_metadata_filter_from_query,
    merge_filter_specs,
    normalize_filter_spec,
)
from security_taxonomy import infer_categories_from_text
from profile_classifier import available_profiles, enabled_retrieval_profiles, profile_for_metadata
from clause_awareness import apply_clause_awareness
from index_contract import validate_manifest
from retrieval_text import build_bm25_text, tokenize_for_retrieval

jieba = load_jieba()

logger = logging.getLogger(__name__)

_RAG = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA"
_VECTOR_STORE_ROOT = Path(os.environ.get("SECURENEXUS_VECTOR_STORE_DIR", str(_RAG / "04_vector_store")))
_FAISS_DIR = str(_VECTOR_STORE_ROOT / "faiss_index")
_CHROMA_DIR = str(_VECTOR_STORE_ROOT / "chroma_db")
_CHROMA_COLLECTION = os.environ.get("SECURENEXUS_CHROMA_COLLECTION", "cyber_security")

_RETRIEVE_MULTIPLIER = 5


def _profile_metadata(doc: dict) -> dict:
    """Normalize profile metadata for new and legacy index entries."""
    normalized = dict(doc)
    profile = profile_for_metadata(
        str(normalized.get("profile", "")),
        str(normalized.get("category", "")),
    )
    configs = {str(item.get("profile", "")): item for item in available_profiles()}
    config = configs.get(profile, {})
    normalized["profile"] = profile
    normalized["scope"] = str(normalized.get("scope") or config.get("scope") or "unknown")
    normalized["industry"] = str(normalized.get("industry") or config.get("industry") or "")
    return normalized


def _filter_by_enabled_profiles(docs: list[dict], profiles: set[str]) -> list[dict]:
    if "all" in profiles:
        return [_profile_metadata(doc) for doc in docs]
    return [
        normalized
        for normalized in (_profile_metadata(doc) for doc in docs)
        if normalized["profile"] in profiles
    ]


def _filter_by_access_scope(docs: list[dict], access_scope: Optional[dict]) -> list[dict]:
    """Enforce RAG visibility server-side; legacy corpus remains public."""
    if not access_scope:
        return docs
    tenant_id = str(access_scope.get("tenant_id") or "")
    user_id = str(access_scope.get("user_id") or "")
    agent_id = str(access_scope.get("agent_id") or "")
    knowledge_base_id = str(access_scope.get("knowledge_base_id") or "")
    allowed = []
    for doc in docs:
        if knowledge_base_id:
            doc_kb = str(doc.get("knowledge_base_id") or "")
            # Legacy public entries have no KB metadata and remain visible only
            # when the selected KB is the public baseline.
            if doc_kb and doc_kb != knowledge_base_id:
                continue
            if not doc_kb and knowledge_base_id != "kb-public-general":
                continue
        visibility = str(doc.get("visibility") or "public")
        if visibility == "public":
            allowed.append(doc)
        elif visibility == "tenant" and tenant_id and doc.get("tenant_id") == tenant_id:
            allowed.append(doc)
        elif visibility == "private" and tenant_id and user_id and (
            doc.get("tenant_id") == tenant_id and doc.get("owner_user_id") == user_id
            and (not doc.get("agent_id") or doc.get("agent_id") == agent_id)
        ):
            allowed.append(doc)
    return allowed

# ---- 熔断器（简单版，专给Reranker用） ----


class _RerankCircuitBreaker:
    def __init__(self, failure_threshold=3, open_timeout=60.0):
        self.failure_threshold = failure_threshold
        self.open_timeout = open_timeout
        self.state = "CLOSED"
        self.failure_count = 0
        self.last_failure_time = 0.0
        self._lock = threading.Lock()

    def is_open(self) -> bool:
        with self._lock:
            if self.state == "OPEN":
                if time.monotonic() - self.last_failure_time > self.open_timeout:
                    self.state = "HALF_OPEN"
                    logger.info("Rerank熔断器: OPEN → HALF_OPEN，允许探测")
                    return False
                return True
            return False

    def record_success(self):
        with self._lock:
            if self.state == "HALF_OPEN":
                logger.info("Rerank熔断器: HALF_OPEN → CLOSED（探测成功）")
            self.state = "CLOSED"
            self.failure_count = 0

    def record_failure(self):
        with self._lock:
            self.failure_count += 1
            self.last_failure_time = time.monotonic()
            if self.failure_count >= self.failure_threshold:
                self.state = "OPEN"
                logger.warning(f"Rerank熔断器: CLOSED → OPEN（连续{self.failure_count}次失败）")

# ---- 重试工具 ----


def _should_retry_rerank(e: Exception) -> bool:
    if isinstance(e, requests.Timeout):
        return True
    if isinstance(e, requests.HTTPError):
        s = e.response.status_code
        if s in (429,) or s >= 500:
            return True
    return False


def _backoff_rerank(attempt: int, base: float = 2.0) -> float:
    sleep = min(base * (2 ** attempt), 30.0)
    return sleep + random.uniform(0, sleep * 0.5)


# 全局共享熔断器
_rerank_circuit_breaker = _RerankCircuitBreaker()


def validate_rerank_results(raw_results: list[dict], candidate_count: int) -> tuple[list[dict], str]:
    """Validate provider output without losing candidates or accepting bad indexes."""
    accepted = []
    seen = set()
    invalid = 0
    for item in raw_results or []:
        try:
            index = int(item.get("index"))
            score = float(item.get("relevance_score"))
        except (TypeError, ValueError, AttributeError):
            invalid += 1
            continue
        if index < 0 or index >= candidate_count or index in seen:
            invalid += 1
            continue
        if not (-1.0 <= score <= 1.0):
            invalid += 1
            continue
        seen.add(index)
        accepted.append({"index": index, "relevance_score": score})
    return accepted, ("invalid_results" if invalid else "ok")


class CyberRetriever:
    """网络安全知识库检索器 — Chroma + FAISS 双库 + 硅基流动 Reranker + 父文档检索 + BM25 混合检索"""

    def __init__(self, embedding_model: str = "quentinz/bge-small-zh-v1.5", use_hybrid: bool = True):
        # 从 DB 读取 Embedding 配置
        emb_cfg = get_llm_config_card('embedding')
        emb_model = emb_cfg.get('model') or embedding_model
        emb_base_url = emb_cfg.get('base_url') or "http://localhost:11434"
        self._embedding_model = OllamaEmbeddings(
            model=emb_model,
            base_url=emb_base_url,
        )
        # 从 DB 读取 Reranker 配置
        rerank_cfg = get_llm_config_card('reranker')
        self._rerank_url = ((rerank_cfg.get('base_url') or '').rstrip(
            '/') + '/rerank') if rerank_cfg.get('base_url') else "https://api.siliconflow.cn/v1/rerank"
        self._rerank_model = rerank_cfg.get('model') or "BAAI/bge-reranker-v2-m3"
        self._rerank_api_key = rerank_cfg.get(
            'api_key', '') or os.environ.get("SILICONFLOW_API_KEY", "")
        self._faiss_db: Optional[FAISS] = None
        self._chroma_client: Optional[chromadb.PersistentClient] = None
        self._chroma_collection = None
        self._parent_index: Optional[dict] = None
        self._bm25: Optional[BM25Okapi] = None
        self._bm25_docs: Optional[list[dict]] = None
        self._use_hybrid = use_hybrid
        self._index_manifest_path = _VECTOR_STORE_ROOT / "index_manifest.json"
        self._index_contract_errors: list[str] = []
        self.last_trace: dict = {}
        self._last_rerank_trace: dict = {"status": "not_run"}
        # BM25 在首次检索时按需构建，避免应用启动阶段读取并扫描全部父文档。
        # 用于匹配文档编号的正则，如 YD/T 2692-2014, GB/T 22239-2019
        self._doc_id_pattern = re.compile(r"([A-Z]+/[T]\s*\d+[-]?\d*)")
        # 否定句式关键词
        self._negation_patterns = re.compile(r"(不能|不含|禁止|除外|不要|不得|不可|不会|没有|不包含|不包括|不应|不允许)")

    def _detect_negation(self, query: str) -> tuple[bool, list[str]]:
        """检测查询中是否含否定句式，并提取正面的负向关键词

        返回：(has_negation, negative_keywords)
        """
        if not query:
            return False, []
        matches = self._negation_patterns.findall(query)
        if not matches:
            return False, []

        # 提取否定词后面的关键词作为负向检测词
        negative_keywords = []
        for m in self._negation_patterns.finditer(query):
            start = m.end()
            # 取否定词后面连续的中文/英文单词（最多5个词）
            rest = query[start:].strip()
            # 取前15个字符中的有效词
            words = []
            buf = ""
            for ch in rest[:15]:
                if ch.isalnum() or '\u4e00' <= ch <= '\u9fff':
                    buf += ch
                elif buf:
                    words.append(buf)
                    buf = ""
            if buf:
                words.append(buf)
            if words:
                negative_keywords.extend(words[:3])

        return True, list(set(negative_keywords))

    @staticmethod
    def _apply_negative_boost(docs: list[dict], neg_keywords: list[str], penalty: float = 0.15) -> list[dict]:
        """对命中负向关键词的 chunk 扣分"""
        if not neg_keywords:
            return docs
        for d in docs:
            content = d.get("content", "")
            hits = sum(1 for kw in neg_keywords if kw in content)
            if hits > 0:
                # 同时影响 score 和 rerank_score
                for key in ("score", "rerank_score"):
                    if d.get(key) is not None:
                        d[key] = max(0.0, d[key] - penalty * hits)
        return docs

    def _extract_doc_ids(self, query: str) -> list[str]:
        """从查询中提取文档编号关键词

        规则：
          1. 优先匹配完整文档编号格式：'YD/T 2692-2014' → 提取 '2692'
          2. 'GB/T 22239-2019' → 提取 '22239'
          3. 如果找不到完整格式，再回退到所有 4+ 位纯数字
        """
        # 优先匹配完整文档编号：字母/数字/斜杠后的数字部分（连字符前）
        full_format = re.findall(r"(?:[\w/]+[/\s])(\d{4,})[-–—]\d{4}", query)
        if full_format:
            return list(set(full_format))
        # 回退：所有 4 位以上数字（排除常见干扰如 '2014' 作为年份）
        ids = re.findall(r"\d{4,}", query)
        return list(set(ids))

    def _filter_by_doc_ids(self, docs: list[dict], doc_ids: list[str]) -> list[dict]:
        """如果查询中含有文档编号或类型，优先保留匹配的片段"""
        if not doc_ids:
            return docs

        # 将 doc_ids 分为数字类和字母类
        num_ids = [did for did in doc_ids if did.isdigit()]
        letter_ids = [did for did in doc_ids if not did.isdigit()]

        filtered = []
        for d in docs:
            file_name = d.get("file_name", "").upper()

            # 逻辑：
            # 1. 如果有数字 ID (如 2692)，文件名必须包含该数字
            # 2. 如果只有字母 ID (如 YDT)，文件名必须包含该缩写
            # 3. 如果两者都有，数字优先级更高

            if num_ids:
                if any(nid in file_name for nid in num_ids):
                    filtered.append(d)
            elif letter_ids:
                if any(lid in file_name.replace("-", "") for lid in letter_ids):
                    filtered.append(d)

        if filtered:
            logger.info(f"文档名预过滤: 命中关键词 {doc_ids}, 过滤后剩余 {len(filtered)}/{len(docs)} 个片段")
            return filtered
        return docs

    def _inject_parent_sections(self, docs: list[dict], doc_ids: list[str]) -> list[dict]:
        """从 parent_texts.json 直接注入匹配文档的父节

        当文档名预过滤命中后，如果文档的技术内容节没被语义搜索召回，
        从父索引中直接取出并注入候选池，由 reranker 统一排序。
        """
        self._load_parent_index()
        if not self._parent_index:
            return []

        # 已存在的 parent_id 或 chunk_id
        existing_ids = set()
        for d in docs:
            eid = d.get("parent_id") or d.get("chunk_id")
            if eid:
                existing_ids.add(eid)

        num_ids = [did for did in doc_ids if did.isdigit()]
        if not num_ids:
            return []

        injected = []
        for pid, pdata in self._parent_index.items():
            fn = pdata["file_name"].upper()
            if not any(nid in fn for nid in num_ids):
                continue
            if pid in existing_ids:
                continue
            injected.append({
                "content": pdata["text"],
                "file_name": pdata["file_name"],
                "category": pdata["category"],
                "section": pdata.get("section", ""),
                "chunk_id": pid,
                "parent_id": pid,
                "profile": pdata.get("profile", ""),
                "scope": pdata.get("scope", ""),
                "industry": pdata.get("industry", ""),
                "visibility": pdata.get("visibility", "public"),
                "tenant_id": pdata.get("tenant_id", ""),
                "owner_user_id": pdata.get("owner_user_id", ""),
                "agent_id": pdata.get("agent_id", ""),
                "document_id": pdata.get("document_id", ""),
                "score": 99.0,
                "rerank_score": None,
                "source": "parent_injection",
            })

        injected.sort(key=lambda x: len(x["content"]), reverse=True)
        return injected[:10]

    def refresh_faiss(self):
        self._faiss_db = None

    def _load_faiss(self):
        if self._faiss_db is not None:
            return
        self._validate_index_contract()
        idx_path = os.path.join(_FAISS_DIR, "index.faiss")
        if not os.path.exists(idx_path):
            raise FileNotFoundError(f"FAISS 索引不存在: {_FAISS_DIR}")
        t0 = time.time()
        # faiss C 层不支持中文路径，先复制到临时目录再加载
        import tempfile
        import shutil
        _tmp_load = os.path.join(tempfile.gettempdir(), "_cyber_faiss_load")
        # 多次重试清理 + 复制（Windows 文件锁可能导致 rmtree 失败）
        for attempt in range(3):
            try:
                if os.path.exists(_tmp_load):
                    shutil.rmtree(_tmp_load)
                shutil.copytree(_FAISS_DIR, _tmp_load)
                break
            except Exception:
                if attempt < 2:
                    time.sleep(0.1)
                else:
                    raise
        try:
            self._faiss_db = FAISS.load_local(
                _tmp_load,
                self._embedding_model,
                allow_dangerous_deserialization=True,
            )
        finally:
            shutil.rmtree(_tmp_load, ignore_errors=True)
        if self._index_manifest_path.exists():
            manifest = json.loads(self._index_manifest_path.read_text(encoding="utf-8"))
            expected_dimension = int(manifest.get("vector_dimension") or 0)
            actual_dimension = int(getattr(self._faiss_db.index, "d", 0) or 0)
            if expected_dimension and actual_dimension and expected_dimension != actual_dimension:
                raise RuntimeError(f"Embedding 维度不匹配: {actual_dimension} != {expected_dimension}")
        logger.info(f"FAISS 加载完成 ({time.time() - t0:.2f}s, {self._faiss_db.index.ntotal} vectors)")

    def _load_chroma(self):
        if self._chroma_collection is not None:
            return
        self._validate_index_contract()
        if not os.path.exists(_CHROMA_DIR):
            raise FileNotFoundError(f"Chroma 库不存在: {_CHROMA_DIR}")
        t0 = time.time()
        self._chroma_client = chromadb.PersistentClient(
            path=_CHROMA_DIR,
            settings=Settings(anonymized_telemetry=False),
        )
        self._chroma_collection = self._chroma_client.get_collection(_CHROMA_COLLECTION)
        metadata = self._chroma_collection.metadata or {}
        expected_space = os.environ.get("SECURENEXUS_CHROMA_SPACE", "cosine")
        actual_space = metadata.get("hnsw:space")
        if actual_space and actual_space != expected_space:
            raise RuntimeError(f"Chroma 距离空间不匹配: {actual_space} != {expected_space}")
        cnt = self._chroma_collection.count()
        logger.info(f"Chroma 加载完成 ({time.time() - t0:.2f}s, {cnt} chunks)")

    def _validate_index_contract(self):
        if self._index_contract_errors:
            raise RuntimeError("索引契约不匹配: " + "; ".join(self._index_contract_errors))
        if not self._index_manifest_path.exists():
            logger.warning("索引 manifest 不存在，使用兼容模式: %s", self._index_manifest_path)
            return
        try:
            manifest = json.loads(self._index_manifest_path.read_text(encoding="utf-8"))
            model = getattr(self._embedding_model, "model", "") or ""
            dimension = int(manifest.get("vector_dimension") or 0)
            self._index_contract_errors = validate_manifest(manifest, model=model, dimension=dimension)
        except Exception as exc:
            self._index_contract_errors = [f"manifest unreadable: {exc}"]
        if self._index_contract_errors:
            raise RuntimeError("索引契约不匹配: " + "; ".join(self._index_contract_errors))

    def _load_parent_index(self):
        if self._parent_index is not None:
            return
        p = _VECTOR_STORE_ROOT / "parent_texts.json"
        if not p.exists():
            logger.warning(f"父文档索引不存在: {p}，跳过父文档检索")
            self._parent_index = {}
            return
        t0 = time.time()
        self._parent_index = json.loads(p.read_text(encoding="utf-8"))
        logger.info(f"父文档索引加载完成: {len(self._parent_index)} 条 ({time.time() - t0:.2f}s)")

    def _bm25_cache_path(self) -> Path:
        return _VECTOR_STORE_ROOT / "bm25_cache.pkl"

    def _build_bm25_index(self):
        """从 parent_texts.json 构建 BM25 索引（懒加载 + 磁盘缓存）"""
        if self._bm25 is not None:
            return
        self._load_parent_index()
        if not self._parent_index:
            logger.warning("父文档索引为空，无法构建 BM25 索引")
            self._bm25 = BM25Okapi([])
            self._bm25_docs = []
            return

        # L4: try loading from cache; invalidate when parent_texts.json is newer
        cache_file = self._bm25_cache_path()
        parent_file = _VECTOR_STORE_ROOT / "parent_texts.json"
        parent_mtime = parent_file.stat().st_mtime if parent_file.exists() else 0
        if cache_file.exists() and cache_file.stat().st_mtime > parent_mtime:
            try:
                import pickle as _pkl
                with open(cache_file, "rb") as _f:
                    cached = _pkl.load(_f)
                self._bm25 = cached["bm25"]
                self._bm25_docs = cached["docs"]
                logger.info(f"BM25 索引从缓存加载: {len(self._bm25_docs)} 条")
                return
            except Exception:
                logger.warning("BM25 缓存加载失败，回退到完整构建")

        t0 = time.time()
        texts = []
        docs = []
        for pid, pdata in self._parent_index.items():
            text = build_bm25_text(pdata)
            if len(text) < 20:
                continue
            tokens = tokenize_for_retrieval(text)
            texts.append(tokens)
            docs.append({
                "content": str(pdata.get("text", "")).strip(),
                "file_name": pdata["file_name"],
                "title": pdata.get("title", ""),
                "standard_name": pdata.get("standard_name", ""),
                "aliases": pdata.get("aliases", []),
                "category": pdata["category"],
                "section": pdata.get("section", ""),
                "chunk_id": pid,
                "parent_id": pid,
                "profile": pdata.get("profile", ""),
                "scope": pdata.get("scope", ""),
                "industry": pdata.get("industry", ""),
                "visibility": pdata.get("visibility", "public"),
                "tenant_id": pdata.get("tenant_id", ""),
                "owner_user_id": pdata.get("owner_user_id", ""),
                "agent_id": pdata.get("agent_id", ""),
                "document_id": pdata.get("document_id", ""),
            })

        self._bm25 = BM25Okapi(texts)
        self._bm25_docs = docs
        logger.info(f"BM25 索引构建完成: {len(docs)} 条 ({time.time() - t0:.2f}s)")

        # L4: save to cache for next startup
        try:
            import pickle as _pkl
            with open(cache_file, "wb") as _f:
                _pkl.dump({"bm25": self._bm25, "docs": docs}, _f)
            logger.info(f"BM25 缓存已写入: {cache_file.name}")
        except Exception as _e:
            logger.warning(f"BM25 缓存写入失败: {_e}")

    def _bm25_search(self, query: str, top_k: int, access_scope: Optional[dict] = None) -> list[dict]:
        """BM25 关键词检索"""
        self._build_bm25_index()
        if not self._bm25_docs:
            return []

        t0 = time.time()
        tokens = tokenize_for_retrieval(query)
        scores = self._bm25.get_scores(tokens)
        top_indices = sorted(
            range(len(scores)), key=lambda i: scores[i], reverse=True
        )[:top_k]

        results = []
        for idx in top_indices:
            if scores[idx] > 0:
                doc = dict(self._bm25_docs[idx])
                doc["score"] = round(scores[idx], 4)
                doc["rerank_score"] = None
                doc["source"] = "bm25"
                results.append(doc)

        results = _filter_by_access_scope(results, access_scope)
        elapsed = time.time() - t0
        logger.info(f"BM25 检索: {len(results)} 条 ({elapsed:.3f}s)")
        return results

    @staticmethod
    def _fusion_weights(query_type: str = "general") -> tuple[float, float]:
        """Return vector/keyword weights for the query's retrieval intent."""
        if query_type in {"standard_lookup", "article_lookup"}:
            return 0.35, 0.65
        if query_type == "comparison":
            return 0.45, 0.55
        return 0.5, 0.5

    @staticmethod
    def _rrf_merge(
        vector_results: list[dict], bm25_results: list[dict], top_k: int,
        k: int = 60, query_type: str = "general",
    ) -> list[dict]:
        """RRF 融合：按查询意图自适应平衡向量和关键词结果。"""
        vector_weight, keyword_weight = CyberRetriever._fusion_weights(query_type)
        seen = {}
        for rank, doc in enumerate(vector_results):
            pid = doc.get("chunk_id") or doc.get("parent_id") or id(doc)
            score = vector_weight / (k + rank + 1)
            if pid not in seen or score > seen[pid]["_rrf_score"]:
                seen[pid] = dict(doc)
                seen[pid]["_rrf_score"] = score
                seen[pid]["_rrf_contrib"] = "vector"

        for rank, doc in enumerate(bm25_results):
            pid = doc.get("chunk_id") or doc.get("parent_id") or id(doc)
            score = keyword_weight / (k + rank + 1)
            if pid not in seen:
                seen[pid] = dict(doc)
                seen[pid]["_rrf_score"] = score
                seen[pid]["_rrf_contrib"] = "bm25"
            else:
                seen[pid]["_rrf_score"] = seen[pid].get("_rrf_score", 0) + score
                seen[pid]["_rrf_contrib"] = "hybrid"

        sorted_docs = sorted(
            seen.values(),
            key=lambda d: d.get("_rrf_score", 0),
            reverse=True,
        )

        for d in sorted_docs:
            d["rank_score"] = round(float(d.pop("_rrf_score", 0.0)), 8)
            d["rank_sources"] = d.pop("_rrf_contrib", "")

        logger.info(
            f"RRF 融合: {len(vector_results)} 向量 + {len(bm25_results)} BM25 → {len(sorted_docs)} 去重")
        return sorted_docs[:top_k]

    @staticmethod
    def _rrf_merge_many(result_sets: list[list[dict]], top_k: int, k: int = 60) -> list[dict]:
        """RRF 融合多个查询分支结果。"""
        seen = {}
        for branch_idx, docs in enumerate(result_sets):
            for rank, doc in enumerate(docs):
                pid = doc.get("chunk_id") or doc.get("parent_id") or id(doc)
                score = 1.0 / (k + rank + 1)
                if pid not in seen:
                    seen[pid] = dict(doc)
                    seen[pid]["_rrf_score"] = score
                    seen[pid]["_rrf_contrib"] = [branch_idx]
                else:
                    seen[pid]["_rrf_score"] = seen[pid].get("_rrf_score", 0) + score
                    seen[pid].setdefault("_rrf_contrib", []).append(branch_idx)

        sorted_docs = sorted(seen.values(), key=lambda d: d.get("_rrf_score", 0), reverse=True)
        for d in sorted_docs:
            d["rank_score"] = round(float(d.pop("_rrf_score", 0.0)), 8)
            d["rank_sources"] = d.pop("_rrf_contrib", [])
        return sorted_docs[:top_k]

    def _resolve_parent_docs(
        self, child_docs: list[dict], top_k: int, access_scope: Optional[dict] = None,
    ) -> list[dict]:
        """将子chunk列表按 parent_id 分组去重，返回带边界的父节上下文

        流程：
          子chunk（按rerank_score或score排序）→ 按parent_id聚合
          → 取每个父节最高分 → 按分排序 → 取top_k → 返回父节内容
        """
        self._load_parent_index()
        if not self._parent_index:
            return child_docs[:top_k]

        use_rerank_score = any(d.get("rerank_score") is not None for d in child_docs)

        parent_best = {}
        for d in child_docs:
            pid = d.get("parent_id") or d.get("chunk_id")
            if not pid:
                continue
            score = d.get("rerank_score") if use_rerank_score else d.get("score", 0)
            if pid not in parent_best or score is None:
                parent_best[pid] = (
                    score,
                    d.get("source", ""),
                    d.get("clause_awareness", 0),
                    d.get("clause_exact_match", False),
                    d.get("clause", ""),
                    {key: d.get(key, "") for key in ("chunk_id", "page", "page_number", "char_start", "char_end", "document_id", "knowledge_base_id", "tenant_id", "owner_user_id", "agent_id")},
                )
            elif score is not None:
                if use_rerank_score:
                    if score > parent_best[pid][0]:
                        parent_best[pid] = (
                            score,
                            d.get("source", ""),
                            d.get("clause_awareness", 0),
                            d.get("clause_exact_match", False),
                            d.get("clause", ""),
                            {key: d.get(key, "") for key in ("chunk_id", "page", "page_number", "char_start", "char_end", "document_id", "knowledge_base_id", "tenant_id", "owner_user_id", "agent_id")},
                        )
                else:
                    if score < parent_best[pid][0]:
                        parent_best[pid] = (
                            score,
                            d.get("source", ""),
                            d.get("clause_awareness", 0),
                            d.get("clause_exact_match", False),
                            d.get("clause", ""),
                            {key: d.get(key, "") for key in ("chunk_id", "page", "page_number", "char_start", "char_end", "document_id", "knowledge_base_id", "tenant_id", "owner_user_id", "agent_id")},
                        )

        sorted_pids = sorted(
            parent_best.keys(),
            key=lambda p: (
                1 if parent_best[p][3] else 0,
                parent_best[p][2] or 0,
                (parent_best[p][0] if parent_best[p][0] is not None else 0)
                if use_rerank_score
                else -(parent_best[p][0] if parent_best[p][0] is not None else 0),
            ),
            reverse=True,
        )

        result = []
        for pid in sorted_pids[:top_k]:
            parent_data = self._parent_index.get(pid)
            if not parent_data:
                continue
            parent_access = _filter_by_access_scope(
                [{
                    "visibility": parent_data.get("visibility", "public"),
                    "tenant_id": parent_data.get("tenant_id", ""),
                    "owner_user_id": parent_data.get("owner_user_id", ""),
                    "agent_id": parent_data.get("agent_id", ""),
                }], access_scope,
            )
            if not parent_access:
                continue
            score, src, clause_score, exact_match, clause, hit_metadata = parent_best[pid]
            content, context_meta = self._build_parent_context(
                parent_data.get("text", ""), clause, hit_metadata.get("content", ""),
            )
            result.append({
                "content": content,
                "file_name": parent_data["file_name"],
                "category": parent_data["category"],
                "section": parent_data.get("section", ""),
                "clause": clause,
                "clause_awareness": clause_score,
                "clause_exact_match": exact_match,
                "chunk_id": pid,
                "parent_id": pid,
                "profile": parent_data.get("profile", ""),
                "scope": parent_data.get("scope", ""),
                "industry": parent_data.get("industry", ""),
                "visibility": parent_data.get("visibility", "public"),
                "tenant_id": parent_data.get("tenant_id", ""),
                "owner_user_id": parent_data.get("owner_user_id", ""),
                "agent_id": parent_data.get("agent_id", ""),
                "document_id": parent_data.get("document_id", ""),
                "score": score if not use_rerank_score else None,
                "rerank_score": score if use_rerank_score else None,
                "source": src,
                "matched_chunk": hit_metadata,
                "parent_context": context_meta,
                "evidence_location": {key: hit_metadata.get(key, "") for key in ("clause", "page", "page_number", "char_start", "char_end")},
            })

        logger.info(f"父文档检索: {len(child_docs)} 子chunk → {len(parent_best)} 父节 → {len(result)} 返回")
        return result

    @staticmethod
    def _build_parent_context(parent_text: str, clause: str = "",
                              matched_text: str = "") -> tuple[str, dict]:
        """Bound parent evidence while retaining the matched clause and nearby context."""
        text = str(parent_text or "")
        max_chars = max(1200, int(os.getenv("RAG_PARENT_CONTEXT_CHARS", "6000")))
        radius = max(300, int(os.getenv("RAG_PARENT_CONTEXT_RADIUS", "1800")))
        if len(text) <= max_chars:
            return text, {"truncated": False, "original_chars": len(text), "returned_chars": len(text)}

        anchors = [str(clause or "").strip()]
        matched = str(matched_text or "").strip()
        if matched:
            anchors.append(matched[:160])
        anchor_pos = -1
        anchor_value = ""
        for anchor in anchors:
            if anchor and len(anchor) >= 2:
                anchor_pos = text.find(anchor)
                if anchor_pos >= 0:
                    anchor_value = anchor
                    break

        if anchor_pos < 0:
            head = max_chars // 2
            content = text[:head] + "\n\n[父节中间内容已省略]\n\n" + text[-(max_chars - head):]
            return content, {
                "truncated": True, "original_chars": len(text), "returned_chars": len(content),
                "strategy": "head_tail", "anchor": "",
            }

        start = max(0, anchor_pos - radius)
        end = min(len(text), anchor_pos + max(len(anchor_value), 1) + radius)
        if end - start > max_chars:
            start = max(0, anchor_pos - max_chars // 2)
            end = min(len(text), start + max_chars)
            start = max(0, end - max_chars)
        while start > 0 and text[start - 1] not in "\n。！？":
            start -= 1
        while end < len(text) and text[end] not in "\n。！？":
            end += 1
        prefix = "[父节前文已省略]\n" if start > 0 else ""
        suffix = "\n[父节后文已省略]" if end < len(text) else ""
        content = prefix + text[start:end].strip() + suffix
        return content, {
            "truncated": True, "original_chars": len(text), "returned_chars": len(content),
            "strategy": "anchor_window", "anchor": anchor_value,
        }

    @staticmethod
    def _clause_number(value: str) -> int | None:
        match = re.search(r"第\s*(\d+)\s*条", str(value or ""))
        if match:
            return int(match.group(1))
        return None

    @classmethod
    def _expand_adjacent_clauses(cls, docs: list[dict], query: str) -> list[dict]:
        """Promote already-recalled adjacent clauses from the same document/section."""
        signals = re.findall(r"第\s*(\d+)\s*条", str(query or ""))
        if not signals or not docs:
            return docs
        requested = int(signals[0])
        exact_docs = [d for d in docs if cls._clause_number(d.get("clause")) == requested]
        if not exact_docs:
            return docs
        exact_keys = {(d.get("document_id") or d.get("file_name"), d.get("section")) for d in exact_docs}
        expanded = list(docs)
        for index, doc in enumerate(docs):
            key = (doc.get("document_id") or doc.get("file_name"), doc.get("section"))
            number = cls._clause_number(doc.get("clause"))
            if key in exact_keys and number is not None and abs(number - requested) <= 1:
                item = dict(doc)
                item["adjacent_clause"] = number != requested
                item["clause_expansion"] = "same_section_adjacent"
                expanded[index] = item
        return expanded

    @staticmethod
    def _diversify_docs(docs: list[dict], top_k: int) -> list[dict]:
        """Apply light MMR-style diversification without changing relevance scores."""
        if len(docs) <= top_k:
            return docs
        selected = []
        remaining = list(docs)
        while remaining and len(selected) < top_k:
            best = None
            best_value = None
            for candidate in remaining:
                base = candidate.get("rerank_score")
                if base is None:
                    base = candidate.get("rank_score", candidate.get("score", 0.0)) or 0.0
                text = str(candidate.get("content", ""))[:3000]
                max_similarity = 0.0
                for prior in selected:
                    prior_text = str(prior.get("content", ""))[:3000]
                    if text and prior_text:
                        max_similarity = max(max_similarity, SequenceMatcher(None, text, prior_text).ratio())
                    if (candidate.get("parent_id") and candidate.get("parent_id") == prior.get("parent_id")):
                        max_similarity = max(max_similarity, 1.0)
                value = float(base) - 0.25 * max_similarity
                if selected and candidate.get("parent_id") in {
                    item.get("parent_id") for item in selected
                } and any(
                    item.get("parent_id") not in {prior.get("parent_id") for prior in selected}
                    for item in remaining
                ):
                    value -= 0.5
                if best_value is None or value > best_value:
                    best, best_value = candidate, value
            selected.append(best)
            remaining.remove(best)
        return selected

    def search(
        self,
        query: str,
        top_k: int = 10,
        use_rerank: bool = True,
        timeout: int = 30,
        sources: tuple = ("faiss", "chroma"),
        use_parent: bool = True,
        use_hybrid: Optional[bool] = None,
        metadata_filter: Optional[MetadataFilterSpec | dict] = None,
        use_chroma_where: bool = False,
        profiles: Optional[set[str] | list[str] | tuple[str, ...]] = None,
        access_scope: Optional[dict] = None,
        query_type: str = "general",
    ) -> list[dict]:
        """执行检索

        参数：
          query:        查询文本
          top_k:        返回结果数
          use_rerank:   是否使用 Reranker 重排序
          timeout:      Reranker API 超时时间（秒）
          sources:      检索源，("faiss",) / ("chroma",) / ("faiss", "chroma")
          use_parent:   是否启用父文档检索
          use_hybrid:   是否启用 BM25 + 向量混合检索（默认跟随实例配置）
          metadata_filter: 可选元数据过滤条件，默认与规则提取条件合并
          use_chroma_where: 是否将高置信 category 条件下推到 Chroma where
          profiles: 允许检索的资料 profile；未传时读取环境配置，默认只允许 general

        返回：
          [{"content", "file_name", "category", "section",
            "chunk_id", "parent_id", "score", "rerank_score", "source"}, ...]
        """
        if use_hybrid is None:
            use_hybrid = self._use_hybrid

        enabled_profiles = set(profiles) if profiles is not None else enabled_retrieval_profiles()
        filter_spec = merge_filter_specs(infer_metadata_filter_from_query(query), metadata_filter)
        seen = {}
        candidate_k = top_k * _RETRIEVE_MULTIPLIER
        trace = {
            "query": query,
            "query_type": query_type,
            "fusion_weights": {
                "vector": self._fusion_weights(query_type)[0],
                "keyword": self._fusion_weights(query_type)[1],
            },
            "metadata_filter": filter_spec.to_dict(),
            "counts": {"faiss": 0, "chroma": 0, "bm25": 0, "merged": 0},
            "chroma_where": None,
            "chroma_where_fallback": False,
            "metadata_filter_fallback": False,
            "hard_filter_no_match": False,
            "enabled_profiles": sorted(enabled_profiles),
            "profile_filter_fallback": False,
            "retrieval_degraded": False,
            "degraded_stages": [],
        }

        # ---- FAISS 召回 ----
        if "faiss" in sources:
            try:
                self._load_faiss()
                t0 = time.time()
                raw = self._faiss_db.similarity_search_with_score(query, k=candidate_k)
                trace["counts"]["faiss"] = len(raw)
                logger.info(f"FAISS 召回 {len(raw)} 条 ({time.time() - t0:.3f}s)")
                for doc, score in raw:
                    cid = doc.metadata.get("chunk_id", "")
                    if cid not in seen:
                        seen[cid] = {
                            "content": doc.page_content,
                            "file_name": doc.metadata.get("file_name", ""),
                            "category": doc.metadata.get("category", ""),
                            "section": doc.metadata.get("section", ""),
                            "clause": doc.metadata.get("clause", ""),
                            "chunk_type": doc.metadata.get("chunk_type", "section"),
                            "chunk_id": cid,
                            "parent_id": doc.metadata.get("parent_id", ""),
                            "profile": doc.metadata.get("profile", ""),
                            "scope": doc.metadata.get("scope", ""),
                            "industry": doc.metadata.get("industry", ""),
                            "visibility": doc.metadata.get("visibility", "public"),
                            "tenant_id": doc.metadata.get("tenant_id", ""),
                            "owner_user_id": doc.metadata.get("owner_user_id", ""),
                            "agent_id": doc.metadata.get("agent_id", ""),
                            "document_id": doc.metadata.get("document_id", ""),
                            "score": round(score, 4),
                            "rerank_score": None,
                            "source": "faiss",
                        }
            except Exception as e:
                trace["retrieval_degraded"] = True
                trace["degraded_stages"].append("faiss")
                logger.warning(f"FAISS 检索失败: {e}")

        # ---- Chroma 召回 ----
        if "chroma" in sources:
            try:
                self._load_chroma()
                t0 = time.time()
                q_emb = self._embedding_model.embed_query(query)
                where = build_chroma_where(filter_spec) if use_chroma_where else None
                trace["chroma_where"] = where
                results = self._chroma_collection.query(
                    query_embeddings=[q_emb],
                    n_results=candidate_k,
                    **({"where": where} if where else {}),
                )
                if where and not results.get("ids", [[]])[0]:
                    trace["chroma_where_fallback"] = True
                    logger.info("Chroma where 查询为空，回退到不带 where 的查询")
                    results = self._chroma_collection.query(
                        query_embeddings=[q_emb],
                        n_results=candidate_k,
                    )
                trace["counts"]["chroma"] = len(results["ids"][0])
                logger.info(f"Chroma 召回 {len(results['ids'][0])} 条 ({time.time() - t0:.3f}s)")
                for j, cid in enumerate(results["ids"][0]):
                    if cid not in seen:
                        meta = results["metadatas"][0][j]
                        seen[cid] = {
                            "content": results["documents"][0][j],
                            "file_name": meta.get("file_name", ""),
                            "category": meta.get("category", ""),
                            "section": meta.get("section", ""),
                            "clause": meta.get("clause", ""),
                            "chunk_type": meta.get("chunk_type", "section"),
                            "chunk_id": cid,
                            "parent_id": meta.get("parent_id", ""),
                            "profile": meta.get("profile", ""),
                            "scope": meta.get("scope", ""),
                            "industry": meta.get("industry", ""),
                            "visibility": meta.get("visibility", "public"),
                            "tenant_id": meta.get("tenant_id", ""),
                            "owner_user_id": meta.get("owner_user_id", ""),
                            "agent_id": meta.get("agent_id", ""),
                            "document_id": meta.get("document_id", ""),
                            "score": round(results["distances"][0][j], 4),
                            "rerank_score": None,
                            "source": "chroma",
                        }
            except Exception as e:
                trace["retrieval_degraded"] = True
                trace["degraded_stages"].append("chroma")
                logger.warning(f"Chroma 检索失败: {e}")

        if not seen:
            # 双库都失败时，尝试 BM25 兜底
            trace["retrieval_degraded"] = True
            trace["degraded_stages"].append("bm25_fallback")
            logger.warning("双库检索均失败，尝试 BM25 兜底检索")
            try:
                bm25_fallback = self._bm25_search(query, candidate_k, access_scope)
                if bm25_fallback:
                    logger.info(f"BM25 兜底成功: {len(bm25_fallback)} 条")
                    docs = bm25_fallback
                else:
                    self.last_trace = trace
                    return []
            except Exception as e:
                logger.error(f"BM25 兜底也失败: {e}")
                trace["degraded_stages"].append("bm25")
                self.last_trace = trace
                return []
        else:
            docs = list(seen.values())
            logger.info(f"双库去重后: {len(docs)} 条")

        # ---- BM25 混合检索 + RRF 融合 ----
        if use_hybrid and seen:
            bm25_results = self._bm25_search(query, candidate_k, access_scope)
            trace["counts"]["bm25"] = len(bm25_results)
            if bm25_results:
                docs = self._rrf_merge(
                    docs, bm25_results, candidate_k, query_type=query_type,
                )
        elif docs:
            for rank, doc in enumerate(docs):
                doc["rank_score"] = round(1.0 / (60 + rank + 1), 8)
                doc["rank_sources"] = doc.get("source", "vector")
        # ---------------------------------
        trace["counts"]["merged"] = len(docs)

        # Access control is the first boundary. Profile narrowing is applied only
        # to documents the caller is already allowed to see.
        before_access = len(docs)
        docs = _filter_by_access_scope(docs, access_scope)
        trace["counts"]["after_access_filter"] = len(docs)
        trace["access_filter_excluded"] = before_access - len(docs)
        before_profile = len(docs)
        normalized_for_profile = [_profile_metadata(item) for item in docs]
        pending_before_profile = sum(
            1 for item in normalized_for_profile if str(item.get("profile") or "") == "pending"
        )
        docs = _filter_by_enabled_profiles(docs, enabled_profiles)
        trace["counts"]["after_profile_filter"] = len(docs)
        trace["profile_filter_excluded"] = before_profile - len(docs)
        trace["pending_profile_excluded"] = (
            pending_before_profile if "pending" not in enabled_profiles else 0
        )
        # Capture which docs were excluded for eval feedback loop (H1)
        excluded_profiles = enabled_profiles | {"pending"}
        profile_excluded_docs = [
            str(item.get("file_name", "")) for item in normalized_for_profile
            if str(item.get("profile") or "") not in excluded_profiles
        ]
        trace["profile_filter_excluded_docs"] = sorted(set(profile_excluded_docs))[:20]
        if not docs:
            logger.info(f"Profile 过滤后无候选: enabled={sorted(enabled_profiles)}")
            self.last_trace = trace
            return []
        # -------------------------------------------------------------------------------

        # --- 负向检索：否定句式扣分 ---
        has_neg, neg_kw = self._detect_negation(query)
        if has_neg:
            logger.info(f"负向检索: 检测到否定句式，负向关键词={neg_kw}")
            docs = self._apply_negative_boost(docs, neg_kw)
        # --------------------------------

        # --- 元数据软加权 + 硬过滤（带回退） ---
        before_meta = len(docs)
        docs = boost_by_metadata(docs, filter_spec)
        if filter_spec.hard_filter:
            filtered_docs = apply_metadata_filter(docs, filter_spec)
            if filtered_docs:
                docs = filtered_docs
                logger.info(f"元数据过滤: {before_meta} → {len(docs)}")
            else:
                trace["hard_filter_no_match"] = True
                trace["counts"]["after_metadata_filter"] = 0
                logger.info("元数据硬过滤无结果，禁止回退到未过滤候选集")
                trace["counts"]["after_metadata_filter"] = before_meta
                logger.info("元数据硬过滤无结果，回退到 profile 过滤后的候选集")
        trace["counts"]["after_metadata_filter"] = len(docs)
        # --------------------------------

        # --- 文档名预过滤 ---
        doc_ids = self._extract_doc_ids(query)
        if doc_ids:
            docs = self._filter_by_doc_ids(docs, doc_ids)
            # --- 父节注入：从 parent_texts.json 直接注入匹配文档的技术节 ---
            injected = self._inject_parent_sections(docs, doc_ids)
            if injected:
                allowed_injected = _filter_by_enabled_profiles(injected, enabled_profiles)
                allowed_injected = _filter_by_access_scope(allowed_injected, access_scope)
                docs.extend(allowed_injected)
                logger.info(f"父节注入: {len(allowed_injected)}/{len(injected)} 条通过 profile 过滤")
        # ------------------------

        # 过滤废弃文档（_deprecated 路径 + ⚠️ 标记）
        clean_docs = [
            d for d in docs
            if "_deprecated" not in d.get("file_name", "")
            and "⚠️" not in d["content"][:50]
        ]
        filtered_count = len(docs) - len(clean_docs)
        if filtered_count:
            logger.info(f"过滤废弃文档: {filtered_count} 条")
            docs = clean_docs
            if not docs:
                logger.warning("所有检索结果均为废弃文档，返回空结果")
                return []

        docs = apply_clause_awareness(docs, query)
        docs = self._expand_adjacent_clauses(docs, query)
        trace["clause_awareness"] = any(float(d.get("clause_awareness", 0) or 0) > 0 for d in docs)

        if use_rerank and self._rerank_api_key:
            docs = self._rerank(query, docs, len(docs), timeout)
            docs = apply_clause_awareness(docs, query)
            rerank_trace = self._last_rerank_trace or {}
            if rerank_trace.get("status") == "degraded":
                trace["retrieval_degraded"] = True
                if "reranker" not in trace["degraded_stages"]:
                    trace["degraded_stages"].append("reranker")
        docs = sorted(
            docs,
            key=lambda x: (
                1 if x.get("clause_exact_match") else 0,
                x.get("rerank_score") if x.get("rerank_score") is not None else x.get("rank_score", 0.0),
                x.get("rank_score", 0.0),
            ),
            reverse=True,
        )
        docs = self._diversify_docs(docs, max(top_k * 2, top_k))
        trace["counts"]["after_rerank"] = len(docs)
        trace["rerank"] = dict(self._last_rerank_trace)

        if use_parent:
            docs = self._resolve_parent_docs(docs, top_k, access_scope)
        else:
            docs = docs[:top_k]
        trace["counts"]["returned"] = len(docs)

        # 确保所有数值为 Python 原生类型（numpy float32 不能 JSON 序列化）
        for d in docs:
            for key in ("score", "rerank_score"):
                if key in d and d[key] is not None:
                    d[key] = round(float(d[key]), 4)

        self.last_trace = trace
        return docs

    def search_multi(
        self,
        queries: list[str],
        top_k: int = 10,
        use_rerank: bool = True,
        timeout: int = 30,
        sources: tuple = ("faiss", "chroma"),
        use_parent: bool = True,
        use_hybrid: Optional[bool] = None,
        metadata_filter: Optional[MetadataFilterSpec | dict] = None,
        rerank_query: Optional[str] = None,
        use_chroma_where: bool = False,
        profiles: Optional[set[str] | list[str] | tuple[str, ...]] = None,
        access_scope: Optional[dict] = None,
        query_type: str = "general",
    ) -> list[dict]:
        """多 Query 召回后统一融合、过滤、重排和父文档聚合。"""
        unique_queries = []
        for q in queries:
            q = (q or "").strip()
            if q and q not in unique_queries:
                unique_queries.append(q)
        if not unique_queries:
            return []

        filter_spec = normalize_filter_spec(metadata_filter)
        branch_results = []
        branch_traces = []
        candidate_k = top_k * _RETRIEVE_MULTIPLIER
        for q in unique_queries:
            docs = self.search(
                q,
                top_k=top_k,
                use_rerank=False,
                timeout=timeout,
                sources=sources,
                use_parent=False,
                use_hybrid=use_hybrid,
                metadata_filter=filter_spec,
                use_chroma_where=use_chroma_where,
                profiles=profiles,
                access_scope=access_scope,
                query_type=query_type,
            )
            branch_results.append(docs)
            branch_traces.append(dict(self.last_trace))

        docs = self._rrf_merge_many(branch_results, candidate_k)
        before_meta = len(docs)
        docs = boost_by_metadata(docs, filter_spec)
        metadata_filter_fallback = False
        hard_filter_no_match = False
        if filter_spec.hard_filter:
            filtered_docs = apply_metadata_filter(docs, filter_spec)
            if filtered_docs:
                docs = filtered_docs
            else:
                hard_filter_no_match = True
                docs = []

        if use_rerank and self._rerank_api_key:
            docs = self._rerank(rerank_query or unique_queries[0], docs, len(docs), timeout)
            docs = apply_clause_awareness(docs, rerank_query or unique_queries[0])
        else:
            docs = apply_clause_awareness(docs, rerank_query or unique_queries[0])
        docs = self._expand_adjacent_clauses(docs, rerank_query or unique_queries[0])

        docs = sorted(
            docs,
            key=lambda x: (
                1 if x.get("clause_exact_match") else 0,
                x.get("rerank_score") if x.get("rerank_score") is not None else x.get("rank_score", 0.0),
                x.get("rank_score", 0.0),
            ),
            reverse=True,
        )
        docs = self._diversify_docs(docs, max(top_k * 2, top_k))

        if use_parent:
            docs = self._resolve_parent_docs(docs, top_k, access_scope)
        else:
            docs = docs[:top_k]

        for d in docs:
            for key in ("score", "rerank_score"):
                if key in d and d[key] is not None:
                    d[key] = round(float(d[key]), 4)

        self.last_trace = {
            "mode": "multi_query",
            "queries": unique_queries,
            "metadata_filter": filter_spec.to_dict(),
            "branches": branch_traces,
            "counts": {
                "branches": [len(b) for b in branch_results],
                "merged": before_meta,
                "after_metadata_filter": len(docs),
                "returned": len(docs),
            },
            "metadata_filter_fallback": metadata_filter_fallback,
            "hard_filter_no_match": hard_filter_no_match,
            "rerank": dict(getattr(self, "_last_rerank_trace", {"status": "not_run"})),
        }
        return docs

    def _rerank(
        self,
        query: str,
        docs: list[dict],
        top_n: int,
        timeout: int,
    ) -> list[dict]:
        """Reranker 重排序（带熔断 + 重试 + 降级日志）"""
        global _rerank_circuit_breaker

        # 熔断检查
        if _rerank_circuit_breaker.is_open():
            logger.warning("Rerank熔断器 OPEN，跳过重排序，退回双库排序")
            self._last_rerank_trace = {"status": "degraded", "reason": "circuit_open", "returned": len(docs)}
            return docs[:top_n]

        t0 = time.time()
        doc_texts = [d["content"] for d in docs]
        for attempt in range(3):
            try:
                resp = requests.post(
                    self._rerank_url,
                    headers={
                        "Authorization": f"Bearer {self._rerank_api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._rerank_model,
                        "query": query,
                        "documents": doc_texts,
                        "top_n": top_n,
                        "return_documents": True,
                    },
                    timeout=timeout,
                )
                resp.raise_for_status()
                data = resp.json()
            except Exception as e:
                if attempt < 2 and _should_retry_rerank(e):
                    wait = _backoff_rerank(attempt)
                    logger.warning(f"Reranker 第{attempt+1}次失败: {e}，{wait:.1f}s后重试")
                    time.sleep(wait)
                    continue
                # 重试耗尽或不可重试错误
                _rerank_circuit_breaker.record_failure()
                logger.warning(f"Reranker API 失败: {e}，退回双库排序")
                self._last_rerank_trace = {"status": "degraded", "reason": "provider_error", "error_type": type(e).__name__, "returned": len(docs)}
                return docs[:top_n]

        _rerank_circuit_breaker.record_success()

        validated, validation_status = validate_rerank_results(data.get("results", []), len(docs))
        ranked = []
        used = set()
        for r in validated:
            doc = dict(docs[r["index"]])
            doc["rerank_score"] = round(r["relevance_score"], 4)
            ranked.append(doc)
            used.add(r["index"])
        for index, doc in enumerate(docs):
            if index not in used:
                preserved = dict(doc)
                preserved["rerank_score"] = None
                ranked.append(preserved)

        self._last_rerank_trace = {
            "status": "completed" if validation_status == "ok" else "degraded",
            "reason": validation_status,
            "candidate_count": len(docs),
            "provider_count": len(data.get("results", []) or []),
            "accepted_count": len(validated),
            "returned": len(ranked[:top_n]),
        }

        logger.info(f"Reranker 重排序完成 ({time.time() - t0:.2f}s)")
        return ranked[:top_n]

    def stats(self) -> dict:
        """返回检索器状态"""
        info = {
            "faiss": False, "faiss_vectors": 0,
            "chroma": False, "chroma_chunks": 0,
            "reranker": self._rerank_model if self._rerank_api_key else None,
            "embedding_model": self._embedding_model.model if hasattr(self._embedding_model, 'model') else "unknown",
        }
        try:
            self._load_faiss()
            info["faiss"] = True
            info["faiss_vectors"] = self._faiss_db.index.ntotal
        except Exception as e:
            logger.warning(f"FAISS 加载失败: {e}")
        try:
            self._load_chroma()
            info["chroma"] = True
            info["chroma_chunks"] = self._chroma_collection.count()
        except Exception as e:
            logger.warning(f"Chroma 加载失败: {e}")
        return info

    def embed_query(self, text: str) -> list[float]:
        """公开 embedding 接口，供 agent 语义检测等场景复用"""
        return self._embedding_model.embed_query(text)


def _expected_category(query: str) -> str | None:
    hits = infer_categories_from_text(query)
    return hits[0] if hits else None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    retriever = CyberRetriever()
    print(f"\n检索器状态: {retriever.stats()}\n")

    test_queries = [
        "网络安全法 网络运营者的安全保护义务",
        "等保三级 安全计算环境 访问控制要求",
        "关键信息基础设施 供应链安全",
        "数据出境安全评估 重要数据",
        "电信和互联网 用户个人信息保护 技术要求",
        "GDPR 数据主体权利 个人数据跨境传输",
    ]

    for source_mode in [("faiss", "chroma"), ("faiss",), ("chroma",)]:
        label = "+".join(source_mode)
        faiss_ok = 0
        chroma_ok = 0
        top1_cat = 0

        for q in test_queries:
            results = retriever.search(q, top_k=3, sources=source_mode)
            if results:
                for r in results:
                    if r["source"] == "faiss":
                        faiss_ok += 1
                    else:
                        chroma_ok += 1
                exp = _expected_category(q)
                if exp and exp == results[0]["category"]:
                    top1_cat += 1

        print(f"{'='*70}")
        print(f"  [{label}] 6 查询测试")
        print(f"{'='*70}")
        print(f"  FAISS 贡献:    {faiss_ok} 条")
        print(f"  Chroma 贡献:   {chroma_ok} 条")
        print(f"  Top-1 分类命中: {top1_cat}/6")
        print()

    print("双库检索链路验证完成 ✅")
