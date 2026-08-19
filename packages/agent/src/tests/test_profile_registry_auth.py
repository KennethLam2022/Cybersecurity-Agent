from auth import is_admin_route


def test_profile_registry_metadata_is_readable_without_admin_token():
    assert is_admin_route("/api/documents/profile-registry") is False


def test_document_write_endpoints_remain_protected():
    assert is_admin_route("/api/documents/profile-migration/confirm") is True
    assert is_admin_route("/api/documents/start-processing") is True
