"""Run a real FastAPI HTTP isolation smoke test against a temporary database."""

from __future__ import annotations

import asyncio
import gc
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[1]
for source in (
    ROOT / "packages" / "agent" / "src",
    ROOT / "packages" / "preprocessor" / "src",
):
    if str(source) not in sys.path:
        sys.path.insert(0, str(source))


async def _run() -> int:
    root = tempfile.mkdtemp(prefix="securenexus-p5-http-")
    try:
        os.environ["PROJECT_ROOT"] = root
        os.environ["ALLOW_LEGACY_LOCAL_WORKSPACE"] = "0"
        from main import app
        from app_state import agent
        from memory import ConversationMemory

        class SmokeRetriever:
            def search_multi(self, queries, **kwargs):
                return []

        class SmokeService:
            def __init__(self):
                self.memory = ConversationMemory(str(Path(root) / "agent_data" / "smoke.db"))
                self.retriever = SmokeRetriever()

        # Keep the real FastAPI routes/auth/session code, but avoid loading the
        # production vector indexes during a lightweight isolation check.
        agent._instance = SmokeService()

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://p5.test") as client:
            suffix = uuid.uuid4().hex[:8]
            accounts = []
            for label in ("first", "second"):
                response = await client.post("/api/auth/register", json={
                    "email": f"{label}-{suffix}@example.test",
                    "password": "correct-horse-17",
                    "display_name": label,
                })
                response.raise_for_status()
                accounts.append(response.json())

            route_payload = {
                "query": "制作网络安全培训PPT，5页",
                "fields": {
                    "audience": "新员工", "purpose": "培训", "scenario": "培训",
                    "scope": "网络安全通用场景",
                },
            }
            for account in accounts:
                response = await client.post(
                    "/api/generation/route", json=route_payload,
                    headers={"Authorization": f"Bearer {account['token']}"},
                )
                response.raise_for_status()
                assert response.json().get("generation", {}).get("id")

            first_headers = {"Authorization": f"Bearer {accounts[0]['token']}"}
            second_headers = {"Authorization": f"Bearer {accounts[1]['token']}"}
            first_items = (await client.get("/api/generation/requests", headers=first_headers)).json()["items"]
            second_items = (await client.get("/api/generation/requests", headers=second_headers)).json()["items"]
            first_ids = {item["id"] for item in first_items}
            second_ids = {item["id"] for item in second_items}
            assert first_ids and second_ids and first_ids.isdisjoint(second_ids)

            # Workflow definitions and runs are administrative, but must still
            # remain scoped to the authenticated organization and Agent.
            admin_headers = dict(first_headers)
            templates = await client.get("/api/admin/workflows/templates", headers=admin_headers)
            templates.raise_for_status()
            workflow_template = next(item for item in templates.json()["items"] if item["key"] == "compliance_qa")
            first_scope = (await client.get("/api/auth/me", headers=first_headers)).json()
            created = await client.post("/api/admin/workflows", headers=admin_headers, json={
                "tenant_id": first_scope["tenant_id"], "agent_id": first_scope["agent_id"],
                "name": "smoke scoped workflow", "description": "", "nodes": workflow_template["nodes"],
                "edges": workflow_template["edges"], "change_note": "HTTP isolation smoke",
            })
            created.raise_for_status()
            second_admin_headers = dict(second_headers)
            cross_scope = await client.get(
                "/api/admin/workflows",
                params={"tenant_id": first_scope["tenant_id"], "agent_id": first_scope["agent_id"]},
                headers=second_admin_headers,
            )
            assert cross_scope.status_code == 403

            knowledge_base = await client.post("/api/admin/knowledge-bases", headers=admin_headers, json={
                "tenant_id": first_scope["tenant_id"], "name": "Tenant scoped KB", "profile": "general",
                "visibility": "tenant",
            })
            knowledge_base.raise_for_status()
            cross_kb = await client.get(
                "/api/admin/knowledge-bases", params={"tenant_id": first_scope["tenant_id"]},
                headers=second_admin_headers,
            )
            assert cross_kb.status_code == 403

            external_app = await client.post("/api/admin/external-apps", headers=admin_headers, json={
                "tenant_id": first_scope["tenant_id"], "name": "Tenant scoped external app", "scopes": ["chat"],
            })
            external_app.raise_for_status()
            app_id = external_app.json()["app"]["id"]
            cross_app = await client.put(
                f"/api/admin/external-apps/{app_id}/status", headers=second_admin_headers,
                json={"enabled": False},
            )
            assert cross_app.status_code == 404

            webhook = await client.post("/api/admin/webhooks", headers=admin_headers, json={
                "tenant_id": first_scope["tenant_id"], "name": "Tenant scoped webhook",
                "url": "https://example.test/hook", "event_types": ["eval.completed"],
            })
            webhook.raise_for_status()
            cross_webhook = await client.put(
                f"/api/admin/webhooks/{webhook.json()['webhook']['id']}/status", headers=second_admin_headers,
                json={"enabled": False},
            )
            assert cross_webhook.status_code == 403

            # Tenant-scoped operational views must be available to each
            # organization's admin, while a requested foreign tenant is denied.
            first_memory = agent.memory
            first_memory.create_notification(
                first_scope["tenant_id"], "eval.completed", "first notification",
                user_id=first_scope["user_id"], agent_id=first_scope["agent_id"],
            )
            own_notifications = await client.get("/api/admin/notifications", headers=first_headers)
            own_notifications.raise_for_status()
            assert any(item["title"] == "first notification" for item in own_notifications.json()["items"])
            cross_notifications = await client.get(
                "/api/admin/notifications", params={"tenant_id": first_scope["tenant_id"]},
                headers=second_headers,
            )
            assert cross_notifications.status_code == 403

            first_memory.record_reflection_run(
                first_scope["tenant_id"], first_scope["user_id"], first_scope["agent_id"],
                "smoke-conversation", {"mode": "chat", "decision": "pass"},
            )
            own_reflection = await client.get("/api/admin/reflection-runs", headers=first_headers)
            own_reflection.raise_for_status()
            assert own_reflection.json()["items"]
            cross_reflection = await client.get(
                "/api/admin/reflection-runs", params={"tenant_id": first_scope["tenant_id"]},
                headers=second_headers,
            )
            assert cross_reflection.status_code == 403

            extension = first_memory.create_capability_extension(
                "skill", "Scoped smoke skill", "1.0", "approved-registry/smoke-skill",
            )
            first_memory.review_capability_extension(extension["id"], "approved", "platform-smoke")
            own_grant = await client.put(
                f"/api/admin/extensions/{extension['id']}/grants/{first_scope['tenant_id']}/{first_scope['agent_id']}",
                headers=first_headers, json={"enabled": True},
            )
            own_grant.raise_for_status()
            cross_grant = await client.put(
                f"/api/admin/extensions/{extension['id']}/grants/{first_scope['tenant_id']}/{first_scope['agent_id']}",
                headers=second_headers, json={"enabled": False},
            )
            assert cross_grant.status_code == 403

            first_conversation = await client.post(
                "/api/conversations", headers=first_headers, json={"title": "Scoped collaboration smoke"},
            )
            first_conversation.raise_for_status()
            conversation_id = first_conversation.json()["id"]
            own_notes = await client.get(
                f"/api/admin/conversations/{conversation_id}/notes", headers=first_headers,
            )
            own_notes.raise_for_status()
            cross_notes = await client.get(
                f"/api/admin/conversations/{conversation_id}/notes", headers=second_headers,
            )
            assert cross_notes.status_code == 404
            print("P5 HTTP isolation smoke: PASS")
            return 0
    finally:
        gc.collect()
        shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_run()))
