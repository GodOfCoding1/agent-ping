"""Push provider abstraction.

A successful ``PushResult`` means the provider *accepted* the message. It is never
treated as device receipt: devices confirm receipt/display through the receipts API.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

from agentping.domain.enums import PushTokenKind


@dataclass(frozen=True)
class PushMessage:
    token: str
    data: dict[str, str]
    token_kind: PushTokenKind = PushTokenKind.FID
    priority: Literal["high", "normal"] = "normal"
    ttl_seconds: int | None = None
    collapse_key: str | None = None


@dataclass(frozen=True)
class PushResult:
    accepted: bool
    provider: str
    message_id: str | None = None
    error_code: str | None = None
    detail: str | None = None
    token_invalid: bool = False
    retryable: bool = False


class PushSender(Protocol):
    name: str

    async def send(self, messages: list[PushMessage]) -> list[PushResult]: ...

    def status(self) -> dict[str, object]: ...


async def close_push(sender: PushSender) -> None:
    """Release provider resources; senders without an ``aclose`` hold none."""
    aclose = getattr(sender, "aclose", None)
    if callable(aclose):
        await aclose()


@dataclass
class LoggingPushSender:
    """Development sender: records messages without contacting any provider.

    Results use provider 'log' so they are never mistaken for FCM acceptance.
    """

    name: str = "log"
    sent: list[PushMessage] = field(default_factory=list)

    async def send(self, messages: list[PushMessage]) -> list[PushResult]:
        self.sent.extend(messages)
        return [
            PushResult(accepted=True, provider=self.name, message_id=f"log-{len(self.sent)}")
            for _ in messages
        ]

    def status(self) -> dict[str, object]:
        return {"provider": self.name, "configured": True, "delivers_to_devices": False}


@dataclass
class DisabledPushSender:
    name: str = "disabled"

    async def send(self, messages: list[PushMessage]) -> list[PushResult]:
        return [
            PushResult(
                accepted=False,
                provider=self.name,
                error_code="push_disabled",
                detail="Push delivery is disabled by configuration",
            )
            for _ in messages
        ]

    def status(self) -> dict[str, object]:
        return {"provider": self.name, "configured": False, "delivers_to_devices": False}
