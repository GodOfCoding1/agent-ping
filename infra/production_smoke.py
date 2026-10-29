"""Verify the private production backend without changing the Android app.

Creates a clearly named test agent/device/reminder, verifies a real Cloud Tasks
dispatch, then revokes/cancels test resources. No secrets or response bodies are printed.
"""

from __future__ import annotations

import asyncio
import json
import secrets
import time
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from production import ORIGIN, gcloud, save


def emit(check: str, **details: Any) -> None:
    print(json.dumps({"check": check, "status": "passed", **details}), flush=True)


def expect(response: httpx2.Response, status: int, check: str) -> Any:
    if response.status_code != status:
        raise RuntimeError(f"{check}: expected HTTP {status}, got {response.status_code}")
    return response.json() if response.content else {}


async def main() -> None:
    token = gcloud("auth", "print-identity-token")
    headers = {
        "Authorization": f"Bearer {token}",
        "X-Serverless-Authorization": f"Bearer {token}",
        "X-AgentPing-Authorization": f"Bearer {token}",
    }
    reminder_id = agent_id = device_id = None
    async with httpx2.AsyncClient(base_url=ORIGIN, headers=headers, timeout=40) as api:
        denied = await api.get(
            "/readyz", headers={"Authorization": "", "X-Serverless-Authorization": ""}
        )
        assert denied.status_code in (401, 403)
        emit("iam_requires_authentication", http_status=denied.status_code)
        expect(await api.get("/health"), 200, "health")
        ready = expect(await api.get("/readyz"), 200, "readiness")
        assert ready["schema_revision"] == "0002"
        emit("live_health_and_database", schema=ready["schema_revision"])
        for path in ("/docs", "/openapi.json"):
            expect(await api.get(path), 404, "production_docs_disabled")
        denied = await api.get(
            "/api/v1/settings", headers={"X-AgentPing-Authorization": "Bearer invalid"}
        )
        assert denied.status_code == 401
        denied_trigger = await api.post("/internal/worker/run", headers={"X-Worker-Token": "wrong"})
        assert denied_trigger.status_code == 401
        emit("application_and_worker_authentication")
        expect(await api.get("/api/v1/settings"), 200, "Google OIDC owner")
        emit("google_oidc_owner")
        try:
            code = expect(await api.post("/api/v1/devices/pairing-codes"), 201, "pairing")["code"]
            registered = expect(
                await api.post(
                    "/api/v1/devices/register",
                    json={
                        "pairing_code": code,
                        "installation_id": f"prod-check-{secrets.token_hex(6)}",
                        "name": "Production backend verification (no phone)",
                        "model": "backend-check",
                        "app_version": "backend-check",
                        "push_token": None,
                    },
                ),
                201,
                "device registration",
            )
            device_id = registered["device"]["id"]
            device_headers = {"X-AgentPing-Authorization": f"Bearer {registered['device_token']}"}
            expect(await api.get("/api/v1/sync", headers=device_headers), 200, "device sync")
            emit("pairing_and_device_sync", phone_delivery_tested=False)
            agent = expect(
                await api.post(
                    "/api/v1/agents",
                    json={
                        "name": f"production-check-{secrets.token_hex(4)}",
                        "token_ttl_days": 1,
                        "scopes": ["reminders:read", "reminders:write", "notifications:send"],
                    },
                ),
                201,
                "agent creation",
            )
            agent_id = agent["agent"]["id"]
            agent_headers = {**headers, "X-AgentPing-Authorization": f"Bearer {agent['token']}"}
            denied = await api.patch("/api/v1/settings", headers=agent_headers, json={})
            assert denied.status_code == 403
            async with (
                httpx2.AsyncClient(headers=agent_headers, timeout=40) as mcp_http,
                Client(
                    streamable_http_client(f"{ORIGIN}/mcp", http_client=mcp_http), cache=None
                ) as mcp,
            ):
                listed = await mcp.list_tools()
                assert len(listed.tools) >= 11
                due = datetime.now(UTC) + timedelta(seconds=35)
                payload = {
                    "title": "Production backend scheduled-work verification",
                    "schedule": {"kind": "once", "at": due.isoformat()},
                    "idempotency_key": f"production-check-{secrets.token_hex(8)}",
                }
                result = await mcp.call_tool("create_reminder", payload)
                assert not result.is_error
                created = result.structured_content
                assert created is not None
                reminder_id = created["reminder_id"]
                occurrence_id = created["next_occurrence"]["id"]
                replay = await mcp.call_tool("create_reminder", payload)
                assert not replay.is_error
                replayed = replay.structured_content
                assert replayed is not None
                assert replayed["reminder_id"] == reminder_id
                emit("mcp_and_idempotency", tools=len(listed.tools))
            # No manual worker trigger here: only actual Cloud Tasks may process this.
            started = time.monotonic()
            fired = None
            while time.monotonic() - started < 120:
                detail = expect(
                    await api.get(f"/api/v1/occurrences/{occurrence_id}"), 200, "occurrence"
                )
                if detail["state"] == "fired":
                    fired = datetime.now(UTC)
                    break
                await asyncio.sleep(5)
            if fired is None:
                raise RuntimeError(
                    "Scheduled Cloud Tasks worker did not process the due occurrence"
                )
            emit(
                "real_scheduled_cloud_tasks_dispatch",
                delay_seconds=round((fired - due).total_seconds(), 2),
            )
            diagnostics = expect(await api.get("/api/v1/diagnostics"), 200, "diagnostics")
            assert diagnostics["environment"] == "production"
            assert diagnostics["outbox"]["dead"] == 0
            assert diagnostics["push"]["provider"] == "fcm"
            emit("production_diagnostics", dead_jobs=0, provider="fcm", embedded_worker=False)
            save(
                "smoke-result.json",
                {
                    "passed_at": datetime.now(UTC).isoformat(),
                    "url": ORIGIN,
                    "schema": "0002",
                    "scheduled_dispatch": True,
                    "phone_delivery_tested": False,
                },
            )
        finally:
            if reminder_id:
                expect(
                    await api.post(f"/api/v1/reminders/{reminder_id}/cancel", json={}),
                    200,
                    "cancel",
                )
            if agent_id:
                expect(await api.post(f"/api/v1/agents/{agent_id}/revoke"), 200, "revoke")
            if device_id:
                expect(await api.delete(f"/api/v1/devices/{device_id}"), 200, "deactivate")
            emit("test_resources_cleaned")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        save("smoke-error.json", {"type": type(error).__name__, "message": str(error)})
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "detail": "Sensitive output withheld",
                }
            ),
            flush=True,
        )
        raise SystemExit(1) from None
