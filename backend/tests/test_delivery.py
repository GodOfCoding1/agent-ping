"""Worker-driven delivery: fallback pushes, escalation, quiet hours, retries, expiry."""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import select

from agentping.db.models import OutboxJob
from agentping.push.base import PushResult
from agentping.services.core import enqueue
from tests.conftest import T0, Harness, auth, create_once, register_device


async def _occurrence(client, occurrence_id: str) -> dict:
    return (await client.get(f"/api/v1/occurrences/{occurrence_id}")).json()


async def _receipt(client, token: str, occurrence: dict, kind: str, at=None) -> dict:
    response = await client.post(
        "/api/v1/devices/me/receipts",
        json={
            "receipts": [
                {
                    "occurrence_id": occurrence["id"],
                    "kind": kind,
                    "occurrence_version": occurrence["version"],
                    "at": (at or T0).isoformat(),
                }
            ]
        },
        headers=auth(token),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def test_send_notification_pushes_immediately_to_all_devices(
    client, harness: Harness
) -> None:
    await register_device(client, installation_id="phone-0001", push_token="token-phone-aaaaaaaaaa")
    await register_device(
        client, installation_id="tablet-0001", push_token="token-tablet-bbbbbbbbb"
    )
    response = await client.post(
        "/api/v1/notifications", json={"title": "Build passed", "body": "main is green"}
    )
    assert response.status_code == 202, response.text
    result = response.json()
    assert result["dispatch_state"] == "queued"
    assert len(result["accepted_targets"]) == 2

    await harness.run_due()
    sent = harness.push.of_type("deliver")
    assert {m.token for m in sent} == {"token-phone-aaaaaaaaaa", "token-tablet-bbbbbbbbb"}
    assert all(m.data["title"] == "Build passed" for m in sent)
    # One deterministic event ID per alert, so each phone dedupes repeated deliveries.
    assert len({m.data["event_id"] for m in sent}) == 1

    detail = await _occurrence(client, result["occurrence_id"])
    assert {d["state"] for d in detail["deliveries"]} == {"provider_accepted"}

    # Re-running the worker never re-sends.
    await harness.advance_and_run(minutes=1)
    assert len(harness.push.of_type("deliver")) == 2


async def test_minimal_push_content_omits_text(client, harness: Harness) -> None:
    await register_device(client)
    settings = (await client.get("/api/v1/settings")).json()
    await client.patch(
        "/api/v1/settings",
        json={"preferences": {**settings["preferences"], "push_content": "minimal"}},
    )
    await client.post("/api/v1/notifications", json={"title": "Private", "body": "secret"})
    await harness.run_due()
    [message] = harness.push.of_type("deliver")
    assert "title" not in message.data and "body" not in message.data


async def test_local_schedule_receipt_suppresses_server_fallback(client, harness: Harness) -> None:
    device = await register_device(client)
    created = await create_once(client, minutes=10)
    occurrence = created["next_occurrence"]
    await _receipt(client, device["device_token"], occurrence, "scheduled")

    await harness.advance_and_run(minutes=10)
    assert harness.push.of_type("deliver") == []
    detail = await _occurrence(client, occurrence["id"])
    assert detail["state"] == "fired"
    assert detail["deliveries"][0]["state"] == "scheduled_locally"


async def test_stale_local_schedule_does_not_suppress_fallback(client, harness: Harness) -> None:
    device = await register_device(client)
    created = await create_once(client, minutes=10)
    stale = created["next_occurrence"]
    await _receipt(client, device["device_token"], stale, "scheduled")
    # Snoozing before the due time changes the occurrence version and time.
    snoozed = await client.post(f"/api/v1/occurrences/{stale['id']}/snooze", json={"minutes": 30})
    assert snoozed.json()["result"] == "applied"
    assert snoozed.json()["occurrence"]["version"] > stale["version"]

    await harness.advance_and_run(minutes=10)
    assert harness.push.of_type("deliver") == []  # original time no longer applies
    await harness.advance_and_run(minutes=30)
    assert len(harness.push.of_type("deliver")) == 1


async def test_persistent_escalates_until_acknowledged(client, harness: Harness) -> None:
    phone = await register_device(
        client, installation_id="phone-0001", push_token="token-phone-aaaaaaaaaa"
    )
    await register_device(
        client, installation_id="tablet-0001", push_token="token-tablet-bbbbbbbbb"
    )
    created = await create_once(client, minutes=1, mode="persistent")
    occurrence_id = created["next_occurrence"]["id"]

    await harness.advance_and_run(minutes=1)
    assert len(harness.push.of_type("deliver")) == 2
    await harness.advance_and_run(minutes=5)  # escalation step 1 (300s)
    steps = [m.data.get("escalation_step") for m in harness.push.of_type("deliver")]
    assert steps.count("1") == 2

    ack = await client.post(
        f"/api/v1/occurrences/{occurrence_id}/acknowledge",
        json={"client_action_id": "phone-ack"},
        headers=auth(phone["device_token"]),
    )
    assert ack.json()["result"] == "applied"
    await harness.run_due()
    dismissals = harness.push.of_type("dismiss")
    assert [m.token for m in dismissals] == ["token-tablet-bbbbbbbbb"]

    before = len(harness.push.of_type("deliver"))
    await harness.advance_and_run(minutes=30)
    assert len(harness.push.of_type("deliver")) == before  # escalation stopped


async def test_unacknowledged_alarm_becomes_missed(client, harness: Harness) -> None:
    await register_device(client)
    created = await create_once(client, minutes=5, mode="alarm")
    occurrence_id = created["next_occurrence"]["id"]
    await harness.advance_and_run(minutes=5)
    assert (await _occurrence(client, occurrence_id))["state"] == "fired"
    await harness.advance_and_run(hours=2)
    assert (await _occurrence(client, occurrence_id))["state"] == "missed"
    reminder = (await client.get(f"/api/v1/reminders/{created['reminder_id']}")).json()
    assert reminder["reminder"]["status"] == "completed"
    events = (await client.get("/api/v1/activity", params={"category": "delivery"})).json()
    assert "occurrence.missed" in {e["event_type"] for e in events["items"]}

    late_ack = await client.post(f"/api/v1/occurrences/{occurrence_id}/acknowledge", json={})
    assert late_ack.json()["result"] == "applied"  # acknowledging a missed alert is allowed


async def test_snooze_refires_and_respects_limit(client, harness: Harness) -> None:
    await register_device(client)
    created = await create_once(
        client,
        minutes=1,
        mode="persistent",
        snooze={"default_minutes": 10, "max_snoozes": 1, "allowed_minutes": [5, 10]},
    )
    occurrence_id = created["next_occurrence"]["id"]
    await harness.advance_and_run(minutes=1)
    first = await client.post(f"/api/v1/occurrences/{occurrence_id}/snooze", json={})
    assert first.json()["occurrence"]["state"] == "snoozed"
    assert first.json()["occurrence"]["scheduled_at"] == "2026-03-02T09:11:00Z"

    pushes = len(harness.push.of_type("deliver"))
    await harness.advance_and_run(minutes=10)
    assert len(harness.push.of_type("deliver")) == pushes + 1
    assert (await _occurrence(client, occurrence_id))["state"] == "fired"

    second = await client.post(f"/api/v1/occurrences/{occurrence_id}/snooze", json={})
    assert second.status_code == 200
    assert second.json()["result"] == "rejected_invalid"
    assert "limit" in second.json()["reason"]
    assert second.json()["occurrence"]["state"] == "fired"


async def test_skip_requires_reason_and_cancels_delivery(client, harness: Harness) -> None:
    await register_device(client)
    created = await create_once(client, minutes=10)
    occurrence_id = created["next_occurrence"]["id"]
    missing = await client.post(f"/api/v1/occurrences/{occurrence_id}/skip", json={})
    assert missing.status_code == 422
    skipped = await client.post(
        f"/api/v1/occurrences/{occurrence_id}/skip", json={"reason": "Already done"}
    )
    assert skipped.json()["occurrence"]["state"] == "skipped"
    await harness.advance_and_run(minutes=15)
    assert harness.push.of_type("deliver") == []


async def test_quiet_hours_defer_normal_but_not_alarm(client, harness: Harness) -> None:
    await register_device(client)
    settings = (await client.get("/api/v1/settings")).json()
    prefs = {
        **settings["preferences"],
        "quiet_hours": {"enabled": True, "start": "09:00", "end": "10:00", "behavior": "defer"},
    }
    assert (await client.patch("/api/v1/settings", json={"preferences": prefs})).status_code == 200

    result = (await client.post("/api/v1/notifications", json={"title": "FYI"})).json()
    assert result["dispatch_state"] == "deferred_quiet_hours"
    await create_once(client, minutes=5, mode="alarm", title="Wake")
    await create_once(client, minutes=5, title="Normal reminder")

    await harness.advance_and_run(minutes=5)
    titles = [m.data["title"] for m in harness.push.of_type("deliver")]
    assert titles == ["Wake"]

    await harness.advance_and_run(minutes=56)  # 10:01, quiet hours over
    titles = sorted(m.data["title"] for m in harness.push.of_type("deliver"))
    assert titles == ["FYI", "Normal reminder", "Wake"]


async def test_quiet_hours_silent_delivers_quietly(client, harness: Harness) -> None:
    await register_device(client)
    settings = (await client.get("/api/v1/settings")).json()
    prefs = {
        **settings["preferences"],
        "quiet_hours": {"enabled": True, "start": "08:00", "end": "11:00", "behavior": "silent"},
    }
    await client.patch("/api/v1/settings", json={"preferences": prefs})
    await create_once(client, minutes=5)
    await harness.advance_and_run(minutes=5)
    [message] = harness.push.of_type("deliver")
    assert message.data["silent"] == "1"


async def test_retryable_push_failure_is_retried_with_backoff(client, harness: Harness) -> None:
    await register_device(client, push_token="flaky-token-cccccccccccc")
    harness.push.failures["flaky-token-cccccccccccc"] = PushResult(
        accepted=False, provider="fake", error_code="UNAVAILABLE", retryable=True
    )
    await client.post("/api/v1/notifications", json={"title": "Retry me"})
    await harness.run_due()
    assert len(harness.push.sent) == 1
    async with harness.runtime.session_factory() as session:
        job = (
            await session.execute(select(OutboxJob).where(OutboxJob.kind == "push.deliver"))
        ).scalar_one()
    assert job.status == "pending" and job.attempts == 1 and job.run_at > harness.clock.now()

    del harness.push.failures["flaky-token-cccccccccccc"]
    await harness.advance_and_run(minutes=2)
    assert len(harness.push.of_type("deliver")) == 2
    diagnostics = (await client.get("/api/v1/diagnostics")).json()
    assert diagnostics["outbox"]["dead"] == 0


async def test_exhausted_push_retries_are_recorded_as_failed(client, harness: Harness) -> None:
    await register_device(client, push_token="broken-token-ddddddddddd")
    harness.push.failures["broken-token-ddddddddddd"] = PushResult(
        accepted=False, provider="fake", error_code="UNAVAILABLE", retryable=True
    )
    created = (await client.post("/api/v1/notifications", json={"title": "Never arrives"})).json()
    for _ in range(10):
        await harness.advance_and_run(minutes=30)
    attempts = [m for m in harness.push.of_type("deliver")]
    assert len(attempts) == harness.runtime.settings.worker_max_attempts
    detail = await _occurrence(client, created["occurrence_id"])
    assert detail["deliveries"][0]["state"] == "provider_failed"
    errors = (await client.get("/api/v1/activity", params={"category": "error"})).json()["items"]
    assert errors[0]["event_type"] == "delivery.provider_failed"


async def test_crashing_job_is_dead_lettered_and_audited(client, harness: Harness) -> None:
    owner = await harness.owner()

    async def enqueue_bogus(ctx):
        await enqueue(ctx, "bogus.kind", {"x": 1})

    await harness.ctx_call(owner, enqueue_bogus)
    await harness.run_due()
    async with harness.runtime.session_factory() as session:
        job = (
            await session.execute(select(OutboxJob).where(OutboxJob.kind == "bogus.kind"))
        ).scalar_one()
    assert job.status == "dead"
    diagnostics = (await client.get("/api/v1/diagnostics")).json()
    assert diagnostics["outbox"]["dead"] == 1
    errors = (await client.get("/api/v1/activity", params={"category": "error"})).json()["items"]
    assert errors[0]["event_type"] == "worker.job_failed"


async def test_invalid_token_marks_device_and_stops_pushing(client, harness: Harness) -> None:
    device = await register_device(client, push_token="expired-token-eeeeeeeeee")
    harness.push.failures["expired-token-eeeeeeeeee"] = PushResult(
        accepted=False, provider="fake", error_code="UNREGISTERED", token_invalid=True
    )
    await client.post("/api/v1/notifications", json={"title": "One"})
    await harness.run_due()
    me = (await client.get("/api/v1/devices/me", headers=auth(device["device_token"]))).json()
    assert me["push_token_status"] == "invalid"
    assert "push_token_invalid" in me["health"]["issues"]

    sent = len(harness.push.sent)
    await client.post("/api/v1/notifications", json={"title": "Two"})
    await harness.run_due()
    assert len(harness.push.sent) == sent

    # The app refreshes its FCM token; delivery resumes.
    refreshed = await client.patch(
        "/api/v1/devices/me",
        json={"push_token": "fresh-token-ffffffffffff"},
        headers=auth(device["device_token"]),
    )
    assert refreshed.json()["push_token_status"] == "valid"
    await client.post("/api/v1/notifications", json={"title": "Three"})
    await harness.run_due()
    assert harness.push.of_type("deliver")[-1].token == "fresh-token-ffffffffffff"


async def test_server_fallback_skipped_when_far_too_late(client, harness: Harness) -> None:
    await register_device(client)
    await create_once(client, minutes=5, mode="alarm")  # 15 minute late grace
    harness.clock.advance(minutes=5 + 60)  # worker was down for an hour
    await harness.run_due()
    assert harness.push.of_type("deliver") == []
    events = (await client.get("/api/v1/activity", params={"category": "delivery"})).json()["items"]
    assert "delivery.server_late" in {e["event_type"] for e in events}


async def test_displayed_receipt_marks_fired_and_records_delivery(client, harness: Harness) -> None:
    device = await register_device(client)
    created = await create_once(client, minutes=10, mode="persistent")
    occurrence = created["next_occurrence"]
    harness.clock.advance(minutes=10)
    result = await _receipt(
        client, device["device_token"], occurrence, "displayed", at=harness.clock.now()
    )
    assert result == {"accepted": 1, "ignored": 0}
    detail = await _occurrence(client, occurrence["id"])
    assert detail["state"] == "fired"
    assert detail["deliveries"][0]["state"] == "displayed"
    await harness.run_due()
    assert harness.push.of_type("deliver") == []  # already displayed on the only device
    expire_jobs = []
    async with harness.runtime.session_factory() as session:
        expire_jobs = list(
            (
                await session.execute(
                    select(OutboxJob).where(OutboxJob.kind == "occurrence.expire")
                )
            ).scalars()
        )
    assert len(expire_jobs) == 1


async def test_device_failure_receipt_is_audited(client, harness: Harness) -> None:
    device = await register_device(client)
    created = await create_once(client, minutes=10, mode="alarm")
    await _receipt(client, device["device_token"], created["next_occurrence"], "failed")
    errors = (await client.get("/api/v1/activity", params={"category": "error"})).json()["items"]
    assert errors[0]["event_type"] == "delivery.device_failed"
    assert timedelta(0) <= harness.clock.now() - T0


async def test_delayed_receipts_cannot_override_snooze_or_cancellation(
    client, harness: Harness
) -> None:
    device = await register_device(client)
    created = await create_once(client, minutes=10)
    old = created["next_occurrence"]
    response = await client.post(f"/api/v1/occurrences/{old['id']}/snooze", json={"minutes": 30})
    current = response.json()["occurrence"]
    await _receipt(client, device["device_token"], current, "scheduled")
    for kind in ("displayed", "unscheduled", "scheduled"):
        assert await _receipt(client, device["device_token"], old, kind) == {
            "accepted": 0,
            "ignored": 1,
        }
    detail = await _occurrence(client, old["id"])
    assert detail["state"] == "snoozed"
    assert detail["deliveries"][0]["state"] == "scheduled_locally"
    assert await _receipt(client, device["device_token"], current, "displayed") == {
        "accepted": 0,
        "ignored": 1,
    }
    await client.post(f"/api/v1/reminders/{created['reminder_id']}/cancel")
    cancelled = await _occurrence(client, old["id"])
    assert await _receipt(client, device["device_token"], cancelled, "displayed") == {
        "accepted": 0,
        "ignored": 1,
    }
    assert (await _occurrence(client, old["id"]))["state"] == "cancelled"


async def test_scheduled_quiet_deferral_can_deliver_after_original_late_grace(
    client, harness: Harness
) -> None:
    await register_device(client, push_token="quiet-phone-registration")
    settings = (await client.get("/api/v1/settings")).json()
    await client.patch(
        "/api/v1/settings",
        json={
            "timezone": "UTC",
            "preferences": {
                **settings["preferences"],
                "quiet_hours": {
                    "enabled": True,
                    "start": "09:00",
                    "end": "10:00",
                    "behavior": "defer",
                },
            },
        },
    )
    created = await create_once(client, minutes=5, mode="normal")
    await harness.advance_and_run(minutes=5)
    assert harness.push.of_type("deliver") == []
    await harness.advance_and_run(minutes=55)
    assert len(harness.push.of_type("deliver")) == 1
    detail = await _occurrence(client, created["next_occurrence"]["id"])
    assert detail["deliveries"][0]["state"] == "provider_accepted"


async def test_old_display_receipt_does_not_suppress_new_snooze_delivery(
    client, harness: Harness
) -> None:
    phone = await register_device(client, push_token="snooze-phone-registration")
    created = await create_once(client, minutes=1, mode="alarm")
    occurrence = created["next_occurrence"]
    await harness.advance_and_run(minutes=1)
    await _receipt(client, phone["device_token"], occurrence, "displayed", at=harness.clock.now())
    await client.post(f"/api/v1/occurrences/{occurrence['id']}/snooze", json={"minutes": 5})
    initial = len(harness.push.of_type("deliver"))
    await harness.advance_and_run(minutes=5)
    assert len(harness.push.of_type("deliver")) == initial + 1
    detail = await _occurrence(client, occurrence["id"])
    assert detail["deliveries"][0]["state"] == "provider_accepted"
    assert detail["deliveries"][0]["displayed_at"] is None


async def test_critical_opt_out_blocks_already_queued_retry(client, harness: Harness) -> None:
    await register_device(client, push_token="critical-retry-registration")
    settings = (await client.get("/api/v1/settings")).json()
    preferences = {**settings["preferences"], "critical_alerts_enabled": True}
    await client.patch("/api/v1/settings", json={"preferences": preferences})
    harness.push.failures["critical-retry-registration"] = PushResult(
        False, "fake", error_code="UNAVAILABLE", retryable=True
    )
    response = await client.post(
        "/api/v1/notifications", json={"title": "Critical", "delivery_mode": "critical"}
    )
    assert response.status_code == 202
    await harness.run_due()
    attempts = len(harness.push.of_type("deliver"))
    assert attempts > 0
    await client.patch(
        "/api/v1/settings", json={"preferences": {**preferences, "critical_alerts_enabled": False}}
    )
    await harness.advance_and_run(minutes=2)
    assert len(harness.push.of_type("deliver")) == attempts


async def test_primary_switch_blocks_retry_to_previous_primary(client, harness: Harness) -> None:
    await register_device(
        client, installation_id="primary-old", push_token="primary-old-registration"
    )
    tablet = await register_device(
        client, installation_id="primary-new", push_token="primary-new-registration"
    )
    harness.push.failures["primary-old-registration"] = PushResult(
        False, "fake", error_code="UNAVAILABLE", retryable=True
    )
    await client.post(
        "/api/v1/notifications", json={"title": "Primary retry", "targets": {"strategy": "primary"}}
    )
    await harness.run_due()
    attempts = len(harness.push.of_type("deliver"))
    assert attempts > 0
    await client.patch(f"/api/v1/devices/{tablet['device']['id']}", json={"is_primary": True})
    await harness.advance_and_run(minutes=2)
    assert len(harness.push.of_type("deliver")) == attempts
