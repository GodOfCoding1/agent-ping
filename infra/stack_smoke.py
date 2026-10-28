"""End-to-end smoke test against a running AgentPing stack (REST, worker, MCP).

Reads the base URL and development token from ``.env.compose`` (or the environment) and
never prints credentials. Creates a throwaway device and agent and cleans them up.

    uv run --project backend --locked python infra/stack_smoke.py
"""

from __future__ import annotations

import asyncio
import json
import os
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx2
from mcp import Client
from mcp.client.stdio import (
    StdioServerParameters,
    get_default_environment,
    stdio_client,
)
from mcp.client.streamable_http import streamable_http_client

ROOT = Path(__file__).resolve().parents[1]
FAILED = False


def env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def emit(check: str, ok: bool, **details: Any) -> None:
    global FAILED
    FAILED = FAILED or not ok
    print(
        json.dumps(
            {"check": check, "status": "ok" if ok else "fail", **details}, default=str
        )
    )


def expect(response: httpx2.Response, status: int, check: str) -> Any:
    if response.status_code != status:
        emit(check, False, http_status=response.status_code, body=response.text[:300])
        raise SystemExit(1)
    return response.json() if response.content else {}


async def wait_for_delivery(
    api: httpx2.AsyncClient, occurrence_id: str, timeout: float = 15.0
) -> tuple[str, float]:
    started = time.monotonic()
    state = "unknown"
    while time.monotonic() - started < timeout:
        detail = (await api.get(f"/api/v1/occurrences/{occurrence_id}")).json()
        states = {d["state"] for d in detail.get("deliveries", [])}
        if states & {"provider_accepted", "device_received", "displayed"}:
            return next(iter(states)), time.monotonic() - started
        state = ",".join(sorted(states)) or detail.get("state", "unknown")
        await asyncio.sleep(0.25)
    return state, time.monotonic() - started


async def main() -> int:
    compose = env_file(ROOT / ".env.compose")
    base_url = os.environ.get("AGENTPING_BASE_URL") or compose.get("PUBLIC_BASE_URL")
    token = os.environ.get("AGENTPING_TOKEN") or compose.get("DEV_AUTH_TOKEN")
    if not base_url or not token:
        print(
            "Set AGENTPING_BASE_URL and AGENTPING_TOKEN or run infra/compose_env.py first."
        )
        return 2
    owner = {"Authorization": f"Bearer {token}"}
    async with httpx2.AsyncClient(base_url=base_url, headers=owner, timeout=20) as api:
        health = expect(await api.get("/healthz"), 200, "healthz")
        ready = expect(await api.get("/readyz"), 200, "readyz")
        emit(
            "health",
            True,
            version=health.get("version"),
            schema=ready.get("schema_revision"),
        )

        unauth = await api.get("/api/v1/reminders", headers={"Authorization": ""})
        emit("auth_required", unauth.status_code == 401, http_status=unauth.status_code)

        code = expect(
            await api.post("/api/v1/devices/pairing-codes"), 201, "pairing_code"
        )["code"]
        registered = expect(
            await api.post(
                "/api/v1/devices/register",
                headers={"Authorization": ""},
                json={
                    "pairing_code": code,
                    "installation_id": f"smoke-{secrets.token_hex(6)}",
                    "name": "Smoke test device",
                    "model": "smoke",
                    "app_version": "smoke",
                    "push_token": "smoke-test-push-token-" + secrets.token_hex(8),
                },
            ),
            201,
            "register_device",
        )
        device_id = registered["device"]["id"]
        device = {"Authorization": f"Bearer {registered['device_token']}"}
        emit(
            "device_paired",
            True,
            device_id=device_id,
            primary=registered["device"]["is_primary"],
        )

        agent = expect(
            await api.post(
                "/api/v1/agents",
                json={
                    "name": f"smoke-{secrets.token_hex(3)}",
                    "scopes": [
                        "reminders:read",
                        "reminders:write",
                        "notifications:send",
                    ],
                    "token_ttl_days": 1,
                },
            ),
            201,
            "create_agent",
        )
        agent_id = agent["agent"]["id"]
        reminder_ids: list[str] = []
        try:
            sent = expect(
                await api.post(
                    "/api/v1/notifications",
                    json={
                        "title": "AgentPing smoke test",
                        "targets": {"strategy": "devices", "device_ids": [device_id]},
                    },
                ),
                202,
                "send_notification",
            )
            reminder_ids.append(sent["notification_id"])
            state, seconds = await wait_for_delivery(api, sent["occurrence_id"])
            emit(
                "worker_push_dispatch",
                state in {"provider_accepted", "device_received", "displayed"},
                delivery_state=state,
                seconds=round(seconds, 2),
            )

            async with (
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {agent['token']}"}, timeout=20
                ) as mcp_http,
                Client(
                    streamable_http_client(
                        f"{base_url.rstrip('/')}/mcp", http_client=mcp_http
                    ),
                    cache=None,
                ) as mcp,
            ):
                tools = await mcp.list_tools()
                emit("mcp_list_tools", len(tools.tools) >= 11, tools=len(tools.tools))
                at = (datetime.now(UTC) + timedelta(hours=3)).isoformat()
                result = await mcp.call_tool(
                    "create_reminder",
                    {"title": "Smoke reminder", "schedule": {"kind": "once", "at": at}},
                )
                created = result.structured_content or json.loads(
                    result.content[0].text
                )
                emit(
                    "mcp_create_reminder",
                    not result.is_error,
                    reminder_status=created.get("status"),
                )
                if not result.is_error:
                    reminder_ids.append(created["reminder_id"])
                    occurrence_id = created["next_occurrence"]["id"]

            proxy = Path(sys.executable).with_name("agentping-mcp-stdio")
            stdio = StdioServerParameters(
                command=str(proxy),
                env={
                    **get_default_environment(),
                    "AGENTPING_MCP_URL": f"{base_url.rstrip('/')}/mcp",
                    "AGENTPING_TOKEN": agent["token"],
                },
            )
            async with Client(stdio_client(stdio), cache=None) as stdio_mcp:
                listed = await stdio_mcp.call_tool("list_reminders", {})
                emit("mcp_stdio_proxy", not listed.is_error)

            if len(reminder_ids) == 2:
                synced = expect(
                    await api.get("/api/v1/sync", headers=device), 200, "device_sync"
                )
                ids = {o["id"] for o in synced["occurrences"]}
                emit("device_sync", occurrence_id in ids, occurrences=len(ids))
                acked = expect(
                    await api.post(
                        "/api/v1/sync/actions",
                        headers=device,
                        json={
                            "actions": [
                                {
                                    "client_action_id": f"smoke-{secrets.token_hex(6)}",
                                    "occurrence_id": occurrence_id,
                                    "action": "acknowledge",
                                    "occurred_at": datetime.now(UTC).isoformat(),
                                }
                            ]
                        },
                    ),
                    200,
                    "device_ack",
                )
                emit("device_ack", acked[0]["result"] == "applied")

            activity = expect(
                await api.get("/api/v1/activity", params={"limit": 20}), 200, "activity"
            )
            emit("activity", bool(activity["items"]), events=len(activity["items"]))
            diagnostics = expect(
                await api.get("/api/v1/diagnostics"), 200, "diagnostics"
            )
            emit(
                "diagnostics",
                diagnostics["outbox"]["dead"] == 0,
                outbox=diagnostics["outbox"],
                workers=len(diagnostics["worker"]["workers"]),
                push=diagnostics["push"].get("provider"),
            )
        finally:
            for reminder_id in reminder_ids:
                await api.post(f"/api/v1/reminders/{reminder_id}/cancel", json={})
            await api.post(f"/api/v1/agents/{agent_id}/revoke")
            await api.delete(f"/api/v1/devices/{device_id}")
            emit("cleanup", True)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
