"""Real local MCP -> paired Android delivery probe; never prints credentials.

./dev android-mcp-check --device-id UUID [--alarm-seconds 300]
Leaves the test notification/reminder available for physical/watch interaction.
The temporary agent is revoked after sending. Only loopback Compose is accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

ROOT = Path(__file__).resolve().parents[1]


async def run(device_id: str, alarm_seconds: int) -> int:
    import uuid

    uuid.UUID(device_id)
    values = dict(
        line.split("=", 1)
        for line in (ROOT / ".env.compose").read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    base = values["PUBLIC_BASE_URL"].rstrip("/")
    parsed = urlsplit(base)
    if parsed.hostname not in {"127.0.0.1", "localhost"} or parsed.username or parsed.password:
        raise ValueError("Only the local Compose API is supported")
    if alarm_seconds and not 10 <= alarm_seconds <= 300:
        raise ValueError("Alarm delay must be 10–300 seconds")
    async with httpx2.AsyncClient(
        base_url=base,
        headers={"Authorization": f"Bearer {values['DEV_AUTH_TOKEN']}"},
        timeout=20,
    ) as api:
        devices = (await api.get("/api/v1/devices")).json()
        if not any(d["id"] == device_id and d["status"] == "active" for d in devices):
            raise ValueError("Select an active paired device")
        response = await api.post(
            "/api/v1/agents",
            json={
                "name": f"Android MCP verification {secrets.token_hex(4)}",
                "scopes": ["notifications:send", "reminders:read", "reminders:write"],
                "token_ttl_days": 1,
            },
        )
        response.raise_for_status()
        agent = response.json()
        try:
            async with (
                httpx2.AsyncClient(
                    headers={"Authorization": f"Bearer {agent['token']}"}, timeout=20
                ) as transport,
                Client(
                    streamable_http_client(f"{base}/mcp", http_client=transport), cache=None
                ) as mcp,
            ):
                key = f"android-mcp-{secrets.token_hex(10)}"
                args: dict[str, object] = {
                    "title": "AgentPing MCP alarm check"
                    if alarm_seconds
                    else "AgentPing watch check",
                    "body": "Acknowledge or snooze from your phone or paired watch.",
                    "delivery_mode": "alarm" if alarm_seconds else "normal",
                    "target_device_ids": [device_id],
                    "idempotency_key": key,
                }
                tool = "send_notification"
                if alarm_seconds:
                    tool = "create_reminder"
                    args.pop("target_device_ids")
                    args["targets"] = {"strategy": "devices", "device_ids": [device_id]}
                    args["schedule"] = {
                        "kind": "once",
                        "at": (datetime.now(UTC) + timedelta(seconds=alarm_seconds)).isoformat(),
                        "timezone": "Asia/Kolkata",
                    }
                result = await mcp.call_tool(tool, args)
                if result.is_error:
                    print(json.dumps({"mcp_accepted": False}))
                    return 1
                data = result.structured_content or json.loads(
                    next(item.text for item in result.content if isinstance(item, TextContent))
                )
                repeat = await mcp.call_tool(tool, args)
                replay = repeat.structured_content or json.loads(
                    next(item.text for item in repeat.content if isinstance(item, TextContent))
                )
                occurrence_id = (
                    data["next_occurrence"]["id"] if alarm_seconds else data["occurrence_id"]
                )
                outcome = {
                    "mcp_accepted": True,
                    "idempotency_replay": replay.get("idempotent_replay", False),
                    "occurrence_id": occurrence_id,
                    "reminder_id": data.get("reminder_id", data.get("notification_id")),
                }
                (ROOT / ".agentping").mkdir(exist_ok=True)
                (ROOT / ".agentping/android-mcp-last.json").write_text(json.dumps(outcome))
                for _ in range(20):
                    detail = (await api.get(f"/api/v1/occurrences/{occurrence_id}")).json()
                    deliveries = detail.get("deliveries", [])
                    if any(
                        d.get("displayed_at") or d.get("scheduled_locally_at") for d in deliveries
                    ):
                        break
                    await asyncio.sleep(1)
                outcome["deliveries"] = [
                    {
                        key: d[key]
                        for key in (
                            "state",
                            "provider",
                            "provider_accepted_at",
                            "device_received_at",
                            "scheduled_locally_at",
                            "displayed_at",
                            "failure_code",
                        )
                    }
                    for d in deliveries
                ]
                print(json.dumps(outcome))
                return 0 if replay.get("idempotent_replay") else 1
        finally:
            await api.post(f"/api/v1/agents/{agent['agent']['id']}/revoke")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device-id", required=True)
    parser.add_argument("--alarm-seconds", type=int, default=0)
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.device_id, args.alarm_seconds))
    except Exception as error:
        print(
            json.dumps(
                {
                    "status": "failed",
                    "error_type": type(error).__name__,
                    "http_status": getattr(getattr(error, "response", None), "status_code", None),
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
