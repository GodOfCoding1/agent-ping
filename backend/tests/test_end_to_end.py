"""Full lifecycle through the public REST API and the worker."""

from __future__ import annotations

from datetime import timedelta

from tests.conftest import T0, Harness, auth, register_device


async def test_reminder_lifecycle_with_device_ack(client, harness: Harness) -> None:
    device = await register_device(client, push_token="fcm-token-device-one-0001")
    device_token = device["device_token"]
    assert device_token.startswith("apd_")
    assert device["device"]["is_primary"] is True

    due = T0 + timedelta(minutes=10)
    response = await client.post(
        "/api/v1/reminders",
        json={
            "title": "Take medication",
            "body": "Blue pill",
            "delivery_mode": "persistent",
            "schedule": {"kind": "once", "at": due.isoformat()},
        },
    )
    assert response.status_code == 201, response.text
    created = response.json()
    assert created["status"] == "active"
    assert created["sync_status"] == "sync_queued"
    assert created["target_device_ids"] == [device["device"]["id"]]
    assert created["next_occurrence"]["scheduled_at"].startswith("2026-03-02T09:10")
    reminder = {"id": created["reminder_id"]}

    detail = (await client.get(f"/api/v1/reminders/{reminder['id']}")).json()
    assert detail["reminder"]["next_occurrence_at"].startswith("2026-03-02T09:10")
    [occurrence] = detail["upcoming"]
    assert occurrence["state"] == "scheduled"
    assert occurrence["id"] == created["next_occurrence"]["id"]

    # The phone syncs the schedule so it can ring offline.
    sync = (await client.get("/api/v1/sync", headers=auth(device_token))).json()
    assert sync["full"] is True
    assert any(o["id"] == occurrence["id"] for o in sync["occurrences"])

    # Nothing is due yet.
    await harness.run_due()
    assert harness.push.of_type("deliver") == []

    # At the due time the server pushes as a fallback (no local-schedule receipt yet).
    await harness.advance_and_run(minutes=10)
    delivered = harness.push.of_type("deliver")
    assert len(delivered) == 1
    assert delivered[0].data["occurrence_id"] == occurrence["id"]
    assert delivered[0].priority == "high"

    state = (await client.get(f"/api/v1/occurrences/{occurrence['id']}")).json()
    assert state["state"] == "fired"
    assert state["deliveries"][0]["state"] == "provider_accepted"

    ack = await client.post(
        f"/api/v1/occurrences/{occurrence['id']}/acknowledge",
        json={"client_action_id": "ack-1", "source": "device_notification"},
        headers=auth(device_token),
    )
    assert ack.status_code == 200, ack.text
    assert ack.json()["result"] == "applied"
    assert ack.json()["occurrence"]["state"] == "acknowledged"

    # Replaying the same offline action is idempotent.
    again = await client.post(
        f"/api/v1/occurrences/{occurrence['id']}/acknowledge",
        json={"client_action_id": "ack-1", "source": "device_notification"},
        headers=auth(device_token),
    )
    assert again.json()["result"] == "duplicate"

    await harness.advance_and_run(minutes=1)
    final = (await client.get(f"/api/v1/reminders/{reminder['id']}")).json()
    assert final["reminder"]["status"] == "completed"

    activity = (await client.get("/api/v1/activity")).json()["items"]
    types = {event["event_type"] for event in activity}
    assert {"reminder.created", "occurrence.acknowledged"} <= types
