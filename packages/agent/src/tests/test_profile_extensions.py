import json

import profile_classifier
from profile_extensions import confirm_profile_extension, propose_profile_extension


def test_profile_extension_proposal_does_not_change_registry(tmp_path, monkeypatch):
    extension_path = tmp_path / "profile_extensions.json"
    monkeypatch.setenv("CYBER_AGENT_PROFILE_EXTENSIONS_PATH", str(extension_path))
    profile_classifier.load_profile_registry.cache_clear()

    proposal = propose_profile_extension(
        industry="制造业", keywords=["制造", "工业控制"], classifier_aliases=["工控安全"]
    )

    assert proposal["profile"].startswith("industry/")
    assert proposal["requires_manual_confirmation"] is True
    assert not extension_path.exists()


def test_confirmed_profile_extension_is_loaded_after_cache_refresh(tmp_path, monkeypatch):
    extension_path = tmp_path / "profile_extensions.json"
    monkeypatch.setenv("CYBER_AGENT_PROFILE_EXTENSIONS_PATH", str(extension_path))
    profile_classifier.load_profile_registry.cache_clear()
    proposal = propose_profile_extension(industry="金融科技", keywords=["支付"])

    stored = confirm_profile_extension(proposal)
    profile_classifier.load_profile_registry.cache_clear()
    profiles = {item["profile"] for item in profile_classifier.available_profiles()}

    assert stored["confirmed"] is True
    assert proposal["profile"] in profiles
    assert json.loads(extension_path.read_text(encoding="utf-8"))[0]["keywords"] == ["支付"]
