from pathlib import Path


def test_document_profile_registry_loads_after_admin_fetch_authentication():
    content = (Path(__file__).parents[1] / "static" / "data_preview.html").read_text(encoding="utf-8")

    assert content.index("function initAdminFetch()") < content.index(
        "fetch('/api/documents/profile-registry')"
    )
    assert content.count("window.fetch = function(input, init)") == 1
    assert "industry/telecom" not in content  # options must come from the registry API
