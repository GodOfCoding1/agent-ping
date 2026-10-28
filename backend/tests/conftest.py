"""Shared fixtures.

Database tests run against real PostgreSQL. By default they use the disposable
Compose test database (`docker compose --profile test up -d test-db`), reading its
generated credentials from `.env.compose.db`. Override with
AGENTPING_TEST_DATABASE_URL (runtime role) and AGENTPING_TEST_MIGRATION_DATABASE_URL
(owner role). Every test starts from empty tables.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from agentping.api.app import create_app
from agentping.auth.principal import USER_SCOPES, Principal
from agentping.auth.service import ensure_user
from agentping.config import Settings
from agentping.db.engine import create_engine as create_async_engine
from agentping.db.engine import create_session_factory
from agentping.db.models import Base
from agentping.domain.enums import ActorKind
from agentping.push.base import PushMessage, PushResult
from agentping.services.runtime import RateLimiter, Runtime
from agentping.worker.runner import Worker

ROOT = Path(__file__).resolve().parents[2]
BACKEND = ROOT / "backend"
DEV_TOKEN = "test-development-token-0123456789abcdef"
T0 = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)


def _compose_urls() -> tuple[str, str] | None:
    path = ROOT / ".env.compose.db"
    if not path.exists():
        return None
    values = dict(
        line.split("=", 1)
        for line in path.read_text().splitlines()
        if "=" in line and not line.startswith("#")
    )
    port = os.environ.get("AGENTPING_TEST_DB_PORT", "55433")
    base = f"127.0.0.1:{port}/agentping"
    return (
        f"postgresql+psycopg://agentping_runtime:{values['RUNTIME_DB_PASSWORD']}@{base}",
        f"postgresql+psycopg://agentping_migrator:{values['POSTGRES_PASSWORD']}@{base}",
    )


@dataclass(frozen=True)
class DatabaseUrls:
    runtime: str
    migration: str


@pytest.fixture(scope="session")
def database_urls() -> DatabaseUrls:
    runtime = os.environ.get("AGENTPING_TEST_DATABASE_URL")
    migration = os.environ.get("AGENTPING_TEST_MIGRATION_DATABASE_URL")
    if not (runtime and migration):
        urls = _compose_urls()
        if urls is None:
            pytest.skip("No test database configured (see tests/conftest.py)")
        runtime, migration = urls
    return DatabaseUrls(runtime, migration)


@pytest.fixture(scope="session")
def migrated(database_urls: DatabaseUrls) -> Iterator[DatabaseUrls]:
    """Apply Alembic migrations once per session using the owner role."""
    engine = create_engine(database_urls.migration, connect_args={"connect_timeout": 5})
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except OperationalError:
        pytest.skip("Test database is not reachable")
    finally:
        engine.dispose()
    env = {
        **os.environ,
        "APP_ENV": "test",
        "MIGRATION_DATABASE_URL": database_urls.migration,
        "DATABASE_URL": database_urls.runtime,
    }
    for args in (["downgrade", "base"], ["upgrade", "head"]):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", *args],
            cwd=BACKEND,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, "alembic " + " ".join(args) + " failed"
    yield database_urls


TABLES = ", ".join(table.name for table in reversed(Base.metadata.sorted_tables))


@pytest.fixture
def clean_db(migrated: DatabaseUrls) -> DatabaseUrls:
    engine = create_engine(migrated.migration)
    with engine.begin() as connection:
        connection.execute(text("SET LOCAL agentping.allow_audit_purge = 'on'"))
        connection.execute(text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    engine.dispose()
    return migrated


class FakeClock:
    def __init__(self, start: datetime = T0) -> None:
        self.current = start

    def now(self) -> datetime:
        return self.current

    def advance(self, **delta: float) -> datetime:
        self.current += timedelta(**delta)
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value


@dataclass
class FakePush:
    """Records sends; tokens in `failures` get the configured provider result."""

    name: str = "fake"
    sent: list[PushMessage] = field(default_factory=list)
    failures: dict[str, PushResult] = field(default_factory=dict)

    async def send(self, messages: list[PushMessage]) -> list[PushResult]:
        results = []
        for message in messages:
            self.sent.append(message)
            failure = self.failures.get(message.token)
            results.append(
                failure
                or PushResult(accepted=True, provider=self.name, message_id=f"m{len(self.sent)}")
            )
        return results

    def status(self) -> dict[str, object]:
        return {"provider": self.name, "configured": True, "delivers_to_devices": False}

    def of_type(self, kind: str) -> list[PushMessage]:
        return [m for m in self.sent if m.data.get("type") == kind]


def make_settings(urls: DatabaseUrls, **overrides: Any) -> Settings:
    values: dict[str, Any] = {
        "app_env": "test",
        "database_url": urls.runtime,
        "migration_database_url": urls.migration,
        "auth_mode": "development",
        "dev_auth_token": DEV_TOKEN,
        "push_provider": "log",
        "public_base_url": "http://testserver",
        "default_timezone": "UTC",
        "rate_limit_per_minute": 100_000,
        "rate_limit_burst": 100_000,
        "worker_listen": False,
        "log_json": False,
    }
    values.update(overrides)
    return Settings(**values)


@dataclass
class Harness:
    runtime: Runtime
    clock: FakeClock
    push: FakePush
    worker: Worker

    async def owner(self) -> Principal:
        async with self.runtime.session_factory() as session, session.begin():
            user = await ensure_user(session, self.runtime)
        return Principal(
            user_id=user.id,
            actor_kind=ActorKind.USER,
            scopes=frozenset(USER_SCOPES),
            auth_method="test",
        )

    async def call(self, principal: Principal, operation: Any, *args: Any, **kwargs: Any) -> Any:
        async with self.runtime.unit(principal) as ctx:
            return await operation(ctx, *args, **kwargs)

    async def ctx_call(self, principal: Principal, fn: Any) -> Any:
        async with self.runtime.unit(principal) as ctx:
            return await fn(ctx)

    async def run_due(self) -> int:
        return await self.worker.drain()

    async def advance_and_run(self, **delta: float) -> int:
        self.clock.advance(**delta)
        return await self.run_due()


@pytest.fixture
def settings_overrides() -> dict[str, Any]:
    return {}


@pytest.fixture
async def harness(
    clean_db: DatabaseUrls, settings_overrides: dict[str, Any]
) -> AsyncIterator[Harness]:
    settings = make_settings(clean_db, **settings_overrides)
    engine = create_async_engine(settings)
    clock = FakeClock()
    push = FakePush()
    runtime = Runtime(
        settings=settings,
        session_factory=create_session_factory(engine),
        push=push,
        clock=clock,
        rate_limiter=RateLimiter(settings.rate_limit_per_minute, settings.rate_limit_burst),
    )
    yield Harness(runtime, clock, push, Worker(runtime, worker_id="test-worker"))
    await engine.dispose()


@pytest.fixture
async def app(harness: Harness) -> AsyncIterator[Any]:
    application = create_app(runtime=harness.runtime)
    async with running_lifespan(application):
        yield application


@pytest.fixture
async def client(app: Any) -> AsyncIterator[httpx.AsyncClient]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
        headers={"Authorization": f"Bearer {DEV_TOKEN}"},
    ) as http:
        yield http


@asynccontextmanager
async def running_lifespan(app: Any) -> AsyncIterator[None]:
    """Run the app lifespan in its own task (anyio scopes must exit in the entering task)."""
    started, stop = asyncio.Event(), asyncio.Event()

    async def run() -> None:
        async with app.router.lifespan_context(app):
            started.set()
            await stop.wait()

    task = asyncio.create_task(run())
    await asyncio.wait({task, asyncio.create_task(started.wait())}, return_when="FIRST_COMPLETED")
    if task.done():
        task.result()
    try:
        yield
    finally:
        stop.set()
        await task


def auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def register_device(
    client: httpx.AsyncClient,
    *,
    installation_id: str = "install-1",
    name: str = "Pixel",
    push_token: str | None = "fcm-registration-token-0001",
    timezone: str | None = "UTC",
    capabilities: dict[str, Any] | None = None,
    push_token_kind: str | None = None,
) -> dict[str, Any]:
    """Pair a device the way the Android app does and return the registration response."""
    code = (await client.post("/api/v1/devices/pairing-codes")).json()["code"]
    body: dict[str, Any] = {
        "pairing_code": code,
        "installation_id": installation_id,
        "name": name,
        "model": "Pixel 9",
        "os_version": "16",
        "app_version": "1.0.0",
        "timezone": timezone,
        "push_token": push_token,
    }
    if capabilities is not None:
        body["capabilities"] = capabilities
    if push_token_kind is not None:
        body["push_token_kind"] = push_token_kind
    response = await client.post(
        "/api/v1/devices/register", json=body, headers={"Authorization": ""}
    )
    assert response.status_code == 201, response.text
    return response.json()


async def create_once(
    client: httpx.AsyncClient,
    *,
    minutes: float = 10,
    title: str = "Reminder",
    mode: str = "normal",
    headers: dict[str, str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    due = T0 + timedelta(minutes=minutes)
    body = {
        "title": title,
        "delivery_mode": mode,
        "schedule": {"kind": "once", "at": due.isoformat()},
        **extra,
    }
    response = await client.post("/api/v1/reminders", json=body, headers=headers or {})
    assert response.status_code == 201, response.text
    return response.json()


async def create_agent(
    client: httpx.AsyncClient,
    name: str = "build-bot",
    scopes: list[str] | None = None,
    **extra: Any,
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "name": name,
        "scopes": scopes
        or [
            "reminders:read",
            "reminders:write",
            "notifications:send",
            "devices:read",
            "activity:read",
        ],
        **extra,
    }
    response = await client.post("/api/v1/agents", json=body)
    assert response.status_code == 201, response.text
    return response.json()
