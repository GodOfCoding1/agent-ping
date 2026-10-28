"""Reminder, notification and occurrence endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Header, Query, status

from agentping.api.deps import Call
from agentping.api.errors import ERROR_RESPONSES
from agentping.domain.enums import ActionKind, DeliveryMode, OccurrenceState, ReminderStatus
from agentping.domain.errors import ValidationFailed
from agentping.domain.schemas import (
    AcknowledgeRequest,
    ActivityEvent,
    CancelReminderResult,
    CreateReminderResult,
    OccurrenceActionResult,
    OccurrenceDetail,
    OccurrenceListItem,
    Page,
    ReminderCreate,
    ReminderDetail,
    ReminderUpdate,
    ReminderView,
    SendNotification,
    SendNotificationResult,
    SkipRequest,
    SnoozeRequest,
    UpdateReminderResult,
)
from agentping.services import activity, reminders
from agentping.services import occurrences as occ
from agentping.services.runtime import ServiceContext
from agentping.services.views import occurrence_view

router = APIRouter(responses=ERROR_RESPONSES)


def _revision_from_if_match(if_match: str | None) -> int | None:
    if if_match is None:
        return None
    value = if_match.strip().removeprefix("W/").strip('"')
    if not value.isdigit():
        raise ValidationFailed('If-Match must contain the reminder revision, e.g. "3"')
    return int(value)


@router.post(
    "/reminders",
    response_model=CreateReminderResult,
    status_code=status.HTTP_201_CREATED,
    tags=["reminders"],
    summary="Create a one-time or recurring reminder",
)
async def create_reminder(
    body: ReminderCreate,
    call: Call,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> CreateReminderResult:
    if idempotency_key and not body.idempotency_key:
        body = body.model_copy(update={"idempotency_key": idempotency_key})
    return await call(reminders.create_reminder, body)


@router.get("/reminders", response_model=Page[ReminderView], tags=["reminders"])
async def list_reminders(
    call: Call,
    status_: Annotated[list[ReminderStatus] | None, Query(alias="status")] = None,
    mode: DeliveryMode | None = None,
    device_id: uuid.UUID | None = None,
    source_agent_id: uuid.UUID | None = None,
    due_after: datetime | None = None,
    due_before: datetime | None = None,
    q: Annotated[str | None, Query(max_length=200)] = None,
    include_notifications: bool = False,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[ReminderView]:
    return await call(
        reminders.list_reminders,
        statuses=status_,
        mode=mode,
        device_id=device_id,
        source_agent_id=source_agent_id,
        due_after=due_after,
        due_before=due_before,
        query=q,
        include_notifications=include_notifications,
        cursor=cursor,
        limit=limit,
    )


@router.get("/reminders/{reminder_id}", response_model=ReminderDetail, tags=["reminders"])
async def get_reminder(
    reminder_id: uuid.UUID,
    call: Call,
    upcoming_limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> ReminderDetail:
    return await call(reminders.get_reminder, reminder_id, upcoming_limit=upcoming_limit)


@router.patch(
    "/reminders/{reminder_id}",
    response_model=UpdateReminderResult,
    tags=["reminders"],
    summary="Partially update a reminder (optimistic concurrency via expected_revision/If-Match)",
)
async def update_reminder(
    reminder_id: uuid.UUID,
    body: ReminderUpdate,
    call: Call,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> UpdateReminderResult:
    revision = _revision_from_if_match(if_match)
    if revision is not None and revision != body.expected_revision:
        raise ValidationFailed("If-Match and expected_revision disagree")
    return await call(reminders.update_reminder, reminder_id, body)


@router.post(
    "/reminders/{reminder_id}/cancel", response_model=CancelReminderResult, tags=["reminders"]
)
async def cancel_reminder(
    reminder_id: uuid.UUID,
    call: Call,
    expected_revision: int | None = None,
    reason: Annotated[str | None, Query(max_length=500)] = None,
) -> CancelReminderResult:
    return await call(
        reminders.cancel_reminder, reminder_id, expected_revision=expected_revision, reason=reason
    )


@router.delete(
    "/reminders/{reminder_id}",
    response_model=CancelReminderResult,
    tags=["reminders"],
    summary="Cancel a reminder (reminders are retained for history, never hard-deleted)",
)
async def delete_reminder(
    reminder_id: uuid.UUID,
    call: Call,
    if_match: Annotated[str | None, Header(alias="If-Match")] = None,
) -> CancelReminderResult:
    return await call(
        reminders.cancel_reminder, reminder_id, expected_revision=_revision_from_if_match(if_match)
    )


@router.get(
    "/reminders/{reminder_id}/history", response_model=Page[ActivityEvent], tags=["reminders"]
)
async def reminder_history(
    reminder_id: uuid.UUID,
    call: Call,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[ActivityEvent]:
    return await call(activity.list_activity, reminder_id=reminder_id, cursor=cursor, limit=limit)


@router.post(
    "/notifications",
    response_model=SendNotificationResult,
    status_code=status.HTTP_202_ACCEPTED,
    tags=["notifications"],
    summary="Send a notification immediately",
)
async def send_notification(
    body: SendNotification,
    call: Call,
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> SendNotificationResult:
    if idempotency_key and not body.idempotency_key:
        body = body.model_copy(update={"idempotency_key": idempotency_key})
    return await call(reminders.send_notification, body)


@router.get("/occurrences", response_model=Page[OccurrenceListItem], tags=["occurrences"])
async def list_occurrences(
    call: Call,
    state: Annotated[list[OccurrenceState] | None, Query()] = None,
    start: Annotated[datetime | None, Query(alias="from")] = None,
    end: Annotated[datetime | None, Query(alias="to")] = None,
    reminder_id: uuid.UUID | None = None,
    cursor: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> Page[OccurrenceListItem]:
    return await call(
        occ.list_occurrences,
        states=state,
        start=start,
        end=end,
        reminder_id=reminder_id,
        cursor=cursor,
        limit=limit,
    )


@router.get("/occurrences/{occurrence_id}", response_model=OccurrenceDetail, tags=["occurrences"])
async def get_occurrence(occurrence_id: uuid.UUID, call: Call) -> OccurrenceDetail:
    return await call(occ.get_occurrence, occurrence_id)


async def _action(
    ctx: ServiceContext,
    occurrence_id: uuid.UUID,
    action: ActionKind,
    body: AcknowledgeRequest | SnoozeRequest | SkipRequest,
) -> OccurrenceActionResult:
    outcome = await occ.apply_action(
        ctx,
        occurrence_id,
        action,
        source=body.source,
        client_action_id=body.client_action_id,
        occurred_at=body.occurred_at,
        snooze_minutes=getattr(body, "minutes", None),
        reason=getattr(body, "reason", None),
        note=getattr(body, "note", None),
    )
    return OccurrenceActionResult(
        result=outcome.result, occurrence=occurrence_view(outcome.occurrence), reason=outcome.reason
    )


@router.post(
    "/occurrences/{occurrence_id}/acknowledge",
    response_model=OccurrenceActionResult,
    tags=["occurrences"],
)
async def acknowledge(
    occurrence_id: uuid.UUID, call: Call, body: AcknowledgeRequest | None = None
) -> OccurrenceActionResult:
    return await call(_action, occurrence_id, ActionKind.ACKNOWLEDGE, body or AcknowledgeRequest())


@router.post(
    "/occurrences/{occurrence_id}/snooze",
    response_model=OccurrenceActionResult,
    tags=["occurrences"],
)
async def snooze(
    occurrence_id: uuid.UUID, call: Call, body: SnoozeRequest | None = None
) -> OccurrenceActionResult:
    return await call(_action, occurrence_id, ActionKind.SNOOZE, body or SnoozeRequest())


@router.post(
    "/occurrences/{occurrence_id}/skip", response_model=OccurrenceActionResult, tags=["occurrences"]
)
async def skip(occurrence_id: uuid.UUID, body: SkipRequest, call: Call) -> OccurrenceActionResult:
    return await call(_action, occurrence_id, ActionKind.SKIP, body)
