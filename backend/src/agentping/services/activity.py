"""Paginated, filterable activity history from the append-only audit trail."""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import or_, select

from agentping.auth.principal import Scope
from agentping.db.models import AgentClient, AuditEvent, Device
from agentping.domain.enums import ActorKind
from agentping.domain.errors import ValidationFailed
from agentping.domain.schemas import ActivityEvent, ActorView, Page
from agentping.services.runtime import ServiceContext

CATEGORIES = {
    "reminder",
    "notification",
    "action",
    "delivery",
    "device",
    "settings",
    "security",
    "agent",
    "error",
}


async def list_activity(
    ctx: ServiceContext,
    *,
    categories: list[str] | None = None,
    reminder_id: uuid.UUID | None = None,
    device_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    event_type: str | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    cursor: str | None = None,
    limit: int = 50,
) -> Page[ActivityEvent]:
    ctx.principal.require(Scope.ACTIVITY_READ)
    limit = max(1, min(limit, 200))
    conditions = [AuditEvent.user_id == ctx.user_id]
    if categories:
        unknown = set(categories) - CATEGORIES
        if unknown:
            raise ValidationFailed(
                f"Unknown categories: {', '.join(sorted(unknown))}",
                details={"allowed": sorted(CATEGORIES)},
            )
        conditions.append(AuditEvent.category.in_(categories))
    if reminder_id:
        conditions.append(AuditEvent.reminder_id == reminder_id)
    if device_id:
        conditions.append(
            or_(AuditEvent.device_id == device_id, AuditEvent.subject_device_id == device_id)
        )
    if agent_id:
        conditions.append(AuditEvent.agent_client_id == agent_id)
    if event_type:
        conditions.append(AuditEvent.event_type.startswith(event_type))
    if since:
        conditions.append(AuditEvent.occurred_at >= since)
    if until:
        conditions.append(AuditEvent.occurred_at < until)
    if cursor:
        try:
            conditions.append(AuditEvent.id < int(cursor))
        except ValueError as error:
            raise ValidationFailed("Invalid pagination cursor") from error
    rows = list(
        (
            await ctx.session.execute(
                select(AuditEvent)
                .where(*conditions)
                .order_by(AuditEvent.id.desc())
                .limit(limit + 1)
            )
        ).scalars()
    )
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        next_cursor = str(rows[-1].id)
    agent_ids = {r.agent_client_id for r in rows if r.agent_client_id}
    device_ids = {r.device_id for r in rows if r.device_id}
    agents: dict[uuid.UUID, str] = {}
    devices: dict[uuid.UUID, str] = {}
    if agent_ids:
        agents = dict(
            (
                await ctx.session.execute(
                    select(AgentClient.id, AgentClient.name).where(AgentClient.id.in_(agent_ids))
                )
            ).all()
        )
    if device_ids:
        devices = dict(
            (
                await ctx.session.execute(
                    select(Device.id, Device.name).where(Device.id.in_(device_ids))
                )
            ).all()
        )
    items = [
        ActivityEvent(
            id=row.id,
            occurred_at=row.occurred_at,
            event_type=row.event_type,
            category=row.category,
            actor=ActorView(
                kind=ActorKind(row.actor_kind),
                agent_client_id=row.agent_client_id,
                agent_name=agents.get(row.agent_client_id) if row.agent_client_id else None,
                device_id=row.device_id,
                device_name=devices.get(row.device_id) if row.device_id else None,
            ),
            reminder_id=row.reminder_id,
            occurrence_id=row.occurrence_id,
            device_id=row.subject_device_id,
            summary=row.summary,
            details=row.details or {},
        )
        for row in rows
    ]
    return Page[ActivityEvent](items=items, next_cursor=next_cursor)
