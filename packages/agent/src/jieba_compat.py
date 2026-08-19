"""Load legacy jieba without importing the deprecated pkg_resources API."""
from __future__ import annotations

import importlib
import sys
from typing import Any


_MISSING = object()
_JIEBA: Any = None


def load_jieba() -> Any:
    """Return jieba using its built-in filesystem resource fallback."""
    global _JIEBA
    if _JIEBA is not None:
        return _JIEBA
    previous = sys.modules.get("pkg_resources", _MISSING)
    sys.modules["pkg_resources"] = None
    try:
        _JIEBA = importlib.import_module("jieba")
    finally:
        if previous is _MISSING:
            sys.modules.pop("pkg_resources", None)
        else:
            sys.modules["pkg_resources"] = previous
    return _JIEBA
