"""Agent authorization (scopes, ownership, limits, revocation) and the MCP server."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import httpx2
import pytest
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from tests.conftest import DEV_TOKEN, T0, Harness, auth, create_agent, create_once, register_device

REQUIRED_TOOLS = {
    "send_notification",
    "create_reminder",
    "update_reminder",
    "cancel_reminder",
    "list_reminders",
    "get_reminder",
    "snooze_occurrence",
    "acknowledge_occurrence",
    "skip_occurrence",
    "list_devices",
    "get_activity",
}


@asynccontextmanager
async def mcp_client(app: Any, token: str):
    http = httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {token}"},
    )
    async with (
        http,
        Client(
            streamable_http_client("http://testserver/mcp", http_client=http), cache=None
        ) as client,
    ):
        yield client


def payload(result: Any) -> Any:
    assert not result.is_error, result.content[0].text
    if result.structured_content is not None:
        structured = result.structured_content
        return structured.get("result", structured) if set(structured) == {"result"} else structured
    return json.loads(result.content[0].text)


def error_text(result: Any) -> str:
    assert result.is_error
    return result.content[0].text


async def test_agent_rest_access_is_scoped_and_attributed(client) -> None:
    await register_device(client)
    agent = await create_agent(client, name="ci-bot")
    token = auth(agent["token"])
    assert agent["token"].startswith("apa_")

    created = await create_once(client, minutes=30, title="Deploy window", headers=token)
    reminder = (
        await client.get(f"/api/v1/reminders/{created['reminder_id']}", headers=token)
    ).json()
    assert reminder["reminder"]["source_agent"]["name"] == "ci-bot"

    assert (
        await client.patch("/api/v1/settings", json={"timezone": "UTC"}, headers=token)
    ).status_code == 403
    assert (await client.get("/api/v1/agents", headers=token)).status_code == 403
    assert (await client.post("/api/v1/devices/pairing-codes", headers=token)).status_code == 403
    assert (
        await client.patch(
            f"/api/v1/devices/{(await client.get('/api/v1/devices')).json()[0]['id']}",
            json={"name": "pwned"},
            headers=token,
        )
    ).status_code == 403

    events = (
        await client.get("/api/v1/activity", params={"agent_id": agent["agent"]["id"]})
    ).json()
    assert events["items"][0]["actor"]["agent_name"] == "ci-bot"


async def test_agents_only_modify_their_own_reminders(client) -> None:
    mine = await create_agent(client, name="agent-a")
    other = await create_agent(client, name="agent-b")
    owner_reminder = await create_once(client, minutes=30)
    agent_reminder = await create_once(client, minutes=30, headers=auth(mine["token"]))

    for target in (owner_reminder, agent_reminder):
        response = await client.patch(
            f"/api/v1/reminders/{target['reminder_id']}",
            json={"expected_revision": 1, "title": "hijack"},
            headers=auth(other["token"]),
        )
        assert response.status_code == 403
    own = await client.patch(
        f"/api/v1/reminders/{agent_reminder['reminder_id']}",
        json={"expected_revision": 1, "title": "mine"},
        headers=auth(mine["token"]),
    )
    assert own.status_code == 200
    # The owner may change anything.
    owner_edit = await client.patch(
        f"/api/v1/reminders/{agent_reminder['reminder_id']}",
        json={"expected_revision": 2, "title": "owner"},
    )
    assert owner_edit.status_code == 200


async def test_missing_scope_is_forbidden(client) -> None:
    reader = await create_agent(client, name="reader", scopes=["reminders:read"])
    response = await client.post(
        "/api/v1/notifications", json={"title": "x"}, headers=auth(reader["token"])
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden"


async def test_critical_requires_scope_even_with_opt_in(client) -> None:
    settings = (await client.get("/api/v1/settings")).json()
    await client.patch(
        "/api/v1/settings",
        json={"preferences": {**settings["preferences"], "critical_alerts_enabled": True}},
    )
    normal = await create_agent(client, name="normal")
    body = {"title": "Pager", "delivery_mode": "critical"}
    assert (
        await client.post("/api/v1/notifications", json=body, headers=auth(normal["token"]))
    ).status_code == 403
    critical = await create_agent(
        client,
        name="pager",
        scopes=["reminders:read", "reminders:write", "notifications:send", "alerts:critical"],
    )
    assert (
        await client.post("/api/v1/notifications", json=body, headers=auth(critical["token"]))
    ).status_code == 202


async def test_agent_cannot_be_granted_admin_scopes(client) -> None:
    response = await client.post(
        "/api/v1/agents", json={"name": "evil", "scopes": ["settings:write"]}
    )
    assert response.status_code == 422


async def test_agent_send_quota(client) -> None:
    agent = await create_agent(client, name="chatty", max_sends_per_hour=2)
    statuses = [
        (
            await client.post(
                "/api/v1/notifications", json={"title": f"n{i}"}, headers=auth(agent["token"])
            )
        ).status_code
        for i in range(3)
    ]
    assert statuses == [202, 202, 429]


async def test_revocation_and_token_lifecycle(client, harness: Harness) -> None:
    agent = await create_agent(client, name="temp")
    agent_id = agent["agent"]["id"]
    second = (await client.post(f"/api/v1/agents/{agent_id}/tokens", params={"ttl_days": 1})).json()
    assert (await client.get("/api/v1/reminders", headers=auth(second["token"]))).status_code == 200

    harness.clock.advance(days=2)
    assert (await client.get("/api/v1/reminders", headers=auth(second["token"]))).status_code == 401
    assert (await client.get("/api/v1/reminders", headers=auth(agent["token"]))).status_code == 200

    token_id = agent["agent"]["tokens"][0]["id"]
    assert (await client.delete(f"/api/v1/agents/tokens/{token_id}")).status_code == 204
    assert (await client.get("/api/v1/reminders", headers=auth(agent["token"]))).status_code == 401

    third = (await client.post(f"/api/v1/agents/{agent_id}/tokens")).json()
    revoked = (await client.post(f"/api/v1/agents/{agent_id}/revoke")).json()
    assert revoked["status"] == "revoked"
    assert (await client.get("/api/v1/reminders", headers=auth(third["token"]))).status_code == 401
    listing = json.dumps((await client.get("/api/v1/agents")).json())
    for raw in (agent["token"], second["token"], third["token"]):
        assert raw not in listing  # only hashes are stored; secrets are shown once
    security = (await client.get("/api/v1/activity", params={"category": "security"})).json()[
        "items"
    ]
    assert any(e["event_type"] == "agent.revoked" for e in security)


async def test_mcp_lists_required_tools(app, client) -> None:
    agent = await create_agent(client)
    async with mcp_client(app, agent["token"]) as mcp:
        tools = await mcp.list_tools()
    names = {tool.name for tool in tools.tools}
    assert REQUIRED_TOOLS <= names
    send = next(t for t in tools.tools if t.name == "send_notification")
    assert "IMMEDIATELY" in (send.description or "")
    assert send.input_schema["properties"]["title"]


async def test_mcp_requires_authentication(app, client) -> None:
    response = await client.post(
        "/mcp",
        headers={"Authorization": "", "Accept": "application/json, text/event-stream"},
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code == 401
    assert "resource_metadata" in response.headers.get("www-authenticate", "")
    metadata = await client.get("/.well-known/oauth-protected-resource/mcp")
    assert metadata.status_code == 200
    assert metadata.json()["resource"] == "http://testserver/mcp"


async def test_mcp_rejects_foreign_host_header(app, client) -> None:
    response = await client.post(
        "/mcp",
        headers={
            "Host": "evil.example",
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    assert response.status_code in (400, 421)


async def test_mcp_end_to_end_workflow(app, client, harness: Harness) -> None:
    await register_device(client)
    agent = await create_agent(client, name="assistant")
    async with mcp_client(app, agent["token"]) as mcp:
        context = payload(await mcp.call_tool("get_schedule_context", {}))
        assert context["user_timezone"] == "UTC"

        devices = payload(await mcp.call_tool("list_devices", {}))
        assert devices[0]["is_primary"] is True

        sent = payload(
            await mcp.call_tool(
                "send_notification",
                {"title": "Build finished", "body": "All green", "idempotency_key": "build-42"},
            )
        )
        assert sent["dispatch_state"] == "queued"
        replay = payload(
            await mcp.call_tool(
                "send_notification",
                {"title": "Build finished", "body": "All green", "idempotency_key": "build-42"},
            )
        )
        assert replay["notification_id"] == sent["notification_id"]
        assert replay["idempotent_replay"] is True

        created = payload(
            await mcp.call_tool(
                "create_reminder",
                {
                    "title": "Stand up and stretch",
                    "delivery_mode": "persistent",
                    "schedule": {"kind": "once", "at": (T0 + timedelta(minutes=30)).isoformat()},
                },
            )
        )
        reminder_id = created["reminder_id"]
        occurrence_id = created["next_occurrence"]["id"]

        recurring = payload(
            await mcp.call_tool(
                "create_reminder",
                {
                    "title": "Weekly review",
                    "schedule": {
                        "kind": "recurring",
                        "start_local": "2026-03-06T16:00:00",
                        "timezone": "Europe/Berlin",
                        "rrule": "FREQ=WEEKLY;BYDAY=FR",
                    },
                },
            )
        )
        assert recurring["next_occurrence"]["scheduled_at"].startswith("2026-03-06T15:00")

        listed = payload(await mcp.call_tool("list_reminders", {"delivery_mode": "persistent"}))
        assert [r["id"] for r in listed["items"]] == [reminder_id]

        detail = payload(await mcp.call_tool("get_reminder", {"reminder_id": reminder_id}))
        assert detail["upcoming"][0]["id"] == occurrence_id

        updated = payload(
            await mcp.call_tool(
                "update_reminder",
                {"reminder_id": reminder_id, "expected_revision": 1, "title": "Stretch!"},
            )
        )
        assert updated["reminder"]["revision"] == 2
        conflict = await mcp.call_tool(
            "update_reminder", {"reminder_id": reminder_id, "expected_revision": 1, "title": "x"}
        )
        assert "[revision_conflict]" in error_text(conflict)

        snoozed = payload(
            await mcp.call_tool(
                "snooze_occurrence", {"occurrence_id": occurrence_id, "minutes": 15}
            )
        )
        assert snoozed["occurrence"]["state"] == "snoozed"
        acked = payload(
            await mcp.call_tool(
                "acknowledge_occurrence", {"occurrence_id": occurrence_id, "idempotency_key": "k1"}
            )
        )
        assert acked["result"] == "applied"

        skip_target = recurring["next_occurrence"]["id"]
        skipped = payload(
            await mcp.call_tool(
                "skip_occurrence", {"occurrence_id": skip_target, "reason": "On holiday"}
            )
        )
        assert skipped["occurrence"]["state"] == "skipped"

        cancelled = payload(
            await mcp.call_tool("cancel_reminder", {"reminder_id": recurring["reminder_id"]})
        )
        assert cancelled["status"] == "cancelled"

        activity = payload(await mcp.call_tool("get_activity", {"limit": 50}))
        types = {e["event_type"] for e in activity["items"]}
        assert {"notification.sent", "reminder.created", "occurrence.acknowledged"} <= types

        bad = await mcp.call_tool(
            "create_reminder",
            {"title": "x", "schedule": {"kind": "once", "at": "2026-03-02T10:00:00"}},
        )
        assert "[ambiguous_datetime]" in error_text(bad)


async def test_mcp_scope_errors_are_tool_errors(app, client) -> None:
    reader = await create_agent(client, name="reader", scopes=["reminders:read"])
    async with mcp_client(app, reader["token"]) as mcp:
        result = await mcp.call_tool("send_notification", {"title": "nope"})
    assert "[forbidden]" in error_text(result)


@pytest.mark.parametrize("token", [DEV_TOKEN])
async def test_mcp_accepts_owner_development_token(app, client, token) -> None:
    async with mcp_client(app, token) as mcp:
        result = payload(await mcp.call_tool("list_reminders", {}))
    assert result["items"] == []
