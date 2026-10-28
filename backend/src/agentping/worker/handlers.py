"""Outbox job handlers. Each runs inside the job's transaction; external effects are
idempotent from the device's perspective (deterministic event IDs)."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, select, text
from sqlalchemy.dialects.postgresql import insert

from agentping.db.models import (
    Device,
    DeviceDelivery,
    DevicePairingCode,
    IdempotencyRecord,
    OutboxJob,
    Reminder,
    ReminderOccurrence,
    SyncChange,
    User,
)
from agentping.domain.enums import (
    DeliveryMode,
    DeliveryState,
    DeviceStatus,
    OccurrenceState,
    PushContent,
    PushTokenKind,
    PushTokenStatus,
    QuietHoursBehavior,
    ReminderStatus,
    ScheduleKind,
    SyncEntity,
)
from agentping.domain.quiet_hours import quiet_hours_end
from agentping.domain.scheduling import load_timezone
from agentping.domain.schemas import EscalationPolicy
from agentping.push.base import PushMessage, PushResult
from agentping.push.payloads import deliver_payload, dismiss_payload, sync_payload
from agentping.services import occurrences as occ
from agentping.services.core import audit, enqueue, record_changes
from agentping.services.devices import advance_delivery
from agentping.services.runtime import ServiceContext, system_principal
from agentping.services.targeting import active_devices, explicit_targets, reminder_resolution
from agentping.services.views import preferences_of

logger = logging.getLogger(__name__)
S = OccurrenceState


class RetryLater(Exception):
    def __init__(self, detail: str, delay_seconds: float | None = None) -> None:
        super().__init__(detail)
        self.detail = detail
        self.delay_seconds = delay_seconds


@dataclass(frozen=True)
class Job:
    id: int
    kind: str
    payload: dict[str, Any]
    user_id: uuid.UUID | None
    attempts: int
    max_attempts: int


Handler = Callable[[ServiceContext, Job], Awaitable[None]]


def _uuid(value: Any) -> uuid.UUID:
    return uuid.UUID(str(value))


def _same_instant(value: str, instant: datetime) -> bool:
    return datetime.fromisoformat(value) == instant


async def _occurrence(
    ctx: ServiceContext, occurrence_id: uuid.UUID
) -> tuple[ReminderOccurrence, Reminder] | None:
    occurrence = (
        await ctx.session.execute(
            select(ReminderOccurrence)
            .where(ReminderOccurrence.id == occurrence_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if occurrence is None:
        return None
    reminder = await ctx.session.get(Reminder, occurrence.reminder_id)
    return (occurrence, reminder) if reminder is not None else None


async def _targets(ctx: ServiceContext, reminder: Reminder) -> list[Device]:
    devices = await active_devices(ctx)
    explicit = (await explicit_targets(ctx, [reminder.id])).get(reminder.id, [])
    return reminder_resolution(reminder, explicit, devices).accepted


async def _delivery(
    ctx: ServiceContext, occurrence: ReminderOccurrence, device: Device
) -> DeviceDelivery:
    delivery = (
        await ctx.session.execute(
            select(DeviceDelivery).where(
                DeviceDelivery.occurrence_id == occurrence.id,
                DeviceDelivery.device_id == device.id,
            )
        )
    ).scalar_one_or_none()
    if delivery is None:
        delivery = DeviceDelivery(
            occurrence_id=occurrence.id,
            device_id=device.id,
            user_id=occurrence.user_id,
            state=DeliveryState.PENDING.value,
            push_attempts=0,
        )
        ctx.session.add(delivery)
        await ctx.session.flush()
    return delivery


def _locally_scheduled(delivery: DeviceDelivery, occurrence: ReminderOccurrence) -> bool:
    return (
        (
            delivery.scheduled_locally_at is not None
            and (delivery.occurrence_version or 0) >= occurrence.version
        )
        or delivery.displayed_at is not None
        and occurrence.state == S.FIRED.value
    )


async def _push_to_targets(
    ctx: ServiceContext,
    occurrence: ReminderOccurrence,
    reminder: Reminder,
    *,
    step: int,
    silent: bool,
) -> int:
    queued = 0
    stamp = int(occurrence.scheduled_at.timestamp())
    for device in await _targets(ctx, reminder):
        delivery = await _delivery(ctx, occurrence, device)
        if _locally_scheduled(delivery, occurrence):
            continue
        if device.push_token_status != PushTokenStatus.VALID.value:
            delivery.failure_code = "no_push_token"
            continue
        await enqueue(
            ctx,
            "push.deliver",
            {
                "occurrence_id": str(occurrence.id),
                "device_id": str(device.id),
                "scheduled_at": occurrence.scheduled_at.isoformat(),
                "step": step,
                "silent": silent,
            },
            dedupe_key=f"push:{occurrence.id}:{device.id}:{stamp}:{step}",
        )
        queued += 1
    return queued


def _mark_fired(ctx: ServiceContext, occurrence: ReminderOccurrence) -> None:
    """The due time has arrived. Devices ring from their local schedule independently, so
    this does not bump ``version`` (which local-schedule receipts are checked against)."""
    occurrence.state = S.FIRED.value
    occurrence.fired_at = occurrence.fired_at or ctx.now
    occurrence.snoozed_until = None


async def handle_due(ctx: ServiceContext, job: Job) -> None:
    user = await ctx.lock_user()
    loaded = await _occurrence(ctx, _uuid(job.payload["occurrence_id"]))
    if loaded is None:
        return
    occurrence, reminder = loaded
    if occurrence.state not in (S.SCHEDULED.value, S.SNOOZED.value, S.FIRED.value):
        return
    if not _same_instant(job.payload["scheduled_at"], occurrence.scheduled_at):
        return
    if reminder.status != ReminderStatus.ACTIVE.value:
        return
    stamp = int(occurrence.expires_at.timestamp())
    await enqueue(
        ctx,
        "occurrence.expire",
        {"occurrence_id": str(occurrence.id), "expires_at": occurrence.expires_at.isoformat()},
        run_at=occurrence.expires_at,
        dedupe_key=f"expire:{occurrence.id}:{stamp}",
    )
    late_by = ctx.now - occurrence.scheduled_at
    if late_by > timedelta(seconds=reminder.late_grace_seconds) or occurrence.expires_at <= ctx.now:
        _mark_fired(ctx, occurrence)
        await audit(
            ctx,
            "delivery.server_late",
            f"Server fallback for '{reminder.title}' skipped: {int(late_by.total_seconds())}s "
            "past due exceeds the late-delivery grace",
            category="delivery",
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
        )
        return
    prefs = preferences_of(user)
    silent = False
    if reminder.respect_quiet_hours and reminder.mode == DeliveryMode.NORMAL.value:
        quiet_end = quiet_hours_end(prefs.quiet_hours, load_timezone(user.timezone), ctx.now)
        if quiet_end is not None:
            if prefs.quiet_hours.behavior is QuietHoursBehavior.DEFER:
                await enqueue(
                    ctx,
                    "occurrence.due",
                    job.payload,
                    run_at=quiet_end,
                    dedupe_key=f"due:{occurrence.id}:{int(occurrence.scheduled_at.timestamp())}:q",
                )
                return
            silent = True
    _mark_fired(ctx, occurrence)
    await _push_to_targets(ctx, occurrence, reminder, step=0, silent=silent)
    policy = EscalationPolicy.model_validate(reminder.escalation_policy or {"enabled": False})
    if reminder.requires_ack and policy.enabled and policy.max_repeats > 0:
        await enqueue(
            ctx,
            "occurrence.escalate",
            {
                "occurrence_id": str(occurrence.id),
                "scheduled_at": occurrence.scheduled_at.isoformat(),
                "step": 1,
            },
            run_at=occurrence.scheduled_at + timedelta(seconds=policy.interval_seconds),
            dedupe_key=f"escalate:{occurrence.id}:{int(occurrence.scheduled_at.timestamp())}:1",
        )


async def handle_escalate(ctx: ServiceContext, job: Job) -> None:
    await ctx.lock_user()
    loaded = await _occurrence(ctx, _uuid(job.payload["occurrence_id"]))
    if loaded is None:
        return
    occurrence, reminder = loaded
    if occurrence.state not in (S.SCHEDULED.value, S.FIRED.value):
        return
    if not _same_instant(job.payload["scheduled_at"], occurrence.scheduled_at):
        return
    if reminder.status != ReminderStatus.ACTIVE.value or not reminder.requires_ack:
        return
    policy = EscalationPolicy.model_validate(reminder.escalation_policy or {"enabled": False})
    step = int(job.payload["step"])
    deadline = occurrence.scheduled_at + timedelta(seconds=policy.max_duration_seconds)
    if not policy.enabled or step > policy.max_repeats or ctx.now > deadline:
        return
    occurrence.escalation_step = step
    queued = await _push_to_targets(ctx, occurrence, reminder, step=step, silent=False)
    await audit(
        ctx,
        "occurrence.escalated",
        f"Escalation {step}/{policy.max_repeats} for '{reminder.title}' "
        f"({queued} server push(es); devices with local schedules repeat locally)",
        category="delivery",
        reminder_id=reminder.id,
        occurrence_id=occurrence.id,
        details={"step": step},
    )
    if step < policy.max_repeats:
        await enqueue(
            ctx,
            "occurrence.escalate",
            {**job.payload, "step": step + 1},
            run_at=occurrence.scheduled_at
            + timedelta(seconds=policy.interval_seconds * (step + 1)),
            dedupe_key=f"escalate:{occurrence.id}:{int(occurrence.scheduled_at.timestamp())}:"
            f"{step + 1}",
        )


async def handle_expire(ctx: ServiceContext, job: Job) -> None:
    user = await ctx.lock_user()
    loaded = await _occurrence(ctx, _uuid(job.payload["occurrence_id"]))
    if loaded is None:
        return
    occurrence, reminder = loaded
    if not _same_instant(job.payload["expires_at"], occurrence.expires_at):
        return
    state = S(occurrence.state)
    unattended = state in (S.SCHEDULED, S.SNOOZED) or (state is S.FIRED and reminder.requires_ack)
    if unattended:
        occurrence.state = S.MISSED.value
        occurrence.missed_at = ctx.now
        occurrence.version += 1
        await ctx.session.flush()
        await record_changes(ctx, [(SyncEntity.OCCURRENCE, occurrence.id, occurrence.version)])
        await occ.enqueue_dismiss(ctx, occurrence, exclude_device=None)
        await audit(
            ctx,
            "occurrence.missed",
            f"'{reminder.title}' expired without "
            + ("acknowledgement" if reminder.requires_ack else "being displayed"),
            category="delivery",
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
            details={"previous_state": state.value},
        )
    await occ.refresh_reminder(ctx, reminder, user)
    if reminder.schedule_kind == ScheduleKind.RECURRING.value:
        await occ.materialize(ctx, reminder, user)


async def handle_dismiss_fanout(ctx: ServiceContext, job: Job) -> None:
    occurrence_id = _uuid(job.payload["occurrence_id"])
    exclude = job.payload.get("exclude_device_id")
    rows = await ctx.session.execute(
        select(DeviceDelivery, Device)
        .join(Device, Device.id == DeviceDelivery.device_id)
        .where(
            DeviceDelivery.occurrence_id == occurrence_id,
            Device.status == DeviceStatus.ACTIVE.value,
            Device.push_token_status == PushTokenStatus.VALID.value,
        )
    )
    for delivery, device in rows.all():
        if exclude and str(device.id) == exclude:
            continue
        reached = (
            delivery.displayed_at
            or delivery.scheduled_locally_at
            or delivery.provider_accepted_at
            or delivery.device_received_at
        )
        if not reached:
            continue
        await enqueue(
            ctx,
            "push.dismiss",
            {"occurrence_id": str(occurrence_id), "device_id": str(device.id)},
            dedupe_key=f"pushdismiss:{occurrence_id}:{device.id}:{job.payload.get('version')}",
        )


async def _invalidate_token(
    ctx: ServiceContext, device: Device, result: PushResult, *, reminder_id: uuid.UUID | None
) -> None:
    await ctx.lock_user()
    await ctx.session.refresh(device)
    if device.push_token_status == PushTokenStatus.INVALID.value:
        return
    device.push_token_status = PushTokenStatus.INVALID.value
    device.revision += 1
    await ctx.session.flush()
    await record_changes(ctx, [(SyncEntity.DEVICE, device.id, device.revision)])
    await audit(
        ctx,
        "device.push_token_invalid",
        f"FCM rejected the registration token for '{device.name}' ({result.error_code}); "
        "the app must refresh it",
        category="error",
        reminder_id=reminder_id,
        subject_device_id=device.id,
        details={"error_code": result.error_code},
    )


async def _send_one(ctx: ServiceContext, message: PushMessage) -> PushResult:
    return (await ctx.runtime.push.send([message]))[0]


async def handle_push_deliver(ctx: ServiceContext, job: Job) -> None:
    payload = job.payload
    occurrence = await ctx.session.get(ReminderOccurrence, _uuid(payload["occurrence_id"]))
    device = await ctx.session.get(Device, _uuid(payload["device_id"]))
    if occurrence is None or device is None:
        return
    reminder = await ctx.session.get(Reminder, occurrence.reminder_id)
    user = await ctx.session.get(User, occurrence.user_id)
    if reminder is None or user is None:
        return
    if occurrence.state not in (S.SCHEDULED.value, S.SNOOZED.value, S.FIRED.value):
        return
    if not _same_instant(payload["scheduled_at"], occurrence.scheduled_at):
        return
    if device.status != DeviceStatus.ACTIVE.value or not device.push_token:
        return
    if device.push_token_status != PushTokenStatus.VALID.value:
        return
    delivery = await _delivery(ctx, occurrence, device)
    deadline = min(
        occurrence.expires_at,
        occurrence.scheduled_at + timedelta(seconds=max(reminder.late_grace_seconds, 60)),
    )
    ttl = int((deadline - ctx.now).total_seconds())
    if ttl <= 0:
        delivery.failure_code = "expired_before_push"
        return
    prefs = preferences_of(user)
    step = int(payload.get("step", 0))
    message = PushMessage(
        token=device.push_token,
        token_kind=PushTokenKind(device.push_token_kind),
        data=deliver_payload(
            reminder,
            occurrence,
            content=PushContent(prefs.push_content),
            silent=bool(payload.get("silent")),
            escalation_step=step,
        ),
        priority="high",
        ttl_seconds=ttl,
    )
    result = await _send_one(ctx, message)
    delivery.push_attempts += 1
    delivery.last_push_at = ctx.now
    delivery.provider = result.provider
    if result.accepted:
        delivery.provider_accepted_at = delivery.provider_accepted_at or ctx.now
        delivery.provider_message_id = result.message_id
        delivery.failure_code = None
        advance_delivery(delivery, DeliveryState.PROVIDER_ACCEPTED)
        await audit(
            ctx,
            "delivery.provider_accepted",
            f"{result.provider.upper()} accepted '{reminder.title}' for '{device.name}' "
            "(not yet confirmed by the device)",
            category="delivery",
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
            subject_device_id=device.id,
            details={"step": step, "provider": result.provider},
        )
        return
    if result.retryable and job.attempts < job.max_attempts:
        raise RetryLater(f"push provider error {result.error_code}")
    delivery.failed_at = ctx.now
    delivery.failure_code = result.error_code
    delivery.failure_detail = result.detail
    advance_delivery(delivery, DeliveryState.PROVIDER_FAILED)
    if result.token_invalid:
        await _invalidate_token(ctx, device, result, reminder_id=reminder.id)
    else:
        await audit(
            ctx,
            "delivery.provider_failed",
            f"Push for '{reminder.title}' to '{device.name}' failed ({result.error_code})",
            category="error",
            reminder_id=reminder.id,
            occurrence_id=occurrence.id,
            subject_device_id=device.id,
            details={"error_code": result.error_code},
        )


async def handle_push_dismiss(ctx: ServiceContext, job: Job) -> None:
    occurrence = await ctx.session.get(ReminderOccurrence, _uuid(job.payload["occurrence_id"]))
    device = await ctx.session.get(Device, _uuid(job.payload["device_id"]))
    if occurrence is None or device is None or not device.push_token:
        return
    if device.status != DeviceStatus.ACTIVE.value:
        return
    if device.push_token_status != PushTokenStatus.VALID.value:
        return
    result = await _send_one(
        ctx,
        PushMessage(
            token=device.push_token,
            token_kind=PushTokenKind(device.push_token_kind),
            data=dismiss_payload(occurrence),
            priority="high",
            ttl_seconds=3600,
        ),
    )
    if result.accepted:
        return
    if result.retryable and job.attempts < job.max_attempts:
        raise RetryLater(f"push provider error {result.error_code}")
    if result.token_invalid:
        await _invalidate_token(ctx, device, result, reminder_id=occurrence.reminder_id)


async def handle_sync_hint(ctx: ServiceContext, job: Job) -> None:
    user = await ctx.session.get(User, ctx.user_id)
    if user is None:
        return
    devices = (
        await ctx.session.execute(
            select(Device).where(
                Device.user_id == user.id,
                Device.status == DeviceStatus.ACTIVE.value,
                Device.push_token_status == PushTokenStatus.VALID.value,
                Device.push_token.is_not(None),
            )
        )
    ).scalars()
    targets = [d for d in devices if (d.last_sync_cursor or 0) < user.sync_seq]
    if not targets:
        return
    messages = [
        PushMessage(
            token=device.push_token or "",
            token_kind=PushTokenKind(device.push_token_kind),
            data=sync_payload(user.sync_seq),
            priority="normal",
            ttl_seconds=24 * 3600,
            collapse_key="sync",
        )
        for device in targets
    ]
    results = await ctx.runtime.push.send(messages)
    retry = False
    for device, result in zip(targets, results, strict=True):
        if result.token_invalid:
            await _invalidate_token(ctx, device, result, reminder_id=None)
        elif not result.accepted and result.retryable:
            retry = True
    if retry and job.attempts < job.max_attempts:
        raise RetryLater("sync hint delivery failed for some devices")


async def handle_maintenance(ctx: ServiceContext, job: Job) -> None:
    settings = ctx.settings
    now = ctx.now
    horizon = now + timedelta(days=settings.occurrence_horizon_days - 1)
    rows = (
        await ctx.session.execute(
            select(Reminder.user_id, Reminder.id)
            .where(
                Reminder.status == ReminderStatus.ACTIVE.value,
                Reminder.schedule_kind == ScheduleKind.RECURRING.value,
                (Reminder.materialized_until.is_(None)) | (Reminder.materialized_until < horizon),
            )
            .order_by(Reminder.user_id, Reminder.id)
        )
    ).all()
    extended = 0
    by_user: dict[uuid.UUID, list[uuid.UUID]] = {}
    for user_id, reminder_id in rows:
        by_user.setdefault(user_id, []).append(reminder_id)
    for user_id, reminder_ids in by_user.items():
        user_ctx = ServiceContext(
            session=ctx.session,
            principal=system_principal(user_id),
            runtime=ctx.runtime,
            source=ctx.source,
            request_id=None,
            now=now,
        )
        user = await user_ctx.lock_user()
        for reminder_id in reminder_ids:
            reminder = await ctx.session.get(Reminder, reminder_id)
            if reminder is not None:
                extended += await occ.materialize(user_ctx, reminder, user)
        ctx.enqueued_work |= user_ctx.enqueued_work
    retention = now - timedelta(days=settings.sync_change_retention_days)
    await ctx.session.execute(delete(SyncChange).where(SyncChange.created_at < retention))
    await ctx.session.execute(delete(IdempotencyRecord).where(IdempotencyRecord.expires_at < now))
    await ctx.session.execute(
        delete(DevicePairingCode).where(DevicePairingCode.expires_at < now - timedelta(days=1))
    )
    await ctx.session.execute(
        delete(OutboxJob).where(
            OutboxJob.status == "succeeded", OutboxJob.finished_at < now - timedelta(days=7)
        )
    )
    await ctx.session.execute(
        delete(OutboxJob).where(
            OutboxJob.status == "dead", OutboxJob.finished_at < now - timedelta(days=30)
        )
    )
    logger.info("maintenance_complete", extra={"occurrence_changes": extended})
    await schedule_maintenance(ctx, now + timedelta(seconds=settings.maintenance_interval_seconds))


async def schedule_maintenance(ctx: ServiceContext, run_at: datetime) -> None:
    await ctx.session.execute(
        insert(OutboxJob)
        .values(
            kind="maintenance.tick",
            payload={},
            user_id=None,
            dedupe_key="maintenance",
            run_at=run_at,
            status="pending",
            max_attempts=ctx.settings.worker_max_attempts,
        )
        .on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text("dedupe_key IS NOT NULL AND status = 'pending'"),
        )
    )


async def on_dead(ctx: ServiceContext, job: Job, error: str) -> None:
    """Record terminal failures so they are visible in activity and diagnostics."""
    if job.kind == "push.deliver" and job.user_id is not None:
        occurrence = await ctx.session.get(ReminderOccurrence, _uuid(job.payload["occurrence_id"]))
        device = await ctx.session.get(Device, _uuid(job.payload["device_id"]))
        if occurrence is not None and device is not None:
            delivery = await _delivery(ctx, occurrence, device)
            delivery.failed_at = ctx.now
            delivery.failure_code = "retries_exhausted"
            delivery.failure_detail = error[:500]
            advance_delivery(delivery, DeliveryState.PROVIDER_FAILED)
    if job.user_id is not None:
        await audit(
            ctx,
            "worker.job_failed",
            f"Background job {job.kind} failed permanently after {job.attempts} attempts",
            category="error",
            details={"job_id": job.id, "kind": job.kind, "error": error[:300]},
        )


HANDLERS: dict[str, Handler] = {
    "occurrence.due": handle_due,
    "occurrence.escalate": handle_escalate,
    "occurrence.expire": handle_expire,
    "occurrence.dismiss": handle_dismiss_fanout,
    "push.deliver": handle_push_deliver,
    "push.dismiss": handle_push_dismiss,
    "push.sync_hint": handle_sync_hint,
    "maintenance.tick": handle_maintenance,
}
