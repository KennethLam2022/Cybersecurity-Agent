import json

import rag_document_sync


class FakeMemory:
    def __init__(self):
        self.calls = []

    def upsert_existing_rag_document(self, **kwargs):
        self.calls.append(kwargs)
        return "created" if len(self.calls) == 1 else "skipped"


def test_sync_preserves_sidecar_metadata_and_is_idempotent(monkeypatch, tmp_path):
    document = tmp_path / "文档.md"
    document.write_text("内容", encoding="utf-8")
    document.with_suffix(".meta.json").write_text(
        json.dumps({"source": "历史入库", "profile": "general"}, ensure_ascii=False),
        encoding="utf-8",
    )
    record = {
        "path": str(document), "file_name": "文档", "category": "通用",
        "profile": "general", "needs_review": False,
    }
    monkeypatch.setattr(rag_document_sync, "scan_profile_migration", lambda limit=None: {
        "total": 1, "pending": 0, "summary": {"general": 1}, "records": [record],
    })
    memory = FakeMemory()
    first = rag_document_sync.sync_existing_rag_documents(memory, changed_by="test")
    second = rag_document_sync.sync_existing_rag_documents(memory, changed_by="test")
    assert first["created"] == 1
    assert second["skipped"] == 1
    assert memory.calls[0]["metadata"]["source"] == "历史入库"
    assert memory.calls[0]["lifecycle_status"] == "published"


def test_preview_marks_unconfirmed_documents_for_review(monkeypatch, tmp_path):
    document = tmp_path / "待确认.md"
    document.write_text("内容", encoding="utf-8")
    monkeypatch.setattr(rag_document_sync, "scan_profile_migration", lambda limit=None: {
        "total": 1, "pending": 1, "summary": {"pending": 1}, "records": [{
            "path": str(document), "file_name": "待确认", "category": "上传文档",
            "profile": "pending", "needs_review": True,
        }],
    })
    result = rag_document_sync.scan_existing_rag_documents()
    assert result["records"][0]["lifecycle_status"] == "review"
    assert result["records"][0]["knowledge_base_id"] == "kb-public-general"
