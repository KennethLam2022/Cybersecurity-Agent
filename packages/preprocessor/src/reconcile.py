"""手动同步 FAISS 和 Chroma 向量库数量"""

import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from incremental_index import _validate_consistency

if __name__ == "__main__":
    print("🔍 开始同步 FAISS ↔ Chroma 向量库...")
    _validate_consistency()
    print("✅ 同步完成，刷新页面查看结果")
