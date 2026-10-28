"""Reminder and immediate-notification use cases (shared by REST and MCP)."""

from __future__ import annotations

import base64
import json
import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, delete, exists, func, or_, select

from agentping.auth.principal import Scope
from agentping.db.models import (
    Device,
    DeviceDelivery,
    Reminder,
    ReminderDeviceTarget,
    ReminderOccurrence,
    User,
)
from agentping.domain.enums import (
    ActorKind,
    DeliveryMode,
    OccurrenceState,
    QuietHoursBehavior,
    ReminderStatus,
    ScheduleKind,
    SyncEntity,
    TargetStrategy,
    TimezoneMode,
)
from agentping.domain.errors import (
    Forbidden,
    InvalidTransition,
    LimitExceeded,
    NotFound,
    RevisionConflict,
    ValidationFailed,
)
from agentping.domain.quiet_hours import quiet_hours_end
from agentping.domain.scheduling import load_timezone
from agentping.domain.schemas import (
    CancelReminderResult,
    CreateReminderResult,
    DeliveryView,
    EscalationPolicy,
    Notice,
    OccurrenceDetail,
    Page,
    RejectedTarget,
    ReminderCreate,
    ReminderDetail,
    ReminderOptions,
    ReminderUpdate,
    ReminderView,
    SendNotification,
    SendNotificationResult,
    SnoozePolicy,
    TargetResult,
    TargetSelection,
    UpdateReminderResult,
)
from agentping.services import occurrences as occ
from agentping.services.core import (
    audit,
    idempotent_lookup,
    idempotent_store,
    record_changes,
    request_fingerprint,
)
from agentping.services.runtime import ServiceContext
from agentping.services.targeting import (
    TargetResolution,
    active_devices,
    permission_notices,
    resolve_targets,
    validate_explicit_devices,
)
from agentping.services.views import occurrence_view, preferences_of, reminder_views

S = OccurrenceState


class CriticalAlertsDisabled(Forbidden):
    code = "critical_alerts_disabled"


def _agent_limit(ctx: ServiceContext, name: str, default: int) -> int:
    value = ctx.principal.limits.get(name)
    return int(value) if value is not None else default


async def _check_agent_limits(
    ctx: ServiceContext, mode: DeliveryMode, kind: ScheduleKind, *, exclude: uuid.UUID | None = None
) -> None:
    principal = ctx.principal
    if not principal.is_agent:
        return
    settings = ctx.settings
    owned = and_(
        Reminder.user_id == ctx.user_id,
        Reminder.source_agent_client_id == principal.agent_client_id,
    )
    if exclude is not None:
        owned = and_(owned, Reminder.id != exclude)
    if kind is ScheduleKind.IMMEDIATE:
        limit = _agent_limit(ctx, "max_sends_per_hour", settings.agent_max_sends_per_hour)
        recent = (
            await ctx.session.execute(
                select(func.count()).where(
                    owned,
                    Reminder.schedule_kind == ScheduleKind.IMMEDIATE.value,
                    Reminder.created_at >= ctx.now - timedelta(hours=1),
                )
            )
        ).scalar_one()
        if recent >= limit:
            raise LimitExceeded(
                f"This agent may send at most {limit} notifications per hour",
                details={"limit": limit, "window": "1h"},
            )
        return
    live = [ReminderStatus.ACTIVE.value, ReminderStatus.PAUSED.value]
    limit = _agent_limit(ctx, "max_active_reminders", settings.agent_max_active_reminders)
    active = (
        await ctx.session.execute(
            select(func.count()).where(
                owned,
                Reminder.status.in_(live),
                Reminder.schedule_kind != ScheduleKind.IMMEDIATE.value,
            )
        )
    ).scalar_one()
    if active >= limit:
        raise LimitExceeded(
            f"This agent may own at most {limit} active reminders", details={"limit": limit}
        )
    if mode.is_ringing:
        limit = _agent_limit(ctx, "max_ringing_reminders", settings.agent_max_ringing_reminders)
        ringing = (
            await ctx.session.execute(
                select(func.count()).where(
                    owned,
                    Reminder.status.in_(live),
                    Reminder.mode.in_([DeliveryMode.ALARM.value, DeliveryMode.CRITICAL.value]),
                )
            )
        ).scalar_one()
        if ringing >= limit:
            raise LimitExceeded(
                f"This agent may own at most {limit} active alarm/critical reminders",
                details={"limit": limit},
            )


def _require_mode(ctx: ServiceContext, mode: DeliveryMode, user: User) -> None:
    if mode is DeliveryMode.CRITICAL:
        ctx.principal.require(Scope.ALERTS_CRITICAL)
        if not preferences_of(user).critical_alerts_enabled:
            raise CriticalAlertsDisabled(
                "Critical alerts are disabled. Only the user can enable them in AgentPing "
                "settings; use delivery_mode 'alarm' or 'persistent' instead"
            )


def _apply_options(
    reminder: Reminder,
    options: ReminderOptions | ReminderUpdate,
    mode: DeliveryMode,
    user: User,
    *,
    creating: bool,
) -> None:
    defaults = occ.MODE_DEFAULTS[mode]

    def pick[T](value: T | None, current: T, default: T) -> T:
        if value is not None:
            return value
        return default if creating else current

    reminder.requires_ack = pick(options.requires_ack, reminder.requires_ack, defaults.requires_ack)
    reminder.expire_after_seconds = (
        options.expire_after_minutes * 60
        if options.expire_after_minutes is not None
        else (
            int(defaults.expire_after.total_seconds())
            if creating
            else reminder.expire_after_seconds
        )
    )
    reminder.late_grace_seconds = (
        options.late_grace_minutes * 60
        if options.late_grace_minutes is not None
        else (int(defaults.late_grace.total_seconds()) if creating else reminder.late_grace_seconds)
    )
    reminder.respect_quiet_hours = pick(
        options.respect_quiet_hours, reminder.respect_quiet_hours, defaults.respect_quiet_hours
    )
    if options.snooze is not None:
        reminder.snooze_policy = options.snooze.model_dump()
    elif creating:
        default_minutes = preferences_of(user).default_snooze_minutes
        reminder.snooze_policy = SnoozePolicy(default_minutes=default_minutes).model_dump()
    if options.escalation is not None:
        reminder.escalation_policy = options.escalation.model_dump()
    elif creating:
        reminder.escalation_policy = defaults.escalation.model_dump()
    if not reminder.requires_ack:
        reminder.escalation_policy = EscalationPolicy(enabled=False).model_dump()
    if options.actions is not None:
        reminder.actions = [action.model_dump(exclude_none=True) for action in options.actions]


async def _set_targets(ctx: ServiceContext, reminder: Reminder, targets: TargetSelection) -> None:
    await validate_explicit_devices(ctx, targets.device_ids)
    reminder.target_strategy = targets.strategy.value
    await ctx.session.execute(
        delete(ReminderDeviceTarget).where(ReminderDeviceTarget.reminder_id == reminder.id)
    )
    ctx.session.add_all(
        ReminderDeviceTarget(reminder_id=reminder.id, device_id=device_id)
        for device_id in dict.fromkeys(targets.device_ids)
    )


async def _resolution(ctx: ServiceContext, reminder: Reminder) -> TargetResolution:
    devices = await active_devices(ctx)
    explicit = list(
        (
            await ctx.session.execute(
                select(ReminderDeviceTarget.device_id).where(
                    ReminderDeviceTarget.reminder_id == reminder.id
                )
            )
        ).scalars()
    )
    return resolve_targets(
        DeliveryMode(reminder.mode), TargetStrategy(reminder.target_strategy), explicit, devices
    )


async def _next_occurrence(ctx: ServiceContext, reminder: Reminder) -> ReminderOccurrence | None:
    return (
        await ctx.session.execute(
            select(ReminderOccurrence)
            .where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.state.in_(occ.PENDING),
            )
            .order_by(ReminderOccurrence.scheduled_at)
            .limit(1)
        )
    ).scalar_one_or_none()


def _new_reminder(ctx: ServiceContext, title: str, body: str, mode: DeliveryMode) -> Reminder:
    principal = ctx.principal
    created_by = (
        ActorKind.USER if principal.actor_kind is ActorKind.DEVICE else principal.actor_kind
    )
    return Reminder(
        id=uuid.uuid4(),
        user_id=ctx.user_id,
        title=title,
        body=body,
        mode=mode.value,
        status=ReminderStatus.ACTIVE.value,
        revision=1,
        source_agent_client_id=principal.agent_client_id,
        created_by_kind=created_by.value,
        created_by_device_id=principal.device_id,
        snooze_policy={},
        escalation_policy={},
        actions=[],
        monthly_day_policy="skip",
    )


async def _reminder_view(ctx: ServiceContext, reminder: Reminder) -> ReminderView:
    return (await reminder_views(ctx, [reminder]))[0]


async def create_reminder(ctx: ServiceContext, data: ReminderCreate) -> CreateReminderResult:
    ctx.principal.require(Scope.REMINDERS_WRITE)
    user = await ctx.lock_user()
    fingerprint = request_fingerprint(data)
    cached = await idempotent_lookup(ctx, "create_reminder", data.idempotency_key, fingerprint)
    if cached is not None:
        return CreateReminderResult.model_validate({**cached, "idempotent_replay": True})
    mode = data.delivery_mode
    _require_mode(ctx, mode, user)
    compiled = occ.compile_schedule(data.schedule, user, ctx)
    await _check_agent_limits(ctx, mode, compiled.kind)

    reminder = _new_reminder(ctx, data.title, data.body, mode)
    occ.apply_compiled(reminder, compiled)
    _apply_options(reminder, data, mode, user, creating=True)
    targets = data.targets or TargetSelection(strategy=occ.MODE_DEFAULTS[mode].strategy)
    reminder.target_strategy = targets.strategy.value
    ctx.session.add(reminder)
    await ctx.session.flush()
    await _set_targets(ctx, reminder, targets)
    await ctx.session.flush()
    await record_changes(ctx, [(SyncEntity.REMINDER, reminder.id, reminder.revision)])
    await occ.materialize(ctx, reminder, user)

    resolution = await _resolution(ctx, reminder)
    notices = list(compiled.notices) + resolution.notices
    notices += [
        Notice(code="target_rejected", message=reason, device_id=device.id)
        for device, reason in resolution.rejected
    ]
    if not resolution.accepted:
        notices.append(
            Notice(
                code="no_target_devices",
                message="No registered device currently accepts this reminder; it will be "
                "delivered to devices that are added or enabled later",
            )
        )
    await audit(
        ctx,
        "reminder.created",
        f"Reminder '{reminder.title}' ({mode.value}, {compiled.kind.value}) created by "
        f"{ctx.principal.describe()}",
        category="reminder",
        reminder_id=reminder.id,
        details={"mode": mode.value, "schedule_kind": compiled.kind.value, "revision": 1},
    )
    next_occurrence = await _next_occurrence(ctx, reminder)
    result = CreateReminderResult(
        reminder_id=reminder.id,
        revision=reminder.revision,
        status=ReminderStatus(reminder.status),
        next_occurrence=occurrence_view(next_occurrence) if next_occurrence else None,
        sync_status="sync_queued" if resolution.accepted else "no_target_devices",
        target_device_ids=resolution.accepted_ids,
        warnings=notices,
        permissions_required=permission_notices(mode, resolution.accepted),
    )
    await idempotent_store(ctx, "create_reminder", data.idempotency_key, fingerprint, result)
    return result


async def _load_reminder(
    ctx: ServiceContext, reminder_id: uuid.UUID, *, for_update: bool = False
) -> Reminder:
    query = select(Reminder).where(Reminder.id == reminder_id, Reminder.user_id == ctx.user_id)
    if for_update:
        query = query.with_for_update()
    reminder = (await ctx.session.execute(query)).scalar_one_or_none()
    if reminder is None:
        raise NotFound("Reminder not found", details={"reminder_id": str(reminder_id)})
    return reminder


async def update_reminder(
    ctx: ServiceContext, reminder_id: uuid.UUID, data: ReminderUpdate
) -> UpdateReminderResult:
    ctx.principal.require(Scope.REMINDERS_WRITE)
    user = await ctx.lock_user()
    reminder = await _load_reminder(ctx, reminder_id, for_update=True)
    occ.require_agent_ownership(ctx, reminder)
    if reminder.revision != data.expected_revision:
        raise RevisionConflict(
            f"Reminder was modified (current revision {reminder.revision}); re-read it and retry",
            details={"current_revision": reminder.revision, "expected": data.expected_revision},
        )
    if reminder.status in (ReminderStatus.CANCELLED.value, ReminderStatus.COMPLETED.value):
        raise InvalidTransition(f"A {reminder.status} reminder cannot be modified")
    fields = data.model_dump(exclude_unset=True, exclude={"expected_revision"})
    if not fields:
        raise ValidationFailed("No fields to update")
    if reminder.schedule_kind == ScheduleKind.IMMEDIATE.value and (
        data.schedule is not None or data.status is not None
    ):
        raise InvalidTransition("Immediate notifications cannot be rescheduled or paused")

    mode = data.delivery_mode or DeliveryMode(reminder.mode)
    notices: list[Notice] = []
    if data.delivery_mode is not None and data.delivery_mode.value != reminder.mode:
        _require_mode(ctx, mode, user)
        await _check_agent_limits(
            ctx, mode, ScheduleKind(reminder.schedule_kind), exclude=reminder.id
        )
        reminder.mode = mode.value
    if data.title is not None:
        reminder.title = data.title
    if data.body is not None:
        reminder.body = data.body
    if data.schedule is not None:
        compiled = occ.compile_schedule(data.schedule, user, ctx)
        occ.apply_compiled(reminder, compiled)
        notices += compiled.notices
    if data.targets is not None:
        await _set_targets(ctx, reminder, data.targets)
    _apply_options(reminder, data, mode, user, creating=False)

    reminder.revision += 1
    if data.status == "paused" and reminder.status == ReminderStatus.ACTIVE.value:
        reminder.status = ReminderStatus.PAUSED.value
        reminder.paused_at = ctx.now
        await occ.supersede_pending(ctx, reminder, reason="paused")
    elif data.status == "active" and reminder.status == ReminderStatus.PAUSED.value:
        reminder.status = ReminderStatus.ACTIVE.value
        reminder.paused_at = None
    await ctx.session.flush()
    await record_changes(ctx, [(SyncEntity.REMINDER, reminder.id, reminder.revision)])
    await occ.materialize(ctx, reminder, user)
    await occ.refresh_reminder(ctx, reminder, user)
    await audit(
        ctx,
        "reminder.updated",
        f"Reminder '{reminder.title}' updated by {ctx.principal.describe()} "
        f"(fields: {', '.join(sorted(fields))})",
        category="reminder",
        reminder_id=reminder.id,
        details={"fields": sorted(fields), "revision": reminder.revision},
    )
    resolution = await _resolution(ctx, reminder)
    next_occurrence = await _next_occurrence(ctx, reminder)
    notices += permission_notices(mode, resolution.accepted)
    return UpdateReminderResult(
        reminder=await _reminder_view(ctx, reminder),
        next_occurrence=occurrence_view(next_occurrence) if next_occurrence else None,
        sync_status="sync_queued" if resolution.accepted else "no_target_devices",
        warnings=notices,
    )


async def cancel_reminder(
    ctx: ServiceContext,
    reminder_id: uuid.UUID,
    *,
    expected_revision: int | None = None,
    reason: str | None = None,
) -> CancelReminderResult:
    ctx.principal.require(Scope.REMINDERS_WRITE)
    user = await ctx.lock_user()
    reminder = await _load_reminder(ctx, reminder_id, for_update=True)
    occ.require_agent_ownership(ctx, reminder)
    if expected_revision is not None and reminder.revision != expected_revision:
        raise RevisionConflict(
            f"Reminder was modified (current revision {reminder.revision})",
            details={"current_revision": reminder.revision, "expected": expected_revision},
        )
    if reminder.status == ReminderStatus.CANCELLED.value:
        return CancelReminderResult(
            reminder_id=reminder.id,
            revision=reminder.revision,
            status=ReminderStatus.CANCELLED,
            cancelled_occurrences=0,
            sync_status="sync_queued",
        )
    if reminder.status == ReminderStatus.COMPLETED.value:
        raise InvalidTransition("Reminder already completed")
    reminder.status = ReminderStatus.CANCELLED.value
    reminder.cancelled_at = ctx.now
    reminder.revision += 1
    count = await occ.cancel_all(ctx, reminder, reason="reminder_cancelled")
    await record_changes(ctx, [(SyncEntity.REMINDER, reminder.id, reminder.revision)])
    await occ.refresh_reminder(ctx, reminder, user)
    await audit(
        ctx,
        "reminder.cancelled",
        f"Reminder '{reminder.title}' cancelled by {ctx.principal.describe()}",
        category="reminder",
        reminder_id=reminder.id,
        details={"cancelled_occurrences": count, "reason": reason or ""},
    )
    resolution = await _resolution(ctx, reminder)
    return CancelReminderResult(
        reminder_id=reminder.id,
        revision=reminder.revision,
        status=ReminderStatus.CANCELLED,
        cancelled_occurrences=count,
        sync_status="sync_queued" if resolution.accepted else "no_target_devices",
    )


def encode_cursor(*values: object) -> str:
    raw = json.dumps([str(v) for v in values]).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode_cursor(cursor: str, size: int) -> list[str]:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        values = json.loads(base64.urlsafe_b64decode(padded))
        if not isinstance(values, list) or len(values) != size:
            raise ValueError
        return [str(v) for v in values]
    except (ValueError, json.JSONDecodeError) as error:
        raise ValidationFailed("Invalid pagination cursor", details={"field": "cursor"}) from error


async def list_reminders(
    ctx: ServiceContext,
    *,
    statuses: list[ReminderStatus] | None = None,
    mode: DeliveryMode | None = None,
    device_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID | None = None,
    due_after: datetime | None = None,
    due_before: datetime | None = None,
    query: str | None = None,
    include_notifications: bool = False,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[ReminderView]:
    ctx.principal.require(Scope.REMINDERS_READ)
    limit = max(1, min(limit, 200))
    conditions = [Reminder.user_id == ctx.user_id]
    if statuses:
        conditions.append(Reminder.status.in_([s.value for s in statuses]))
    if mode:
        conditions.append(Reminder.mode == mode.value)
    if not include_notifications:
        conditions.append(Reminder.schedule_kind != ScheduleKind.IMMEDIATE.value)
    if source_agent_id:
        conditions.append(Reminder.source_agent_client_id == source_agent_id)
    if due_after:
        conditions.append(Reminder.next_occurrence_at >= due_after)
    if due_before:
        conditions.append(Reminder.next_occurrence_at < due_before)
    if query:
        pattern = "%" + query.replace("%", r"\%").replace("_", r"\_") + "%"
        conditions.append(or_(Reminder.title.ilike(pattern), Reminder.body.ilike(pattern)))
    if device_id:
        is_primary = exists().where(
            Device.id == device_id, Device.user_id == ctx.user_id, Device.is_primary.is_(True)
        )
        conditions.append(
            or_(
                Reminder.target_strategy == TargetStrategy.ALL.value,
                and_(Reminder.target_strategy == TargetStrategy.PRIMARY.value, is_primary),
                exists().where(
                    ReminderDeviceTarget.reminder_id == Reminder.id,
                    ReminderDeviceTarget.device_id == device_id,
                ),
            )
        )
    if cursor:
        created, last_id = decode_cursor(cursor, 2)
        created_at = datetime.fromisoformat(created)
        conditions.append(
            or_(
                Reminder.created_at < created_at,
                and_(Reminder.created_at == created_at, Reminder.id < uuid.UUID(last_id)),
            )
        )
    rows = list(
        (
            await ctx.session.execute(
                select(Reminder)
                .where(*conditions)
                .order_by(Reminder.created_at.desc(), Reminder.id.desc())
                .limit(limit + 1)
            )
        ).scalars()
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = encode_cursor(rows[-1].created_at.isoformat(), rows[-1].id)
    return Page[ReminderView](items=await reminder_views(ctx, rows), next_cursor=next_cursor)


async def get_reminder(
    ctx: ServiceContext, reminder_id: uuid.UUID, *, upcoming_limit: int = 10
) -> ReminderDetail:
    ctx.principal.require(Scope.REMINDERS_READ)
    reminder = await _load_reminder(ctx, reminder_id)
    upcoming = list(
        (
            await ctx.session.execute(
                select(ReminderOccurrence)
                .where(
                    ReminderOccurrence.reminder_id == reminder.id,
                    ReminderOccurrence.state.in_(occ.PENDING),
                )
                .order_by(ReminderOccurrence.scheduled_at)
                .limit(max(1, min(upcoming_limit, 50)))
            )
        ).scalars()
    )
    recent = list(
        (
            await ctx.session.execute(
                select(ReminderOccurrence)
                .where(
                    ReminderOccurrence.reminder_id == reminder.id,
                    ReminderOccurrence.state.not_in([S.SCHEDULED.value, S.SUPERSEDED.value]),
                )
                .order_by(ReminderOccurrence.scheduled_at.desc())
                .limit(10)
            )
        ).scalars()
    )
    deliveries: dict[uuid.UUID, list[DeliveryView]] = {}
    if recent:
        rows = await ctx.session.execute(
            select(DeviceDelivery).where(DeviceDelivery.occurrence_id.in_([o.id for o in recent]))
        )
        for delivery in rows.scalars():
            deliveries.setdefault(delivery.occurrence_id, []).append(
                DeliveryView.model_validate(delivery)
            )
    return ReminderDetail(
        reminder=await _reminder_view(ctx, reminder),
        upcoming=[occurrence_view(o) for o in upcoming],
        recent=[
            OccurrenceDetail(**occurrence_view(o).model_dump(), deliveries=deliveries.get(o.id, []))
            for o in recent
        ],
    )


async def send_notification(ctx: ServiceContext, data: SendNotification) -> SendNotificationResult:
    ctx.principal.require(Scope.NOTIFICATIONS_SEND)
    user = await ctx.lock_user()
    fingerprint = request_fingerprint(data)
    cached = await idempotent_lookup(ctx, "send_notification", data.idempotency_key, fingerprint)
    if cached is not None:
        return SendNotificationResult.model_validate({**cached, "idempotent_replay": True})
    mode = data.delivery_mode
    _require_mode(ctx, mode, user)
    await _check_agent_limits(ctx, mode, ScheduleKind.IMMEDIATE)
    targets = data.targets or TargetSelection(
        strategy=TargetStrategy.ALL if mode is DeliveryMode.NORMAL else TargetStrategy.PRIMARY
    )

    reminder = _new_reminder(ctx, data.title, data.body, mode)
    options = ReminderOptions(requires_ack=data.requires_ack, actions=data.actions)
    _apply_options(reminder, options, mode, user, creating=True)
    if data.ttl_seconds is not None:
        reminder.expire_after_seconds = data.ttl_seconds
        reminder.late_grace_seconds = data.ttl_seconds

    prefs = preferences_of(user)
    tz = load_timezone(user.timezone)
    deliver_at = ctx.now
    deferred = False
    quiet_end = quiet_hours_end(prefs.quiet_hours, tz, ctx.now)
    if (
        quiet_end is not None
        and reminder.respect_quiet_hours
        and mode is DeliveryMode.NORMAL
        and prefs.quiet_hours.behavior is QuietHoursBehavior.DEFER
    ):
        deliver_at = quiet_end
        deferred = True
    reminder.schedule_kind = ScheduleKind.IMMEDIATE.value
    reminder.schedule = {"kind": "immediate", "at": deliver_at.isoformat()}
    reminder.timezone = user.timezone
    reminder.timezone_mode = TimezoneMode.FIXED.value
    reminder.first_at = deliver_at
    reminder.target_strategy = targets.strategy.value
    ctx.session.add(reminder)
    await ctx.session.flush()
    await _set_targets(ctx, reminder, targets)
    await ctx.session.flush()
    await record_changes(ctx, [(SyncEntity.REMINDER, reminder.id, reminder.revision)])
    await occ.materialize(ctx, reminder, user)
    occurrence = await _next_occurrence(ctx, reminder)
    assert occurrence is not None

    resolution = await _resolution(ctx, reminder)
    for device in resolution.accepted:
        ctx.session.add(
            DeviceDelivery(
                occurrence_id=occurrence.id,
                device_id=device.id,
                user_id=ctx.user_id,
                state="pending",
                push_attempts=0,
            )
        )
    errors: list[str] = []
    if not resolution.accepted:
        errors.append("No registered device accepts this notification")
    await audit(
        ctx,
        "notification.sent",
        f"Notification '{reminder.title}' ({mode.value}) queued by {ctx.principal.describe()} "
        f"for {len(resolution.accepted)} device(s)",
        category="notification",
        reminder_id=reminder.id,
        occurrence_id=occurrence.id,
        details={
            "mode": mode.value,
            "targets": [str(d) for d in resolution.accepted_ids],
            "deferred_until": deliver_at.isoformat() if deferred else None,
        },
    )
    dispatch = (
        "no_targets"
        if not resolution.accepted
        else ("deferred_quiet_hours" if deferred else "queued")
    )
    result = SendNotificationResult(
        notification_id=reminder.id,
        occurrence_id=occurrence.id,
        accepted_targets=[TargetResult(device_id=d.id, name=d.name) for d in resolution.accepted],
        rejected_targets=[
            RejectedTarget(device_id=d.id, reason=reason) for d, reason in resolution.rejected
        ],
        dispatch_state=dispatch,  # type: ignore[arg-type]
        errors=errors,
        warnings=resolution.notices + permission_notices(mode, resolution.accepted),
    )
    await idempotent_store(ctx, "send_notification", data.idempotency_key, fingerprint, result)
    return result
