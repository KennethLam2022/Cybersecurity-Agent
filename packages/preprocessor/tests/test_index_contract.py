import os
import sys

_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

from index_contract import compare_manifests, manifest_for, normalize_metadata, validate_manifest


def test_index_contract_normalizes_shared_metadata_and_manifest():
    chunks = [{"content": "文本", "chunk_id": "a", "file_name": "x.md", "page": 2}]
    metadata = normalize_metadata(chunks[0])
    manifest = manifest_for(chunks, vector_dimension=3)

    assert metadata["chunk_id"] == "a"
    assert metadata["page"] == 2
    assert manifest["chunk_count"] == 1
    assert manifest["chunks"][0]["text_hash"]
    assert validate_manifest(manifest, model=manifest["embedding_model"], dimension=3) == []


def test_index_contract_rejects_model_dimension_and_space_mismatch():
    manifest = manifest_for([], vector_dimension=3)
    manifest["embedding_model"] = "different"
    manifest["vector_dimension"] = 4
    manifest["chroma_space"] = "l2"

    errors = validate_manifest(manifest, model="expected", dimension=3)

    assert {"embedding model mismatch", "embedding dimension mismatch", "chroma distance space mismatch"} <= set(errors)


def test_manifest_comparison_detects_text_and_metadata_drift():
    left = manifest_for([{"content": "a", "chunk_id": "1"}], vector_dimension=3)
    right = manifest_for([{"content": "b", "chunk_id": "1"}], vector_dimension=3)

    report = compare_manifests(left, right)

    assert report["same"] is False
    assert report["changed"] == ["1"]
