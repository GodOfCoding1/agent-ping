"""Firebase Cloud Messaging sender using the Firebase Admin SDK (HTTP v1 API).

Credentials come from Google Application Default Credentials. Locally that is
impersonated ADC for the development sender identity; no private keys are used.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import firebase_admin
from firebase_admin import credentials, exceptions, messaging

from agentping.domain.enums import PushTokenKind
from agentping.push.base import PushMessage, PushResult

logger = logging.getLogger(__name__)

_RETRYABLE = (
    messaging.QuotaExceededError,
    messaging.ThirdPartyAuthError,
    exceptions.UnavailableError,
    exceptions.InternalError,
    exceptions.DeadlineExceededError,
    exceptions.UnknownError,
    exceptions.ResourceExhaustedError,
    exceptions.UnauthenticatedError,
    exceptions.PermissionDeniedError,
)
_TOKEN_INVALID = (messaging.UnregisteredError, messaging.SenderIdMismatchError)


def classify_error(error: BaseException) -> PushResult:
    code = getattr(error, "code", None) or type(error).__name__
    detail = type(error).__name__
    if isinstance(error, _TOKEN_INVALID):
        return PushResult(False, "fcm", error_code=str(code), detail=detail, token_invalid=True)
    if isinstance(error, exceptions.InvalidArgumentError):
        message = str(error).lower()
        invalid_token = "registration token" in message or "not a valid fcm" in message
        return PushResult(
            False, "fcm", error_code=str(code), detail=detail, token_invalid=invalid_token
        )
    if isinstance(error, _RETRYABLE):
        return PushResult(False, "fcm", error_code=str(code), detail=detail, retryable=True)
    if isinstance(error, exceptions.FirebaseError):
        return PushResult(False, "fcm", error_code=str(code), detail=detail)
    return PushResult(False, "fcm", error_code="transport_error", detail=detail, retryable=True)


class FcmPushSender:
    name = "fcm"

    def __init__(
        self, project_id: str, *, package_name: str | None = None, dry_run: bool = False
    ) -> None:
        if not project_id:
            raise ValueError("FIREBASE_PROJECT_ID is required for PUSH_PROVIDER=fcm")
        self._project_id = project_id
        self._package_name = package_name
        self._dry_run = dry_run
        self._app: firebase_admin.App | None = None

    def _ensure_app(self) -> firebase_admin.App:
        if self._app is None:
            self._app = firebase_admin.initialize_app(
                credentials.ApplicationDefault(),
                {"projectId": self._project_id},
                name=f"agentping-{id(self)}",
            )
        return self._app

    def _build(self, message: PushMessage) -> messaging.Message:
        android: dict[str, Any] = {"priority": message.priority}
        if self._package_name:
            android["restricted_package_name"] = self._package_name
        if message.ttl_seconds is not None:
            android["ttl"] = timedelta(seconds=max(0, message.ttl_seconds))
        if message.collapse_key:
            android["collapse_key"] = message.collapse_key
        target_field = "fid" if message.token_kind is PushTokenKind.FID else "token"
        target = {target_field: message.token}
        return messaging.Message(
            data=message.data, android=messaging.AndroidConfig(**android), **target
        )

    def _send_sync(self, messages: list[PushMessage]) -> list[PushResult]:
        app = self._ensure_app()
        batch = messaging.send_each(
            [self._build(m) for m in messages], dry_run=self._dry_run, app=app
        )
        results: list[PushResult] = []
        for response in batch.responses:
            if response.success:
                results.append(PushResult(True, "fcm", message_id=response.message_id))
            else:
                results.append(classify_error(response.exception))
        return results

    async def send(self, messages: list[PushMessage]) -> list[PushResult]:
        if not messages:
            return []
        try:
            return await asyncio.to_thread(self._send_sync, messages)
        except (
            Exception
        ) as error:  # credential refresh/transport failures before per-message results
            logger.warning("fcm_batch_failed", extra={"error_type": type(error).__name__})
            return [classify_error(error) for _ in messages]

    def status(self) -> dict[str, object]:
        return {
            "provider": self.name,
            "configured": True,
            "project_id": self._project_id,
            "dry_run": self._dry_run,
            "delivers_to_devices": not self._dry_run,
        }

    async def aclose(self) -> None:
        if self._app is not None:
            app, self._app = self._app, None
            # delete_app closes the SDK's async HTTP client with asyncio.run(), which cannot
            # run inside an active event loop.
            await asyncio.to_thread(firebase_admin.delete_app, app)
