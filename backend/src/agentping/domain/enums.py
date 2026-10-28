"""Enumerations shared by the database, REST API, MCP tools and worker."""

from enum import StrEnum


class DeliveryMode(StrEnum):
    NORMAL = "normal"
    PERSISTENT = "persistent"
    ALARM = "alarm"
    CRITICAL = "critical"

    @property
    def is_ringing(self) -> bool:
        return self in {DeliveryMode.ALARM, DeliveryMode.CRITICAL}

    @property
    def is_urgent(self) -> bool:
        return self is not DeliveryMode.NORMAL


class ScheduleKind(StrEnum):
    IMMEDIATE = "immediate"
    ONCE = "once"
    RECURRING = "recurring"


class TimezoneMode(StrEnum):
    FIXED = "fixed"
    FLOATING = "floating"


class TargetStrategy(StrEnum):
    PRIMARY = "primary"
    ALL = "all"
    DEVICES = "devices"


class ReminderStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class OccurrenceState(StrEnum):
    SCHEDULED = "scheduled"
    FIRED = "fired"
    SNOOZED = "snoozed"
    ACKNOWLEDGED = "acknowledged"
    SKIPPED = "skipped"
    MISSED = "missed"
    CANCELLED = "cancelled"
    SUPERSEDED = "superseded"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_STATES

    @property
    def is_pending(self) -> bool:
        return self in PENDING_STATES


TERMINAL_STATES = frozenset(
    {
        OccurrenceState.ACKNOWLEDGED,
        OccurrenceState.SKIPPED,
        OccurrenceState.MISSED,
        OccurrenceState.CANCELLED,
    }
)
PENDING_STATES = frozenset(
    {OccurrenceState.SCHEDULED, OccurrenceState.FIRED, OccurrenceState.SNOOZED}
)


class DeliveryState(StrEnum):
    PENDING = "pending"
    SUPPRESSED = "suppressed"
    PROVIDER_ACCEPTED = "provider_accepted"
    PROVIDER_FAILED = "provider_failed"
    DEVICE_RECEIVED = "device_received"
    SCHEDULED_LOCALLY = "scheduled_locally"
    DISPLAYED = "displayed"
    DISMISSED = "dismissed"
    FAILED = "failed"


class ReceiptKind(StrEnum):
    RECEIVED = "received"
    SCHEDULED = "scheduled"
    UNSCHEDULED = "unscheduled"
    DISPLAYED = "displayed"
    DISMISSED = "dismissed"
    SKIPPED_LATE = "skipped_late"
    FAILED = "failed"


class ActionKind(StrEnum):
    ACKNOWLEDGE = "acknowledge"
    SNOOZE = "snooze"
    SKIP = "skip"


class ActionSource(StrEnum):
    DEVICE_NOTIFICATION = "device_notification"
    DEVICE_APP = "device_app"
    WATCH = "watch"
    MCP = "mcp"
    API = "api"
    SYSTEM = "system"


class ActionResult(StrEnum):
    APPLIED = "applied"
    DUPLICATE = "duplicate"
    REJECTED_STALE = "rejected_stale"
    REJECTED_INVALID = "rejected_invalid"


class ActorKind(StrEnum):
    USER = "user"
    AGENT = "agent"
    DEVICE = "device"
    SYSTEM = "system"


class DeviceStatus(StrEnum):
    ACTIVE = "active"
    DEACTIVATED = "deactivated"


class PushTokenStatus(StrEnum):
    MISSING = "missing"
    VALID = "valid"
    INVALID = "invalid"


class PushTokenKind(StrEnum):
    """How FCM addresses the app instance: Firebase Installation ID or legacy token."""

    FID = "fid"
    REGISTRATION_TOKEN = "registration_token"


class JobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    DEAD = "dead"


class AgentStatus(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"


class QuietHoursBehavior(StrEnum):
    SILENT = "silent"
    DEFER = "defer"


class PushContent(StrEnum):
    FULL = "full"
    MINIMAL = "minimal"


class LockScreenVisibility(StrEnum):
    PUBLIC = "public"
    PRIVATE = "private"
    SECRET = "secret"


class MonthlyDayPolicy(StrEnum):
    SKIP = "skip"
    CLAMP = "clamp"


class SyncEntity(StrEnum):
    REMINDER = "reminder"
    OCCURRENCE = "occurrence"
    DEVICE = "device"
    SETTINGS = "settings"
