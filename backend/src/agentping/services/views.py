"""Convert ORM rows to API/MCP views."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import datetime

from sqlalchemy import select

from agentping.db.models import AgentClient, Device, Reminder, ReminderOccurrence, User
from agentping.domain.enums import (
    ActorKind,
    DeliveryMode,
    ReminderStatus,
    TargetStrategy,
    TimezoneMode,
)
from agentping.domain.schemas import (
    AgentRef,
    DeviceView,
    EscalationPolicy,
    ModeSettings,
    NotificationButton,
    OccurrenceView,
    ReminderView,
    SettingsView,
    SnoozePolicy,
    TargetView,
    UserPreferences,
)
from agentping.services.runtime import ServiceContext
from agentping.services.targeting import (
    active_devices,
    device_health,
    explicit_targets,
    reminder_resolution,
)


def occurrence_view(occurrence: ReminderOccurrence) -> OccurrenceView:
    return OccurrenceView.model_validate(occurrence)


def device_view(device: Device, now: datetime) -> DeviceView:
    return DeviceView(
        id=device.id,
        name=device.name,
        installation_id=device.installation_id,
        platform=device.platform,
        model=device.model,
        os_version=device.os_version,
        app_version=device.app_version,
        timezone=device.timezone,
        is_primary=device.is_primary,
        status=device.status,
        push_token_status=device.push_token_status,
        push_token_kind=device.push_token_kind,
        capabilities=device.capabilities or {},
        mode_settings=ModeSettings.model_validate(device.mode_settings or {}),
        last_sync_at=device.last_sync_at,
        last_seen_at=device.last_seen_at,
        created_at=device.created_at,
        revision=device.revision,
        health=device_health(device, now),
    )


def preferences_of(user: User) -> UserPreferences:
    return UserPreferences.model_validate(user.preferences or {})


def settings_view(user: User) -> SettingsView:
    return SettingsView(
        timezone=user.timezone,
        timezone_source=user.timezone_source,  # type: ignore[arg-type]
        preferences=preferences_of(user),
        revision=user.settings_revision,
    )


async def reminder_views(
    ctx: ServiceContext,
    reminders: Sequence[Reminder],
    devices: list[Device] | None = None,
) -> list[ReminderView]:
    if not reminders:
        return []
    devices = devices if devices is not None else await active_devices(ctx)
    targets = await explicit_targets(ctx, [r.id for r in reminders])
    agent_ids = {r.source_agent_client_id for r in reminders if r.source_agent_client_id}
    agents: dict[uuid.UUID, str] = {}
    if agent_ids:
        rows = await ctx.session.execute(
            select(AgentClient.id, AgentClient.name).where(AgentClient.id.in_(agent_ids))
        )
        agents = {agent_id: name for agent_id, name in rows}
    views = []
    for reminder in reminders:
        explicit = targets.get(reminder.id, [])
        resolved = reminder_resolution(reminder, explicit, devices).accepted_ids
        views.append(build_reminder_view(reminder, explicit, resolved, agents))
    return views


def build_reminder_view(
    reminder: Reminder,
    explicit: list[uuid.UUID],
    resolved: list[uuid.UUID],
    agents: dict[uuid.UUID, str],
) -> ReminderView:
    agent = None
    if reminder.source_agent_client_id is not None:
        agent = AgentRef(
            id=reminder.source_agent_client_id,
            name=agents.get(reminder.source_agent_client_id, "unknown"),
        )
    return ReminderView(
        id=reminder.id,
        title=reminder.title,
        body=reminder.body,
        delivery_mode=DeliveryMode(reminder.mode),
        status=ReminderStatus(reminder.status),
        revision=reminder.revision,
        schedule=reminder.schedule,
        timezone=reminder.timezone,
        timezone_mode=TimezoneMode(reminder.timezone_mode),
        targets=TargetView(
            strategy=TargetStrategy(reminder.target_strategy),
            device_ids=explicit,
            resolved_device_ids=resolved,
        ),
        requires_ack=reminder.requires_ack,
        snooze=SnoozePolicy.model_validate(reminder.snooze_policy or {}),
        escalation=EscalationPolicy.model_validate(
            reminder.escalation_policy or {"enabled": False}
        ),
        expire_after_minutes=reminder.expire_after_seconds // 60,
        late_grace_minutes=reminder.late_grace_seconds // 60,
        respect_quiet_hours=reminder.respect_quiet_hours,
        actions=[NotificationButton.model_validate(a) for a in reminder.actions or []],
        source_agent=agent,
        created_by=ActorKind(reminder.created_by_kind),
        next_occurrence_at=reminder.next_occurrence_at,
        created_at=reminder.created_at,
        updated_at=reminder.updated_at,
        paused_at=reminder.paused_at,
        cancelled_at=reminder.cancelled_at,
        completed_at=reminder.completed_at,
    )
