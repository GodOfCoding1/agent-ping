"""Pydantic schemas shared by the REST API and MCP tools."""

from __future__ import annotations

import uuid
from datetime import datetime, time
from typing import Annotated, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from agentping.domain.enums import (
    ActionKind,
    ActionResult,
    ActionSource,
    ActorKind,
    DeliveryMode,
    DeliveryState,
    LockScreenVisibility,
    MonthlyDayPolicy,
    OccurrenceState,
    PushContent,
    PushTokenKind,
    QuietHoursBehavior,
    ReceiptKind,
    ReminderStatus,
    TargetStrategy,
    TimezoneMode,
)

Title = Annotated[str, Field(min_length=1, max_length=200)]
Body = Annotated[str, Field(max_length=4000)]
IdempotencyKey = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9._:\-]+$")]


def _valid_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(f"Unknown IANA timezone {value!r} (e.g. 'Asia/Kolkata')") from error
    return value


TimezoneName = Annotated[
    str,
    Field(min_length=1, max_length=64, examples=["Asia/Kolkata"]),
    AfterValidator(_valid_timezone),
]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OutputModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Schedules


class OnceSchedule(StrictModel):
    """A single occurrence at an absolute instant."""

    kind: Literal["once"] = "once"
    at: datetime | None = Field(
        default=None,
        description="Absolute time WITH a UTC offset, e.g. '2026-10-29T09:00:00+05:30' or "
        "'2026-10-29T03:30:00Z'. Use either 'at' or 'local_datetime'.",
    )
    local_datetime: datetime | None = Field(
        default=None,
        description="Local wall-clock time WITHOUT offset, e.g. '2026-10-29T09:00:00'. "
        "Requires 'timezone' (or uses the user's home timezone).",
    )
    timezone: TimezoneName | None = Field(
        default=None,
        description="IANA timezone such as 'Asia/Kolkata'. Defaults to the user's timezone.",
    )

    @model_validator(mode="after")
    def _one_time_source(self) -> OnceSchedule:
        if (self.at is None) == (self.local_datetime is None):
            raise ValueError("Provide exactly one of 'at' or 'local_datetime'")
        return self


class RecurringSchedule(StrictModel):
    """A recurring schedule defined by an RFC 5545 RRULE over local wall-clock times."""

    kind: Literal["recurring"] = "recurring"
    start_local: datetime = Field(
        description="First local wall-clock occurrence WITHOUT offset (acts as DTSTART), "
        "e.g. '2026-10-29T07:30:00'."
    )
    timezone: TimezoneName | None = Field(
        default=None,
        description="IANA timezone. Required for timezone_mode='fixed' unless the user's "
        "timezone should be used.",
    )
    timezone_mode: TimezoneMode = Field(
        default=TimezoneMode.FIXED,
        description="'fixed' keeps the given IANA timezone forever. 'floating' follows the "
        "user's current home timezone (e.g. when travelling).",
    )
    rrule: str = Field(
        min_length=6,
        max_length=500,
        description="RFC 5545 RRULE without DTSTART, e.g. 'FREQ=DAILY', "
        "'FREQ=WEEKLY;BYDAY=MO,WE,FR', 'FREQ=MONTHLY;BYMONTHDAY=1', "
        "'FREQ=MONTHLY;BYDAY=-1FR'.",
    )
    until: datetime | None = Field(
        default=None,
        description="Optional inclusive end. With offset = absolute; without offset = local.",
    )
    count: int | None = Field(default=None, ge=1, le=10_000)
    monthly_day_policy: MonthlyDayPolicy = Field(
        default=MonthlyDayPolicy.SKIP,
        description="For days 29-31 or Feb 29: 'skip' months lacking the day (RFC 5545) or "
        "'clamp' to the last day of the month.",
    )


Schedule = Annotated[OnceSchedule | RecurringSchedule, Field(discriminator="kind")]


class TargetSelection(StrictModel):
    strategy: TargetStrategy = Field(
        description="'primary' = the user's primary device; 'all' = every active device that "
        "accepts this mode; 'devices' = only device_ids."
    )
    device_ids: list[uuid.UUID] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _ids_match_strategy(self) -> TargetSelection:
        if self.strategy is TargetStrategy.DEVICES and not self.device_ids:
            raise ValueError("strategy 'devices' requires at least one device_id")
        if self.strategy is not TargetStrategy.DEVICES and self.device_ids:
            raise ValueError("device_ids are only allowed with strategy 'devices'")
        return self


class SnoozePolicy(StrictModel):
    default_minutes: int = Field(default=10, ge=1, le=720)
    max_snoozes: int = Field(default=5, ge=0, le=20)
    allowed_minutes: list[int] = Field(
        default_factory=lambda: [5, 10, 15, 30, 60],
        max_length=8,
        description="Snooze choices shown on the notification. Explicit durations of "
        "1-720 minutes are still accepted from the app, API and agents.",
    )

    @field_validator("allowed_minutes")
    @classmethod
    def _bounded(cls, value: list[int]) -> list[int]:
        if any(not 1 <= minutes <= 720 for minutes in value):
            raise ValueError("allowed_minutes values must be between 1 and 720")
        return sorted(set(value))


class EscalationPolicy(StrictModel):
    """Bounded repeat alerts until acknowledged."""

    enabled: bool = True
    interval_seconds: int = Field(default=300, ge=60, le=3600)
    max_repeats: int = Field(default=3, ge=0, le=12)
    max_duration_seconds: int = Field(default=3600, ge=60, le=6 * 3600)

    @model_validator(mode="after")
    def _bounded(self) -> EscalationPolicy:
        if self.enabled and self.interval_seconds * self.max_repeats > self.max_duration_seconds:
            raise ValueError("interval_seconds * max_repeats must not exceed max_duration_seconds")
        return self


class NotificationButton(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]{1,32}$")
    label: str = Field(min_length=1, max_length=40)
    kind: Literal["acknowledge", "snooze", "skip", "open"]
    snooze_minutes: int | None = Field(default=None, ge=1, le=720)


class ReminderOptions(StrictModel):
    targets: TargetSelection | None = Field(
        default=None,
        description="Defaults: normal/persistent -> all devices; alarm/critical -> primary.",
    )
    requires_ack: bool | None = Field(
        default=None, description="Defaults to true for persistent, alarm and critical."
    )
    snooze: SnoozePolicy | None = None
    escalation: EscalationPolicy | None = Field(
        default=None, description="Repeat alerts until acknowledged (persistent/critical)."
    )
    expire_after_minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)
    late_grace_minutes: int | None = Field(
        default=None,
        ge=0,
        le=24 * 60,
        description="How late a device may still alert if it was offline at the due time.",
    )
    respect_quiet_hours: bool | None = None
    actions: list[NotificationButton] = Field(default_factory=list, max_length=3)


class ReminderCreate(ReminderOptions):
    title: Title
    body: Body = ""
    delivery_mode: DeliveryMode = DeliveryMode.NORMAL
    schedule: Schedule
    idempotency_key: IdempotencyKey | None = None


class ReminderUpdate(StrictModel):
    expected_revision: int = Field(ge=1, description="Current revision (optimistic concurrency).")
    title: Title | None = None
    body: Body | None = None
    delivery_mode: DeliveryMode | None = None
    schedule: Schedule | None = None
    targets: TargetSelection | None = None
    requires_ack: bool | None = None
    snooze: SnoozePolicy | None = None
    escalation: EscalationPolicy | None = None
    expire_after_minutes: int | None = Field(default=None, ge=1, le=7 * 24 * 60)
    late_grace_minutes: int | None = Field(default=None, ge=0, le=24 * 60)
    respect_quiet_hours: bool | None = None
    actions: list[NotificationButton] | None = Field(default=None, max_length=3)
    status: Literal["active", "paused"] | None = Field(
        default=None, description="'paused' suspends future occurrences; 'active' resumes."
    )


class SendNotification(StrictModel):
    title: Title
    body: Body = ""
    delivery_mode: DeliveryMode = DeliveryMode.NORMAL
    targets: TargetSelection | None = Field(
        default=None, description="Defaults to all devices (normal) or primary (others)."
    )
    ttl_seconds: int | None = Field(
        default=None,
        ge=30,
        le=7 * 24 * 3600,
        description="Drop the notification if it cannot be delivered within this time.",
    )
    requires_ack: bool | None = None
    actions: list[NotificationButton] = Field(default_factory=list, max_length=3)
    idempotency_key: IdempotencyKey | None = None


# ---------------------------------------------------------------------------
# Occurrence actions


class AcknowledgeRequest(StrictModel):
    client_action_id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    source: ActionSource | None = None
    occurred_at: datetime | None = None
    note: str | None = Field(default=None, max_length=500)


class SnoozeRequest(StrictModel):
    minutes: int | None = Field(default=None, ge=1, le=720)
    client_action_id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    source: ActionSource | None = None
    occurred_at: datetime | None = None


class SkipRequest(StrictModel):
    reason: str = Field(min_length=1, max_length=500)
    client_action_id: Annotated[str, Field(min_length=1, max_length=100)] | None = None
    source: ActionSource | None = None
    occurred_at: datetime | None = None


class DeviceAction(StrictModel):
    client_action_id: str = Field(min_length=1, max_length=100)
    occurrence_id: uuid.UUID
    action: ActionKind
    occurred_at: datetime
    source: ActionSource = ActionSource.DEVICE_NOTIFICATION
    snooze_minutes: int | None = Field(default=None, ge=1, le=720)
    reason: str | None = Field(default=None, max_length=500)


class DeviceActionBatch(StrictModel):
    actions: list[DeviceAction] = Field(min_length=1, max_length=200)


class DeviceReceipt(StrictModel):
    occurrence_id: uuid.UUID
    kind: ReceiptKind
    occurrence_version: int = Field(ge=1)
    at: datetime
    detail: str | None = Field(default=None, max_length=500)


class DeviceReceiptBatch(StrictModel):
    receipts: list[DeviceReceipt] = Field(min_length=1, max_length=200)


# ---------------------------------------------------------------------------
# Devices


class ChannelState(StrictModel):
    enabled: bool = True
    importance: int | None = Field(default=None, ge=0, le=5)
    bypass_dnd: bool | None = None


class DeviceCapabilities(StrictModel):
    sdk_int: int | None = Field(default=None, ge=21, le=100)
    notifications_permitted: bool | None = None
    exact_alarms_permitted: bool | None = None
    full_screen_intent_permitted: bool | None = None
    dnd_access_granted: bool | None = None
    ignoring_battery_optimizations: bool | None = None
    background_restricted: bool | None = None
    wear_paired: bool | None = None
    channels: dict[DeliveryMode, ChannelState] = Field(default_factory=dict)


class ModeSettings(StrictModel):
    normal: bool = True
    persistent: bool = True
    alarm: bool = True
    critical: bool = True


class DeviceRegister(StrictModel):
    pairing_code: str | None = Field(default=None, min_length=6, max_length=64)
    installation_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:\-]+$")
    name: str = Field(min_length=1, max_length=120)
    model: str = Field(default="", max_length=120)
    os_version: str = Field(default="", max_length=40)
    app_version: str = Field(default="", max_length=40)
    timezone: TimezoneName | None = None
    push_token: str | None = Field(
        default=None,
        min_length=20,
        max_length=4096,
        description="FCM target: the Firebase Installation ID delivered to onRegistered(), "
        "or a legacy registration token (set push_token_kind).",
    )
    push_token_kind: PushTokenKind = PushTokenKind.FID
    capabilities: DeviceCapabilities = Field(default_factory=DeviceCapabilities)


class DeviceSelfUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    app_version: str | None = Field(default=None, max_length=40)
    os_version: str | None = Field(default=None, max_length=40)
    timezone: TimezoneName | None = None
    push_token: str | None = Field(default=None, min_length=20, max_length=4096)
    push_token_kind: PushTokenKind = PushTokenKind.FID
    capabilities: DeviceCapabilities | None = None


class DeviceUpdate(StrictModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    is_primary: Literal[True] | None = Field(
        default=None, description="Set true to make this the primary ringing device."
    )
    mode_settings: ModeSettings | None = None


class DeviceHealth(BaseModel):
    state: Literal["healthy", "degraded", "stale", "unreachable", "deactivated"]
    issues: list[str]


class DeviceView(OutputModel):
    id: uuid.UUID
    name: str
    installation_id: str
    platform: str
    model: str
    os_version: str
    app_version: str
    timezone: str | None
    is_primary: bool
    status: str
    push_token_status: str
    push_token_kind: str
    capabilities: dict[str, object]
    mode_settings: ModeSettings
    last_sync_at: datetime | None
    last_seen_at: datetime | None
    created_at: datetime
    revision: int
    health: DeviceHealth


class DeviceRegistered(BaseModel):
    device: DeviceView
    device_token: str | None = Field(
        default=None, description="Shown once. Store securely (Android Keystore)."
    )
    token_expires_at: datetime | None = None


class PairingCodeCreated(BaseModel):
    code: str
    expires_at: datetime


# ---------------------------------------------------------------------------
# Views


class AgentRef(BaseModel):
    id: uuid.UUID
    name: str


class TargetView(BaseModel):
    strategy: TargetStrategy
    device_ids: list[uuid.UUID]
    resolved_device_ids: list[uuid.UUID]


class ReminderView(BaseModel):
    id: uuid.UUID
    title: str
    body: str
    delivery_mode: DeliveryMode
    status: ReminderStatus
    revision: int
    schedule: dict[str, object]
    timezone: str
    timezone_mode: TimezoneMode
    targets: TargetView
    requires_ack: bool
    snooze: SnoozePolicy
    escalation: EscalationPolicy
    expire_after_minutes: int
    late_grace_minutes: int
    respect_quiet_hours: bool
    actions: list[NotificationButton]
    source_agent: AgentRef | None
    created_by: ActorKind
    next_occurrence_at: datetime | None
    created_at: datetime
    updated_at: datetime
    paused_at: datetime | None
    cancelled_at: datetime | None
    completed_at: datetime | None


class OccurrenceView(OutputModel):
    id: uuid.UUID
    reminder_id: uuid.UUID
    reminder_revision: int
    version: int
    original_at: datetime
    scheduled_at: datetime
    local_wall: str
    state: OccurrenceState
    expires_at: datetime
    fired_at: datetime | None
    acknowledged_at: datetime | None
    ack_source: str | None
    snoozed_until: datetime | None
    snooze_count: int
    skipped_at: datetime | None
    skip_reason: str | None
    missed_at: datetime | None
    cancelled_at: datetime | None
    cancel_reason: str | None


class DeliveryView(OutputModel):
    device_id: uuid.UUID
    state: DeliveryState
    provider: str | None
    push_attempts: int
    provider_accepted_at: datetime | None
    device_received_at: datetime | None
    scheduled_locally_at: datetime | None
    displayed_at: datetime | None
    dismissed_at: datetime | None
    failed_at: datetime | None
    failure_code: str | None


class OccurrenceDetail(OccurrenceView):
    deliveries: list[DeliveryView] = Field(default_factory=list)


class ReminderDetail(BaseModel):
    reminder: ReminderView
    upcoming: list[OccurrenceView]
    recent: list[OccurrenceDetail]


class OccurrenceListItem(OccurrenceView):
    title: str
    delivery_mode: DeliveryMode
    requires_ack: bool
    reminder_status: ReminderStatus


class Notice(BaseModel):
    code: str
    message: str
    device_id: uuid.UUID | None = None


class CreateReminderResult(BaseModel):
    reminder_id: uuid.UUID
    revision: int
    status: ReminderStatus
    next_occurrence: OccurrenceView | None
    sync_status: Literal["sync_queued", "no_target_devices"]
    target_device_ids: list[uuid.UUID]
    warnings: list[Notice]
    permissions_required: list[Notice]
    idempotent_replay: bool = False


class UpdateReminderResult(BaseModel):
    reminder: ReminderView
    next_occurrence: OccurrenceView | None
    sync_status: Literal["sync_queued", "no_target_devices"]
    warnings: list[Notice]


class CancelReminderResult(BaseModel):
    reminder_id: uuid.UUID
    revision: int
    status: ReminderStatus
    cancelled_occurrences: int
    sync_status: Literal["sync_queued", "no_target_devices"]


class TargetResult(BaseModel):
    device_id: uuid.UUID
    name: str


class RejectedTarget(BaseModel):
    device_id: uuid.UUID | None
    reason: str


class SendNotificationResult(BaseModel):
    notification_id: uuid.UUID
    occurrence_id: uuid.UUID
    accepted_targets: list[TargetResult]
    rejected_targets: list[RejectedTarget]
    dispatch_state: Literal["queued", "deferred_quiet_hours", "no_targets"]
    errors: list[str]
    warnings: list[Notice]
    note: str = (
        "'queued' means durably stored and scheduled for push. Provider acceptance and device "
        "receipt are tracked separately; see get_reminder."
    )
    idempotent_replay: bool = False


class OccurrenceActionResult(BaseModel):
    result: ActionResult
    occurrence: OccurrenceView
    reason: str | None = None


class DeviceActionResult(BaseModel):
    client_action_id: str
    result: ActionResult
    reason: str | None = None
    occurrence: OccurrenceView | None = None


class ReceiptResult(BaseModel):
    accepted: int
    ignored: int


class ActorView(BaseModel):
    kind: ActorKind
    agent_client_id: uuid.UUID | None = None
    agent_name: str | None = None
    device_id: uuid.UUID | None = None
    device_name: str | None = None


class ActivityEvent(BaseModel):
    id: int
    occurred_at: datetime
    event_type: str
    category: str
    actor: ActorView
    reminder_id: uuid.UUID | None
    occurrence_id: uuid.UUID | None
    device_id: uuid.UUID | None
    summary: str
    details: dict[str, object]


class Page[T](BaseModel):
    items: list[T]
    next_cursor: str | None = None


# ---------------------------------------------------------------------------
# Settings


class QuietHours(StrictModel):
    enabled: bool = False
    start: time = time(22, 0)
    end: time = time(7, 0)
    behavior: QuietHoursBehavior = QuietHoursBehavior.SILENT


class UserPreferences(StrictModel):
    quiet_hours: QuietHours = Field(default_factory=QuietHours)
    critical_alerts_enabled: bool = Field(
        default=False, description="User opt-in. Agents can never change this."
    )
    default_snooze_minutes: int = Field(default=10, ge=1, le=720)
    push_content: PushContent = PushContent.FULL
    lock_screen_visibility: LockScreenVisibility = LockScreenVisibility.PRIVATE
    alarm_sound: str = Field(default="default", max_length=200)
    vibrate: bool = True


class SettingsView(BaseModel):
    timezone: str
    timezone_source: Literal["manual", "primary_device"]
    preferences: UserPreferences
    revision: int


class SettingsUpdate(StrictModel):
    expected_revision: int | None = Field(default=None, ge=1)
    timezone: TimezoneName | None = None
    timezone_source: Literal["manual", "primary_device"] | None = None
    preferences: UserPreferences | None = None


# ---------------------------------------------------------------------------
# Agents


class AgentCreate(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=500)
    scopes: list[str] = Field(min_length=1, max_length=10)
    max_active_reminders: int | None = Field(default=None, ge=1, le=10_000)
    max_ringing_reminders: int | None = Field(default=None, ge=0, le=1000)
    max_sends_per_hour: int | None = Field(default=None, ge=0, le=10_000)
    token_ttl_days: int | None = Field(default=90, ge=1, le=3650)


class AgentUpdate(StrictModel):
    scopes: list[str] | None = Field(default=None, min_length=1, max_length=10)
    description: str | None = Field(default=None, max_length=500)
    max_active_reminders: int | None = Field(default=None, ge=1, le=10_000)
    max_ringing_reminders: int | None = Field(default=None, ge=0, le=1000)
    max_sends_per_hour: int | None = Field(default=None, ge=0, le=10_000)


class AgentTokenView(BaseModel):
    id: uuid.UUID
    token_prefix: str
    scopes: list[str]
    created_at: datetime
    expires_at: datetime | None
    revoked_at: datetime | None
    last_used_at: datetime | None


class AgentView(BaseModel):
    id: uuid.UUID
    name: str
    description: str
    kind: str
    scopes: list[str]
    status: str
    limits: dict[str, int]
    created_at: datetime
    revoked_at: datetime | None
    last_used_at: datetime | None
    tokens: list[AgentTokenView]


class AgentTokenIssued(BaseModel):
    agent: AgentView
    token: str = Field(description="Shown once. Use as 'Authorization: Bearer <token>'.")
    expires_at: datetime | None


# ---------------------------------------------------------------------------
# Sync


class SyncOccurrence(OccurrenceView):
    deliver_on_this_device: bool
    mode: DeliveryMode


class SyncResponse(BaseModel):
    cursor: int
    full: bool
    has_more: bool
    server_time: datetime
    horizon_end: datetime
    reminders: list[ReminderView]
    occurrences: list[SyncOccurrence]
    devices: list[DeviceView]
    settings: SettingsView | None
