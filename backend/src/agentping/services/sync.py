"""Incremental and full schedule synchronization for devices, plus offline action replay."""

from __future__ import annotations

import uuid
from datetime import timedelta

from sqlalchemy import func, or_, select

from agentping.auth.principal import Scope
from agentping.db.models import Device, Reminder, ReminderOccurrence, SyncChange, User
from agentping.domain.enums import DeliveryMode, OccurrenceState, ReminderStatus, SyncEntity
from agentping.domain.errors import ValidationFailed
from agentping.domain.schemas import (
    DeviceActionBatch,
    DeviceActionResult,
    ReminderView,
    SyncOccurrence,
    SyncResponse,
)
from agentping.services.occurrences import apply_action
from agentping.services.runtime import ServiceContext
from agentping.services.targeting import active_devices
from agentping.services.views import (
    device_view,
    occurrence_view,
    reminder_views,
    settings_view,
)

FULL_SYNC_LOOKBACK = timedelta(hours=24)
MAX_CHANGES = 1000


def _sync_occurrences(
    occurrences: list[ReminderOccurrence],
    views: dict[uuid.UUID, ReminderView],
    device_id: uuid.UUID | None,
) -> list[SyncOccurrence]:
    result = []
    for occurrence in occurrences:
        view = views.get(occurrence.reminder_id)
        if view is None:
            continue
        result.append(
            SyncOccurrence(
                **occurrence_view(occurrence).model_dump(),
                deliver_on_this_device=device_id is not None
                and device_id in view.targets.resolved_device_ids,
                mode=DeliveryMode(view.delivery_mode),
            )
        )
    return result


async def _mark_synced(ctx: ServiceContext, cursor: int) -> None:
    if ctx.principal.device_id is None:
        return
    device = await ctx.session.get(Device, ctx.principal.device_id)
    if device is not None:
        device.last_sync_at = ctx.now
        device.last_seen_at = ctx.now
        device.last_sync_cursor = cursor


async def full_sync(ctx: ServiceContext, user: User) -> SyncResponse:
    cursor = user.sync_seq
    horizon_end = ctx.now + timedelta(days=ctx.settings.occurrence_horizon_days)
    since = ctx.now - FULL_SYNC_LOOKBACK
    occurrences = list(
        (
            await ctx.session.execute(
                select(ReminderOccurrence)
                .where(
                    ReminderOccurrence.user_id == user.id,
                    ReminderOccurrence.scheduled_at <= horizon_end,
                    or_(
                        ReminderOccurrence.state.in_(
                            [
                                OccurrenceState.SCHEDULED.value,
                                OccurrenceState.FIRED.value,
                                OccurrenceState.SNOOZED.value,
                            ]
                        ),
                        ReminderOccurrence.updated_at >= since,
                    ),
                )
                .order_by(ReminderOccurrence.scheduled_at)
            )
        ).scalars()
    )
    referenced = {o.reminder_id for o in occurrences}
    reminders = list(
        (
            await ctx.session.execute(
                select(Reminder).where(
                    Reminder.user_id == user.id,
                    or_(
                        Reminder.status.in_(
                            [ReminderStatus.ACTIVE.value, ReminderStatus.PAUSED.value]
                        ),
                        Reminder.id.in_(referenced) if referenced else Reminder.id.is_(None),
                        Reminder.updated_at >= since,
                    ),
                )
            )
        ).scalars()
    )
    devices = await active_devices(ctx)
    views = {view.id: view for view in await reminder_views(ctx, reminders, devices)}
    await _mark_synced(ctx, cursor)
    return SyncResponse(
        cursor=cursor,
        full=True,
        has_more=False,
        server_time=ctx.now,
        horizon_end=horizon_end,
        reminders=list(views.values()),
        occurrences=_sync_occurrences(occurrences, views, ctx.principal.device_id),
        devices=[device_view(d, ctx.now) for d in devices],
        settings=settings_view(user),
    )


async def sync(ctx: ServiceContext, cursor: int | None, *, limit: int = 500) -> SyncResponse:
    """Return changes after ``cursor``; fall back to a full snapshot when required.

    The returned ``cursor`` must be stored only after the client has durably applied the
    response. Entities are returned at their *current* version, so applying them is
    idempotent and replays are safe.
    """
    ctx.principal.require(Scope.REMINDERS_READ)
    if cursor is not None and cursor < 0:
        raise ValidationFailed("cursor must be >= 0")
    limit = max(1, min(limit, MAX_CHANGES))
    user = await ctx.user()
    if cursor is None or cursor > user.sync_seq:
        return await full_sync(ctx, user)
    oldest = (
        await ctx.session.execute(
            select(func.min(SyncChange.seq)).where(SyncChange.user_id == user.id)
        )
    ).scalar_one_or_none()
    if cursor < user.sync_seq and (oldest is None or oldest > cursor + 1):
        return await full_sync(ctx, user)

    rows = list(
        (
            await ctx.session.execute(
                select(SyncChange)
                .where(SyncChange.user_id == user.id, SyncChange.seq > cursor)
                .order_by(SyncChange.seq)
                .limit(limit + 1)
            )
        ).scalars()
    )
    has_more = len(rows) > limit
    rows = rows[:limit]
    new_cursor = rows[-1].seq if rows else cursor
    ids: dict[str, set[uuid.UUID]] = {entity.value: set() for entity in SyncEntity}
    for row in rows:
        ids[row.entity_type].add(row.entity_id)

    # Device preferences/primary selection change resolved targets even when the
    # reminder revision is unchanged. Return a fresh replica so old local alarms
    # are removed on the previous primary and installed on the new one.
    if ids[SyncEntity.DEVICE.value] or ids[SyncEntity.SETTINGS.value]:
        return await full_sync(ctx, user)

    occurrences = []
    if ids[SyncEntity.OCCURRENCE.value]:
        occurrences = list(
            (
                await ctx.session.execute(
                    select(ReminderOccurrence).where(
                        ReminderOccurrence.id.in_(ids[SyncEntity.OCCURRENCE.value])
                    )
                )
            ).scalars()
        )
    reminder_ids = ids[SyncEntity.REMINDER.value] | {o.reminder_id for o in occurrences}
    reminders = []
    if reminder_ids:
        reminders = list(
            (
                await ctx.session.execute(
                    select(Reminder).where(
                        Reminder.id.in_(reminder_ids), Reminder.user_id == user.id
                    )
                )
            ).scalars()
        )
    devices = await active_devices(ctx)
    views = {view.id: view for view in await reminder_views(ctx, reminders, devices)}
    changed_devices = []
    if ids[SyncEntity.DEVICE.value]:
        changed_devices = list(
            (
                await ctx.session.execute(
                    select(Device).where(
                        Device.id.in_(ids[SyncEntity.DEVICE.value]), Device.user_id == user.id
                    )
                )
            ).scalars()
        )
    await _mark_synced(ctx, new_cursor)
    return SyncResponse(
        cursor=new_cursor,
        full=False,
        has_more=has_more,
        server_time=ctx.now,
        horizon_end=ctx.now + timedelta(days=ctx.settings.occurrence_horizon_days),
        reminders=[views[r] for r in ids[SyncEntity.REMINDER.value] if r in views]
        + [v for k, v in views.items() if k not in ids[SyncEntity.REMINDER.value]],
        occurrences=_sync_occurrences(occurrences, views, ctx.principal.device_id),
        devices=[device_view(d, ctx.now) for d in changed_devices],
        settings=settings_view(user) if ids[SyncEntity.SETTINGS.value] else None,
    )


async def apply_device_actions(
    ctx: ServiceContext, batch: DeviceActionBatch
) -> list[DeviceActionResult]:
    """Replay a device's offline action queue in the order the user performed them."""
    ctx.principal.require(Scope.DEVICE)
    await ctx.lock_user()
    results = []
    for action in sorted(batch.actions, key=lambda a: a.occurred_at):
        try:
            outcome = await apply_action(
                ctx,
                action.occurrence_id,
                action.action,
                source=action.source,
                client_action_id=action.client_action_id,
                occurred_at=action.occurred_at,
                snooze_minutes=action.snooze_minutes,
                reason=action.reason or ("Skipped on device" if action.action == "skip" else None),
            )
            results.append(
                DeviceActionResult(
                    client_action_id=action.client_action_id,
                    result=outcome.result,
                    reason=outcome.reason,
                    occurrence=occurrence_view(outcome.occurrence),
                )
            )
        except Exception as error:
            from agentping.domain.enums import ActionResult
            from agentping.domain.errors import DomainError

            if not isinstance(error, DomainError):
                raise
            results.append(
                DeviceActionResult(
                    client_action_id=action.client_action_id,
                    result=ActionResult.REJECTED_INVALID,
                    reason=error.message,
                )
            )
    return results
