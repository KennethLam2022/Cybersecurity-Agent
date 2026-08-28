"""P5 capability routing for SecureNexus.

Routing is deterministic and deliberately conservative: ordinary security
questions stay on the existing RAG path, while document and presentation
requests first collect the minimum delivery context.
"""

from __future__ import annotations

import re
from typing import Any


_SECURITY_TERMS = (
    "网络安全", "信息安全", "数据安全", "等保", "关保", "密码", "漏洞", "安全",
    "应急", "合规", "隐私", "个人信息", "风险评估", "零信任", "审计",
)
_PPT_TERMS = ("ppt", "pptx", "幻灯片", "演示文稿", "汇报材料", "课件")
_WRITING_TERMS = (
    "写一篇", "写个", "撰写", "起草", "生成制度", "生成方案", "生成报告",
    "管理办法", "实施方案", "应急预案", "整改报告", "风险评估报告", "检查表",
    "对比分析", "文档",
)

_REQUIRED = {
    "writing": ("document_type", "audience", "purpose", "scope"),
    "presentation": ("audience", "purpose", "scenario", "page_count", "scope"),
}


def _has_any(query: str, terms: tuple[str, ...]) -> bool:
    text = query.lower()
    return any(term.lower() in text for term in terms)


def _infer_fields(query: str, mode: str) -> dict[str, str]:
    text = str(query or "").strip()
    fields: dict[str, str] = {}
    if "制度" in text or "管理办法" in text:
        fields["document_type"] = "网络安全制度"
    elif "方案" in text:
        fields["document_type"] = "网络安全实施方案"
    elif "报告" in text:
        fields["document_type"] = "网络安全报告"
    elif "预案" in text:
        fields["document_type"] = "网络安全应急预案"
    elif "检查表" in text:
        fields["document_type"] = "网络安全检查表"
    elif "对比" in text:
        fields["document_type"] = "法规或标准对比分析"
    if "管理层" in text or "领导" in text:
        fields["audience"] = "管理层"
    elif "培训" in text or "课件" in text:
        fields["audience"] = "培训对象"
    elif "技术" in text or "运维" in text:
        fields["audience"] = "技术与运维人员"
    if "培训" in text:
        fields["purpose"] = "网络安全培训"
        fields["scenario"] = "培训"
    elif "汇报" in text:
        fields["purpose"] = "管理汇报"
        fields["scenario"] = "汇报"
    elif "演练" in text:
        fields["purpose"] = "应急演练"
        fields["scenario"] = "演练"
    elif mode == "writing":
        fields["purpose"] = "网络安全管理与合规交付"
    if "通用" in text:
        fields["scope"] = "网络安全通用场景"
    match = re.search(r"(?:约|大约|共)?\s*(\d{1,2})\s*(?:页|page)", text, re.I)
    if match:
        fields["page_count"] = match.group(1)
    return fields


def route_capability(query: str, supplied: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify a request and report mandatory clarifications without guessing."""
    query = str(query or "").strip()
    supplied_clean = {
        str(key): str(value).strip()
        for key, value in (supplied or {}).items()
        if value is not None and str(value).strip()
    }
    if _has_any(query, _PPT_TERMS):
        mode = "presentation"
        confidence = 0.97
    elif _has_any(query, _WRITING_TERMS):
        mode = "writing"
        confidence = 0.89
    else:
        return {
            "mode": "chat", "confidence": 0.96, "network_security": _has_any(query, _SECURITY_TERMS),
            "missing_fields": [], "clarification": "", "fields": supplied_clean,
        }

    fields = {**_infer_fields(query, mode), **supplied_clean}
    security = _has_any(query, _SECURITY_TERMS) or any(
        term in str(fields.get("document_type", "")) for term in ("网络安全", "安全", "合规")
    )
    if not security:
        return {
            "mode": mode, "confidence": confidence, "network_security": False,
            "missing_fields": [], "fields": fields,
            "clarification": "安枢的写作与 PPT 生成功能仅处理网络安全相关交付物。请说明需要覆盖的网络安全主题、法规标准或安全场景。",
        }
    missing = [field for field in _REQUIRED[mode] if not fields.get(field)]
    labels = {
        "document_type": "文档类型", "audience": "目标受众", "purpose": "交付目的",
        "scope": "适用组织或行业范围", "scenario": "使用场景", "page_count": "页数或时长",
    }
    clarification = ""
    if missing:
        clarification = "为避免擅自假定交付边界，请补充：" + "、".join(labels[item] for item in missing) + "。"
    return {
        "mode": mode, "confidence": confidence, "network_security": True,
        "missing_fields": missing, "clarification": clarification, "fields": fields,
    }


def build_outline(mode: str, query: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Build an editable, evidence-aware outline after clarification is complete."""
    title = str(fields.get("title") or "").strip()
    if not title:
        if mode == "presentation":
            title = f"{fields.get('purpose', '网络安全')}汇报"
        else:
            title = str(fields.get("document_type") or "网络安全文档")
    if mode == "presentation":
        pages = int(str(fields.get("page_count") or "8"))
        pages = max(5, min(pages, 30))
        sections = [
            "背景与目标", "适用范围与现状", "风险与合规要求", "总体方案",
            "重点控制措施", "实施路线与责任分工", "资源与保障", "结论与下一步",
        ][:pages]
        while len(sections) < pages:
            sections.insert(-1, f"专题分析 {len(sections) - 6}")
        return {"title": title, "mode": mode, "pages": [
            {"page": index + 1, "title": section, "points": ["待结合授权资料补充证据与数据"]}
            for index, section in enumerate(sections)
        ], "query": query, "fields": fields}
    sections = ["编制说明", "适用范围与术语", "职责与原则", "管理与技术要求", "执行流程", "监督检查", "附则"]
    return {"title": title, "mode": mode, "sections": [
        {"heading": heading, "points": ["待结合授权资料补充可核验要求"]}
        for heading in sections
    ], "query": query, "fields": fields}
