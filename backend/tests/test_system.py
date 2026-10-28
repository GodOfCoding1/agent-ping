"""Health, diagnostics, error envelope, limits, worker trigger and database safeguards."""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import DBAPIError

from tests.conftest import Harness, create_once, register_device

BACKEND = Path(__file__).resolve().parents[1]
SCHEMA_HEAD = ScriptDirectory.from_config(Config(BACKEND / "alembic.ini")).get_current_head()


async def test_health_and_readiness(client) -> None:
    health = await client.get("/healthz", headers={"Authorization": ""})
    assert health.json()["status"] == "ok"
    ready = await client.get("/readyz", headers={"Authorization": ""})
    assert ready.status_code == 200
    assert ready.json()["schema_revision"] == SCHEMA_HEAD


async def test_request_id_and_security_headers(client) -> None:
    response = await client.get("/healthz", headers={"X-Request-ID": "trace-abcdef123"})
    assert response.headers["x-request-id"] == "trace-abcdef123"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["cache-control"] == "no-store"
    generated = await client.get("/healthz")
    assert len(generated.headers["x-request-id"]) == 32


async def test_not_found_uses_error_envelope(client) -> None:
    response = await client.get("/api/v1/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
    missing = await client.get("/api/v1/reminders/00000000-0000-0000-0000-000000000000")
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "not_found"


async def test_oversized_body_rejected(client) -> None:
    response = await client.post(
        "/api/v1/notifications",
        content=b'{"title": "' + b"x" * 70_000 + b'"}',
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["error"]["code"] == "payload_too_large"


@pytest.fixture
def settings_overrides(request) -> dict:
    return getattr(request, "param", {})


@pytest.mark.parametrize(
    "settings_overrides", [{"rate_limit_per_minute": 1, "rate_limit_burst": 3}], indirect=True
)
async def test_rate_limit(client) -> None:
    statuses = [(await client.get("/api/v1/reminders")).status_code for _ in range(5)]
    assert statuses[:3] == [200, 200, 200]
    assert statuses[-1] == 429
    limited = await client.get("/api/v1/reminders")
    assert int(limited.headers["retry-after"]) >= 1


async def test_diagnostics_reports_worker_outbox_push_and_devices(client, harness: Harness) -> None:
    await register_device(client)
    await create_once(client, minutes=0.1)
    harness.clock.advance(minutes=5)
    before = (await client.get("/api/v1/diagnostics")).json()
    assert before["outbox"]["overdue"] >= 1
    assert before["push"]["provider"] == "fake"
    assert before["devices"][0]["state"] in ("healthy", "degraded")
    await harness.run_due()
    after = (await client.get("/api/v1/diagnostics")).json()
    assert after["outbox"]["overdue"] == 0
    assert after["database"]["schema_revision"] == SCHEMA_HEAD


async def test_worker_trigger_disabled_without_token(client) -> None:
    response = await client.post("/internal/worker/run", headers={"X-Worker-Token": "x"})
    assert response.status_code == 404


@pytest.mark.parametrize(
    "settings_overrides", [{"worker_trigger_token": "trigger-secret-0123456789"}], indirect=True
)
async def test_worker_trigger_processes_due_jobs(client, harness: Harness) -> None:
    await register_device(client)
    await client.post("/api/v1/notifications", json={"title": "via trigger"})
    denied = await client.post("/internal/worker/run", headers={"X-Worker-Token": "wrong"})
    assert denied.status_code == 401
    response = await client.post(
        "/internal/worker/run", headers={"X-Worker-Token": "trigger-secret-0123456789"}
    )
    assert response.status_code == 200
    assert response.json()["processed"] >= 1
    assert harness.push.of_type("deliver")


async def test_runtime_role_cannot_rewrite_audit_or_ddl(client, clean_db) -> None:
    await create_once(client)
    engine = create_engine(clean_db.runtime)
    try:
        with engine.connect() as connection:
            with pytest.raises(DBAPIError):
                connection.execute(text("UPDATE audit_events SET summary = 'tampered'"))
            connection.rollback()
            with pytest.raises(DBAPIError):
                connection.execute(text("DELETE FROM audit_events"))
            connection.rollback()
            with pytest.raises(DBAPIError):
                connection.execute(text("SET agentping.allow_audit_purge = 'on'"))
                connection.execute(text("DELETE FROM audit_events"))
            connection.rollback()
            with pytest.raises(DBAPIError):
                connection.execute(text("CREATE TABLE sneaky (id int)"))
            connection.rollback()
            count: int = connection.execute(text("SELECT count(*) FROM audit_events")).scalar_one()
            assert count >= 1
    finally:
        engine.dispose()


async def test_openapi_document_is_served(client) -> None:
    spec = (await client.get("/openapi.json")).json()
    assert "/api/v1/reminders" in spec["paths"]
    assert "/api/v1/sync" in spec["paths"]
