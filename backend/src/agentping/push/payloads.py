"""FCM data-message payloads. All values are strings (FCM requirement); total < 4 KB.

Messages are data-only so the app controls channels, actions, de-duplication and
privacy. ``event_id`` is deterministic, letting devices drop repeated pushes.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from agentping.db.models import Reminder, ReminderOccurrence
from agentping.domain.enums import PushContent

PAYLOAD_VERSION = "1"
EVENT_NAMESPACE = uuid.UUID("2b0b7c43-6f0e-5d55-8f2a-9a4c3f1e7d21")
MAX_PUSH_BODY = 1000


def event_id(*parts: object) -> str:
    return str(uuid.uuid5(EVENT_NAMESPACE, "/".join(str(part) for part in parts)))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def deliver_payload(
    reminder: Reminder,
    occurrence: ReminderOccurrence,
    *,
    content: PushContent,
    silent: bool,
    escalation_step: int,
) -> dict[str, str]:
    data = {
        "v": PAYLOAD_VERSION,
        "type": "deliver",
        "event_id": event_id(
            "deliver", occurrence.id, occurrence.version, occurrence.scheduled_at, escalation_step
        ),
        "occurrence_id": str(occurrence.id),
        "reminder_id": str(reminder.id),
        "occurrence_version": str(occurrence.version),
        "reminder_revision": str(reminder.revision),
        "mode": reminder.mode,
        "scheduled_at": _iso(occurrence.scheduled_at),
        "expires_at": _iso(occurrence.expires_at),
        "requires_ack": "1" if reminder.requires_ack else "0",
        "silent": "1" if silent else "0",
        "escalation_step": str(escalation_step),
    }
    if content is PushContent.FULL:
        data["title"] = reminder.title
        body = reminder.body or ""
        data["body"] = body[:MAX_PUSH_BODY]
        if len(body) > MAX_PUSH_BODY:
            data["truncated"] = "1"
        if reminder.actions:
            data["actions"] = json.dumps(reminder.actions, separators=(",", ":"))
    return data


def dismiss_payload(occurrence: ReminderOccurrence) -> dict[str, str]:
    return {
        "v": PAYLOAD_VERSION,
        "type": "dismiss",
        "event_id": event_id("dismiss", occurrence.id, occurrence.version),
        "occurrence_id": str(occurrence.id),
        "occurrence_version": str(occurrence.version),
        "state": occurrence.state,
    }


def sync_payload(cursor: int) -> dict[str, str]:
    return {
        "v": PAYLOAD_VERSION,
        "type": "sync",
        "event_id": event_id("sync", cursor),
        "cursor": str(cursor),
    }
