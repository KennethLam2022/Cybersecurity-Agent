from memory import ConversationMemory


def test_global_search_covers_tenant_metadata_and_hides_other_tenants(tmp_path):
    memory = ConversationMemory(str(tmp_path / "search.db"))
    first = memory.register_user("first@example.com", "Correct-Horse-91", "First")
    second = memory.register_user("second@example.com", "Correct-Horse-92", "Second")
    first_conversation = memory.create_conversation(
        tenant_id=first["tenant_id"], user_id=first["id"], agent_id=first["agent_id"], title="Incident review",
    )
    memory.create_conversation(
        tenant_id=second["tenant_id"], user_id=second["id"], agent_id=second["agent_id"], title="Incident private",
    )
    items = memory.global_admin_search("Incident", first["tenant_id"])
    assert {(item["type"], item["id"]) for item in items} == {("conversation", first_conversation["id"])}
