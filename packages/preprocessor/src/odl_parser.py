import json
import time
import logging
import subprocess
from pathlib import Path
from typing import Optional

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger(__name__)


def _parse_docx(path: Path) -> str:
    """用 python-docx 解析 .docx 文件。也支持 .doc 文件（自动转换）。"""
    import docx
    doc = docx.Document(str(path))
    paragraphs = []
    for para in doc.paragraphs:
        text = para.text.strip()
        if text:
            # 保留标题层级（如有）
            style_name = para.style.name if para.style else ""
            if "Heading" in style_name:
                level = style_name.replace("Heading", "").strip()
                if level.isdigit():
                    prefix = "#" * int(level)
                    paragraphs.append(f"{prefix} {text}")
                else:
                    paragraphs.append(text)
            else:
                paragraphs.append(text)
    return "\n\n".join(paragraphs)


def _convert_doc_to_docx(path: Path) -> Path:
    """将 .doc 转换为临时 .docx（使用 Microsoft Word COM）。"""
    import win32com.client
    import pythoncom

    pythoncom.CoInitialize()
    docx_path = path.with_suffix(".docx_converted.docx")
    word = None
    try:
        word = win32com.client.Dispatch("Word.Application")
        word.Visible = False
        word.DisplayAlerts = False
        doc = word.Documents.Open(str(path.absolute()), False, False, False)
        doc.SaveAs(str(docx_path.absolute()), FileFormat=16)
        doc.Close()
        logger.info(f"  .doc → .docx 转换完成: {docx_path.name}")
        return docx_path
    except Exception as e:
        logger.error(f"  .doc 转换失败: {e}")
        raise
    finally:
        if word:
            word.Quit()
        pythoncom.CoUninitialize()


def _parse_pdf_odl(path: Path) -> str:
    """用 OpenDataLoader PDF 解析 PDF，输出 Markdown。失败/超时时回退到 pypdf"""
    import opendataloader_pdf
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    temp_dir = path.parent / ".odl_temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    def _run_odl():
        opendataloader_pdf.convert(
            input_path=[str(path.absolute())],
            output_dir=str(temp_dir.absolute()),
            format="markdown,json",
            quiet=True,
            markdown_with_html=True,
        )
        stem = path.stem
        md_path = temp_dir / f"{stem}.md"
        if md_path.exists():
            return md_path.read_text(encoding="utf-8")
        return ""

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            fut = pool.submit(_run_odl)
            try:
                result = fut.result(timeout=300)
                return result
            except FutureTimeout:
                logger.warning(f"  ⏰ OpenDataLoader 超时(300s)，回退到 pypdf")
                return ""
    except Exception as e:
        logger.warning(f"  OpenDataLoader 解析失败，回退到 pypdf: {e}")
        import pypdf
        try:
            with path.open("rb") as f:
                reader = pypdf.PdfReader(f)
                pages = []
                for i, page in enumerate(reader.pages):
                    text = page.extract_text() or ""
                    pages.append(text)
                return "\n\n---\n\n".join(pages)
        except Exception as e2:
            logger.error(f"  pypdf 回退也失败: {e2}")
            return ""
    finally:
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)


def _is_garbled(text: str) -> bool:
    """检测 pypdf 提取的文本是否因编码错误出现大量乱码。

    当 UTF-8 字节被错误解码为 Latin-1 时，会产生私有区字符、
    拉丁扩展字符、欧元符号等典型乱码信号。
    """
    sample = text[:2000].strip()
    if not sample or len(sample) < 30:
        return False

    n = len(sample)

    private_area = sum(1 for ch in sample if 0xE000 <= ord(ch) <= 0xF8FF)
    if private_area > 0:
        logger.warning(f"  检测到 {private_area} 个私有区字符，判定为乱码")
        return True

    if '\ufffd' in sample:
        logger.warning(f"  检测到替换字符 U+FFFD，判定为乱码")
        return True

    if '\u20ac' in sample:
        logger.warning(f"  检测到欧元符号 U+20AC，判定为乱码")
        return True

    latin_ext = sum(1 for ch in sample if 0x0080 <= ord(ch) <= 0x02FF)
    if n > 0 and latin_ext / n > 0.10:
        logger.warning(f"  拉丁扩展字符占比 {latin_ext/n:.1%}，判定为乱码")
        return True

    katakana = sum(1 for ch in sample if 0x30A0 <= ord(ch) <= 0x30FF)
    if katakana > 5 and latin_ext > 5:
        logger.warning(f"  检测到片假名({katakana})+拉丁扩展({latin_ext})混合，判定为乱码")
        return True

    return False


def _has_text_layer(path: Path) -> bool:
    """检测 PDF 是否有文本层（非扫描件）。"""
    try:
        import pypdf
        with path.open("rb") as f:
            reader = pypdf.PdfReader(f)
            for i in range(min(3, len(reader.pages))):
                text = reader.pages[i].extract_text()
                if text and len(text.strip()) > 50:
                    return True
            return False
    except Exception:
        return True


def _parse_pdf_ocr(path: Path) -> str:
    """用 pypdfium2 + RapidOCR 解析扫描件 PDF。"""
    import pypdfium2 as pdfium
    from rapidocr import RapidOCR

    ocr = RapidOCR()
    pages_text = []

    pdf = pdfium.PdfDocument(str(path))
    n_pages = len(pdf)

    for i in range(n_pages):
        page = pdf[i]
        bitmap = page.render(scale=2)
        img = bitmap.to_numpy()
        result = ocr(img)

        if result is not None and result.txts:
            page_text = "\n".join(result.txts)
        else:
            page_text = ""
        pages_text.append(page_text)
        logger.info(f"  OCR 第 {i+1}/{n_pages} 页: {len(page_text)} 字符")

    pdf.close()
    return "\n\n---\n\n".join(pages_text)


def _parse_image(path: Path) -> str:
    """用 RapidOCR 解析单张图片，输出 Markdown 文本。"""
    from rapidocr import RapidOCR

    try:
        from PIL import Image
        img_pil = Image.open(str(path))
    except Exception as e:
        logger.error(f"  图片打开失败: {e}")
        return ""

    ocr = RapidOCR()
    import numpy as np
    img_array = np.array(img_pil.convert("RGB"))
    result = ocr(img_array)

    if result is not None and result.txts:
        text = "\n".join(result.txts)
    else:
        text = ""
    logger.info(f"  图片 OCR: {len(text)} 字符")
    return text


def _parse_excel(path: Path) -> str:
    """用 openpyxl 解析 Excel，按知识库表格策略输出 Markdown。

    策略（来自知识库 2.3-表格处理）：
    1. 小/中表格 → 整表作为 Markdown 表格（保持完整性）
    2. 大表格（>20行或>10列） → 序列化为 attribute-value 对
    3. 每个 sheet 独立输出
    """
    import openpyxl

    wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
    sheets_text = []
    sheet_index = 0

    for sheet_name in wb.sheetnames:
        ws = wb[sheet_name]
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            continue

        cleaned = []
        for row in rows:
            cleaned.append([str(c) if c is not None else "" for c in row])

        sheet_text = f"## Sheet: {sheet_name}\n\n"

        if len(cleaned) <= 20 and len(cleaned[0]) <= 10:
            cleaned_20 = cleaned[:20]
            if len(cleaned) > 20:
                logger.info(f"  Sheet '{sheet_name}' 行数 {len(cleaned)} > 20，仅转前 20 行为表格，后续序列化")
            sheet_text += _table_to_markdown(cleaned_20)
            if len(cleaned) > 20:
                sheet_text += "\n\n### 完整序列化（属性-值对）\n\n"
                sheet_text += _table_serialize(cleaned)
        else:
            sheet_text += _table_serialize(cleaned)

        sheets_text.append(sheet_text)
        sheet_index += 1

    wb.close()
    return "\n\n---\n\n".join(sheets_text)


def _table_to_markdown(rows: list) -> str:
    """将二维数组渲染为 Markdown 表格。"""
    if not rows:
        return ""
    lines = []
    header = "| " + " | ".join(str(h) for h in rows[0]) + " |"
    sep = "| " + " | ".join("---" for _ in rows[0]) + " |"
    lines.append(header)
    lines.append(sep)
    for row in rows[1:]:
        lines.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(lines)


def _table_serialize(rows: list) -> str:
    """将大表格序列化为 attribute-value 对（知识库推荐策略）。

    每行转为一组 key=value 描述，适配 LLM 理解。
    """
    if not rows or not rows[0]:
        return ""
    headers = rows[0]
    entries = []
    for row in rows[1:]:
        pairs = []
        for i, cell in enumerate(row):
            if i < len(headers) and cell:
                pairs.append(f"{headers[i]}: {cell}")
        if pairs:
            entries.append("; ".join(pairs))
    return "\n".join(entries)


def _count_pages(path: Path) -> int:
    """统一获取文档的页数/计量单位。"""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _count_pdf_pages(path)
    elif ext in (".xlsx", ".xls"):
        try:
            import openpyxl
            wb = openpyxl.load_workbook(str(path), data_only=True, read_only=True)
            n = len(wb.sheetnames)
            wb.close()
            return n
        except Exception:
            return 1
    elif ext in (".jpg", ".jpeg", ".png"):
        return 1
    else:
        return 1


class OdlParser:
    """文档解析器：ODL(文本型PDF/DOCX) + RapidOCR(扫描件PDF/图片) + Excel。"""

    def __init__(self, force_ocr: bool = False):
        self.force_ocr = force_ocr

    def parse(self, file_path: str) -> dict:
        """解析单份文档，返回结构化 JSON。"""
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")

        ext = path.suffix.lower()
        supported = (".pdf", ".docx", ".doc", ".jpg", ".jpeg", ".png", ".xlsx", ".xls")
        if ext not in supported:
            raise ValueError(f"不支持的格式: {ext}，仅支持 {supported}")

        logger.info(f"开始解析: {path.name}")
        start = time.time()

        if ext == ".pdf":
            is_scanned = self.force_ocr or not _has_text_layer(path)
            if is_scanned:
                logger.info(f"  检测为扫描件，使用 RapidOCR")
                engine = "ocr"
                full_markdown = _parse_pdf_ocr(path)
            else:
                logger.info(f"  检测为文本型 PDF，使用 OpenDataLoader")
                engine = "odl"
                full_markdown = _parse_pdf_odl(path)
                if full_markdown and _is_garbled(full_markdown[:2000]):
                    logger.warning(f"  pypdf 回退输出乱码，切换为 OCR")
                    engine = "ocr"
                    full_markdown = _parse_pdf_ocr(path)
        elif ext in (".jpg", ".jpeg", ".png"):
            engine = "ocr"
            full_markdown = _parse_image(path)
        elif ext in (".xlsx", ".xls"):
            engine = "excel"
            full_markdown = _parse_excel(path)
        elif ext == ".doc":
            logger.info(f"  .doc 格式，转换后使用 python-docx")
            engine = "docx"
            docx_path = _convert_doc_to_docx(path)
            try:
                full_markdown = _parse_docx(docx_path)
            finally:
                if docx_path.exists():
                    docx_path.unlink()
        else:
            engine = "docx"
            full_markdown = _parse_docx(path)

        elapsed = time.time() - start
        logger.info(f"解析完成: {path.name} ({elapsed:.1f}s, {len(full_markdown)} 字符)")

        parts = path.parts
        rel_parts = parts[-3:] if len(parts) >= 3 else parts[-2:] if len(parts) >= 2 else parts
        file_id = "/".join(rel_parts)

        return {
            "file_id": file_id,
            "source_path": str(path.absolute()),
            "format": ext.lstrip("."),
            "total_pages": _count_pages(path),
            "full_markdown": full_markdown,
            "parse_time_seconds": round(elapsed, 2),
            "engine": engine,
        }

    def parse_and_save(self, file_path: str, output_dir: str):
        """解析文档并保存 JSON 到指定目录。"""
        data = self.parse(file_path)

        out_dir = Path(output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

        src_path = Path(file_path)
        json_name = src_path.stem + ".json"
        out_path = out_dir / json_name

        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

        logger.info(f"已保存: {out_path}")
        return data


def _count_pdf_pages(path: Path) -> int:
    """快速获取 PDF 页数。"""
    try:
        import pypdf
        with path.open("rb") as f:
            reader = pypdf.PdfReader(f)
            return len(reader.pages)
    except Exception:
        return 1


def quick_test(file_path: str, output_dir: Optional[str] = None):
    """快速测试解析效果，打印摘要信息。"""
    parser = OdlParser()
    data = parser.parse(file_path)

    logger.info(f"\n{'='*60}")
    logger.info(f"文件: {Path(file_path).name}")
    logger.info(f"格式: {data['format']}")
    logger.info(f"页数: {data['total_pages']}")
    logger.info(f"耗时: {data['parse_time_seconds']}s")
    logger.info(f"内容长度: {len(data['full_markdown'])} 字符")
    if data["full_markdown"]:
        logger.info(f"\n--- 前 2000 字符预览 ---")
        logger.info(data["full_markdown"][:2000])
        logger.info(f"\n--- 后 500 字符预览 ---")
        logger.info(data["full_markdown"][-500:])
    else:
        logger.info("⚠️  内容为空！")
    logger.info(f"{'='*60}\n")

    if output_dir:
        parser.parse_and_save(file_path, output_dir)

    return data


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        logger.info("用法: python odl_parser.py <文件路径> [输出目录]")
        sys.exit(1)
    quick_test(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)
