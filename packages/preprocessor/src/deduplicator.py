"""
三层文档去重引擎

  Layer 1 — 文件级硬去重 (毫秒级)
  Layer 2 — SimHash 文本指纹软去重 (秒级)
  Layer 3 — Embedding 向量语义去重 (入库后)

完全独立，不依赖 LLM，SimHash 用内置 hashlib。
标准号提取覆盖 GB/GB-T/YD/YD-T/JR-T/GM-T 等运营商常用标准。
"""

import os
import re
import json
import hashlib
import struct
import threading
from pathlib import Path
from collections import Counter
from typing import Optional

# ── 标准号正则 ──────────────────────────────────────────────
# 匹配: GB/T 20984-2007, GB 17859-1999, YD/T 2698-2015,
#       GB/T 22239.1-2024, YD/T 2584-2015,
#       GB_T_20984_2007（下划线格式）, GB_T_20984-2007 等
STANDARD_ID_PATTERN = re.compile(
    r"(?P<prefix>(?:GBT|GB|GB/T|GB/T|GB_Z|GB_T|GB_Z|YD|YD/T|YD/T|YD_T|JR/T|JR_T|GM/T|GM_T|LB/T|LB_T|DB\d{2}/T|GA/T|GA|SF/T|MZ/T|WB/T|CJ/T|JG/T)"
    r"(?:\s*[_\-/\+])?\s*)"
    r"(?P<number>\d+(?:\.\d+)?)"
    r"(?:[\s_\-－]*?(?P<year>(?:19|20)\d{2}(?!\d)))?"
)

# 文件名副本标记
COPY_MARKERS = re.compile(
    r"[（(]\d+[)）]|"
    r"\s*[_－\-]\s*副本\s*|"
    r"\s*副本\s*|"
    r"\s*Copy\s*|"
    r"\s*拷贝\s*|"
    r"\s*备份\s*|"
    r"\s*复制\s*|"
    r"\s*\((\d+)\)\s*",
    re.IGNORECASE,
)

YEAR_ANY = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")

# ────────────────────────────────────────────────────────────
# Layer 1: 文件级硬去重
# ────────────────────────────────────────────────────────────


def normalize_stem(name: str) -> str:
    """标准化文件名主干：去副本标记、去标准号、去版本号、去特殊字符、转小写"""
    # 手动取 stem: 只去掉最后一个点后的扩展名（如 .pdf, .md），保留中间的序号点
    if "." in name:
        # 检查最后一个点是否可能是扩展名
        last_dot = name.rfind(".")
        after_dot = name[last_dot + 1:]
        if after_dot.isalpha() and len(after_dot) <= 4:
            stem = name[:last_dot]
        else:
            stem = name
    else:
        stem = name
    # 去副本标记
    stem = COPY_MARKERS.sub(" ", stem).strip()
    # 去标准号 (GB/T XXXX-YYYY, YD/T XXXX, GB_T_XXXX_YYYY 等)
    stem = STANDARD_ID_PATTERN.sub("", stem).strip()
    # 兜底: 去标准号下划线格式 GB_T_20984_2007, YD_T_2698_2015 等
    # 以及 GB_T 20984-2007（空格分隔）
    stem = re.sub(
        r"(?:[_\-]?\b(?:GBT|GB|GB_T|GB_Z|YD|YD_T|JR_T|GM_T|LB_T|GA_T|SF_T)"  # prefix
        r"(?:\s*[_\-]?\s*)\d+(?:\s*[_\-]\s*\d{4})?)",  # number + optional year
        "", stem, flags=re.IGNORECASE,
    ).strip()
    # 去版本号前缀 (2.1, 3.0 等)
    stem = re.sub(r"^[\d.]+[\s\-_　]*", "", stem)
    # 去特殊字符
    stem = re.sub(r"[《》「」『』【】\[\]{}「」『』,，;；：:！!？?]", " ", stem)
    # 合并空格、转小写
    stem = re.sub(r"[_\s\-]+", "_", stem).strip("_")
    return stem.lower()


def _component_signature(name: str) -> str:
    """组件级签名：提取标准号+年份+标题核心 token → 排序后拼接

    用于标准化文件名差异大的同名文件匹配。
    例如: "3.3《信息安全技术 信息安全事件分类分级指南》GB_Z 20986-2007"
       和 "2007《信息安全技术 信息安全事件分类分级指南》GB_Z 20986"
       都 → "20986_gb_z_信息安全技术_信息安全事件分类分级指南"
    """
    stem = name.rsplit(".", 1)[0] if "." in name else name
    # 提取标准号
    sid = extract_standard_id(name)
    if not sid:
        return ""
    prefix = sid["prefix"].lower().replace("/", "_").replace("-", "_")
    number = sid["number"]
    year = sid["year"] if sid["year"] else ""
    # 去掉标准号 + 年份后取标题纯文本
    title = stem
    title = STANDARD_ID_PATTERN.sub("", title).strip()
    title = re.sub(
        r"(?:[_\-]?\b(?:GBT|GB|GB_T|GB_Z|YD|YD_T|JR_T|GM_T|LB_T|GA_T|SF_T)"
        r"(?:\s*[_\-]?\s*)\d+(?:\s*[_\-]\s*\d{4})?)",
        "", title, flags=re.IGNORECASE,
    ).strip()
    title = re.sub(r"\d{4}", "", title).strip()
    title = re.sub(r"[\d.]+", "", title).strip()
    title = re.sub(r"[《》「」『』【】\[\]{}，；：！？\s\-_]+", "_", title).strip("_")
    tokens = sorted(set(t for t in re.split(r"[_\s\-]+", title) if len(t) >= 2))
    parts = [number, prefix]
    if year:
        parts.append(year)
    parts.extend(tokens)
    return "_".join(parts)


def extract_standard_id(name: str) -> dict | None:
    """从文件名提取标准号

    返回:
        {"prefix": "GB/T", "number": "20984", "year": "2007",
         "full": "GB/T 20984-2007", "year_int": 2007}
        或 None
    """
    m = STANDARD_ID_PATTERN.search(name)
    if not m:
        # 尝试匹配更松散的模式（无空格号及各种分隔符）
        loose = re.search(
            r"(GBT|GB|GB_T|GB/Z|GB_Z|YD|YD_T|JR_T|GM_T|YD/T)"  # prefix
            r"(?:\s*[_\-/\+]?\s*)"  # optional separator
            r"(\d+(?:\.\d+)?)"    # number
            r"(?:\s*[－\-_]\s*(\d{4}))?",  # optional year
            name,
        )
        if loose:
            prefix = loose.group(1).strip().rstrip("_/\\- +")
            if prefix.upper() in ("GBT", "GB_T"):
                prefix = "GB/T"
            number = loose.group(2)
            year_str = loose.group(3) or ""
        else:
            return None
    else:
        prefix = m.group("prefix").strip().rstrip("_/\\- +")
        if prefix.upper() in ("GBT", "GB_T"):
            prefix = "GB/T"
        number = m.group("number")
        year_str = m.group("year") if m.group("year") else ""

    year_int = int(year_str) if year_str and len(year_str) == 4 else 0
    # 兜底：inline 未捕获年份时，从全文件名中搜索 4 位年份
    if not year_int:
        for ym in YEAR_ANY.finditer(name):
            y = int(ym.group(1))
            if 2000 <= y <= 2099:
                year_str = str(y)
                year_int = y
                break
    full = f"{prefix} {number}"
    if year_str:
        full += f"-{year_str}"
    return {
        "prefix": prefix,
        "number": number,
        "year": year_str,
        "year_int": year_int,
        "full": full,
    }


def extract_year(name: str) -> int | None:
    """从文件名提取年份"""
    # 先尝试标准号提取
    sid = extract_standard_id(name)
    if sid and sid["year_int"]:
        return sid["year_int"]
    # 兜底：任意 4 位年份
    for m in YEAR_ANY.finditer(name):
        y = int(m.group(1))
        if 2000 <= y <= 2099:
            return y
    return None


def file_md5(filepath: str, sample_only: bool = True) -> str:
    """计算文件 MD5

    Args:
        sample_only: True = 只读首尾各 64KB（快）, False = 全文件
    """
    h = hashlib.md5()
    size = os.path.getsize(filepath)
    if sample_only and size > 256 * 1024:
        # 首 64KB + 尾 64KB → 128KB 采样
        with open(filepath, "rb") as f:
            h.update(f.read(65536))
            f.seek(-65536, os.SEEK_END)
            h.update(f.read(65536))
        h.update(struct.pack("<Q", size))
    else:
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    return h.hexdigest()


def get_file_signature(filepath: str) -> dict:
    """文件级签名：大小 + MD5"""
    size = os.path.getsize(filepath)
    md5 = file_md5(filepath, sample_only=size > 256 * 1024)
    return {"size": size, "md5": md5}


# ────────────────────────────────────────────────────────────
# Layer 2: SimHash 文本指纹
# ────────────────────────────────────────────────────────────

class SimHash:
    """SimHash 文本指纹（64 位），用于检测近似重复"""

    FINGERPRINT_BITS = 64

    def __init__(self, text: str = ""):
        self.fingerprint = 0
        if text:
            self.build(text)

    def _hash_token(self, token: str) -> int:
        """对单个 token 生成 64 位哈希"""
        h = hashlib.md5(token.encode("utf-8"))
        return int(h.hexdigest()[:16], 16)

    def build(self, text: str):
        """从文本构建 SimHash 指纹"""
        # 简单分词（中文按字+词混合，英文按空格）
        tokens = self._tokenize(text)
        if not tokens:
            self.fingerprint = 0
            return

        # TF 加权
        weights = Counter(tokens)
        v = [0] * self.FINGERPRINT_BITS

        for token, weight in weights.items():
            h = self._hash_token(token)
            for i in range(self.FINGERPRINT_BITS):
                bit = (h >> i) & 1
                if bit:
                    v[i] += weight
                else:
                    v[i] -= weight

        fp = 0
        for i in range(self.FINGERPRINT_BITS):
            if v[i] > 0:
                fp |= (1 << i)
        self.fingerprint = fp

    def _tokenize(self, text: str) -> list[str]:
        """简易分词：中文按单字 + 英文按空格分词"""
        tokens = []
        # 英文/数字按空格
        for word in re.split(r"[\s,，。；;：:！!？?（）()【】\[\]{}""''《》<>/\\|]+", text):
            word = word.strip()
            if not word:
                continue
            if re.search(r"[\u4e00-\u9fff]", word):
                # 中文字符拆成单字（兼顾中文词语粒度）
                for ch in word:
                    if '\u4e00' <= ch <= '\u9fff':
                        tokens.append(ch)
                    else:
                        tokens.append(ch)
            else:
                tokens.append(word.lower())
        return tokens

    def distance(self, other: "SimHash") -> int:
        """计算汉明距离"""
        x = self.fingerprint ^ other.fingerprint
        dist = 0
        while x:
            dist += 1
            x &= x - 1
        return dist

    def similarity(self, other: "SimHash") -> float:
        """计算相似度 (0~1)"""
        d = self.distance(other)
        return 1.0 - d / self.FINGERPRINT_BITS

    def __repr__(self):
        return f"SimHash({self.fingerprint:016x})"


def is_simhash_duplicate(text1: str, text2: str, threshold: int = 3) -> bool:
    """判断两段文本是否为 SimHash 近似重复

    Args:
        threshold: 汉明距离阈值（≤3 视为重复，经验值）
    """
    h1 = SimHash(text1)
    h2 = SimHash(text2)
    return h1.distance(h2) <= threshold


# ────────────────────────────────────────────────────────────
# Layer 3: Embedding 向量语义去重
# ────────────────────────────────────────────────────────────

_EMBED_LOCK = threading.Lock()
_EMBED_MODEL = None


def _get_embedder():
    """懒加载 Embedding 模型"""
    global _EMBED_MODEL
    if _EMBED_MODEL is not None:
        return _EMBED_MODEL
    with _EMBED_LOCK:
        if _EMBED_MODEL is not None:
            return _EMBED_MODEL
        try:
            from langchain_ollama import OllamaEmbeddings
            _EMBED_MODEL = OllamaEmbeddings(
                model="quentinz/bge-small-zh-v1.5",
                base_url="http://localhost:11434",
            )
        except ImportError:
            # fallback: 不用 langchain
            try:
                from sentence_transformers import SentenceTransformer
                _EMBED_MODEL = SentenceTransformer("BAAI/bge-small-zh-v1.5")
            except ImportError:
                raise RuntimeError("Layer 3 需要 langchain-ollama 或 sentence-transformers")
    return _EMBED_MODEL


def compute_embedding(text: str) -> list[float]:
    """计算文本的 Embedding 向量"""
    emb = _get_embedder()
    if hasattr(emb, "embed_query"):
        return emb.embed_query(text)
    elif hasattr(emb, "encode"):
        return emb.encode([text])[0].tolist()
    else:
        raise TypeError(f"不支持的 embedder 类型: {type(emb)}")


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """余弦相似度"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def semantic_deduplicate(
    chunks: list[dict],
    threshold: float = 0.95,
) -> list[dict]:
    """向量语义去重：合并相似度超过阈值的 chunk

    Args:
        chunks: [{"id": str, "content": str, ...}, ...]
        threshold: 余弦相似度阈值 (0.95 = 极高相似才合并)

    Returns:
        去重后的 chunks（保留第一个出现的）
    """
    if not chunks:
        return chunks

    keep = []
    keep_embs = []

    for c in chunks:
        emb = compute_embedding(c.get("content", ""))
        is_dup = False
        for ke in keep_embs:
            if cosine_similarity(emb, ke) >= threshold:
                is_dup = True
                break
        if not is_dup:
            keep.append(c)
            keep_embs.append(emb)

    return keep


# ────────────────────────────────────────────────────────────
# 整合入口
# ────────────────────────────────────────────────────────────

class DedupResult:
    """去重检测结果"""

    def __init__(self):
        self.is_duplicate = False
        self.reason = ""
        self.layer = 0            # 1/2/3
        self.matched_files: list[str] = []
        self.incoming_year: Optional[int] = None
        self.existing_year: Optional[int] = None
        self.existing_standard: Optional[str] = None
        self.fuzzy_match: str = ""
        self.file_signature: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "duplicate": self.is_duplicate,
            "reason": self.reason,
            "layer": self.layer,
            "matched_files": self.matched_files,
            "incoming_year": self.incoming_year,
            "existing_year": self.existing_year,
            "existing_standard": self.existing_standard,
            "fuzzy_match": self.fuzzy_match,
            "file_signature": self.file_signature,
        }


class Deduplicator:
    """三层文档去重引擎

    用法:
        dedup = Deduplicator()
        result = dedup.check_file("/path/to/file.pdf")   # Layer 1
        result = dedup.check_text(text, "filename")       # Layer 2
        chunks = dedup.deduplicate_chunks(chunks)          # Layer 3
    """

    def __init__(self, source_dirs: list[str] | None = None, rag_data: str | None = None):
        self._source_dirs = source_dirs or []
        self._rag_data = Path(rag_data) if rag_data else None
        self._existing_cache: dict | None = None
        self._comp_index: dict[str, list[str]] = {}
        self._cache_lock = threading.Lock()

    def set_source_dirs(self, dirs: list[str]):
        self._source_dirs = dirs
        self._existing_cache = None

    def invalidate_cache(self):
        self._existing_cache = None
        self._comp_index.clear()

    def _collect_existing_files(self) -> dict:
        """{normalized_stem: [(year_or_None, original_stem, filepath_or_None, standard_id_or_None)]}"""
        if self._existing_cache is not None:
            return self._existing_cache

        existing: dict = {}
        # 组件签名 → stem 的映射（用于同名不同格式文件匹配）
        self._comp_index: dict[str, list[str]] = {}

        # 从 cleaned 目录
        if self._rag_data:
            cleaned = self._rag_data / "03_cleaned"
            if cleaned.is_dir():
                for md in cleaned.rglob("*.md"):
                    orig = md.name
                    norm = normalize_stem(orig)
                    year = extract_year(orig)
                    sid = extract_standard_id(orig)
                    if norm not in existing:
                        existing[norm] = []
                    existing[norm].append((year, md.name, str(md), sid))
                    # 建立组件签名索引
                    comp = _component_signature(orig)
                    if comp:
                        if comp not in self._comp_index:
                            self._comp_index[comp] = []
                        self._comp_index[comp].append(norm)

        # 从 source_dirs
        for d in self._source_dirs:
            dp = Path(d)
            if not dp.is_dir():
                continue
            for f in dp.iterdir():
                if f.suffix.lower() not in (".pdf", ".docx", ".doc", ".txt", ".md"):
                    continue
                norm = normalize_stem(f.name)
                year = extract_year(f.name)
                sid = extract_standard_id(f.name)
                if norm not in existing:
                    existing[norm] = []
                if not any(e[1] == f.name for e in existing[norm]):
                    existing[norm].append((year, f.name, str(f), sid))
                comp = _component_signature(f.name)
                if comp:
                    if comp not in self._comp_index:
                        self._comp_index[comp] = []
                    self._comp_index[comp].append(norm)

        self._existing_cache = existing
        return existing

    def invalidate_cache(self):
        self._existing_cache = None

    # ── Layer 1 ──────────────────────────────────────────

    def check_file(self, filepath: str, filename: str | None = None) -> DedupResult:
        """Layer 1: 文件级硬去重（耗时 <1ms）"""
        result = DedupResult()
        fname = filename or os.path.basename(filepath)
        fpath = Path(filepath) if os.path.exists(filepath) else None

        # 1a. 文件签名
        if fpath:
            sig = get_file_signature(str(fpath))
            result.file_signature = sig
            # 精确匹配：源目录已有同名文件
            for d in self._source_dirs:
                target = Path(d) / fname
                if target.exists():
                    result.is_duplicate = True
                    result.reason = f"源目录已存在同名文件: {fname}"
                    result.layer = 1
                    result.matched_files.append(str(target))
                    return result

        # 1b. 标准号提取 + 年份管控
        sid = extract_standard_id(fname)
        year = extract_year(fname)
        result.incoming_year = year

        existing = self._collect_existing_files()
        stem = normalize_stem(fname)

        if stem in existing:
            entries = existing[stem]
            result.fuzzy_match = stem
            result.matched_files = [e[1] for e in entries]

            # 检查标准号
            if sid:
                result.existing_standard = sid["full"]
                # 找同标准号的条目
                same_std = [e for e in entries if e[3] and e[3].get(
                    "number") == sid["number"] and e[3].get("prefix") == sid["prefix"]]
                if same_std:
                    existing_years = [e[0] for e in same_std if e[0]]
                    if existing_years:
                        max_ey = max(existing_years)
                        result.existing_year = max_ey
                        if year and year < max_ey:
                            result.is_duplicate = True
                            result.reason = f"标准 {sid['full']} 已有 {max_ey} 年版（当前为 {year} 年版），保留最新版本"
                            result.layer = 1
                            return result
                        elif year and year == max_ey:
                            result.is_duplicate = True
                            result.reason = f"标准 {sid['full']} {year} 年版已在库中"
                            result.layer = 1
                            return result
                        # year > max_ey: 新版本 → 判重，让新文件替换旧版
                        if year and year > max_ey:
                            result.is_duplicate = True
                            result.reason = f"标准 {sid['full']} 新版本（{year}）替换旧版（{max_ey}）"
                            result.layer = 1
                            result.matched_files = [e[1] for e in same_std]
                            result.existing_year = max_ey
                            return result

            # 无标准号 + 无年份：模糊名称匹配即判重
            if not year:
                # 检查是否有同名文件（非副本）
                exact_match = [e for e in entries if normalize_stem(
                    e[1]) == stem and not COPY_MARKERS.search(e[1])]
                if exact_match:
                    result.is_duplicate = True
                    result.reason = f"库中已有相似文件: {exact_match[0][1]}"
                    result.layer = 1
                    return result

        # 1c. 标准号交叉匹配：文件名不同但标准号相同（如 GB_T 20984-2007 → 2.1《...》GB_T_20984_2007）
        if sid and not result.is_duplicate:
            for key, entries in existing.items():
                same_std = [e for e in entries if e[3] and e[3].get("number") == sid["number"] and e[3].get(
                    "prefix").replace("_", "/") == sid["prefix"].replace("_", "/")]
                if same_std:
                    result.fuzzy_match = key
                    result.matched_files = [e[1] for e in same_std]
                    result.existing_standard = same_std[0][3]["full"] if same_std[0][3] else ""
                    existing_years = [e[0] for e in same_std if e[0]]
                    if existing_years:
                        max_ey = max(existing_years)
                        result.existing_year = max_ey
                        if year and year < max_ey:
                            result.is_duplicate = True
                            result.reason = f"标准 {sid['full']} 已有 {max_ey} 年版（当前为 {year} 年版），保留最新版本"
                            result.layer = 1
                            return result
                        elif year and year == max_ey:
                            result.is_duplicate = True
                            result.reason = f"标准 {sid['full']} {year} 年版已在库中"
                            result.layer = 1
                            return result
                    else:
                        # 同标准号但无年份 → 判重
                        result.is_duplicate = True
                        result.reason = f"库中已有同标准号文件: {same_std[0][1]}"
                        result.layer = 1
                        return result

        # 1d. 组件签名兜底匹配：前几轮都没命中，用标准号 + 标题 token 相似度
        if not result.is_duplicate:
            in_comp = _component_signature(fname)
            if in_comp and self._comp_index:
                matched_stems = self._comp_index.get(in_comp, [])
                if matched_stems:
                    all_matched = []
                    for ms in matched_stems:
                        entries = existing.get(ms, [])
                        for e in entries:
                            if e[1] not in all_matched:
                                all_matched.append(e[1])
                    if all_matched:
                        result.is_duplicate = True
                        result.reason = f"库中已有同标准文件（组件匹配）: {all_matched[0]}"
                        result.layer = 1
                        result.matched_files = all_matched
                        result.fuzzy_match = in_comp
                        return result

        return result

    # ── Layer 2 ──────────────────────────────────────────

    def check_text(self, parsed_text: str, filename: str, existing_fingerprints: list[int] | None = None) -> DedupResult:
        """Layer 2: SimHash 文本指纹软去重（需已解析文本，耗时 10~100ms）"""
        result = DedupResult()
        if not parsed_text:
            return result

        fp = SimHash(parsed_text[:10000])  # 只用前 10000 字加速
        if existing_fingerprints:
            for efp in existing_fingerprints:
                existing = SimHash()
                existing.fingerprint = efp
                if fp.distance(existing) <= 3:
                    # 文本级近似重复
                    result.is_duplicate = True
                    result.reason = f"文本与库中已有文档高度相似 (SimHash 汉明距离 ≤ 3)"
                    result.layer = 2
                    return result

        return result

    # ── Layer 3 ──────────────────────────────────────────

    def deduplicate_chunks(self, chunks: list[dict], threshold: float = 0.95) -> list[dict]:
        """Layer 3: 向量语义去重（入库后调用，耗时取决于 chunk 数）"""
        if not chunks:
            return chunks
        return semantic_deduplicate(chunks, threshold)


# ── 便捷函数 ─────────────────────────────────────────────

def quick_check_file(filepath: str) -> dict:
    """快速文件级去重检测（自包含，无需初始化 Deduplicator）"""
    dedup = Deduplicator()
    result = dedup.check_file(filepath)
    return result.to_dict()


def extract_standard_id_from_text(text: str) -> list[dict]:
    """从文本中提取所有标准号"""
    results = []
    for m in STANDARD_ID_PATTERN.finditer(text):
        year_str = m.group("year") or ""
        year_int = int(year_str) if year_str else 0
        results.append({
            "prefix": m.group("prefix").strip(),
            "number": m.group("number"),
            "year": year_str,
            "year_int": year_int,
            "full": f"{m.group('prefix').strip()} {m.group('number')}{' - ' + year_str if year_str else ''}",
        })
    return results
