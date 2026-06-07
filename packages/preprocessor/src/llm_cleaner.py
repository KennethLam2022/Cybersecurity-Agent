import os
import json
import time
import logging
from pathlib import Path
from typing import Optional
from openai import OpenAI

from memory import get_llm_config_card

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class LlmCleaner:
    """基于 LLM 的文档清洗与结构化。

    优先从 llm_configs 表读取 'chunk' 卡片配置（管理员可在后台修改），
    回退到系统环境变量 SILICONFLOW_API_KEY / SILICONFLOW_API_BASE / SILICONFLOW_MODEL。
    """

    def __init__(self):
        # 从 DB 读取文档清洗卡片配置
        chunk_cfg = get_llm_config_card('chunk')
        self.api_key = chunk_cfg.get('api_key') or os.environ.get("SILICONFLOW_API_KEY")
        self.api_base = chunk_cfg.get('base_url') or os.environ.get(
            "SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1"
        )
        self.model = chunk_cfg.get('model') or os.environ.get(
            "SILICONFLOW_MODEL", "Pro/MiniMaxAI/MiniMax-M2.5"
        )
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0.1"))
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

        if not self.api_key:
            raise ValueError(
                "文档清洗 LLM 未配置。请先在「后端模型」页面设置「文档清洗/切片 LLM」卡片，\n"
                "或设置环境变量: $env:SILICONFLOW_API_KEY=\"sk-你的key\""
            )

        self.client = OpenAI(
            base_url=self.api_base,
            api_key=self.api_key,
        )

    def clean_document(self, raw_text: str, file_id: str) -> dict:
        """对单份原始解析文本进行 LLM 清洗与结构化。

        输入: raw_text (来自 odl_parser 解析的 full_markdown)
        输出: {
            "file_id": str,
            "title": str,
            "standard_id": str,
            "body_markdown": str,  # 清洗后的正文
            "key_clauses": [str],  # 关键条款列表
        }
        """
        system_prompt = """你是一个网络安全标准文档清洗专家。请将以下原始解析文本清洗为统一格式：

## 标题
[文档标题]

**标准编号：** [标准号]

## 正文
[保留原文层级结构，修正OCR错误，去除乱码]

## 关键条款
- **条款X.X：** [条款内容]
- **条款X.X：** [条款内容]

要求：
1. 保留原有的标题层级（# ## ###）
2. 表格用HTML格式保留
3. OCR乱码/特殊字符需修正或删除
4. 条款编号保持原样
5. 输出为纯Markdown格式"""

        user_prompt = f"文件标识: {file_id}\n\n原始内容:\n\n{raw_text}"

        logger.info(f"LLM 清洗开始: {file_id} ({len(raw_text)} 字符)")
        start = time.time()

        max_retries = 5
        last_error = None
        for attempt in range(max_retries):
            try:
                response = self.client.chat.completions.create(
                    model=self.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    temperature=self.temperature,
                    max_tokens=self.max_tokens,
                    timeout=300,
                    stream=False,
                )
                last_error = None
                break
            except Exception as e:
                last_error = e
                status = getattr(e, 'status_code', 0) or (
                    e.http_status if hasattr(e, 'http_status') else 0)
                if status in (429, 500, 503) and attempt < max_retries - 1:
                    wait = 2 ** attempt + (hash(file_id) % 100) / 100.0
                    logger.warning(f"  LLM API {status} 错误 (第{attempt+1}次重试，等待{wait:.1f}s): {e}")
                    time.sleep(wait)
                    continue
                logger.error(f"  LLM API 调用失败 (已重试{attempt}次): {e}")
                raise

        cleaned_text = response.choices[0].message.content
        elapsed = time.time() - start

        logger.info(f"LLM 清洗完成: {file_id} ({elapsed:.1f}s, {len(cleaned_text)} 字符)")
        logger.info(
            f"  Token 使用: 输入={response.usage.prompt_tokens}, 输出={response.usage.completion_tokens}")

        return {
            "file_id": file_id,
            "cleaned_markdown": cleaned_text,
            "token_usage": {
                "prompt": response.usage.prompt_tokens,
                "completion": response.usage.completion_tokens,
            },
            "clean_time_seconds": round(elapsed, 2),
        }

    def clean_and_save(self, raw_text: str, file_id: str, output_dir: str):
        """清洗并保存到指定目录。"""
        data = self.clean_document(raw_text, file_id)

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        md_name = file_id.replace("/", "_").replace("\\", "_") + ".md"
        out_path = out_dir / md_name

        with open(out_path, "w", encoding="utf-8") as f:
            f.write(data["cleaned_markdown"])

        logger.info(f"已保存: {out_path}")
        return data


def quick_clean(raw_text: str, file_id: str = "test"):
    """快速测试 LLM 清洗效果。"""
    cleaner = LlmCleaner()
    data = cleaner.clean_document(raw_text, file_id)

    logger.info(f"\n{'='*60}")
    logger.info(f"文件: {file_id}")
    logger.info(f"耗时: {data['clean_time_seconds']}s")
    logger.info(f"Token: {data['token_usage']}")
    logger.info(f"\n--- 清洗结果 ---")
    logger.info(data["cleaned_markdown"][:3000])
    logger.info(f"{'='*60}\n")
    return data


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        text = sys.stdin.read()
    else:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            text = f.read()
    quick_clean(text, sys.argv[2] if len(sys.argv) > 2 else "stdin")
