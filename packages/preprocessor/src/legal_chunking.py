"""Structure-aware Markdown chunking for laws, regulations, standards, and policies."""
from __future__ import annotations

import re
from typing import Any

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
_CLAUSE_RE = re.compile(
    r"(?m)^\s*(?:\*\*)?(第[一二三四五六七八九十百千万零〇两\d]+条)(?:\*\*)?\s*"
)


def _strip_heading(line: str) -> str:
    match = _HEADING_RE.match(line.strip())
    return match.group(2).strip() if match else ""


def _sections(markdown: str) -> tuple[str, list[tuple[str, str]]]:
    title = ""
    current = "前言"
    lines: list[str] = []
    sections: list[tuple[str, str]] = []

    def flush() -> None:
        if lines:
            sections.append((current, "\n".join(lines).strip()))

    for raw in str(markdown or "").splitlines():
        heading = _HEADING_RE.match(raw)
        if heading:
            level, heading_text = len(heading.group(1)), heading.group(2).strip()
            if not title and level == 1:
                title = heading_text
            if level <= 3:
                flush()
                current = heading_text
                lines.clear()
                lines.append(raw)
                continue
        lines.append(raw)
    flush()
    return title, sections


def _with_context(title: str, section: str, content: str) -> str:
    prefix = []
    if title:
        prefix.append(f"文档：{title}")
    if section and section != title:
        prefix.append(f"章节：{section}")
    return "\n".join(prefix + [content.strip()]).strip()


def _split_section(text: str, chunk_size: int, overlap: int) -> list[str]:
    if len(text) <= chunk_size:
        return [text]
    parts = [p.strip() for p in re.split(r"\n\n+|\n|(?<=[。；])", text) if p.strip()]
    result: list[str] = []
    current = ""
    for part in parts:
        if current and len(current) + len(part) + 1 > chunk_size:
            result.append(current)
            tail = current[-overlap:] if overlap else ""
            current = f"{tail}\n{part}".strip()
        else:
            current = f"{current}\n{part}".strip()
    if current:
        result.append(current)
    return result or [text]


def chunk_markdown_document(markdown: str, file_name: str = "", chunk_size: int = 800) -> list[dict[str, Any]]:
    """Split a cleaned Markdown document by legal clauses when available.

    Clause chunks retain document and section context. Non-clause sections use
    recursive splitting so guidance documents continue to work with the same API.
    """
    title, sections = _sections(markdown)
    title = title or file_name
    chunks: list[dict[str, Any]] = []
    section_index = 0

    for section, section_text in sections:
        body_text = re.sub(r"^\s*#{1,6}\s+.*?$", "", section_text, flags=re.M)
        if not re.sub(r"[\s#*_`-]+", "", body_text).strip():
            section_index += 1
            continue
        matches = list(_CLAUSE_RE.finditer(section_text))
        if matches:
            preamble = re.sub(r"^\s*#{1,6}\s+.*?$", "", section_text.split(matches[0].group(1), 1)[0], flags=re.M).strip()
            if re.sub(r"[\s#*_`-]+", "", preamble).strip():
                chunks.append({
                    "content": _with_context(title, section, preamble),
                    "section": section,
                    "clause": "",
                    "chunk_type": "section",
                    "section_index": section_index,
                    "piece_index": 0,
                })
            for clause_index, match in enumerate(matches):
                end = matches[clause_index + 1].start() if clause_index + 1 < len(matches) else len(section_text)
                clause = match.group(1)
                content = section_text[match.start():end].strip()
                chunks.append({
                    "content": _with_context(title, section, content),
                    "section": section,
                    "clause": clause,
                    "chunk_type": "clause",
                    "section_index": section_index,
                    "clause_index": clause_index,
                })
        else:
            pieces = _split_section(section_text, chunk_size, min(80, chunk_size // 4))
            for piece_index, piece in enumerate(pieces):
                chunks.append({
                    "content": _with_context(title, section, piece),
                    "section": section,
                    "clause": "",
                    "chunk_type": "section",
                    "section_index": section_index,
                    "piece_index": piece_index,
                })
        section_index += 1

    return chunks
