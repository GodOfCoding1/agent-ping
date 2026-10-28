"""Resolve which devices receive a reminder, and which device permissions are missing."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import select

from agentping.db.models import Device, Reminder, ReminderDeviceTarget
from agentping.domain.enums import DeliveryMode, DeviceStatus, PushTokenStatus, TargetStrategy
from agentping.domain.errors import DeviceNotAccessible
from agentping.domain.schemas import DeviceHealth, Notice
from agentping.services.runtime import ServiceContext

STALE_AFTER = timedelta(days=3)


@dataclass
class TargetResolution:
    accepted: list[Device] = field(default_factory=list)
    rejected: list[tuple[Device, str]] = field(default_factory=list)
    notices: list[Notice] = field(default_factory=list)

    @property
    def accepted_ids(self) -> list[uuid.UUID]:
        return [device.id for device in self.accepted]


async def active_devices(ctx: ServiceContext) -> list[Device]:
    return list(
        (
            await ctx.session.execute(
                select(Device)
                .where(Device.user_id == ctx.user_id, Device.status == DeviceStatus.ACTIVE.value)
                .order_by(Device.created_at, Device.id)
            )
        ).scalars()
    )


async def explicit_targets(
    ctx: ServiceContext, reminder_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, list[uuid.UUID]]:
    if not reminder_ids:
        return {}
    rows = await ctx.session.execute(
        select(ReminderDeviceTarget.reminder_id, ReminderDeviceTarget.device_id).where(
            ReminderDeviceTarget.reminder_id.in_(reminder_ids)
        )
    )
    result: dict[uuid.UUID, list[uuid.UUID]] = {}
    for reminder_id, device_id in rows:
        result.setdefault(reminder_id, []).append(device_id)
    return result


async def validate_explicit_devices(ctx: ServiceContext, device_ids: Sequence[uuid.UUID]) -> None:
    if not device_ids:
        return
    found = set(
        (
            await ctx.session.execute(
                select(Device.id).where(
                    Device.id.in_(device_ids),
                    Device.user_id == ctx.user_id,
                    Device.status == DeviceStatus.ACTIVE.value,
                )
            )
        ).scalars()
    )
    missing = [str(device_id) for device_id in device_ids if device_id not in found]
    if missing:
        raise DeviceNotAccessible(
            "Device(s) not found, deactivated or not owned by this user: " + ", ".join(missing),
            details={"device_ids": missing},
        )


def _fallback_primary(devices: list[Device]) -> Device | None:
    if not devices:
        return None
    return sorted(
        devices,
        key=lambda d: (
            d.last_seen_at is None,
            -(d.last_seen_at.timestamp() if d.last_seen_at else 0),
        ),
    )[0]


def resolve_targets(
    mode: DeliveryMode,
    strategy: TargetStrategy,
    explicit_ids: Sequence[uuid.UUID],
    devices: list[Device],
) -> TargetResolution:
    resolution = TargetResolution()
    if strategy is TargetStrategy.PRIMARY:
        primary = next((d for d in devices if d.is_primary), None)
        if primary is None:
            primary = _fallback_primary(devices)
            if primary is not None:
                resolution.notices.append(
                    Notice(
                        code="no_primary_device",
                        message=f"No primary device is set; using '{primary.name}'",
                        device_id=primary.id,
                    )
                )
        candidates = [primary] if primary else []
    elif strategy is TargetStrategy.ALL:
        candidates = list(devices)
    else:
        wanted = set(explicit_ids)
        candidates = [d for d in devices if d.id in wanted]
    for device in candidates:
        if not (device.mode_settings or {}).get(mode.value, True):
            resolution.rejected.append((device, f"{mode.value} alerts are disabled on this device"))
        else:
            resolution.accepted.append(device)
    return resolution


def permission_notices(mode: DeliveryMode, devices: Sequence[Device]) -> list[Notice]:
    notices: list[Notice] = []
    for device in devices:
        caps = device.capabilities or {}

        def add(code: str, message: str, device: Device = device) -> None:
            notices.append(Notice(code=code, message=message, device_id=device.id))

        if device.push_token_status != PushTokenStatus.VALID.value:
            add(
                "push_unavailable",
                f"'{device.name}' has no valid push token; it relies on its next sync",
            )
        if caps.get("notifications_permitted") is False:
            add("notifications_permission", f"'{device.name}' has notifications disabled")
        channel = (caps.get("channels") or {}).get(mode.value) or {}
        if channel.get("enabled") is False:
            add("channel_disabled", f"'{device.name}' has the {mode.value} channel turned off")
        if mode is not DeliveryMode.NORMAL and caps.get("exact_alarms_permitted") is False:
            add(
                "exact_alarm_permission",
                f"'{device.name}' cannot schedule exact alarms; delivery may be delayed",
            )
        if mode.is_ringing and caps.get("full_screen_intent_permitted") is False:
            add(
                "full_screen_intent_permission",
                f"'{device.name}' cannot show full-screen alarms; a heads-up alert is used",
            )
        if mode is DeliveryMode.CRITICAL and not caps.get("dnd_access_granted"):
            add(
                "dnd_not_bypassed",
                f"'{device.name}' has not granted Do Not Disturb access; critical alerts "
                "will not override Do Not Disturb",
            )
        if caps.get("background_restricted"):
            add("background_restricted", f"'{device.name}' restricts background activity")
    return notices


def device_health(device: Device, now: datetime) -> DeviceHealth:
    if device.status != DeviceStatus.ACTIVE.value:
        return DeviceHealth(state="deactivated", issues=[])
    caps = device.capabilities or {}
    issues: list[str] = []
    if device.push_token_status == PushTokenStatus.INVALID.value:
        issues.append("push_token_invalid")
    elif device.push_token_status == PushTokenStatus.MISSING.value:
        issues.append("push_token_missing")
    checks = {
        "notifications_permitted": "notifications_not_permitted",
        "exact_alarms_permitted": "exact_alarms_not_permitted",
        "full_screen_intent_permitted": "full_screen_intent_not_permitted",
        "ignoring_battery_optimizations": "battery_optimizations_active",
    }
    for key, issue in checks.items():
        if caps.get(key) is False:
            issues.append(issue)
    if caps.get("background_restricted"):
        issues.append("background_restricted")
    for mode, channel in (caps.get("channels") or {}).items():
        if isinstance(channel, dict) and channel.get("enabled") is False:
            issues.append(f"channel_{mode}_disabled")
    stale = device.last_seen_at is None or now - device.last_seen_at > STALE_AFTER
    if stale:
        issues.append("not_seen_recently")
    if stale and device.push_token_status != PushTokenStatus.VALID.value:
        state = "unreachable"
    elif stale:
        state = "stale"
    elif issues:
        state = "degraded"
    else:
        state = "healthy"
    return DeviceHealth(state=state, issues=issues)  # type: ignore[arg-type]


def reminder_resolution(
    reminder: Reminder, explicit: Sequence[uuid.UUID], devices: list[Device]
) -> TargetResolution:
    return resolve_targets(
        DeliveryMode(reminder.mode), TargetStrategy(reminder.target_strategy), explicit, devices
    )
