"""Live resource/refresh checks with a real user's normally issued Auth0 access token."""

from __future__ import annotations

import asyncio
import hashlib
import json
import secrets
import time
from datetime import UTC, datetime, timedelta

import httpx
import httpx2
import psycopg
from agentping.auth.oidc import OidcVerifier
from agentping.config import Settings
from mcp import Client
from mcp.client.streamable_http import streamable_http_client
from oauth_production import db_url
from production import ORIGIN, read, save


async def resource(raw: str, *, write: bool = False) -> dict:
    async with (
        httpx2.AsyncClient(headers={"Authorization": "Bearer " + raw}, timeout=60) as http,
        Client(streamable_http_client(ORIGIN + "/mcp", http_client=http), cache=None) as mcp,
    ):
        listed = await mcp.list_tools()
        assert len(listed.tools) == 12
        context = await mcp.call_tool("get_schedule_context", {})
        assert not context.is_error
        devices = await mcp.call_tool("list_devices", {})
        assert not devices.is_error
        result = {"tools": len(listed.tools), "read_succeeded": True}
        if write:
            due = datetime.now(UTC) + timedelta(seconds=35)
            payload = {
                "title": "OAuth production scheduled-work verification",
                "schedule": {"kind": "once", "at": due.isoformat()},
                "idempotency_key": "oauth-acceptance-" + secrets.token_hex(8),
            }
            created = await mcp.call_tool("create_reminder", payload)
            assert not created.is_error
            data = created.structured_content
            assert data and data.get("next_occurrence")
            replayed = await mcp.call_tool("create_reminder", payload)
            assert not replayed.is_error
            assert replayed.structured_content["reminder_id"] == data["reminder_id"]
            result.update(
                reminder_id=data["reminder_id"], occurrence_id=data["next_occurrence"]["id"]
            )
        return result


async def main() -> None:
    auth, tokens = read("auth0.json"), read("live-user-a-tokens.json")
    verifier = OidcVerifier(
        Settings(
            auth_mode="oidc",
            oidc_issuer=auth["issuer"],
            oidc_audience=auth["audience"],
            public_base_url=ORIGIN,
        )
    )
    try:
        verified = await verifier.verify(tokens["access_token"])
        assert verified.client_id == auth["codex_client_id"]
        first = await resource(tokens["access_token"], write=True)
        print(
            "Real Auth0 access token verified; 12 MCP tools and "
            "scoped read/write/idempotency passed.",
            flush=True,
        )
        response = httpx.post(
            auth["issuer"] + "oauth/token",
            json={
                "grant_type": "refresh_token",
                "client_id": auth["codex_client_id"],
                "refresh_token": tokens["refresh_token"],
            },
            timeout=30,
        )
        assert response.status_code == 200
        refreshed = response.json()
        assert (
            refreshed.get("refresh_token") and refreshed["refresh_token"] != tokens["refresh_token"]
        )
        renewed = await verifier.verify(refreshed["access_token"])
        assert renewed.subject == verified.subject and renewed.client_id == verified.client_id
        await resource(refreshed["access_token"])
        save("live-user-a-refreshed-tokens.json", refreshed)
        print(
            "Rotating refresh token, identity binding and a fresh MCP connection passed.",
            flush=True,
        )

        # Verify actual scheduled work without manually invoking the worker.
        def check_due() -> bool:
            with psycopg.connect(db_url(read("database.json")["runtime_url"])) as connection:
                row = connection.execute(
                    "SELECT status FROM outbox_jobs WHERE kind='occurrence.due' "
                    "AND payload->>'occurrence_id'=%s",
                    (first["occurrence_id"],),
                ).fetchone()
                return bool(row and row[0] == "succeeded")

        started = time.monotonic()
        while time.monotonic() - started < 110:
            if await asyncio.to_thread(check_due):
                first["scheduled_work_processed"] = True
                break
            await asyncio.sleep(5)
        assert first.get("scheduled_work_processed")
        async with (
            httpx2.AsyncClient(
                headers={"Authorization": "Bearer " + refreshed["access_token"]}, timeout=60
            ) as http,
            Client(streamable_http_client(ORIGIN + "/mcp", http_client=http), cache=None) as mcp,
        ):
            cancelled = await mcp.call_tool(
                "cancel_reminder",
                {
                    "reminder_id": first["reminder_id"],
                    "reason": "OAuth deployment verification finished",
                },
            )
            assert not cancelled.is_error
        proof = {
            **first,
            "issuer": auth["issuer"],
            "client_id": verified.client_id,
            "subject_hash": hashlib.sha256(verified.subject.encode()).hexdigest()[:16],
            "signature_audience_scope_verified": True,
            "refresh_rotated": True,
            "reconnected": True,
            "phone_delivery_verified": False,
            "two_live_users_verified": False,
        }
        save("oauth-live-user-a-proof.json", proof)
        print(
            json.dumps(
                {k: v for k, v in proof.items() if k not in ["reminder_id", "occurrence_id"]}
            )
        )
    finally:
        await verifier.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as error:
        import traceback

        save(
            "oauth-live-smoke-error.json",
            {
                "type": type(error).__name__,
                "frames": [
                    {"file": f.filename, "line": f.lineno}
                    for f in traceback.extract_tb(error.__traceback__)
                ],
            },
        )
        print("Live OAuth resource check failed; diagnostic details saved privately.")
        raise SystemExit(1) from None
