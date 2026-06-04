"""手动处理遗留文件 — 复用 main.py 的并行管道"""
import sys, os, json, logging, threading, queue, time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent

sys.path.insert(0, str(_PROJECT_ROOT / "packages" / "preprocessor" / "src"))
from odl_parser import OdlParser
from llm_cleaner import LlmCleaner
from deduplicator import Deduplicator

CLEANED_DIR = _PROJECT_ROOT / "RAG_DATA" / "03_cleaned" / "上传文档"

FILES = [
    r"D:\学习资料\AI COURSE\项目\网络安全-移动运营商智能Agent\upload_staging\755e915d4c6e\2.1《信息安全技术  信息安全风险评估规范》GB_T 20984-2007.pdf",
    r"D:\学习资料\AI COURSE\项目\网络安全-移动运营商智能Agent\upload_staging\1a60862aa3ae\INCIDENT_RESPONSE_COOKBOOK_For_Windows_v1.1.pdf",
]


def parse_file(file_path: str) -> tuple[str, str] | None:
    """生产者：解析文件 → (raw_text, file_stem)"""
    try:
        fp = Path(file_path)
        logger.info(f"  解析: {fp.name}")
        parser = OdlParser()
        parse_data = parser.parse(str(fp))
        raw = parse_data["full_markdown"]
        logger.info(f"  解析完成: {fp.name} ({len(raw)} 字符)")
        return (raw, fp.stem)
    except Exception as e:
        logger.error(f"  ❌ 解析失败 {Path(file_path).name}: {e}")
        return None


def clean_and_save(raw_text: str, file_stem: str) -> str | None:
    """消费者：清洗 → 保存 .md"""
    try:
        logger.info(f"  LLM 清洗: {file_stem} ({len(raw_text)} 字符)...")
        cleaner = LlmCleaner()
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(cleaner.clean_document, raw_text, file_stem)
            try:
                clean_data = fut.result(timeout=600)
            except TimeoutError:
                logger.error(f"  ❌ 清洗超时: {file_stem}")
                return None
        cleaned = clean_data["cleaned_markdown"]

        CLEANED_DIR.mkdir(parents=True, exist_ok=True)
        md_name = file_stem.replace(" ", "_").replace("-", "_") + ".md"
        md_path = CLEANED_DIR / md_name
        md_path.write_text(cleaned, encoding="utf-8")
        logger.info(f"  ✅ 已保存: {md_path.name} ({len(cleaned)} 字符)")
        return str(md_path)
    except Exception as e:
        logger.error(f"  ❌ 清洗失败 {file_stem}: {e}")
        return None


def main():
    logger.info(f"=== 并行处理 {len(FILES)} 个遗留文件 ===")

    producer_queue: queue.Queue = queue.Queue(maxsize=8)
    producer_done = threading.Event()
    new_md_paths: list[str] = []
    md_lock = threading.Lock()

    # 生产者线程 — 串行解析
    def producer():
        for fp in FILES:
            if not Path(fp).exists():
                logger.warning(f"  文件不存在，跳过: {fp}")
                continue
            parsed = parse_file(fp)
            producer_queue.put(parsed)
        producer_done.set()

    # 消费者线程 — 清洗/保存
    def consumer():
        while True:
            try:
                parsed = producer_queue.get(timeout=5)
            except queue.Empty:
                if producer_done.is_set():
                    break
                continue
            if parsed is not None:
                raw_text, file_stem = parsed
                md_path = clean_and_save(raw_text, file_stem)
                if md_path:
                    with md_lock:
                        new_md_paths.append(md_path)

    t1 = threading.Thread(target=producer, daemon=True)
    t2 = threading.Thread(target=consumer, daemon=True)
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # 增量索引
    if new_md_paths:
        logger.info(f"\n=== 增量索引 ({len(new_md_paths)} 个文件) ===")
        preprocessor_dir = str(_PROJECT_ROOT / "packages" / "preprocessor" / "src")
        import subprocess
        args = [sys.executable, "incremental_index.py"] + new_md_paths
        result = subprocess.run(args, cwd=preprocessor_dir, capture_output=True, text=True, timeout=600)
        for line in result.stdout.split("\n"):
            if line.strip():
                logger.info(f"  {line.strip()}")
        if result.returncode != 0:
            logger.error(f"  ❌ 增量索引失败: {result.stderr[:500]}")
        else:
            logger.info("  ✅ 增量索引完成")
    else:
        logger.info("没有新文件需要索引")

    logger.info("=== 全部完成 ===")


if __name__ == "__main__":
    main()
