from __future__ import annotations

import google.auth.credentials
import pytest
from firebase_admin import credentials, exceptions, messaging

from agentping.domain.enums import PushTokenKind
from agentping.push.base import LoggingPushSender, PushMessage, close_push
from agentping.push.fcm import FcmPushSender, classify_error


class AnonymousCredential(credentials.Base):
    def get_credential(self) -> google.auth.credentials.Credentials:
        return google.auth.credentials.AnonymousCredentials()


def test_message_build_is_data_only_and_restricted() -> None:
    sender = FcmPushSender("demo-project", package_name="com.errorfactory.agentping.debug")
    message = sender._build(
        PushMessage(
            token="t" * 30,
            data={"type": "deliver"},
            priority="high",
            ttl_seconds=60,
            collapse_key="occ",
        )
    )
    assert message.notification is None
    assert message.fid == "t" * 30
    assert message.token is None
    assert message.data == {"type": "deliver"}
    assert message.android.priority == "high"
    assert message.android.restricted_package_name == "com.errorfactory.agentping.debug"
    assert message.android.collapse_key == "occ"


def test_legacy_registration_token_target() -> None:
    sender = FcmPushSender("demo-project")
    with pytest.warns(DeprecationWarning):
        message = sender._build(
            PushMessage(
                token="r" * 30, token_kind=PushTokenKind.REGISTRATION_TOKEN, data={"type": "sync"}
            )
        )
    assert message.token == "r" * 30
    assert message.fid is None


def test_error_classification() -> None:
    unregistered = classify_error(messaging.UnregisteredError("gone"))
    assert unregistered.token_invalid and not unregistered.retryable
    bad_token = classify_error(
        exceptions.InvalidArgumentError("The registration token is not a valid FCM token")
    )
    assert bad_token.token_invalid
    unavailable = classify_error(exceptions.UnavailableError("try later"))
    assert unavailable.retryable and not unavailable.token_invalid
    transport = classify_error(ConnectionError("reset"))
    assert transport.retryable and transport.error_code == "transport_error"


async def test_fcm_sender_closes_inside_running_event_loop(monkeypatch) -> None:
    monkeypatch.setattr(credentials, "ApplicationDefault", AnonymousCredential)
    sender = FcmPushSender("demo-project")
    app = sender._ensure_app()
    messaging._get_messaging_service(app)  # creates the async HTTP client delete_app closes
    await close_push(sender)
    await close_push(sender)  # idempotent
    await close_push(LoggingPushSender())
