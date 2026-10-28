"""Schedule compilation, occurrence materialization and occurrence actions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import and_, func, or_, select

from agentping.auth.principal import Scope
from agentping.db.models import (
    DeviceDelivery,
    NotificationAction,
    Reminder,
    ReminderOccurrence,
    User,
)
from agentping.domain import scheduling as sched
from agentping.domain.enums import (
    ActionKind,
    ActionResult,
    ActionSource,
    ActorKind,
    DeliveryMode,
    MonthlyDayPolicy,
    OccurrenceState,
    ReminderStatus,
    ScheduleKind,
    SyncEntity,
    TargetStrategy,
    TimezoneMode,
)
from agentping.domain.errors import Forbidden, InvalidSchedule, NotFound, ValidationFailed
from agentping.domain.schemas import (
    DeliveryView,
    EscalationPolicy,
    Notice,
    OccurrenceDetail,
    OccurrenceListItem,
    OccurrenceView,
    OnceSchedule,
    Page,
    RecurringSchedule,
    SnoozePolicy,
)
from agentping.domain.states import decide_action
from agentping.services.core import audit, enqueue, record_changes
from agentping.services.runtime import ServiceContext

S = OccurrenceState
PENDING = [S.SCHEDULED.value, S.FIRED.value, S.SNOOZED.value]
MAX_SCHEDULE_AHEAD = timedelta(days=5 * 366)


@dataclass(frozen=True)
class ModeDefaults:
    requires_ack: bool
    expire_after: timedelta
    late_grace: timedelta
    respect_quiet_hours: bool
    strategy: TargetStrategy
    escalation: EscalationPolicy


MODE_DEFAULTS: dict[DeliveryMode, ModeDefaults] = {
    DeliveryMode.NORMAL: ModeDefaults(
        False,
        timedelta(hours=24),
        timedelta(hours=12),
        True,
        TargetStrategy.ALL,
        EscalationPolicy(enabled=False),
    ),
    DeliveryMode.PERSISTENT: ModeDefaults(
        True,
        timedelta(hours=12),
        timedelta(hours=2),
        True,
        TargetStrategy.ALL,
        EscalationPolicy(enabled=True, interval_seconds=300, max_repeats=3),
    ),
    DeliveryMode.ALARM: ModeDefaults(
        True,
        timedelta(hours=2),
        timedelta(minutes=15),
        False,
        TargetStrategy.PRIMARY,
        EscalationPolicy(enabled=False),
    ),
    DeliveryMode.CRITICAL: ModeDefaults(
        True,
        timedelta(hours=6),
        timedelta(minutes=30),
        False,
        TargetStrategy.PRIMARY,
        EscalationPolicy(
            enabled=True, interval_seconds=120, max_repeats=5, max_duration_seconds=1800
        ),
    ),
}


@dataclass
class CompiledSchedule:
    kind: ScheduleKind
    definition: dict[str, Any]
    timezone: str
    timezone_mode: TimezoneMode
    rrule: str | None = None
    start_local: datetime | None = None
    first_at: datetime | None = None
    until_at: datetime | None = None
    count: int | None = None
    monthly_day_policy: MonthlyDayPolicy = MonthlyDayPolicy.SKIP
    notices: list[Notice] = field(default_factory=list)


def compile_schedule(
    schedule: OnceSchedule | RecurringSchedule, user: User, ctx: ServiceContext
) -> CompiledSchedule:
    now = ctx.now
    if isinstance(schedule, OnceSchedule):
        tz_name = schedule.timezone or user.timezone
        tz = sched.load_timezone(tz_name)
        notices: list[Notice] = []
        if schedule.at is not None:
            first_at = sched.require_aware(schedule.at, "schedule.at")
        else:
            assert schedule.local_datetime is not None
            local = sched.parse_local_datetime(schedule.local_datetime, "schedule.local_datetime")
            resolved = sched.localize(local, tz)
            first_at = resolved.instant
            if resolved.adjusted_for_gap:
                notices.append(
                    Notice(
                        code="dst_gap",
                        message="The local time does not exist (DST gap); it was moved "
                        f"forward to {first_at.astimezone(tz).isoformat()}",
                    )
                )
            if resolved.ambiguous:
                notices.append(
                    Notice(
                        code="dst_ambiguous",
                        message="The local time occurs twice (DST overlap); the first "
                        "occurrence was used",
                    )
                )
        if first_at < now - timedelta(seconds=60):
            raise InvalidSchedule(
                "The scheduled time is in the past. Use send_notification for immediate "
                "delivery, or provide a future time",
                details={"field": "schedule", "now": now.isoformat()},
            )
        if first_at > now + MAX_SCHEDULE_AHEAD:
            raise InvalidSchedule("Schedules more than 5 years ahead are not supported")
        return CompiledSchedule(
            kind=ScheduleKind.ONCE,
            definition=schedule.model_dump(mode="json", exclude_none=True),
            timezone=tz_name,
            timezone_mode=TimezoneMode.FIXED,
            first_at=first_at,
            notices=notices,
        )

    start_local = sched.parse_local_datetime(schedule.start_local, "schedule.start_local")
    if schedule.timezone_mode is TimezoneMode.FLOATING:
        tz_name = user.timezone
    else:
        tz_name = schedule.timezone or user.timezone
    tz = sched.load_timezone(tz_name)
    until_at = None
    if schedule.until is not None:
        if schedule.until.tzinfo is None:
            until_at = sched.localize(schedule.until, tz).instant
        else:
            until_at = sched.require_aware(schedule.until, "schedule.until")
    spec = sched.RecurrenceSpec(
        start_local=start_local,
        timezone=tz_name,
        rule=schedule.rrule,
        count=schedule.count,
        until_utc=until_at,
        monthly_day_policy=schedule.monthly_day_policy,
    )
    warnings = sched.validate_recurrence(
        spec, now=now, min_interval_seconds=ctx.settings.min_recurrence_interval_seconds
    )
    parsed = sched.parse_rrule(schedule.rrule, tz)
    first = sched.first_instance(spec, now)
    return CompiledSchedule(
        kind=ScheduleKind.RECURRING,
        definition=schedule.model_dump(mode="json", exclude_none=True),
        timezone=tz_name,
        timezone_mode=schedule.timezone_mode,
        rrule=parsed.normalized(),
        start_local=start_local,
        first_at=first.original_at if first else None,
        until_at=until_at or parsed.until_utc,
        count=schedule.count or parsed.count,
        monthly_day_policy=schedule.monthly_day_policy,
        notices=[Notice(code="schedule", message=w) for w in warnings],
    )


def apply_compiled(reminder: Reminder, compiled: CompiledSchedule) -> None:
    reminder.schedule_kind = compiled.kind.value
    reminder.schedule = compiled.definition
    reminder.timezone = compiled.timezone
    reminder.timezone_mode = compiled.timezone_mode.value
    reminder.rrule = compiled.rrule
    reminder.start_local = compiled.start_local
    reminder.first_at = compiled.first_at
    reminder.until_at = compiled.until_at
    reminder.occurrence_count = compiled.count
    reminder.monthly_day_policy = compiled.monthly_day_policy.value


def recurrence_spec(reminder: Reminder, user: User) -> sched.RecurrenceSpec:
    assert reminder.rrule is not None and reminder.start_local is not None
    tz_name = (
        user.timezone if reminder.timezone_mode == TimezoneMode.FLOATING else reminder.timezone
    )
    return sched.RecurrenceSpec(
        start_local=reminder.start_local,
        timezone=tz_name,
        rule=reminder.rrule,
        count=reminder.occurrence_count,
        until_utc=reminder.until_at,
        monthly_day_policy=MonthlyDayPolicy(reminder.monthly_day_policy),
    )


def _desired_instances(
    reminder: Reminder, user: User, ctx: ServiceContext
) -> tuple[list[sched.Instance], datetime | None, sched.RecurrenceSpec | None]:
    """Instances to materialize, the effective window end, and the recurrence spec."""
    if reminder.schedule_kind != ScheduleKind.RECURRING.value:
        assert reminder.first_at is not None
        tz = sched.load_timezone(reminder.timezone)
        instance = sched.Instance(
            original_at=reminder.first_at,
            local_wall=sched.to_local_naive(reminder.first_at, tz),
            adjusted_for_gap=False,
        )
        return [instance], None, None
    spec = recurrence_spec(reminder, user)
    end = ctx.now + timedelta(days=ctx.settings.occurrence_horizon_days)
    limit = ctx.settings.occurrence_horizon_count
    instances = sched.instances_between(spec, ctx.now, end, limit)
    window_end = instances[-1].original_at if len(instances) >= limit else end
    return instances, window_end, spec


async def _enqueue_due(ctx: ServiceContext, occurrence: ReminderOccurrence) -> None:
    stamp = int(occurrence.scheduled_at.timestamp())
    await enqueue(
        ctx,
        "occurrence.due",
        {"occurrence_id": str(occurrence.id), "scheduled_at": occurrence.scheduled_at.isoformat()},
        run_at=occurrence.scheduled_at,
        dedupe_key=f"due:{occurrence.id}:{stamp}",
    )


async def materialize(ctx: ServiceContext, reminder: Reminder, user: User) -> int:
    """Bring stored occurrences in line with the reminder's current schedule.

    Returns the number of occurrences created, revived or superseded.
    """
    if reminder.status != ReminderStatus.ACTIVE.value:
        return 0
    desired, window_end, spec = _desired_instances(reminder, user, ctx)
    query = select(ReminderOccurrence).where(ReminderOccurrence.reminder_id == reminder.id)
    if reminder.schedule_kind == ScheduleKind.RECURRING.value:
        query = query.where(ReminderOccurrence.original_at >= ctx.now)
    existing = {o.original_at: o for o in (await ctx.session.execute(query)).scalars()}
    expire_after = timedelta(seconds=reminder.expire_after_seconds)
    changes: list[tuple[SyncEntity, uuid.UUID, int]] = []
    desired_at = {instance.original_at for instance in desired}

    for instance in desired:
        occurrence = existing.get(instance.original_at)
        if occurrence is None:
            occurrence = ReminderOccurrence(
                id=sched.occurrence_id(reminder.id, instance.original_at),
                reminder_id=reminder.id,
                user_id=reminder.user_id,
                reminder_revision=reminder.revision,
                version=1,
                original_at=instance.original_at,
                scheduled_at=instance.original_at,
                local_wall=instance.local_wall.isoformat(),
                state=S.SCHEDULED.value,
                expires_at=instance.original_at + expire_after,
                snooze_count=0,
                escalation_step=0,
            )
            ctx.session.add(occurrence)
            await ctx.session.flush()
            await _enqueue_due(ctx, occurrence)
        elif occurrence.state == S.SUPERSEDED.value:
            occurrence.state = S.SCHEDULED.value
            occurrence.scheduled_at = occurrence.original_at
            occurrence.local_wall = instance.local_wall.isoformat()
            occurrence.cancelled_at = None
            occurrence.cancel_reason = None
            occurrence.reminder_revision = reminder.revision
            occurrence.expires_at = occurrence.original_at + expire_after
            occurrence.version += 1
            await _enqueue_due(ctx, occurrence)
        elif S(occurrence.state).is_pending and occurrence.reminder_revision != reminder.revision:
            occurrence.reminder_revision = reminder.revision
            occurrence.local_wall = instance.local_wall.isoformat()
            if occurrence.state != S.SNOOZED.value:
                occurrence.expires_at = occurrence.original_at + expire_after
            occurrence.version += 1
        else:
            continue
        changes.append((SyncEntity.OCCURRENCE, occurrence.id, occurrence.version))

    for original_at, occurrence in existing.items():
        if original_at in desired_at or occurrence.state != S.SCHEDULED.value:
            continue
        if original_at < ctx.now:
            continue
        if (
            spec is not None
            and window_end is not None
            and original_at > window_end
            and _is_instance(spec, original_at)
        ):
            continue
        occurrence.state = S.SUPERSEDED.value
        occurrence.cancelled_at = ctx.now
        occurrence.cancel_reason = "schedule_changed"
        occurrence.reminder_revision = reminder.revision
        occurrence.version += 1
        changes.append((SyncEntity.OCCURRENCE, occurrence.id, occurrence.version))

    reminder.materialized_until = window_end or reminder.first_at
    await ctx.session.flush()
    await record_changes(ctx, changes)
    await refresh_reminder(ctx, reminder, user, spec=spec, window_end=window_end)
    return len(changes)


def _is_instance(spec: sched.RecurrenceSpec, at: datetime) -> bool:
    instance = sched.first_instance(spec, at)
    return instance is not None and instance.original_at == at


async def supersede_pending(
    ctx: ServiceContext, reminder: Reminder, *, reason: str, include_active: bool = False
) -> list[ReminderOccurrence]:
    """Supersede not-yet-due occurrences (pause). With include_active also cancel ringing ones."""
    states = [S.SCHEDULED.value]
    if include_active:
        states += [S.FIRED.value, S.SNOOZED.value]
    rows = list(
        (
            await ctx.session.execute(
                select(ReminderOccurrence).where(
                    ReminderOccurrence.reminder_id == reminder.id,
                    ReminderOccurrence.state.in_(states),
                )
            )
        ).scalars()
    )
    changes = []
    for occurrence in rows:
        occurrence.state = S.SUPERSEDED.value
        occurrence.cancelled_at = ctx.now
        occurrence.cancel_reason = reason
        occurrence.version += 1
        changes.append((SyncEntity.OCCURRENCE, occurrence.id, occurrence.version))
    await ctx.session.flush()
    await record_changes(ctx, changes)
    return rows


async def cancel_all(ctx: ServiceContext, reminder: Reminder, *, reason: str) -> int:
    rows = list(
        (
            await ctx.session.execute(
                select(ReminderOccurrence).where(
                    ReminderOccurrence.reminder_id == reminder.id,
                    ReminderOccurrence.state.in_(PENDING + [S.SUPERSEDED.value]),
                )
            )
        ).scalars()
    )
    changes = []
    for occurrence in rows:
        was_active = occurrence.state in (S.FIRED.value, S.SNOOZED.value)
        occurrence.state = S.CANCELLED.value
        occurrence.cancelled_at = ctx.now
        occurrence.cancel_reason = reason
        occurrence.version += 1
        changes.append((SyncEntity.OCCURRENCE, occurrence.id, occurrence.version))
        if was_active or occurrence.scheduled_at <= ctx.now:
            await enqueue_dismiss(ctx, occurrence, exclude_device=ctx.principal.device_id)
    await ctx.session.flush()
    await record_changes(ctx, changes)
    return len(rows)


async def refresh_reminder(
    ctx: ServiceContext,
    reminder: Reminder,
    user: User,
    *,
    spec: sched.RecurrenceSpec | None = None,
    window_end: datetime | None = None,
) -> None:
    next_at = (
        await ctx.session.execute(
            select(func.min(ReminderOccurrence.scheduled_at)).where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.state.in_([S.SCHEDULED.value, S.SNOOZED.value]),
            )
        )
    ).scalar_one_or_none()
    reminder.next_occurrence_at = next_at
    if reminder.status != ReminderStatus.ACTIVE.value:
        return
    # A fired occurrence that needs no acknowledgement has been delivered.
    open_states = PENDING if reminder.requires_ack else [S.SCHEDULED.value, S.SNOOZED.value]
    pending = (
        await ctx.session.execute(
            select(func.count()).where(
                ReminderOccurrence.reminder_id == reminder.id,
                ReminderOccurrence.state.in_(open_states),
            )
        )
    ).scalar_one()
    if pending:
        return
    if reminder.schedule_kind == ScheduleKind.RECURRING.value:
        spec = spec or recurrence_spec(reminder, user)
        after = window_end or (ctx.now + timedelta(days=ctx.settings.occurrence_horizon_days))
        if sched.first_instance(spec, after) is not None:
            return
    reminder.status = ReminderStatus.COMPLETED.value
    reminder.completed_at = ctx.now
    reminder.revision += 1
    await record_changes(ctx, [(SyncEntity.REMINDER, reminder.id, reminder.revision)])
    await audit(
        ctx,
        "reminder.completed",
        f"Reminder '{reminder.title}' completed (no further occurrences)",
        category="reminder",
        reminder_id=reminder.id,
    )


async def enqueue_dismiss(
    ctx: ServiceContext, occurrence: ReminderOccurrence, *, exclude_device: uuid.UUID | None
) -> None:
    await enqueue(
        ctx,
        "occurrence.dismiss",
        {
            "occurrence_id": str(occurrence.id),
            "version": occurrence.version,
            "exclude_device_id": str(exclude_device) if exclude_device else None,
        },
        dedupe_key=f"dismiss:{occurrence.id}:{occurrence.version}",
    )


async def list_occurrences(
    ctx: ServiceContext,
    *,
    states: list[OccurrenceState] | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    reminder_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[OccurrenceListItem]:
    """Occurrences ordered by due time (powers the Today/overdue views)."""
    ctx.principal.require(Scope.REMINDERS_READ)
    limit = max(1, min(limit, 200))
    conditions = [ReminderOccurrence.user_id == ctx.user_id]
    if states:
        conditions.append(ReminderOccurrence.state.in_([s.value for s in states]))
    else:
        conditions.append(ReminderOccurrence.state != S.SUPERSEDED.value)
    if start:
        conditions.append(ReminderOccurrence.scheduled_at >= start)
    if end:
        conditions.append(ReminderOccurrence.scheduled_at < end)
    if reminder_id:
        conditions.append(ReminderOccurrence.reminder_id == reminder_id)
    if cursor:
        try:
            stamp, last = cursor.split("|", 1)
            after = datetime.fromisoformat(stamp)
            last_id = uuid.UUID(last)
        except ValueError as error:
            raise ValidationFailed("Invalid pagination cursor") from error
        conditions.append(
            or_(
                ReminderOccurrence.scheduled_at > after,
                and_(ReminderOccurrence.scheduled_at == after, ReminderOccurrence.id > last_id),
            )
        )
    rows = list(
        (
            await ctx.session.execute(
                select(ReminderOccurrence, Reminder)
                .join(Reminder, Reminder.id == ReminderOccurrence.reminder_id)
                .where(*conditions)
                .order_by(ReminderOccurrence.scheduled_at, ReminderOccurrence.id)
                .limit(limit + 1)
            )
        ).all()
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last_occurrence = rows[-1][0]
        next_cursor = f"{last_occurrence.scheduled_at.isoformat()}|{last_occurrence.id}"
    items = [
        OccurrenceListItem(
            **OccurrenceView.model_validate(o).model_dump(),
            title=r.title,
            delivery_mode=DeliveryMode(r.mode),
            requires_ack=r.requires_ack,
            reminder_status=ReminderStatus(r.status),
        )
        for o, r in rows
    ]
    return Page[OccurrenceListItem](items=items, next_cursor=next_cursor)


async def get_occurrence(ctx: ServiceContext, occurrence_id: uuid.UUID) -> OccurrenceDetail:
    ctx.principal.require(Scope.REMINDERS_READ)
    occurrence, _ = await load_occurrence(ctx, occurrence_id)
    deliveries = (
        await ctx.session.execute(
            select(DeviceDelivery).where(DeviceDelivery.occurrence_id == occurrence.id)
        )
    ).scalars()
    return OccurrenceDetail(
        **OccurrenceView.model_validate(occurrence).model_dump(),
        deliveries=[DeliveryView.model_validate(d) for d in deliveries],
    )


# ---------------------------------------------------------------------------
# Actions


@dataclass
class ActionOutcome:
    result: ActionResult
    occurrence: ReminderOccurrence
    reason: str | None = None


async def load_occurrence(
    ctx: ServiceContext, occurrence_id: uuid.UUID, *, for_update: bool = False
) -> tuple[ReminderOccurrence, Reminder]:
    query = select(ReminderOccurrence).where(
        ReminderOccurrence.id == occurrence_id, ReminderOccurrence.user_id == ctx.user_id
    )
    if for_update:
        query = query.with_for_update()
    occurrence = (await ctx.session.execute(query)).scalar_one_or_none()
    if occurrence is None:
        raise NotFound("Occurrence not found", details={"occurrence_id": str(occurrence_id)})
    reminder = await ctx.session.get(Reminder, occurrence.reminder_id)
    assert reminder is not None
    return occurrence, reminder


def require_agent_ownership(ctx: ServiceContext, reminder: Reminder) -> None:
    principal = ctx.principal
    if principal.is_agent and reminder.source_agent_client_id != principal.agent_client_id:
        raise Forbidden(
            "Agents may only modify reminders they created",
            details={"reminder_id": str(reminder.id)},
        )


def _action_time(ctx: ServiceContext, occurred_at: datetime | None) -> datetime:
    if occurred_at is None or occurred_at.tzinfo is None:
        return ctx.now
    if ctx.now - timedelta(hours=24) <= occurred_at <= ctx.now + timedelta(seconds=60):
        return min(occurred_at, ctx.now)
    return ctx.now


async def apply_action(
    ctx: ServiceContext,
    occurrence_id: uuid.UUID,
    action: ActionKind,
    *,
    source: ActionSource | None = None,
    client_action_id: str | None = None,
    occurred_at: datetime | None = None,
    snooze_minutes: int | None = None,
    reason: str | None = None,
    note: str | None = None,
) -> ActionOutcome:
    ctx.principal.require(Scope.REMINDERS_WRITE)
    user = await ctx.lock_user()
    source = source or ctx.source
    if ctx.principal.actor_kind is ActorKind.AGENT:
        source = ActionSource.MCP if ctx.source is ActionSource.MCP else ActionSource.API
    dedupe_key = f"{ctx.principal.actor_key}:{client_action_id}" if client_action_id else None
    if dedupe_key:
        previous = (
            await ctx.session.execute(
                select(NotificationAction).where(
                    NotificationAction.user_id == ctx.user_id,
                    NotificationAction.dedupe_key == dedupe_key,
                )
            )
        ).scalar_one_or_none()
        if previous is not None:
            occurrence, _ = await load_occurrence(ctx, previous.occurrence_id)
            result = ActionResult(previous.result)
            if result is ActionResult.APPLIED:
                result = ActionResult.DUPLICATE
            return ActionOutcome(result, occurrence, previous.reason or "replayed action")

    occurrence, reminder = await load_occurrence(ctx, occurrence_id, for_update=True)
    require_agent_ownership(ctx, reminder)
    at = _action_time(ctx, occurred_at)
    decision = decide_action(S(occurrence.state), action)
    result, reject_reason = decision.result, decision.reason
    details: dict[str, Any] = {"previous_state": occurrence.state}
    snooze = SnoozePolicy.model_validate(reminder.snooze_policy or {})

    if result is ActionResult.APPLIED and action is ActionKind.SNOOZE:
        if occurrence.snooze_count >= snooze.max_snoozes:
            result = ActionResult.REJECTED_INVALID
            reject_reason = f"snooze limit reached ({snooze.max_snoozes})"
    if result is ActionResult.APPLIED and action is ActionKind.SKIP and not reason:
        result = ActionResult.REJECTED_INVALID
        reject_reason = "a reason is required to skip an occurrence"

    if result is ActionResult.APPLIED:
        if action is ActionKind.ACKNOWLEDGE:
            occurrence.state = S.ACKNOWLEDGED.value
            occurrence.acknowledged_at = at
            occurrence.ack_source = source.value
            occurrence.ack_device_id = ctx.principal.device_id
        elif action is ActionKind.SKIP:
            occurrence.state = S.SKIPPED.value
            occurrence.skipped_at = at
            occurrence.skip_reason = reason
        else:
            user_default = (user.preferences or {}).get("default_snooze_minutes")
            minutes = snooze_minutes or snooze.default_minutes or user_default or 10
            until = at + timedelta(minutes=minutes)
            occurrence.state = S.SNOOZED.value
            occurrence.snoozed_until = until
            occurrence.scheduled_at = until
            occurrence.snooze_count += 1
            occurrence.expires_at = max(
                occurrence.expires_at, until + timedelta(seconds=reminder.expire_after_seconds)
            )
            details["snooze_minutes"] = minutes
            details["snoozed_until"] = until.isoformat()
        occurrence.version += 1
        await ctx.session.flush()
        await record_changes(ctx, [(SyncEntity.OCCURRENCE, occurrence.id, occurrence.version)])
        if action is ActionKind.SNOOZE:
            await _enqueue_due(ctx, occurrence)
        else:
            await enqueue_dismiss(ctx, occurrence, exclude_device=ctx.principal.device_id)
        await refresh_reminder(ctx, reminder, user)

    ctx.session.add(
        NotificationAction(
            user_id=ctx.user_id,
            occurrence_id=occurrence.id,
            reminder_id=reminder.id,
            action=action.value,
            source=source.value,
            actor_kind=ctx.principal.actor_kind.value,
            agent_client_id=ctx.principal.agent_client_id,
            device_id=ctx.principal.device_id,
            dedupe_key=dedupe_key,
            occurred_at=at,
            result=result.value,
            reason=reject_reason or (reason if action is ActionKind.SKIP else note),
            details=details,
        )
    )
    verb = {"acknowledge": "acknowledged", "snooze": "snoozed", "skip": "skipped"}[action.value]
    if result is ActionResult.APPLIED:
        summary = f"Occurrence of '{reminder.title}' {verb} via {source.value}"
        event = f"occurrence.{verb}"
    else:
        summary = f"Rejected {action.value} of '{reminder.title}': {reject_reason}"
        event = "occurrence.action_rejected"
    await audit(
        ctx,
        event,
        summary,
        category="action",
        reminder_id=reminder.id,
        occurrence_id=occurrence.id,
        details={**details, "result": result.value, "source": source.value},
    )
    return ActionOutcome(result, occurrence, reject_reason)
