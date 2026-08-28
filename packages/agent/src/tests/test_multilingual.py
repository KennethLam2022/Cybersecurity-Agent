from pathlib import Path

from agent import build_prompt_messages
from memory import ConversationMemory


def test_user_language_is_scoped_and_defaults_to_chinese(tmp_path):
    memory = ConversationMemory(str(tmp_path / "language.db"))
    assert memory.get_user_language("tenant-1", "user-1", "agent-1") == "zh-CN"
    assert memory.set_user_language("tenant-1", "user-1", "agent-1", "en-US") == "en-US"
    assert memory.get_user_language("tenant-1", "user-1", "agent-1") == "en-US"
    assert memory.get_user_language("tenant-1", "user-2", "agent-1") == "zh-CN"
    try:
        memory.set_user_language("tenant-1", "user-1", "agent-1", "fr-FR")
    except ValueError as exc:
        assert "zh-CN" in str(exc)
    else:
        raise AssertionError("unsupported language must fail")


def test_prompt_contains_selected_response_language():
    english = build_prompt_messages("What is network security?", [], include_example=False, response_language="en-US")[0][0]["content"]
    chinese = build_prompt_messages("什么是网络安全？", [], include_example=False, response_language="zh-CN")[0][0]["content"]
    assert "Response language" in english
    assert "回答语言" in chinese


def test_embed_chat_translates_dynamic_states_without_mixed_language():
    page = Path(__file__).parents[1] / "static" / "embed_chat.html"
    content = page.read_text(encoding="utf-8")

    assert "const translations" in content
    assert "function tr(key)" in content
    assert "tr('processing')" in content
    assert "tr('missingToken')" in content
    assert "tr('requestFailed')" in content
    assert "tr('noAnswer')" in content
    assert "pending.textContent='请求失败：'+e.message" not in content


def test_standalone_admin_pages_share_language_switcher():
    static_dir = Path(__file__).parents[1] / "static"
    helper = (static_dir / "page_i18n.js").read_text(encoding="utf-8")
    assert "securenexus_language" in helper
    assert "data-page-language" in helper
    assert "MutationObserver" in helper
    assert "Object.fromEntries" in helper
    for name in (
        "email_notifications.html", "langfuse_config.html", "sso_config.html",
        "admin_model_preview.html", "data_preview.html", "knowledge_base_governance.html",
    ):
        page = (static_dir / name).read_text(encoding="utf-8")
        assert 'data-page-language' in page
        assert '/static/page_i18n.js' in page


def test_frontend_account_area_has_localized_login_controls():
    page = (Path(__file__).parents[1] / "templates" / "index.html").read_text(encoding="utf-8")
    for fragment in (
        "const UI_TRANSLATIONS",
        "function applyUiLanguage()",
        "accountTitle:",
        "accountTitle: 'Sign in to SecureNexus'",
        "memoryEnabledLabel",
        "applyUiLanguage();",
        "uiText('ldapStatus')",
    ):
        assert fragment in page, fragment
