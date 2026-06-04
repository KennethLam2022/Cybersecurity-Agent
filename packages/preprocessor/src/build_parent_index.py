"""建立父文档索引：扫描所有MD文件，按 ## 标题切分，保存父节完整文本

输出：vector_store/parent_texts.json
  {"parent_id": {"text": "完整节文本", "file_name": "...", "category": "...", "section": "..."}, ...}

无需重建 FAISS/Chroma，索引端完全不动。
"""
import json, logging
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_RAG = Path(__file__).resolve().parent.parent.parent.parent / "RAG_DATA"
BASE = _RAG / "03_cleaned"
STORE_DIR = _RAG / "04_vector_store"
STORE_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT = STORE_DIR / "parent_texts.json"


def extract_sections(file_path: str) -> list[dict]:
    """按 ## / ### / # 标题切分文档，返回每个节的完整文本"""
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
        result.append({
            "section": sec_title,
            "text": text,
        })
    return result


def build_index():
    files = []
    for cat_dir in sorted(BASE.iterdir()):
        if not cat_dir.is_dir() or cat_dir.name in ("test_output", "_deprecated", "_archive"):
            continue
        for md_file in sorted(cat_dir.glob("*.md")):
            files.append((str(md_file), cat_dir.name, md_file.stem))

    logger.info(f"共 {len(files)} 份文档")

    parent_index = {}
    for fp, cat, stem in files:
        sections = extract_sections(fp)
        for idx, sec in enumerate(sections):
            parent_id = f"{stem}__s{idx}"
            parent_index[parent_id] = {
                "text": sec["text"],
                "file_name": stem,
                "category": cat,
                "section": sec["section"],
            }

    OUTPUT.write_text(
        json.dumps(parent_index, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(f"父文档索引已保存: {OUTPUT}")
    logger.info(f"共 {len(parent_index)} 个父节")

    # 统计
    from collections import Counter
    cat_count = Counter(v["category"] for v in parent_index.values())
    logger.info("按分类:")
    for cat, cnt in cat_count.most_common():
        logger.info(f"  {cat}: {cnt}")
    avg_len = sum(len(v["text"]) for v in parent_index.values()) / len(parent_index)
    logger.info(f"平均父节长度: {avg_len:.0f} 字符")


if __name__ == "__main__":
    build_index()