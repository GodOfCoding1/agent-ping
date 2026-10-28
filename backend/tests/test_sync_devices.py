"""Device pairing/credentials, multi-device behaviour, incremental sync and offline replay."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from tests.conftest import T0, Harness, auth, create_once, register_device


async def test_pairing_code_is_single_use_and_expires(client, harness: Harness) -> None:
    code = (await client.post("/api/v1/devices/pairing-codes")).json()["code"]
    body = {"pairing_code": code, "installation_id": "install-0001", "name": "Pixel"}
    first = await client.post("/api/v1/devices/register", json=body, headers={"Authorization": ""})
    assert first.status_code == 201
    reuse = await client.post(
        "/api/v1/devices/register",
        json={**body, "installation_id": "install-0002"},
        headers={"Authorization": ""},
    )
    assert reuse.status_code == 401

    stale = (await client.post("/api/v1/devices/pairing-codes")).json()["code"]
    harness.clock.advance(minutes=11)
    expired = await client.post(
        "/api/v1/devices/register",
        json={**body, "pairing_code": stale, "installation_id": "install-0003"},
        headers={"Authorization": ""},
    )
    assert expired.status_code == 401


async def test_unauthenticated_requests_are_rejected(client) -> None:
    response = await client.get("/api/v1/reminders", headers={"Authorization": ""})
    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Bearer")
    bogus = await client.get("/api/v1/reminders", headers=auth("apa_not-a-real-token"))
    assert bogus.status_code == 401


async def test_reregistering_rotates_credential(client) -> None:
    first = await register_device(client, installation_id="install-0001")
    second = await register_device(client, installation_id="install-0001", name="Pixel renamed")
    assert first["device"]["id"] == second["device"]["id"]
    assert second["device"]["name"] == "Pixel renamed"
    old = await client.get("/api/v1/devices/me", headers=auth(first["device_token"]))
    assert old.status_code == 401
    new = await client.get("/api/v1/devices/me", headers=auth(second["device_token"]))
    assert new.status_code == 200

    rotated = (
        await client.post(
            "/api/v1/devices/me/rotate-credential", headers=auth(second["device_token"])
        )
    ).json()
    assert (
        await client.get("/api/v1/devices/me", headers=auth(second["device_token"]))
    ).status_code == 401
    assert (
        await client.get("/api/v1/devices/me", headers=auth(rotated["device_token"]))
    ).status_code == 200


async def test_paired_device_acts_for_owner_but_device_endpoints_need_device(client) -> None:
    """The Android app is the owner's control surface, so a paired phone can manage
    settings and agents; device-only endpoints still require a device credential."""
    device = await register_device(client)
    token = auth(device["device_token"])
    assert (await client.get("/api/v1/reminders", headers=token)).status_code == 200
    assert (
        await client.patch("/api/v1/settings", json={"timezone": "UTC"}, headers=token)
    ).status_code == 200
    assert (await client.get("/api/v1/devices/me")).status_code == 403
    receipts = await client.post(
        "/api/v1/devices/me/receipts",
        json={
            "receipts": [
                {
                    "occurrence_id": device["device"]["id"],
                    "kind": "received",
                    "occurrence_version": 1,
                    "at": "2026-03-02T09:00:00Z",
                }
            ]
        },
    )
    assert receipts.status_code == 403


async def test_push_token_moves_between_installations(client) -> None:
    a = await register_device(
        client, installation_id="install-aaaa", push_token="shared-token-zzzzzzzzzz"
    )
    b = await register_device(
        client, installation_id="install-bbbb", push_token="shared-token-zzzzzzzzzz"
    )
    devices = {d["id"]: d for d in (await client.get("/api/v1/devices")).json()}
    assert devices[a["device"]["id"]]["push_token_status"] == "missing"
    assert devices[b["device"]["id"]]["push_token_status"] == "valid"


async def test_push_targets_fid_by_default_and_supports_legacy_tokens(
    client, harness: Harness
) -> None:
    phone = await register_device(
        client, installation_id="phone-fid-01", push_token="fid-phone-aaaaaaaaaaaaa"
    )
    tablet = await register_device(
        client,
        installation_id="tablet-legacy",
        push_token="legacy-token-bbbbbbbbbbbb",
        push_token_kind="registration_token",
    )
    assert phone["device"]["push_token_kind"] == "fid"
    assert tablet["device"]["push_token_kind"] == "registration_token"

    await client.post(
        "/api/v1/notifications", json={"title": "Hello", "targets": {"strategy": "all"}}
    )
    await harness.run_due()
    kinds = {m.token: m.token_kind.value for m in harness.push.of_type("deliver")}
    assert kinds == {
        "fid-phone-aaaaaaaaaaaaa": "fid",
        "legacy-token-bbbbbbbbbbbb": "registration_token",
    }

    upgraded = await client.patch(
        "/api/v1/devices/me",
        json={"push_token": "fid-tablet-cccccccccccc", "push_token_kind": "fid"},
        headers=auth(tablet["device_token"]),
    )
    assert upgraded.json()["push_token_kind"] == "fid"


async def test_primary_device_targeting_and_switching(client, harness: Harness) -> None:
    phone = await register_device(
        client, installation_id="phone-0001", push_token="token-phone-aaaaaaaaaa"
    )
    tablet = await register_device(
        client, installation_id="tablet-0001", push_token="token-tablet-bbbbbbbbb"
    )
    assert phone["device"]["is_primary"] and not tablet["device"]["is_primary"]

    alarm = await create_once(client, minutes=5, mode="alarm")
    assert alarm["target_device_ids"] == [phone["device"]["id"]]

    switched = await client.patch(
        f"/api/v1/devices/{tablet['device']['id']}", json={"is_primary": True}
    )
    assert switched.json()["is_primary"] is True
    devices = {d["id"]: d["is_primary"] for d in (await client.get("/api/v1/devices")).json()}
    assert devices == {phone["device"]["id"]: False, tablet["device"]["id"]: True}

    await harness.advance_and_run(minutes=5)
    assert [m.token for m in harness.push.of_type("deliver")] == ["token-tablet-bbbbbbbbb"]


async def test_mode_disabled_on_device_is_reported(client) -> None:
    phone = await register_device(
        client, installation_id="phone-0001", push_token="token-phone-aaaaaaaaaa"
    )
    tablet = await register_device(
        client, installation_id="tablet-0001", push_token="token-tablet-bbbbbbbbb"
    )
    await client.patch(
        f"/api/v1/devices/{tablet['device']['id']}",
        json={
            "mode_settings": {"normal": False, "persistent": True, "alarm": True, "critical": True}
        },
    )
    response = (await client.post("/api/v1/notifications", json={"title": "Hi"})).json()
    assert [t["device_id"] for t in response["accepted_targets"]] == [phone["device"]["id"]]
    assert response["rejected_targets"][0]["device_id"] == tablet["device"]["id"]


async def test_explicit_targets_must_belong_to_user(client) -> None:
    response = await client.post(
        "/api/v1/notifications",
        json={
            "title": "x",
            "targets": {
                "strategy": "devices",
                "device_ids": ["00000000-0000-0000-0000-000000000001"],
            },
        },
    )
    assert response.status_code == 403


async def test_deactivation_revokes_and_promotes_new_primary(client) -> None:
    phone = await register_device(
        client, installation_id="phone-0001", push_token="token-phone-aaaaaaaaaa"
    )
    tablet = await register_device(
        client, installation_id="tablet-0001", push_token="token-tablet-bbbbbbbbb"
    )
    removed = await client.delete(f"/api/v1/devices/{phone['device']['id']}")
    assert removed.json()["status"] == "deactivated"
    assert (
        await client.get("/api/v1/devices/me", headers=auth(phone["device_token"]))
    ).status_code == 401
    [remaining] = (await client.get("/api/v1/devices")).json()
    assert remaining["id"] == tablet["device"]["id"] and remaining["is_primary"] is True
    everything = (await client.get("/api/v1/devices", params={"include_inactive": True})).json()
    assert len(everything) == 2


async def test_permission_revocation_is_visible(client) -> None:
    device = await register_device(
        client, capabilities={"notifications_permitted": True, "exact_alarms_permitted": True}
    )
    updated = await client.patch(
        "/api/v1/devices/me",
        json={"capabilities": {"notifications_permitted": True, "exact_alarms_permitted": False}},
        headers=auth(device["device_token"]),
    )
    assert "exact_alarms_not_permitted" in updated.json()["health"]["issues"]
    created = await create_once(client, minutes=10, mode="alarm")
    assert created["permissions_required"]
    events = (await client.get("/api/v1/activity", params={"category": "error"})).json()["items"]
    assert [e["event_type"] for e in events] == ["device.permission_revoked"]


async def test_full_then_incremental_sync(client, harness: Harness) -> None:
    device = await register_device(client)
    token = auth(device["device_token"])
    full = (await client.get("/api/v1/sync", headers=token)).json()
    assert full["full"] is True and full["settings"] is not None
    cursor = full["cursor"]

    created = await create_once(client, minutes=30, title="Incremental")
    delta = (await client.get("/api/v1/sync", params={"cursor": cursor}, headers=token)).json()
    assert delta["full"] is False
    assert [r["title"] for r in delta["reminders"]] == ["Incremental"]
    [occurrence] = delta["occurrences"]
    assert occurrence["id"] == created["next_occurrence"]["id"]
    assert occurrence["deliver_on_this_device"] is True
    assert delta["cursor"] > cursor

    nothing = (
        await client.get("/api/v1/sync", params={"cursor": delta["cursor"]}, headers=token)
    ).json()
    assert nothing["reminders"] == [] and nothing["occurrences"] == []
    assert nothing["cursor"] == delta["cursor"]

    # A cursor from the future (e.g. restored backup) forces a safe full resync.
    future = (await client.get("/api/v1/sync", params={"cursor": 10_000}, headers=token)).json()
    assert future["full"] is True


async def test_sync_pagination_with_many_changes(client) -> None:
    device = await register_device(client)
    token = auth(device["device_token"])
    cursor = (await client.get("/api/v1/sync", headers=token)).json()["cursor"]
    for index in range(6):
        await create_once(client, minutes=20 + index, title=f"R{index}")
    seen: set[str] = set()
    while True:
        page = (
            await client.get("/api/v1/sync", params={"cursor": cursor, "limit": 4}, headers=token)
        ).json()
        seen |= {r["title"] for r in page["reminders"]}
        cursor = page["cursor"]
        if not page["has_more"]:
            break
    assert seen == {f"R{i}" for i in range(6)}


async def test_sync_hint_push_sent_to_stale_devices(client, harness: Harness) -> None:
    device = await register_device(client)
    await create_once(client, minutes=30)
    await harness.advance_and_run(seconds=5)
    assert len(harness.push.of_type("sync")) == 1
    await client.get("/api/v1/sync", headers=auth(device["device_token"]))
    await create_once(client, minutes=40)
    await client.get("/api/v1/sync", params={"cursor": 10**6}, headers=auth(device["device_token"]))
    await harness.advance_and_run(seconds=5)
    assert len(harness.push.of_type("sync")) == 1  # device is up to date


async def test_offline_action_replay_is_ordered_and_deduplicated(client, harness: Harness) -> None:
    device = await register_device(client)
    token = auth(device["device_token"])
    created = await create_once(client, minutes=1, mode="persistent")
    occurrence_id = created["next_occurrence"]["id"]
    await harness.advance_and_run(minutes=1)

    batch = {
        "actions": [
            {
                "client_action_id": "a2",
                "occurrence_id": occurrence_id,
                "action": "acknowledge",
                "occurred_at": (T0 + timedelta(minutes=5)).isoformat(),
            },
            {
                "client_action_id": "a1",
                "occurrence_id": occurrence_id,
                "action": "snooze",
                "snooze_minutes": 5,
                "occurred_at": (T0 + timedelta(minutes=2)).isoformat(),
            },
        ]
    }
    harness.clock.advance(minutes=10)
    results = (await client.post("/api/v1/sync/actions", json=batch, headers=token)).json()
    assert [(r["client_action_id"], r["result"]) for r in results] == [
        ("a1", "applied"),
        ("a2", "applied"),
    ]
    replay = (await client.post("/api/v1/sync/actions", json=batch, headers=token)).json()
    assert {r["result"] for r in replay} == {"duplicate"}
    detail = (await client.get(f"/api/v1/occurrences/{occurrence_id}")).json()
    assert detail["state"] == "acknowledged"
    assert detail["acknowledged_at"].startswith("2026-03-02T09:05")


async def test_concurrent_acknowledgements_from_two_devices(client, harness: Harness) -> None:
    phone = await register_device(
        client, installation_id="phone-0001", push_token="token-phone-aaaaaaaaaa"
    )
    watch = await register_device(
        client, installation_id="tablet-0001", push_token="token-tablet-bbbbbbbbb"
    )
    created = await create_once(client, minutes=1, mode="persistent")
    occurrence_id = created["next_occurrence"]["id"]
    await harness.advance_and_run(minutes=1)

    async def ack(device: dict, action_id: str) -> str:
        response = await client.post(
            f"/api/v1/occurrences/{occurrence_id}/acknowledge",
            json={"client_action_id": action_id},
            headers=auth(device["device_token"]),
        )
        return response.json()["result"]

    results = await asyncio.gather(ack(phone, "p1"), ack(watch, "w1"))
    assert sorted(results) == ["applied", "duplicate"]
    actions = (await client.get("/api/v1/activity", params={"category": "action"})).json()["items"]
    assert len([a for a in actions if a["event_type"] == "occurrence.acknowledged"]) == 1


async def test_ack_racing_with_cancel(client, harness: Harness) -> None:
    device = await register_device(client)
    created = await create_once(client, minutes=1, mode="persistent")
    occurrence_id = created["next_occurrence"]["id"]
    await harness.advance_and_run(minutes=1)
    ack, cancel = await asyncio.gather(
        client.post(
            f"/api/v1/occurrences/{occurrence_id}/acknowledge",
            json={"client_action_id": "race"},
            headers=auth(device["device_token"]),
        ),
        client.post(f"/api/v1/reminders/{created['reminder_id']}/cancel"),
    )
    # Writes are serialized per user, so exactly one ordering wins; never a lost update.
    final = (await client.get(f"/api/v1/occurrences/{occurrence_id}")).json()["state"]
    if ack.json()["result"] == "applied":
        assert final == "acknowledged"
        assert cancel.status_code == 409
        assert cancel.json()["error"]["code"] == "invalid_state_transition"
    else:
        assert ack.json()["result"] == "rejected_stale"
        assert final == "cancelled"
        assert cancel.status_code == 200


async def test_settings_revision_conflict_and_timezone_follow(client) -> None:
    settings = (await client.get("/api/v1/settings")).json()
    ok = await client.patch(
        "/api/v1/settings",
        json={"expected_revision": settings["revision"], "timezone": "Europe/London"},
    )
    assert ok.json()["timezone"] == "Europe/London"
    stale = await client.patch(
        "/api/v1/settings",
        json={"expected_revision": settings["revision"], "timezone": "Asia/Tokyo"},
    )
    assert stale.status_code == 409

    await client.patch("/api/v1/settings", json={"timezone_source": "primary_device"})
    device = await register_device(client, timezone="Asia/Kolkata")
    assert (await client.get("/api/v1/settings")).json()["timezone"] == "Asia/Kolkata"
    await client.patch(
        "/api/v1/devices/me", json={"timezone": "Asia/Tokyo"}, headers=auth(device["device_token"])
    )
    assert (await client.get("/api/v1/settings")).json()["timezone"] == "Asia/Tokyo"


async def test_floating_reminder_follows_timezone_change(client) -> None:
    response = await client.post(
        "/api/v1/reminders",
        json={
            "title": "Morning pills",
            "schedule": {
                "kind": "recurring",
                "start_local": "2026-03-03T08:00:00",
                "timezone": "Asia/Kolkata",
                "timezone_mode": "floating",
                "rrule": "FREQ=DAILY",
            },
        },
    )
    assert response.status_code == 201, response.text
    reminder_id = response.json()["reminder_id"]
    await client.patch("/api/v1/settings", json={"timezone": "Asia/Kolkata"})
    before = (await client.get("/api/v1/occurrences", params={"reminder_id": reminder_id})).json()[
        "items"
    ]
    assert before[0]["scheduled_at"] == "2026-03-03T02:30:00Z"
    await client.patch("/api/v1/settings", json={"timezone": "Europe/London"})
    after = (
        await client.get(
            "/api/v1/occurrences", params={"reminder_id": reminder_id, "state": "scheduled"}
        )
    ).json()["items"]
    assert after[0]["scheduled_at"] == "2026-03-03T08:00:00Z"
    assert after[0]["local_wall"].startswith("2026-03-03T08:00")
