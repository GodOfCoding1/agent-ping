"""User settings. Only the user (never an agent) can change safety preferences."""

from __future__ import annotations

from sqlalchemy import select

from agentping.auth.principal import Scope
from agentping.db.models import Reminder, User
from agentping.domain.enums import ReminderStatus, SyncEntity, TimezoneMode
from agentping.domain.errors import Forbidden, RevisionConflict
from agentping.domain.scheduling import load_timezone
from agentping.domain.schemas import SettingsUpdate, SettingsView
from agentping.services.core import audit, record_changes
from agentping.services.occurrences import materialize
from agentping.services.runtime import ServiceContext
from agentping.services.views import preferences_of, settings_view


async def get_settings(ctx: ServiceContext) -> SettingsView:
    ctx.principal.require(Scope.REMINDERS_READ)
    return settings_view(await ctx.user())


async def apply_timezone_change(ctx: ServiceContext, user: User, timezone: str) -> int:
    """Switch the user's home timezone and re-anchor floating reminders to it."""
    load_timezone(timezone)
    previous = user.timezone
    user.timezone = timezone
    user.settings_revision += 1
    await record_changes(ctx, [(SyncEntity.SETTINGS, user.id, user.settings_revision)])
    floating = list(
        (
            await ctx.session.execute(
                select(Reminder).where(
                    Reminder.user_id == user.id,
                    Reminder.timezone_mode == TimezoneMode.FLOATING.value,
                    Reminder.status.in_([ReminderStatus.ACTIVE.value, ReminderStatus.PAUSED.value]),
                )
            )
        ).scalars()
    )
    for reminder in floating:
        reminder.timezone = timezone
        reminder.revision += 1
        await record_changes(ctx, [(SyncEntity.REMINDER, reminder.id, reminder.revision)])
        await materialize(ctx, reminder, user)
    await audit(
        ctx,
        "settings.timezone_changed",
        f"Home timezone changed from {previous} to {timezone}; "
        f"{len(floating)} floating reminder(s) re-anchored",
        category="settings",
        details={"from": previous, "to": timezone, "floating_reminders": len(floating)},
    )
    return len(floating)


async def update_settings(ctx: ServiceContext, data: SettingsUpdate) -> SettingsView:
    if ctx.principal.is_agent:
        raise Forbidden("Agents cannot change user settings or safety preferences")
    ctx.principal.require(Scope.SETTINGS_WRITE)
    user = await ctx.lock_user()
    if data.expected_revision is not None and data.expected_revision != user.settings_revision:
        raise RevisionConflict(
            "Settings changed on another device; reload and retry",
            details={"current_revision": user.settings_revision},
        )
    changed: list[str] = []
    if data.preferences is not None:
        before = preferences_of(user)
        if data.preferences != before:
            user.preferences = data.preferences.model_dump(mode="json")
            changed.append("preferences")
            if before.critical_alerts_enabled != data.preferences.critical_alerts_enabled:
                state = "enabled" if data.preferences.critical_alerts_enabled else "disabled"
                await audit(
                    ctx,
                    "settings.critical_alerts_changed",
                    f"Critical alerts {state} by the user",
                    category="security",
                )
    if data.timezone_source is not None and data.timezone_source != user.timezone_source:
        user.timezone_source = data.timezone_source
        changed.append("timezone_source")
    if changed:
        user.settings_revision += 1
        await record_changes(ctx, [(SyncEntity.SETTINGS, user.id, user.settings_revision)])
        await audit(
            ctx,
            "settings.updated",
            f"Settings updated ({', '.join(changed)})",
            category="settings",
            details={"fields": changed},
        )
    if data.timezone is not None and data.timezone != user.timezone:
        await apply_timezone_change(ctx, user, data.timezone)
    return settings_view(user)
