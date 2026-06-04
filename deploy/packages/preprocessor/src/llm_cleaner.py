import os
import json
import time
import logging
from pathlib import Path
from typing import Optional
from openai import OpenAI

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


class LlmCleaner:
    """基于 SiliconFlow API 的 LLM 清洗与结构化。

    从系统环境变量读取 API 配置：
      SILICONFLOW_API_KEY    - 必填，API 密钥
      SILICONFLOW_API_BASE   - 可选，默认 https://api.siliconflow.cn/v1
      SILICONFLOW_MODEL      - 可选，默认 Pro/MiniMaxAI/MiniMax-M2.5
    """

    def __init__(self):
        self.api_key = os.environ.get("SILICONFLOW_API_KEY")
        self.api_base = os.environ.get(
            "SILICONFLOW_API_BASE", "https://api.siliconflow.cn/v1"
        )
        self.model = os.environ.get(
            "SILICONFLOW_MODEL", "Pro/MiniMaxAI/MiniMax-M2.5"
        )
        self.temperature = float(os.environ.get("LLM_TEMPERATURE", "0.1"))
        self.max_tokens = int(os.environ.get("LLM_MAX_TOKENS", "8192"))

        if not self.api_key:
            raise ValueError(
                "SILICONFLOW_API_KEY 未设置。\n"
                "请运行: $env:SILICONFLOW_API_KEY=\"sk-你的key\""
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

        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            stream=False,
        )

        cleaned_text = response.choices[0].message.content
        elapsed = time.time() - start

        logger.info(f"LLM 清洗完成: {file_id} ({elapsed:.1f}s, {len(cleaned_text)} 字符)")
        logger.info(f"  Token 使用: 输入={response.usage.prompt_tokens}, 输出={response.usage.completion_tokens}")

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

    print(f"\n{'='*60}")
    print(f"文件: {file_id}")
    print(f"耗时: {data['clean_time_seconds']}s")
    print(f"Token: {data['token_usage']}")
    print(f"\n--- 清洗结果 ---")
    print(data["cleaned_markdown"][:3000])
    print(f"{'='*60}\n")
    return data


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        text = sys.stdin.read()
    else:
        with open(sys.argv[1], "r", encoding="utf-8") as f:
            text = f.read()
    quick_clean(text, sys.argv[2] if len(sys.argv) > 2 else "stdin")