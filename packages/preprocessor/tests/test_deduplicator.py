from deduplicator import Deduplicator


def test_invalidate_cache_clears_file_and_component_indexes(tmp_path):
    dedup = Deduplicator(rag_data=str(tmp_path))
    dedup._existing_cache = {"cached": []}
    dedup._comp_index = {"signature": ["document"]}

    dedup.invalidate_cache()

    assert dedup._existing_cache is None
    assert dedup._comp_index == {}
