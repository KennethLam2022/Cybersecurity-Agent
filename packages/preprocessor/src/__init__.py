"""文档预处理模块 — 文本解析、去重、清洗、向量化

使用示例：
    from deduplicator import Deduplicator
    from retriever import CyberRetriever
    from odl_parser import OdlParser
"""

__all__ = [
    "Deduplicator",
    "CyberRetriever",
    "OdlParser",
    "LlmCleaner",
]


def __getattr__(name):
    if name == "Deduplicator":
        from .deduplicator import Deduplicator
        return Deduplicator
    if name == "CyberRetriever":
        from .retriever import CyberRetriever
        return CyberRetriever
    if name == "OdlParser":
        from .odl_parser import OdlParser
        return OdlParser
    if name == "LlmCleaner":
        from .llm_cleaner import LlmCleaner
        return LlmCleaner
    raise AttributeError(name)
