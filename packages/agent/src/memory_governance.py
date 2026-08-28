"""Review-first L1 user profile proposal execution."""
from __future__ import annotations

import json


ALLOWED_PROFILE_FIELDS = {
    "display_name", "occupation", "response_style", "security_focus",
    "organization_type", "industry_profile",
}


def propose_user_profile(memory, llm, tenant_id: str, user_id: str, agent_id: str,
                         statement: str, source_conversation_id: str = "",
                         usage_sink=None) -> dict:
    statement = str(statement or "").strip()[:2000]
    if not statement:
        raise ValueError("用户表达不能为空")
    from reflection_engine import PROMPT_ASSET_DEFAULTS
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    asset = memory.get_active_prompt_asset("memory_profile_proposal")
    fallback = {"fields": {}, "rationale": "未配置画像提议模型，未生成候选。"}
    if llm is None or not asset.get("template"):
        result = fallback
    else:
        prompt = asset["template"].format(statement=statement)
        try:
            response = llm.chat([{"role": "user", "content": prompt}], temperature=0.0, max_tokens=800)
            if usage_sink: usage_sink(response, getattr(llm, "model", ""))
            text = str((response or {}).get("content") or "")
            start, end = text.find("{"), text.rfind("}")
            result = json.loads(text[start:end + 1]) if start >= 0 and end > start else fallback
        except (TypeError, ValueError, json.JSONDecodeError):
            result = fallback
    fields = result.get("fields") if isinstance(result, dict) and isinstance(result.get("fields"), dict) else {}
    clean_fields = {key: str(value).strip()[:300] for key, value in fields.items()
                    if key in ALLOWED_PROFILE_FIELDS and str(value).strip()}
    return memory.create_memory_profile_proposal(
        tenant_id, user_id, agent_id, clean_fields,
        str(result.get("rationale") or "")[:2000] if isinstance(result, dict) else "",
        int(asset.get("version") or 0), source_conversation_id,
    ) if clean_fields else {"status": "skipped", "reason": "no_safe_profile_fields", "prompt_version": asset.get("version", 0)}


def propose_memory_conflict(memory, llm, tenant_id: str, user_id: str, agent_id: str,
                            existing_memory_id: int, statement: str, usage_sink=None) -> dict:
    statement = str(statement or "").strip()[:2000]
    existing = memory._get_scoped_memory(tenant_id, user_id, agent_id, existing_memory_id)
    if not existing: raise ValueError("既有记忆不存在")
    if not statement: raise ValueError("新陈述不能为空")
    from reflection_engine import PROMPT_ASSET_DEFAULTS
    memory.ensure_prompt_assets(PROMPT_ASSET_DEFAULTS)
    asset = memory.get_active_prompt_asset("memory_conflict")
    if llm is None or not asset.get("template"):
        return {"status": "skipped", "reason": "conflict_model_unconfigured", "prompt_version": asset.get("version", 0)}
    field = next((tag for tag in existing.get("tags", []) if tag != "profile"), "memory")
    try:
        response = llm.chat([{"role": "user", "content": asset["template"].format(field=field, statement=statement)}],
                            temperature=0.0, max_tokens=800)
        if usage_sink: usage_sink(response, getattr(llm, "model", ""))
        text = str((response or {}).get("content") or ""); start, end = text.find("{"), text.rfind("}")
        payload = json.loads(text[start:end + 1]) if start >= 0 and end > start else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    decision = str(payload.get("decision") or "").lower()
    proposed = str(payload.get("proposed_content") or "").strip()
    if decision not in {"confirm", "update"} or not proposed:
        return {"status": "skipped", "reason": "invalid_conflict_proposal", "prompt_version": asset.get("version", 0)}
    return memory.create_memory_conflict_proposal(tenant_id, user_id, agent_id, existing_memory_id,
                                                   proposed, str(payload.get("rationale") or ""), asset.get("version", 0))
