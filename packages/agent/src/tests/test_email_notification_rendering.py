from pathlib import Path


def test_email_notification_api_values_are_rendered_as_text():
    page = (Path(__file__).parents[1] / "static" / "email_notifications.html").read_text(encoding="utf-8")
    load_policies = page[page.index("async function loadPolicies"):page.index("async function savePolicy")]
    load_deliveries = page[page.index("async function loadDeliveries"):page.index("$('save').onclick")]

    assert "row.textContent=" in load_policies
    assert "row.textContent=" in load_deliveries
    assert ".innerHTML" not in load_policies
    assert ".innerHTML" not in load_deliveries
