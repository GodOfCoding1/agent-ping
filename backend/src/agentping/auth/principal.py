"""Authenticated principals and scopes."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from agentping.domain.enums import ActorKind
from agentping.domain.errors import Forbidden


class Scope:
    REMINDERS_READ = "reminders:read"
    REMINDERS_WRITE = "reminders:write"
    NOTIFICATIONS_SEND = "notifications:send"
    ALERTS_CRITICAL = "alerts:critical"
    DEVICES_READ = "devices:read"
    ACTIVITY_READ = "activity:read"
    DEVICES_MANAGE = "devices:manage"
    SETTINGS_WRITE = "settings:write"
    AGENTS_MANAGE = "agents:manage"
    DEVICE = "device"


AGENT_GRANTABLE_SCOPES = frozenset(
    {
        Scope.REMINDERS_READ,
        Scope.REMINDERS_WRITE,
        Scope.NOTIFICATIONS_SEND,
        Scope.ALERTS_CRITICAL,
        Scope.DEVICES_READ,
        Scope.ACTIVITY_READ,
    }
)
DEFAULT_AGENT_SCOPES = frozenset(
    {
        Scope.REMINDERS_READ,
        Scope.REMINDERS_WRITE,
        Scope.NOTIFICATIONS_SEND,
        Scope.DEVICES_READ,
        Scope.ACTIVITY_READ,
    }
)
USER_SCOPES = AGENT_GRANTABLE_SCOPES | {
    Scope.DEVICES_MANAGE,
    Scope.SETTINGS_WRITE,
    Scope.AGENTS_MANAGE,
}
DEVICE_SCOPES = USER_SCOPES | {Scope.DEVICE}


@dataclass(frozen=True)
class Principal:
    user_id: uuid.UUID
    actor_kind: ActorKind
    scopes: frozenset[str]
    auth_method: str
    agent_client_id: uuid.UUID | None = None
    agent_name: str | None = None
    device_id: uuid.UUID | None = None
    token_id: uuid.UUID | None = None
    limits: dict[str, int] = field(default_factory=dict)

    def has(self, scope: str) -> bool:
        return scope in self.scopes

    def require(self, *scopes: str) -> None:
        missing = [scope for scope in scopes if scope not in self.scopes]
        if missing:
            raise Forbidden(
                f"Missing required scope(s): {', '.join(missing)}",
                details={"required_scopes": missing},
            )

    @property
    def actor_key(self) -> str:
        if self.agent_client_id is not None:
            return f"agent:{self.agent_client_id}"
        if self.device_id is not None:
            return f"device:{self.device_id}"
        return f"user:{self.user_id}"

    @property
    def is_agent(self) -> bool:
        return self.actor_kind is ActorKind.AGENT

    def describe(self) -> str:
        if self.agent_name:
            return f"agent '{self.agent_name}'"
        if self.device_id:
            return "device"
        return "user"
