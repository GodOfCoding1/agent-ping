"""Device registration, preferences, receipts and lifecycle."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select, update

from agentping.auth import tokens
from agentping.auth.principal import DEVICE_SCOPES, Principal, Scope
from agentping.db.models import (
    AccessToken,
    Device,
    DeviceDelivery,
    DevicePairingCode,
    Reminder,
    ReminderOccurrence,
    User,
)
from agentping.domain.enums import (
    ActionSource,
    ActorKind,
    DeliveryState,
    DeviceStatus,
    OccurrenceState,
    PushTokenKind,
    PushTokenStatus,
    ReceiptKind,
    SyncEntity,
)
from agentping.domain.errors import Forbidden, NotFound, Unauthenticated, ValidationFailed
from agentping.domain.scheduling import load_timezone
from agentping.domain.schemas import (
    DeviceReceiptBatch,
    DeviceRegister,
    DeviceRegistered,
    DeviceSelfUpdate,
    DeviceUpdate,
    DeviceView,
    PairingCodeCreated,
    ReceiptResult,
)
from agentping.services.core import audit, record_changes
from agentping.services.occurrences import refresh_reminder
from agentping.services.runtime import Runtime, ServiceContext
from agentping.services.settings import apply_timezone_change
from agentping.services.views import device_view

DELIVERY_RANK = {
    DeliveryState.PENDING: 0,
    DeliveryState.SUPPRESSED: 0,
    DeliveryState.PROVIDER_FAILED: 1,
    DeliveryState.FAILED: 1,
    DeliveryState.PROVIDER_ACCEPTED: 2,
    DeliveryState.DEVICE_RECEIVED: 3,
    DeliveryState.SCHEDULED_LOCALLY: 4,
    DeliveryState.DISPLAYED: 5,
    DeliveryState.DISMISSED: 6,
}


def advance_delivery(delivery: DeviceDelivery, state: DeliveryState) -> None:
    current = DeliveryState(delivery.state)
    if DELIVERY_RANK[state] >= DELIVERY_RANK[current]:
        delivery.state = state.value


def reset_delivery_version(delivery: DeviceDelivery, version: int) -> None:
    """Current delivery evidence must never describe an older snoozed revision."""
    if (delivery.occurrence_version or 0) >= version:
        return
    delivery.occurrence_version = version
    delivery.displayed_at = None
    delivery.scheduled_locally_at = None
    delivery.dismissed_at = None
    delivery.device_received_at = None
    delivery.provider_accepted_at = None
    delivery.failed_at = None
    delivery.failure_code = None
    delivery.failure_detail = None
    delivery.state = DeliveryState.PENDING.value


async def create_pairing_code(ctx: ServiceContext) -> PairingCodeCreated:
    ctx.principal.require(Scope.DEVICES_MANAGE)
    code = tokens.generate_pairing_code()
    expires_at = ctx.now + timedelta(seconds=ctx.settings.pairing_code_ttl_seconds)
    ctx.session.add(
        DevicePairingCode(
            user_id=ctx.user_id, code_hash=tokens.hash_secret(code), expires_at=expires_at
        )
    )
    await audit(
        ctx, "device.pairing_code_created", "Device pairing code created", category="device"
    )
    return PairingCodeCreated(code=code, expires_at=expires_at)


async def register_with_pairing_code(
    runtime: Runtime, data: DeviceRegister, *, request_id: str | None
) -> DeviceRegistered:
    if not data.pairing_code:
        raise Unauthenticated("A pairing code or an authenticated session is required")
    code_hash = tokens.hash_secret(tokens.normalize_pairing_code(data.pairing_code))
    async with runtime.session_factory() as session, session.begin():
        now = runtime.clock.now()
        code = (
            await session.execute(
                select(DevicePairingCode)
                .where(DevicePairingCode.code_hash == code_hash)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if code is None or code.used_at is not None or code.expires_at <= now:
            raise Unauthenticated("Pairing code is invalid, expired or already used")
        principal = Principal(
            user_id=code.user_id,
            actor_kind=ActorKind.USER,
            scopes=frozenset({Scope.DEVICES_MANAGE}),
            auth_method="pairing_code",
        )
        ctx = ServiceContext(
            session=session,
            principal=principal,
            runtime=runtime,
            source=ActionSource.API,
            request_id=request_id,
            now=now,
        )
        result = await register_device(ctx, data)
        code.used_at = now
        code.used_by_device_id = result.device.id
    if ctx.enqueued_work:
        runtime.signal.notify()
    return result


async def _claim_push_token(
    ctx: ServiceContext, device: Device, push_token: str, kind: PushTokenKind
) -> None:
    if (
        device.push_token == push_token
        and device.push_token_kind == kind.value
        and device.push_token_status == PushTokenStatus.VALID.value
    ):
        return
    await ctx.session.execute(
        update(Device)
        .where(Device.push_token == push_token, Device.id != device.id)
        .values(push_token=None, push_token_status=PushTokenStatus.MISSING.value)
    )
    device.push_token = push_token
    device.push_token_kind = kind.value
    device.push_token_status = PushTokenStatus.VALID.value
    device.push_token_updated_at = ctx.now


async def _issue_device_token(ctx: ServiceContext, device: Device) -> tuple[str, datetime]:
    await ctx.session.execute(
        update(AccessToken)
        .where(AccessToken.device_id == device.id, AccessToken.revoked_at.is_(None))
        .values(revoked_at=ctx.now)
    )
    raw = tokens.generate_token(tokens.DEVICE_PREFIX)
    expires_at = ctx.now + timedelta(days=ctx.settings.device_token_ttl_days)
    ctx.session.add(
        AccessToken(
            user_id=ctx.user_id,
            device_id=device.id,
            token_prefix=tokens.display_prefix(raw),
            token_hash=tokens.hash_secret(raw),
            scopes=sorted(DEVICE_SCOPES),
            label=f"device:{device.name}"[:120],
            expires_at=expires_at,
        )
    )
    return raw, expires_at


async def register_device(ctx: ServiceContext, data: DeviceRegister) -> DeviceRegistered:
    ctx.principal.require(Scope.DEVICES_MANAGE)
    if ctx.principal.is_agent:
        raise Forbidden("Agents cannot register devices")
    user = await ctx.lock_user()
    if data.timezone:
        load_timezone(data.timezone)
    device = (
        await ctx.session.execute(
            select(Device).where(
                Device.user_id == ctx.user_id, Device.installation_id == data.installation_id
            )
        )
    ).scalar_one_or_none()
    has_primary = (
        await ctx.session.execute(
            select(Device.id).where(
                Device.user_id == ctx.user_id,
                Device.is_primary.is_(True),
                Device.status == DeviceStatus.ACTIVE.value,
            )
        )
    ).first() is not None
    created = device is None
    if device is None:
        device = Device(
            id=uuid.uuid4(),
            user_id=ctx.user_id,
            installation_id=data.installation_id,
            name=data.name,
            mode_settings={},
            capabilities={},
            revision=1,
            push_token_status=PushTokenStatus.MISSING.value,
            is_primary=False,
            status=DeviceStatus.ACTIVE.value,
        )
        ctx.session.add(device)
    else:
        device.revision += 1
    device.name = data.name
    device.model = data.model
    device.os_version = data.os_version
    device.app_version = data.app_version
    device.timezone = data.timezone
    device.capabilities = data.capabilities.model_dump(mode="json", exclude_none=True)
    device.status = DeviceStatus.ACTIVE.value
    device.deactivated_at = None
    device.last_seen_at = ctx.now
    if not has_primary:
        device.is_primary = True
    await ctx.session.flush()
    if data.push_token:
        await _claim_push_token(ctx, device, data.push_token, data.push_token_kind)
    raw, expires_at = await _issue_device_token(ctx, device)
    await ctx.session.flush()
    await record_changes(ctx, [(SyncEntity.DEVICE, device.id, device.revision)])
    await audit(
        ctx,
        "device.registered" if created else "device.reregistered",
        f"Device '{device.name}' {'registered' if created else 're-registered'}"
        + (" as primary" if device.is_primary else ""),
        category="device",
        subject_device_id=device.id,
        details={"installation_id": device.installation_id, "primary": device.is_primary},
    )
    if device.is_primary and data.timezone:
        await _follow_device_timezone(ctx, user, data.timezone)
    return DeviceRegistered(
        device=device_view(device, ctx.now), device_token=raw, token_expires_at=expires_at
    )


async def _follow_device_timezone(ctx: ServiceContext, user: User, timezone: str) -> None:
    if user.timezone_source == "primary_device" and user.timezone != timezone:
        await apply_timezone_change(ctx, user, timezone)


async def _own_device(ctx: ServiceContext) -> Device:
    ctx.principal.require(Scope.DEVICE)
    device_id = ctx.principal.device_id
    device = await ctx.session.get(Device, device_id) if device_id else None
    if device is None or device.status != DeviceStatus.ACTIVE.value:
        raise Forbidden("Device is not registered or has been deactivated")
    return device


async def get_self(ctx: ServiceContext) -> DeviceView:
    return device_view(await _own_device(ctx), ctx.now)


async def update_self(ctx: ServiceContext, data: DeviceSelfUpdate) -> DeviceView:
    user = await ctx.lock_user()
    device = await _own_device(ctx)
    changed: list[str] = []
    if data.name is not None and data.name != device.name:
        device.name = data.name
        changed.append("name")
    if data.app_version is not None and data.app_version != device.app_version:
        device.app_version = data.app_version
        changed.append("app_version")
    if data.os_version is not None and data.os_version != device.os_version:
        device.os_version = data.os_version
        changed.append("os_version")
    if data.timezone is not None and data.timezone != device.timezone:
        load_timezone(data.timezone)
        device.timezone = data.timezone
        changed.append("timezone")
    if data.push_token is not None and (
        data.push_token != device.push_token
        or data.push_token_kind.value != device.push_token_kind
        or device.push_token_status != PushTokenStatus.VALID.value
    ):
        await _claim_push_token(ctx, device, data.push_token, data.push_token_kind)
        changed.append("push_token")
    lost: list[str] = []
    if data.capabilities is not None:
        new_caps = data.capabilities.model_dump(mode="json", exclude_none=True)
        old_caps = device.capabilities or {}
        lost = [
            key
            for key in (
                "notifications_permitted",
                "exact_alarms_permitted",
                "full_screen_intent_permitted",
                "dnd_access_granted",
            )
            if old_caps.get(key) is True and new_caps.get(key) is False
        ]
        if new_caps != old_caps:
            device.capabilities = new_caps
            changed.append("capabilities")
    device.last_seen_at = ctx.now
    if changed:
        device.revision += 1
        await ctx.session.flush()
        await record_changes(ctx, [(SyncEntity.DEVICE, device.id, device.revision)])
        await audit(
            ctx,
            "device.updated",
            f"Device '{device.name}' updated ({', '.join(changed)})",
            category="device",
            subject_device_id=device.id,
            details={"fields": changed},
        )
    if lost:
        await audit(
            ctx,
            "device.permission_revoked",
            f"Device '{device.name}' lost permissions: {', '.join(lost)}",
            category="error",
            subject_device_id=device.id,
            details={"permissions": lost},
        )
    if "timezone" in changed and device.is_primary and data.timezone:
        await _follow_device_timezone(ctx, user, data.timezone)
    return device_view(device, ctx.now)


async def rotate_device_token(ctx: ServiceContext) -> DeviceRegistered:
    await ctx.lock_user()
    device = await _own_device(ctx)
    raw, expires_at = await _issue_device_token(ctx, device)
    await audit(
        ctx,
        "device.token_rotated",
        f"Device '{device.name}' rotated its credential",
        category="security",
        subject_device_id=device.id,
    )
    return DeviceRegistered(
        device=device_view(device, ctx.now), device_token=raw, token_expires_at=expires_at
    )


async def list_devices(ctx: ServiceContext, *, include_inactive: bool = False) -> list[DeviceView]:
    ctx.principal.require(Scope.DEVICES_READ)
    query = select(Device).where(Device.user_id == ctx.user_id)
    if not include_inactive:
        query = query.where(Device.status == DeviceStatus.ACTIVE.value)
    rows = (await ctx.session.execute(query.order_by(Device.created_at, Device.id))).scalars()
    return [device_view(device, ctx.now) for device in rows]


async def _load_device(ctx: ServiceContext, device_id: uuid.UUID) -> Device:
    device = (
        await ctx.session.execute(
            select(Device)
            .where(Device.id == device_id, Device.user_id == ctx.user_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if device is None:
        raise NotFound("Device not found", details={"device_id": str(device_id)})
    return device


async def update_device(
    ctx: ServiceContext, device_id: uuid.UUID, data: DeviceUpdate
) -> DeviceView:
    ctx.principal.require(Scope.DEVICES_MANAGE)
    await ctx.lock_user()
    device = await _load_device(ctx, device_id)
    if device.status != DeviceStatus.ACTIVE.value:
        raise ValidationFailed("Deactivated devices cannot be modified")
    changes: list[tuple[SyncEntity, uuid.UUID, int]] = []
    fields: list[str] = []
    if data.is_primary and not device.is_primary:
        others = list(
            (
                await ctx.session.execute(
                    select(Device).where(
                        Device.user_id == ctx.user_id,
                        Device.is_primary.is_(True),
                        Device.id != device.id,
                    )
                )
            ).scalars()
        )
        for other in others:
            other.is_primary = False
            other.revision += 1
            changes.append((SyncEntity.DEVICE, other.id, other.revision))
        await ctx.session.flush()
        device.is_primary = True
        fields.append("is_primary")
    if data.name is not None and data.name != device.name:
        device.name = data.name
        fields.append("name")
    if data.mode_settings is not None:
        device.mode_settings = data.mode_settings.model_dump()
        fields.append("mode_settings")
    if fields:
        device.revision += 1
        changes.append((SyncEntity.DEVICE, device.id, device.revision))
        await ctx.session.flush()
        await record_changes(ctx, changes)
        await audit(
            ctx,
            "device.preferences_updated",
            f"Device '{device.name}' updated ({', '.join(fields)})",
            category="device",
            subject_device_id=device.id,
            details={"fields": fields, "mode_settings": device.mode_settings},
        )
    return device_view(device, ctx.now)


async def deactivate_device(ctx: ServiceContext, device_id: uuid.UUID) -> DeviceView:
    if ctx.principal.device_id != device_id:
        ctx.principal.require(Scope.DEVICES_MANAGE)
    await ctx.lock_user()
    device = await _load_device(ctx, device_id)
    if device.status == DeviceStatus.DEACTIVATED.value:
        return device_view(device, ctx.now)
    was_primary = device.is_primary
    device.status = DeviceStatus.DEACTIVATED.value
    device.deactivated_at = ctx.now
    device.is_primary = False
    device.push_token = None
    device.push_token_status = PushTokenStatus.MISSING.value
    device.revision += 1
    await ctx.session.execute(
        update(AccessToken)
        .where(AccessToken.device_id == device.id, AccessToken.revoked_at.is_(None))
        .values(revoked_at=ctx.now)
    )
    await ctx.session.flush()
    changes = [(SyncEntity.DEVICE, device.id, device.revision)]
    promoted = None
    if was_primary:
        promoted = (
            await ctx.session.execute(
                select(Device)
                .where(Device.user_id == ctx.user_id, Device.status == DeviceStatus.ACTIVE.value)
                .order_by(Device.last_seen_at.desc().nulls_last(), Device.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if promoted is not None:
            promoted.is_primary = True
            promoted.revision += 1
            changes.append((SyncEntity.DEVICE, promoted.id, promoted.revision))
    await ctx.session.flush()
    await record_changes(ctx, changes)
    await audit(
        ctx,
        "device.deactivated",
        f"Device '{device.name}' removed"
        + (f"; '{promoted.name}' is now primary" if promoted else ""),
        category="device",
        subject_device_id=device.id,
    )
    return device_view(device, ctx.now)


async def record_receipts(ctx: ServiceContext, batch: DeviceReceiptBatch) -> ReceiptResult:
    user = await ctx.lock_user()
    device = await _own_device(ctx)
    device.last_seen_at = ctx.now
    accepted = ignored = 0
    fired: list[ReminderOccurrence] = []
    ids = {receipt.occurrence_id for receipt in batch.receipts}
    occurrences = {
        o.id: o
        for o in (
            await ctx.session.execute(
                select(ReminderOccurrence)
                .where(ReminderOccurrence.id.in_(ids), ReminderOccurrence.user_id == ctx.user_id)
                .with_for_update()
            )
        ).scalars()
    }
    deliveries = {
        d.occurrence_id: d
        for d in (
            await ctx.session.execute(
                select(DeviceDelivery).where(
                    DeviceDelivery.occurrence_id.in_(ids), DeviceDelivery.device_id == device.id
                )
            )
        ).scalars()
    }
    for receipt in batch.receipts:
        occurrence = occurrences.get(receipt.occurrence_id)
        if occurrence is None or receipt.occurrence_version != occurrence.version:
            # A delayed old receipt must not clear a newer local schedule or mark
            # a snoozed/cancelled occurrence displayed.
            ignored += 1
            continue
        if receipt.kind in (ReceiptKind.SCHEDULED, ReceiptKind.DISPLAYED) and (
            occurrence.state
            not in {
                OccurrenceState.SCHEDULED.value,
                OccurrenceState.SNOOZED.value,
                OccurrenceState.FIRED.value,
            }
            or (
                receipt.kind is ReceiptKind.DISPLAYED
                and occurrence.scheduled_at > ctx.now + timedelta(seconds=90)
            )
        ):
            ignored += 1
            continue
        at = receipt.at if receipt.at.tzinfo else receipt.at.replace(tzinfo=ctx.now.tzinfo)
        at = min(at, ctx.now)
        delivery = deliveries.get(occurrence.id)
        if delivery is None:
            delivery = DeviceDelivery(
                occurrence_id=occurrence.id,
                device_id=device.id,
                user_id=ctx.user_id,
                state=DeliveryState.PENDING.value,
                push_attempts=0,
            )
            ctx.session.add(delivery)
            deliveries[occurrence.id] = delivery
        reset_delivery_version(delivery, receipt.occurrence_version)
        kind = receipt.kind
        if kind is ReceiptKind.RECEIVED:
            delivery.device_received_at = delivery.device_received_at or at
            advance_delivery(delivery, DeliveryState.DEVICE_RECEIVED)
        elif kind is ReceiptKind.SCHEDULED:
            if receipt.occurrence_version < (delivery.occurrence_version or 0):
                ignored += 1
                continue
            delivery.scheduled_locally_at = at
            delivery.occurrence_version = receipt.occurrence_version
            delivery.device_received_at = delivery.device_received_at or at
            advance_delivery(delivery, DeliveryState.SCHEDULED_LOCALLY)
        elif kind is ReceiptKind.UNSCHEDULED:
            delivery.scheduled_locally_at = None
            delivery.occurrence_version = receipt.occurrence_version
            if delivery.state == DeliveryState.SCHEDULED_LOCALLY.value:
                delivery.state = DeliveryState.DEVICE_RECEIVED.value
        elif kind is ReceiptKind.DISPLAYED:
            delivery.displayed_at = at
            delivery.device_received_at = delivery.device_received_at or at
            advance_delivery(delivery, DeliveryState.DISPLAYED)
            state = OccurrenceState(occurrence.state)
            due = occurrence.scheduled_at <= ctx.now + timedelta(seconds=90)
            if state is OccurrenceState.SCHEDULED or (state is OccurrenceState.SNOOZED and due):
                # Like the due handler, firing does not bump the version that other
                # devices' local-schedule receipts are compared against.
                occurrence.state = OccurrenceState.FIRED.value
                occurrence.fired_at = at
                occurrence.snoozed_until = None
                fired.append(occurrence)
            elif state is OccurrenceState.FIRED and occurrence.fired_at is None:
                occurrence.fired_at = at
        elif kind is ReceiptKind.DISMISSED:
            delivery.dismissed_at = at
            advance_delivery(delivery, DeliveryState.DISMISSED)
        elif kind in (ReceiptKind.FAILED, ReceiptKind.SKIPPED_LATE):
            delivery.failed_at = at
            delivery.failure_code = (
                "skipped_late" if kind is ReceiptKind.SKIPPED_LATE else "device_failure"
            )
            delivery.failure_detail = receipt.detail
            if (
                DELIVERY_RANK[DeliveryState(delivery.state)]
                < DELIVERY_RANK[DeliveryState.DISPLAYED]
            ):
                delivery.state = DeliveryState.FAILED.value
            reminder = await ctx.session.get(Reminder, occurrence.reminder_id)
            await audit(
                ctx,
                "delivery.device_failed" if kind is ReceiptKind.FAILED else "delivery.skipped_late",
                f"'{device.name}' could not alert for '{reminder.title if reminder else '?'}'"
                + (f": {receipt.detail}" if receipt.detail else ""),
                category="error" if kind is ReceiptKind.FAILED else "delivery",
                reminder_id=occurrence.reminder_id,
                occurrence_id=occurrence.id,
                subject_device_id=device.id,
            )
        accepted += 1
    await ctx.session.flush()
    for occurrence in fired:
        reminder = await ctx.session.get(Reminder, occurrence.reminder_id)
        if reminder is not None:
            await audit(
                ctx,
                "occurrence.fired",
                f"'{reminder.title}' alerted on '{device.name}'",
                category="delivery",
                reminder_id=reminder.id,
                occurrence_id=occurrence.id,
                subject_device_id=device.id,
            )
            await refresh_reminder(ctx, reminder, user)
    return ReceiptResult(accepted=accepted, ignored=ignored)


async def touch(ctx: ServiceContext) -> Device:
    device = await _own_device(ctx)
    if device.last_seen_at is None or ctx.now - device.last_seen_at > timedelta(seconds=60):
        device.last_seen_at = ctx.now
    return device
