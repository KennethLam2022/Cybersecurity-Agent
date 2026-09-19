import builtins

import pytest

from deduplicator import _get_embedder
import deduplicator


def test_local_sentence_transformer_is_not_loaded_by_default(monkeypatch):
    imported = []

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        imported.append(name)
        if name == "langchain_ollama":
            raise ImportError("optional dependency is unavailable")
        if name == "sentence_transformers":
            raise AssertionError("local fallback must be explicitly enabled")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(deduplicator, "_EMBED_MODEL", None)
    monkeypatch.delenv("SECURENEXUS_ENABLE_LOCAL_SENTENCE_TRANSFORMER", raising=False)

    with pytest.raises(RuntimeError, match="SECURENEXUS_ENABLE_LOCAL_SENTENCE_TRANSFORMER"):
        _get_embedder()

    assert "langchain_ollama" in imported
    assert "sentence_transformers" not in imported


def test_local_sentence_transformer_errors_are_actionable(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "langchain_ollama":
            raise ImportError("optional dependency is unavailable")
        if name == "sentence_transformers":
            raise ValueError("Keras 3 is not supported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(deduplicator, "_EMBED_MODEL", None)
    monkeypatch.setenv("SECURENEXUS_ENABLE_LOCAL_SENTENCE_TRANSFORMER", "1")

    with pytest.raises(RuntimeError, match="local-embedding") as exc_info:
        _get_embedder()

    assert "Keras 3 is not supported" in str(exc_info.value)
