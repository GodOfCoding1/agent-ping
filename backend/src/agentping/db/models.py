"""SQLAlchemy ORM models. Alembic migrations are the source of truth for the schema."""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    MetaData,
    PrimaryKeyConstraint,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from agentping.domain import enums as e

NAMING = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)
    # Fetch server-generated values (e.g. updated_at) via RETURNING; async sessions cannot
    # lazy-load expired attributes.
    __mapper_args__ = {"eager_defaults": True}


def _enum_check(column: str, enum: type[StrEnum], name: str | None = None) -> CheckConstraint:
    values = ", ".join(f"'{member.value}'" for member in enum)
    return CheckConstraint(f"{column} IN ({values})", name=name or f"{column}_valid")


TZ = DateTime(timezone=True)


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created() -> Mapped[datetime]:
    return mapped_column(TZ, nullable=False, server_default=func.now())


def _updated() -> Mapped[datetime]:
    return mapped_column(TZ, nullable=False, server_default=func.now(), onupdate=func.now())


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str | None] = mapped_column(String(320))
    oidc_subject: Mapped[str | None] = mapped_column(String(255), unique=True)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="active", server_default="active"
    )
    display_name: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, default="UTC")
    timezone_source: Mapped[str] = mapped_column(String(20), nullable=False, default="manual")
    preferences: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    settings_revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    sync_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        CheckConstraint("timezone_source IN ('manual', 'primary_device')", name="tz_source_valid"),
        CheckConstraint("status IN ('active', 'suspended', 'deleted')", name="status_valid"),
    )


class UserIdentity(Base):
    __tablename__ = "user_identities"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    issuer: Mapped[str] = mapped_column(String(512), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint("issuer", "subject", name="uq_user_identities_issuer_subject"),
    )


class PortalLogin(Base):
    __tablename__ = "portal_logins"

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    encrypted_data: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZ, nullable=False, index=True)


class PortalSession(Base):
    __tablename__ = "portal_sessions"

    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    encrypted_data: Mapped[str] = mapped_column(Text, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(TZ, nullable=False, index=True)
    created_at: Mapped[datetime] = _created()


class UserQuota(Base):
    __tablename__ = "user_quotas"

    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(TZ, primary_key=True, index=True)
    requests: Mapped[int] = mapped_column(Integer, nullable=False, default=1)


class AgentClient(Base):
    __tablename__ = "agent_clients"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")
    kind: Mapped[str] = mapped_column(String(20), nullable=False, default="token")
    external_client_id: Mapped[str | None] = mapped_column(String(255))
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    limits: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    oauth_not_before: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_agent_clients_user_name"),
        Index(
            "uq_agent_clients_external",
            "user_id",
            "external_client_id",
            unique=True,
            postgresql_where=text("external_client_id IS NOT NULL"),
        ),
        _enum_check("status", e.AgentStatus),
        CheckConstraint("kind IN ('token', 'oidc', 'development')", name="kind_valid"),
    )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    installation_id: Mapped[str] = mapped_column(String(128), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    platform: Mapped[str] = mapped_column(String(20), nullable=False, default="android")
    model: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    os_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    app_version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    timezone: Mapped[str | None] = mapped_column(String(64))
    push_token: Mapped[str | None] = mapped_column(Text)
    push_token_kind: Mapped[str] = mapped_column(
        String(20), nullable=False, default="fid", server_default="fid"
    )
    push_token_status: Mapped[str] = mapped_column(String(20), nullable=False, default="missing")
    push_token_updated_at: Mapped[datetime | None] = mapped_column(TZ)
    capabilities: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    mode_settings: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    last_sync_at: Mapped[datetime | None] = mapped_column(TZ)
    last_sync_cursor: Mapped[int | None] = mapped_column(BigInteger)
    last_seen_at: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    deactivated_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        UniqueConstraint("user_id", "installation_id", name="uq_devices_user_installation"),
        Index(
            "uq_devices_one_primary",
            "user_id",
            unique=True,
            postgresql_where=text("is_primary AND status = 'active'"),
        ),
        Index("ix_devices_push_token", "push_token"),
        _enum_check("status", e.DeviceStatus),
        _enum_check("push_token_status", e.PushTokenStatus),
        _enum_check("push_token_kind", e.PushTokenKind),
    )


class AccessToken(Base):
    __tablename__ = "access_tokens"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    agent_client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_clients.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    token_prefix: Mapped[str] = mapped_column(String(16), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scopes: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, default=list)
    label: Mapped[str] = mapped_column(String(120), nullable=False, default="")
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime | None] = mapped_column(TZ)
    revoked_at: Mapped[datetime | None] = mapped_column(TZ)
    last_used_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        CheckConstraint(
            "(agent_client_id IS NULL) <> (device_id IS NULL)", name="exactly_one_owner"
        ),
        Index("ix_access_tokens_agent", "agent_client_id"),
        Index("ix_access_tokens_device", "device_id"),
    )


class DevicePairingCode(Base):
    __tablename__ = "device_pairing_codes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(TZ)
    used_by_device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False, default="")
    mode: Mapped[str] = mapped_column(String(20), nullable=False)
    schedule_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    schedule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    timezone_mode: Mapped[str] = mapped_column(String(20), nullable=False, default="fixed")
    rrule: Mapped[str | None] = mapped_column(Text)
    start_local: Mapped[datetime | None] = mapped_column(DateTime(timezone=False))
    first_at: Mapped[datetime | None] = mapped_column(TZ)
    until_at: Mapped[datetime | None] = mapped_column(TZ)
    occurrence_count: Mapped[int | None] = mapped_column(Integer)
    monthly_day_policy: Mapped[str] = mapped_column(String(10), nullable=False, default="skip")
    target_strategy: Mapped[str] = mapped_column(String(20), nullable=False)
    requires_ack: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    snooze_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    escalation_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    actions: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, server_default=text("'[]'::jsonb")
    )
    expire_after_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    late_grace_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    respect_quiet_hours: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    source_agent_client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_clients.id"))
    created_by_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    created_by_device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    next_occurrence_at: Mapped[datetime | None] = mapped_column(TZ)
    materialized_until: Mapped[datetime | None] = mapped_column(TZ)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    paused_at: Mapped[datetime | None] = mapped_column(TZ)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZ)
    completed_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        _enum_check("mode", e.DeliveryMode),
        _enum_check("schedule_kind", e.ScheduleKind),
        _enum_check("timezone_mode", e.TimezoneMode),
        _enum_check("target_strategy", e.TargetStrategy),
        _enum_check("status", e.ReminderStatus),
        _enum_check("monthly_day_policy", e.MonthlyDayPolicy),
        _enum_check("created_by_kind", e.ActorKind),
        CheckConstraint("revision >= 1", name="revision_positive"),
        CheckConstraint("char_length(title) >= 1", name="title_not_empty"),
        Index("ix_reminders_user_status_next", "user_id", "status", "next_occurrence_at"),
        Index("ix_reminders_user_created", "user_id", "created_at", "id"),
        Index("ix_reminders_source_agent", "source_agent_client_id"),
    )


class ReminderDeviceTarget(Base):
    __tablename__ = "reminder_device_targets"

    reminder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )

    __table_args__ = (
        PrimaryKeyConstraint("reminder_id", "device_id"),
        Index("ix_reminder_device_targets_device", "device_id"),
    )


class ReminderOccurrence(Base):
    __tablename__ = "reminder_occurrences"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    reminder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    reminder_revision: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    original_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    scheduled_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    local_wall: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="scheduled")
    expires_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    fired_at: Mapped[datetime | None] = mapped_column(TZ)
    acknowledged_at: Mapped[datetime | None] = mapped_column(TZ)
    ack_source: Mapped[str | None] = mapped_column(String(30))
    ack_device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    snoozed_until: Mapped[datetime | None] = mapped_column(TZ)
    snooze_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    skipped_at: Mapped[datetime | None] = mapped_column(TZ)
    skip_reason: Mapped[str | None] = mapped_column(String(500))
    missed_at: Mapped[datetime | None] = mapped_column(TZ)
    cancelled_at: Mapped[datetime | None] = mapped_column(TZ)
    cancel_reason: Mapped[str | None] = mapped_column(String(60))
    escalation_step: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("reminder_id", "original_at", name="uq_occurrences_reminder_original"),
        Index("ix_occurrences_user_state_scheduled", "user_id", "state", "scheduled_at"),
        Index("ix_occurrences_reminder_scheduled", "reminder_id", "scheduled_at"),
        _enum_check("state", e.OccurrenceState),
    )


class DeviceDelivery(Base):
    __tablename__ = "device_deliveries"

    id: Mapped[uuid.UUID] = _uuid_pk()
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("devices.id"), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    state: Mapped[str] = mapped_column(String(30), nullable=False, default="pending")
    occurrence_version: Mapped[int | None] = mapped_column(Integer)
    provider: Mapped[str | None] = mapped_column(String(20))
    provider_message_id: Mapped[str | None] = mapped_column(String(255))
    push_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_push_at: Mapped[datetime | None] = mapped_column(TZ)
    provider_accepted_at: Mapped[datetime | None] = mapped_column(TZ)
    device_received_at: Mapped[datetime | None] = mapped_column(TZ)
    scheduled_locally_at: Mapped[datetime | None] = mapped_column(TZ)
    displayed_at: Mapped[datetime | None] = mapped_column(TZ)
    dismissed_at: Mapped[datetime | None] = mapped_column(TZ)
    failed_at: Mapped[datetime | None] = mapped_column(TZ)
    failure_code: Mapped[str | None] = mapped_column(String(60))
    failure_detail: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()

    __table_args__ = (
        UniqueConstraint("occurrence_id", "device_id", name="uq_deliveries_occurrence_device"),
        Index("ix_deliveries_device_state", "device_id", "state"),
        _enum_check("state", e.DeliveryState),
    )


class NotificationAction(Base):
    __tablename__ = "notification_actions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    occurrence_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminder_occurrences.id", ondelete="CASCADE"), nullable=False
    )
    reminder_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reminders.id", ondelete="CASCADE"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_clients.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    occurred_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    received_at: Mapped[datetime] = _created()
    result: Mapped[str] = mapped_column(String(30), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )

    __table_args__ = (
        Index(
            "uq_notification_actions_dedupe",
            "user_id",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL"),
        ),
        Index("ix_notification_actions_occurrence", "occurrence_id"),
        _enum_check("action", e.ActionKind),
        _enum_check("source", e.ActionSource),
        _enum_check("result", e.ActionResult),
    )


class OutboxJob(Base):
    __tablename__ = "outbox_jobs"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id"))
    dedupe_key: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    run_at: Mapped[datetime] = mapped_column(TZ, nullable=False, server_default=func.now())
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=8)
    locked_by: Mapped[str | None] = mapped_column(String(120))
    locked_until: Mapped[datetime | None] = mapped_column(TZ)
    last_error: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = _created()
    updated_at: Mapped[datetime] = _updated()
    finished_at: Mapped[datetime | None] = mapped_column(TZ)

    __table_args__ = (
        Index(
            "uq_outbox_jobs_active_dedupe",
            "dedupe_key",
            unique=True,
            postgresql_where=text("dedupe_key IS NOT NULL AND status = 'pending'"),
        ),
        Index(
            "ix_outbox_jobs_ready",
            "run_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_outbox_jobs_lease",
            "locked_until",
            postgresql_where=text("status = 'running'"),
        ),
        _enum_check("status", e.JobStatus),
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    occurred_at: Mapped[datetime] = _created()
    event_type: Mapped[str] = mapped_column(String(60), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    actor_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_client_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("agent_clients.id"))
    device_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("devices.id"))
    reminder_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    occurrence_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    subject_device_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default=text("'{}'::jsonb")
    )
    request_id: Mapped[str | None] = mapped_column(String(64))

    __table_args__ = (
        Index("ix_audit_events_user_id", "user_id", "id"),
        Index("ix_audit_events_user_reminder", "user_id", "reminder_id", "id"),
        Index("ix_audit_events_user_category", "user_id", "category", "id"),
        _enum_check("actor_kind", e.ActorKind),
    )


class SyncChange(Base):
    __tablename__ = "sync_changes"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    entity_type: Mapped[str] = mapped_column(String(20), nullable=False)
    entity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    entity_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = _created()

    __table_args__ = (
        UniqueConstraint("user_id", "seq", name="uq_sync_changes_user_seq"),
        Index("ix_sync_changes_created", "created_at"),
        _enum_check("entity_type", e.SyncEntity),
    )


class IdempotencyRecord(Base):
    __tablename__ = "idempotency_records"

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id"), nullable=False)
    actor_key: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(60), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    response: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = _created()
    expires_at: Mapped[datetime] = mapped_column(TZ, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id", "actor_key", "operation", "key", name="uq_idempotency_records_scope"
        ),
        Index("ix_idempotency_records_expires", "expires_at"),
    )


class WorkerHeartbeat(Base):
    __tablename__ = "worker_heartbeats"

    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    version: Mapped[str] = mapped_column(String(40), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(TZ, nullable=False)
    last_batch_at: Mapped[datetime | None] = mapped_column(TZ)
    jobs_succeeded: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    jobs_failed: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
