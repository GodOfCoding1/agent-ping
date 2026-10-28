"""Cross-cutting persistence helpers: audit trail, sync change log, outbox and idempotency."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert

from agentping.db.models import AuditEvent, IdempotencyRecord, OutboxJob, SyncChange, User
from agentping.domain.enums import SyncEntity
from agentping.domain.errors import IdempotencyConflict
from agentping.services.runtime import ServiceContext

IDEMPOTENCY_TTL = timedelta(hours=48)


async def audit(
    ctx: ServiceContext,
    event_type: str,
    summary: str,
    *,
    category: str,
    reminder_id: uuid.UUID | None = None,
    occurrence_id: uuid.UUID | None = None,
    subject_device_id: uuid.UUID | None = None,
    details: dict[str, Any] | None = None,
    user_id: uuid.UUID | None = None,
) -> None:
    principal = ctx.principal
    ctx.session.add(
        AuditEvent(
            user_id=user_id or principal.user_id,
            event_type=event_type,
            category=category,
            actor_kind=principal.actor_kind.value,
            agent_client_id=principal.agent_client_id,
            device_id=principal.device_id,
            reminder_id=reminder_id,
            occurrence_id=occurrence_id,
            subject_device_id=subject_device_id,
            summary=summary[:500],
            details=details or {},
            request_id=ctx.request_id,
            occurred_at=ctx.now,
        )
    )


async def record_changes(
    ctx: ServiceContext, changes: Sequence[tuple[SyncEntity, uuid.UUID, int]]
) -> int | None:
    """Append ordered sync changes. Incrementing users.sync_seq row-locks the user, so
    sequence numbers are gap-free and commit in order per user."""
    if not changes:
        return None
    count = len(changes)
    last = (
        await ctx.session.execute(
            update(User)
            .where(User.id == ctx.user_id)
            .values(sync_seq=User.sync_seq + count)
            .returning(User.sync_seq)
        )
    ).scalar_one()
    first = last - count + 1
    ctx.session.add_all(
        SyncChange(
            user_id=ctx.user_id,
            seq=first + index,
            entity_type=entity.value,
            entity_id=entity_id,
            entity_version=version,
        )
        for index, (entity, entity_id, version) in enumerate(changes)
    )
    await enqueue(
        ctx,
        "push.sync_hint",
        {"user_id": str(ctx.user_id)},
        run_at=ctx.now + timedelta(seconds=2),
        dedupe_key=f"sync_hint:{ctx.user_id}",
    )
    return last


async def enqueue(
    ctx: ServiceContext,
    kind: str,
    payload: dict[str, Any],
    *,
    run_at: datetime | None = None,
    dedupe_key: str | None = None,
    max_attempts: int | None = None,
) -> None:
    statement = insert(OutboxJob).values(
        kind=kind,
        payload=payload,
        user_id=ctx.user_id,
        dedupe_key=dedupe_key,
        run_at=run_at or ctx.now,
        max_attempts=max_attempts or ctx.settings.worker_max_attempts,
        status="pending",
    )
    if dedupe_key is not None:
        statement = statement.on_conflict_do_nothing(
            index_elements=["dedupe_key"],
            index_where=text("dedupe_key IS NOT NULL AND status = 'pending'"),
        )
    await ctx.session.execute(statement)
    await ctx.notify_worker()


def request_fingerprint(payload: BaseModel | dict[str, Any]) -> str:
    data = (
        payload.model_dump(mode="json", exclude={"idempotency_key"})
        if isinstance(payload, BaseModel)
        else payload
    )
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


async def idempotent_lookup(
    ctx: ServiceContext, operation: str, key: str | None, fingerprint: str
) -> dict[str, Any] | None:
    if key is None:
        return None
    record = (
        await ctx.session.execute(
            select(IdempotencyRecord).where(
                IdempotencyRecord.user_id == ctx.user_id,
                IdempotencyRecord.actor_key == ctx.principal.actor_key,
                IdempotencyRecord.operation == operation,
                IdempotencyRecord.key == key,
                IdempotencyRecord.expires_at > ctx.now,
            )
        )
    ).scalar_one_or_none()
    if record is None:
        return None
    if record.request_hash != fingerprint:
        raise IdempotencyConflict(
            "This idempotency_key was already used with different parameters",
            details={"idempotency_key": key, "operation": operation},
        )
    return record.response


async def idempotent_store(
    ctx: ServiceContext, operation: str, key: str | None, fingerprint: str, response: BaseModel
) -> None:
    if key is None:
        return
    await ctx.session.execute(
        insert(IdempotencyRecord)
        .values(
            user_id=ctx.user_id,
            actor_key=ctx.principal.actor_key,
            operation=operation,
            key=key,
            request_hash=fingerprint,
            response=response.model_dump(mode="json"),
            expires_at=ctx.now + IDEMPOTENCY_TTL,
        )
        .on_conflict_do_update(
            constraint="uq_idempotency_records_scope",
            set_={
                "request_hash": fingerprint,
                "response": response.model_dump(mode="json"),
                "expires_at": ctx.now + IDEMPOTENCY_TTL,
                "created_at": ctx.now,
            },
        )
    )
