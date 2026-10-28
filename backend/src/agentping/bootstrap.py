"""Build runtime dependencies from settings."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from agentping.config import PushProvider, Settings
from agentping.db.engine import create_engine, create_session_factory
from agentping.push.base import DisabledPushSender, LoggingPushSender, PushSender
from agentping.services.runtime import Runtime


def build_push_sender(settings: Settings) -> PushSender:
    if settings.push_provider is PushProvider.FCM:
        from agentping.push.fcm import FcmPushSender

        return FcmPushSender(
            settings.firebase_project_id,
            package_name=settings.fcm_restricted_package_name or None,
            dry_run=settings.fcm_dry_run,
        )
    if settings.push_provider is PushProvider.LOG:
        return LoggingPushSender()
    return DisabledPushSender()


def build_runtime(
    settings: Settings, *, push: PushSender | None = None
) -> tuple[Runtime, AsyncEngine]:
    engine = create_engine(settings)
    runtime = Runtime(
        settings=settings,
        session_factory=create_session_factory(engine),
        push=push or build_push_sender(settings),
    )
    return runtime, engine


def listen_url(settings: Settings) -> str | None:
    """LISTEN needs a session-level connection; PgBouncer transaction pooling cannot keep it."""
    if not settings.worker_listen:
        return None
    if settings.worker_listen_database_url.get_secret_value():
        from agentping.config import checked_database_url

        url = checked_database_url(
            settings.worker_listen_database_url.get_secret_value(), settings, migration=True
        )
    else:
        url = settings.runtime_database_url()
        if "pooler" in (url.host or ""):
            return None
    return url.set(drivername="postgresql").render_as_string(hide_password=False)
