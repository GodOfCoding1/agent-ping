"""Reminder CRUD, validation, idempotency, concurrency control and listing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from tests.conftest import T0, Harness, auth, create_once, register_device


async def test_local_datetime_with_timezone(client, harness: Harness) -> None:
    await register_device(client)
    response = await client.post(
        "/api/v1/reminders",
        json={
            "title": "Standup",
            "schedule": {
                "kind": "once",
                "local_datetime": "2026-03-02T18:30:00",
                "timezone": "Asia/Kolkata",
            },
        },
    )
    assert response.status_code == 201, response.text
    assert response.json()["next_occurrence"]["scheduled_at"] == "2026-03-02T13:00:00Z"


async def test_offsetless_instant_is_rejected_as_ambiguous(client) -> None:
    response = await client.post(
        "/api/v1/reminders",
        json={"title": "x", "schedule": {"kind": "once", "at": "2026-03-02T18:30:00"}},
    )
    assert response.status_code == 422
    error = response.json()["error"]
    assert error["code"] == "ambiguous_datetime"
    assert error["request_id"]


async def test_past_time_is_rejected_with_guidance(client) -> None:
    response = await client.post(
        "/api/v1/reminders",
        json={
            "title": "x",
            "schedule": {"kind": "once", "at": (T0 - timedelta(hours=1)).isoformat()},
        },
    )
    assert response.status_code == 422
    assert "send_notification" in response.json()["error"]["message"]


async def test_validation_errors_use_stable_format(client) -> None:
    response = await client.post("/api/v1/reminders", json={"title": ""})
    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "validation_failed"
    fields = {e["field"] for e in body["details"]["errors"]}
    assert "title" in fields and "schedule" in fields


async def test_idempotent_create(client) -> None:
    body = {
        "title": "Pay rent",
        "schedule": {"kind": "once", "at": (T0 + timedelta(days=1)).isoformat()},
    }
    first = await client.post("/api/v1/reminders", json=body, headers={"Idempotency-Key": "rent-1"})
    second = await client.post(
        "/api/v1/reminders", json=body, headers={"Idempotency-Key": "rent-1"}
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["reminder_id"] == second.json()["reminder_id"]
    assert second.json()["idempotent_replay"] is True
    listed = (await client.get("/api/v1/reminders")).json()["items"]
    assert len(listed) == 1

    changed = await client.post(
        "/api/v1/reminders",
        json={**body, "title": "Different"},
        headers={"Idempotency-Key": "rent-1"},
    )
    assert changed.status_code == 409
    assert changed.json()["error"]["code"] == "idempotency_conflict"


async def test_no_devices_reports_no_targets(client) -> None:
    created = await create_once(client)
    assert created["sync_status"] == "no_target_devices"
    assert created["target_device_ids"] == []


async def test_update_requires_current_revision_and_reschedules(client) -> None:
    await register_device(client)
    created = await create_once(client, minutes=30)
    reminder_id = created["reminder_id"]
    old_occurrence = created["next_occurrence"]["id"]

    stale = await client.patch(
        f"/api/v1/reminders/{reminder_id}", json={"expected_revision": 99, "title": "New"}
    )
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "revision_conflict"
    assert stale.json()["error"]["details"]["current_revision"] == 1

    new_time = (T0 + timedelta(hours=2)).isoformat()
    updated = await client.patch(
        f"/api/v1/reminders/{reminder_id}",
        json={
            "expected_revision": 1,
            "title": "Moved",
            "schedule": {"kind": "once", "at": new_time},
        },
        headers={"If-Match": '"1"'},
    )
    assert updated.status_code == 200, updated.text
    result = updated.json()
    assert result["reminder"]["revision"] == 2
    assert result["reminder"]["title"] == "Moved"
    assert result["next_occurrence"]["scheduled_at"] == "2026-03-02T11:00:00Z"
    assert result["next_occurrence"]["id"] != old_occurrence

    old = (await client.get(f"/api/v1/occurrences/{old_occurrence}")).json()
    assert old["state"] == "superseded"

    mismatch = await client.patch(
        f"/api/v1/reminders/{reminder_id}",
        json={"expected_revision": 2, "title": "x"},
        headers={"If-Match": '"1"'},
    )
    assert mismatch.status_code == 422


async def test_title_only_edit_keeps_occurrence_identity(client) -> None:
    created = await create_once(client, minutes=30)
    reminder_id = created["reminder_id"]
    updated = await client.patch(
        f"/api/v1/reminders/{reminder_id}", json={"expected_revision": 1, "title": "Renamed"}
    )
    assert updated.json()["next_occurrence"]["id"] == created["next_occurrence"]["id"]


async def test_cancel_then_ack_is_rejected_as_stale(client, harness: Harness) -> None:
    device = await register_device(client)
    created = await create_once(client, mode="persistent")
    reminder_id = created["reminder_id"]
    occurrence_id = created["next_occurrence"]["id"]

    cancelled = await client.delete(f"/api/v1/reminders/{reminder_id}")
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["cancelled_occurrences"] == 1

    ack = await client.post(
        f"/api/v1/occurrences/{occurrence_id}/acknowledge",
        json={"client_action_id": "late-ack"},
        headers=auth(device["device_token"]),
    )
    assert ack.status_code == 200
    assert ack.json()["result"] == "rejected_stale"

    # A cancelled reminder never fires.
    await harness.advance_and_run(minutes=15)
    assert harness.push.of_type("deliver") == []

    again = await client.post(f"/api/v1/reminders/{reminder_id}/cancel")
    assert again.json()["cancelled_occurrences"] == 0

    edit = await client.patch(
        f"/api/v1/reminders/{reminder_id}", json={"expected_revision": 2, "title": "x"}
    )
    assert edit.status_code in (409, 422)


async def test_list_filters_and_pagination(client) -> None:
    for index in range(5):
        await create_once(client, minutes=10 + index, title=f"Task {index}")
    await create_once(client, minutes=60, title="Wake up", mode="alarm")

    page1 = (await client.get("/api/v1/reminders", params={"limit": 4})).json()
    assert len(page1["items"]) == 4 and page1["next_cursor"]
    page2 = (
        await client.get("/api/v1/reminders", params={"limit": 4, "cursor": page1["next_cursor"]})
    ).json()
    assert len(page2["items"]) == 2 and page2["next_cursor"] is None
    ids = {r["id"] for r in page1["items"] + page2["items"]}
    assert len(ids) == 6

    alarms = (await client.get("/api/v1/reminders", params={"mode": "alarm"})).json()["items"]
    assert [r["title"] for r in alarms] == ["Wake up"]
    found = (await client.get("/api/v1/reminders", params={"q": "task 3"})).json()["items"]
    assert [r["title"] for r in found] == ["Task 3"]
    due = (
        await client.get(
            "/api/v1/reminders",
            params={"due_before": (T0 + timedelta(minutes=12)).isoformat()},
        )
    ).json()["items"]
    assert {r["title"] for r in due} == {"Task 0", "Task 1"}

    bad = await client.get("/api/v1/reminders", params={"cursor": "garbage"})
    assert bad.status_code == 422


async def test_recurring_materializes_horizon_and_pause_resume(client, harness: Harness) -> None:
    await register_device(client)
    response = await client.post(
        "/api/v1/reminders",
        json={
            "title": "Water plants",
            "schedule": {
                "kind": "recurring",
                "start_local": "2026-03-02T19:00:00",
                "timezone": "Asia/Kolkata",
                "rrule": "FREQ=DAILY",
            },
        },
    )
    assert response.status_code == 201, response.text
    reminder_id = response.json()["reminder_id"]
    occurrences = (
        await client.get("/api/v1/occurrences", params={"reminder_id": reminder_id, "limit": 200})
    ).json()["items"]
    assert len(occurrences) == 14  # 14-day horizon
    assert occurrences[0]["scheduled_at"] == "2026-03-02T13:30:00Z"
    assert all(o["state"] == "scheduled" for o in occurrences)

    paused = await client.patch(
        f"/api/v1/reminders/{reminder_id}", json={"expected_revision": 1, "status": "paused"}
    )
    assert paused.json()["reminder"]["status"] == "paused"
    states = {
        o["state"]
        for o in (
            await client.get(
                "/api/v1/occurrences", params={"reminder_id": reminder_id, "state": "scheduled"}
            )
        ).json()["items"]
    }
    assert states == set()

    resumed = await client.patch(
        f"/api/v1/reminders/{reminder_id}", json={"expected_revision": 2, "status": "active"}
    )
    assert resumed.json()["reminder"]["status"] == "active"
    revived = (
        await client.get(
            "/api/v1/occurrences", params={"reminder_id": reminder_id, "state": "scheduled"}
        )
    ).json()["items"]
    assert len(revived) == 14
    assert revived[0]["id"] == occurrences[0]["id"]  # stable deterministic identity

    # Maintenance extends the horizon as time passes.
    harness.clock.advance(days=7)
    await harness.worker.ensure_maintenance()
    await harness.run_due()
    later = (
        await client.get(
            "/api/v1/occurrences",
            params={
                "reminder_id": reminder_id,
                "from": (T0 + timedelta(days=7)).isoformat(),
                "limit": 200,
            },
        )
    ).json()["items"]
    assert later[-1]["scheduled_at"] >= "2026-03-22"


async def test_recurring_rule_validation_errors(client) -> None:
    response = await client.post(
        "/api/v1/reminders",
        json={
            "title": "Spam",
            "schedule": {
                "kind": "recurring",
                "start_local": "2026-03-02T09:00:00",
                "timezone": "UTC",
                "rrule": "FREQ=MINUTELY",
            },
        },
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_schedule"


async def test_critical_requires_user_opt_in(client) -> None:
    await register_device(client)
    body = {
        "title": "Server down",
        "delivery_mode": "critical",
        "schedule": {"kind": "once", "at": (T0 + timedelta(minutes=5)).isoformat()},
    }
    refused = await client.post("/api/v1/reminders", json=body)
    assert refused.status_code == 403
    assert refused.json()["error"]["code"] == "critical_alerts_disabled"

    settings = (await client.get("/api/v1/settings")).json()
    prefs = {**settings["preferences"], "critical_alerts_enabled": True}
    updated = await client.patch("/api/v1/settings", json={"preferences": prefs})
    assert updated.status_code == 200, updated.text
    allowed = await client.post("/api/v1/reminders", json=body)
    assert allowed.status_code == 201, allowed.text


async def test_today_view_lists_due_and_overdue(client, harness: Harness) -> None:
    await register_device(client)
    await create_once(client, minutes=5, title="Soon", mode="persistent")
    await create_once(client, minutes=60 * 30, title="Tomorrow")
    await harness.advance_and_run(minutes=6)
    today = (
        await client.get(
            "/api/v1/occurrences",
            params={
                "from": datetime(2026, 3, 2, tzinfo=UTC).isoformat(),
                "to": datetime(2026, 3, 3, tzinfo=UTC).isoformat(),
            },
        )
    ).json()["items"]
    assert [(o["title"], o["state"]) for o in today] == [("Soon", "fired")]
    assert today[0]["requires_ack"] is True


async def test_history_endpoint(client) -> None:
    created = await create_once(client)
    history = (await client.get(f"/api/v1/reminders/{created['reminder_id']}/history")).json()
    assert [e["event_type"] for e in history["items"]] == ["reminder.created"]
    assert history["items"][0]["actor"]["kind"] == "user"
