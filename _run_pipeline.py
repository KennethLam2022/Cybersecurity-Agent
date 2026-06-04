"""Pipeline: 处理项目测试文档 — 完全复用 main.py 的页面 pipeline 逻辑"""
import sys, os, json, time, logging, subprocess
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, TimeoutError

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)

ROOT = Path(__file__).parent
AGENT = ROOT / "packages" / "agent" / "src"
SRC = ROOT / "项目测试文档"
OUT = ROOT / "RAG_DATA" / "03_cleaned" / "10-项目测试文档"

sys.path.insert(0, str(ROOT / "packages" / "preprocessor" / "src"))
sys.path.insert(0, str(AGENT))

_enc_key = AGENT / "agent_data" / ".encryption_key"
if _enc_key.exists():
    os.environ["LLM_KEY_ENCRYPTION_KEY"] = _enc_key.read_text(encoding="utf-8").strip()

# 先调用服务端清理环境
try:
    import urllib.request
    req = urllib.request.Request("http://127.0.0.1:8000/api/admin/cleanup", method="POST")
    with urllib.request.urlopen(req, timeout=10) as resp:
        result = json.loads(resp.read().decode())
        logger.info(f"🧹 环境清理: {result.get('message', 'ok')}")
except Exception as e:
    logger.warning(f"⚠️ 环境清理调用失败 (服务未启动?): {e}")

# 先验证 key
from memory import get_llm_config_card
chunk = get_llm_config_card("chunk")
ak = chunk.get("api_key", "")
logger.info(f"读到的 key: {ak[:10]}...{ak[-4:] if len(ak)>=4 else ''} (len={len(ak)})")

from odl_parser import OdlParser
from llm_cleaner import LlmCleaner
from deduplicator import Deduplicator

OUT.mkdir(parents=True, exist_ok=True)

SUPPORTED = {".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".xlsx", ".xls"}
files = sorted([
    f for f in SRC.iterdir()
    if f.suffix.lower() in SUPPORTED and not f.name.startswith("~$")
], key=lambda p: p.stat().st_size)

logger.info(f"项目测试文档: 共 {len(files)} 个文件")

dedup = Deduplicator()
parser = OdlParser()
cleaner = LlmCleaner()

# 验证 cleaner 里的 key
logger.info(f"cleaner key: {cleaner.api_key[:10]}...{cleaner.api_key[-4:]}")
logger.info(f"cleaner base_url: {cleaner.api_base}")
logger.info(f"cleaner model: {cleaner.model}")

total = len(files)
success_count = 0
fail_count = 0
new_md_paths = []

start_all = time.time()

for idx, fpath in enumerate(files, 1):
    logger.info(f"[{idx}/{total}] {fpath.name}")

    dr = dedup.check_file("", fpath.name)
    if dr.is_duplicate:
        logger.warning(f"  ⚠️ Layer 1 重复: {dr.reason}")
        continue

    try:
        parse_data = parser.parse(str(fpath))
        raw_text = parse_data["full_markdown"]
        engine = parse_data["engine"]
        logger.info(f"  + 解析完成 ({engine}, {len(raw_text)} 字符)")
    except Exception as e:
        logger.error(f"  ❌ 解析失败: {e}")
        fail_count += 1
        continue

    if not raw_text or len(raw_text.strip()) < 10:
        logger.warning(f"  ! 内容为空，跳过")
        continue

    dr2 = dedup.check_text(raw_text, fpath.name)
    if dr2.is_duplicate:
        logger.warning(f"  ⚠️ Layer 2 重复: {dr2.reason}")
        continue

    try:
        logger.info(f"  LLM 清洗中...")
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(cleaner.clean_document, raw_text, fpath.stem)
            clean_data = fut.result(timeout=600)
        cleaned = clean_data["cleaned_markdown"]
    except TimeoutError:
        logger.error(f"  ❌ 清洗超时")
        fail_count += 1
        continue
    except Exception as e:
        logger.error(f"  ❌ 清洗失败: {e}")
        fail_count += 1
        continue

    md_name = fpath.stem.replace(" ", "_").replace("-", "_") + ".md"
    md_path = OUT / md_name
    md_path.write_text(cleaned, encoding="utf-8")
    new_md_paths.append(str(md_path))
    success_count += 1
    logger.info(f"  ✅ 已保存: {md_name}")

elapsed = time.time() - start_all
logger.info(f"\n{'='*50}")
logger.info(f"解析+清洗完成: {success_count} 成功, {fail_count} 失败, {elapsed:.0f}s")

if new_md_paths:
    logger.info(f"\n{'='*50}")
    logger.info(f"开始增量索引 ({len(new_md_paths)} 个文件)...")
    args = [sys.executable, "incremental_index.py"] + new_md_paths
    try:
        result = subprocess.run(args, cwd=str(ROOT / "packages" / "preprocessor" / "src"), capture_output=True, text=True, timeout=600)
        for line in result.stdout.split("\n"):
            if line.strip():
                logger.info(f"  {line.strip()}")
        if result.returncode == 0:
            logger.info(f"✅ 增量索引完成")
        else:
            logger.error(f"❌ 增量索引失败: {result.stderr[:300]}")
    except subprocess.TimeoutExpired:
        logger.error(f"❌ 增量索引超时")

logger.info(f"\n{'='*50}")
logger.info(f"全部完成!")
logger.info(f"  成功: {success_count}")
logger.info(f"  失败: {fail_count}")
logger.info(f"  总耗时: {elapsed:.0f}s")