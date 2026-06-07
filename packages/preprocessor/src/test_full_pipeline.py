from incremental_index import incremental_index
from llm_cleaner import LlmCleaner
from odl_parser import OdlParser
import os
import sys
import time

# 添加路径
_SRC = os.path.dirname(os.path.abspath(__file__))
_AGENT_SRC = os.path.join(_SRC, "..", "..", "agent", "src")
for p in [_SRC, _AGENT_SRC]:
    if p not in sys.path:
        sys.path.insert(0, p)

PDF = r"D:\学习资料\AI COURSE\项目\网络安全-移动运营商智能Agent\项目测试文档\2.3《信息安全技术  信息安全风险处理实施指南》GBT 33132-2016.pdf"
_RAG = os.path.join(_SRC, "..", "..", "..", "RAG_DATA")
OUT_DIR = os.path.join(_RAG, "03_cleaned", "test_output")
os.makedirs(OUT_DIR, exist_ok=True)

# 1. 解析
print("=" * 60)
print("STEP 1/4: 解析文档...")
print("=" * 60)


parser = OdlParser()
t0 = time.time()
data = parser.parse(PDF)
raw = data["full_markdown"]
pages = data.get("total_pages", "?")
print(f"[PASS] 解析完成: {pages}页, {len(raw)}字符, {time.time()-t0:.1f}s")

# 2. 清洗
print("\n" + "=" * 60)
print("STEP 2/4: LLM 清洗...")
print("=" * 60)


cleaner = LlmCleaner()
t0 = time.time()
clean_data = cleaner.clean_document(raw, os.path.basename(PDF))
cleaned = clean_data["cleaned_markdown"]
token_u = clean_data["token_usage"]
print(f"[PASS] 清洗完成: {len(cleaned)}字符")
print(f"       Token: P={token_u['prompt']} / C={token_u['completion']}, {time.time()-t0:.1f}s")

# 3. 保存
print("\n" + "=" * 60)
print("STEP 3/4: 保存 .md...")
print("=" * 60)

stem = os.path.splitext(os.path.basename(PDF))[0].replace(" ", "_").replace("-", "_")
md_path = os.path.join(OUT_DIR, stem + ".md")
with open(md_path, "w", encoding="utf-8") as f:
    f.write(cleaned)
print(f"[PASS] 已保存: {md_path}")
print(f"       大小: {os.path.getsize(md_path) / 1024:.0f} KB")

# 4. 增量索引
print("\n" + "=" * 60)
print("STEP 4/4: 增量索引 (父文档 + FAISS + Chroma)...")
print("=" * 60)


result = incremental_index([md_path])
print(f"[PASS] 父文档索引: 新加 {result['parent_added']} 节")
print(f"[PASS] 向量索引: {result['faiss_added']} chunks")

print("\n" + "=" * 60)
print("全流程完成 ✅")
print("=" * 60)
