"""Cloud wake-up scheduling uses durable job state, including crash/retry recovery."""

from datetime import timedelta
from unittest.mock import AsyncMock

import certifi
import pytest
from sqlalchemy import update

from agentping.config import Settings, checked_database_url
from agentping.db.models import OutboxJob
from agentping.worker.cloud_tasks import CloudTasksWakeup
from tests.conftest import create_once


def test_full_tls_verification_resolves_runtime_ca_bundle() -> None:
    url = checked_database_url(
        "postgresql://localhost/test?sslmode=verify-full&sslrootcert=system",
        Settings(),
        migration=False,
    )
    assert url.query["sslmode"] == "verify-full"
    assert url.query["sslrootcert"] == certifi.where()


@pytest.fixture
def settings_overrides(request) -> dict:
    return getattr(request, "param", {})


async def test_wakeup_schedules_actual_due_time_not_idle_poll_interval(
    client, harness, monkeypatch
) -> None:
    await create_once(client, minutes=120)
    expected = harness.clock.now() + timedelta(hours=2)
    await harness.advance_and_run(seconds=3)  # Drain the immediate device sync hint first.
    wakeup = CloudTasksWakeup(harness.runtime)
    enqueue = AsyncMock()
    monkeypatch.setattr(wakeup, "_enqueue", enqueue)
    await wakeup.notify()
    _, run_at = enqueue.call_args.args
    assert run_at == expected
    await wakeup.notify()
    assert enqueue.call_args == enqueue.call_args_list[0]


async def test_wakeup_recovers_lease_and_retry_with_distinct_task_identity(
    client, harness, monkeypatch
) -> None:
    await create_once(client, minutes=0)
    wakeup = CloudTasksWakeup(harness.runtime)
    enqueue = AsyncMock()
    monkeypatch.setattr(wakeup, "_enqueue", enqueue)
    await wakeup.notify()
    first_id = enqueue.call_args.args[0]
    lease = harness.clock.now() + timedelta(seconds=60)
    async with harness.runtime.session_factory() as session, session.begin():
        await session.execute(
            update(OutboxJob).values(status="running", locked_until=lease, attempts=1)
        )
    await wakeup.notify()
    task_id, run_at = enqueue.call_args.args
    assert task_id != first_id
    assert run_at == lease


async def test_empty_queue_does_not_schedule_idle_queries(harness, monkeypatch) -> None:
    wakeup = CloudTasksWakeup(harness.runtime)
    enqueue = AsyncMock()
    monkeypatch.setattr(wakeup, "_enqueue", enqueue)
    await wakeup.notify()
    enqueue.assert_not_called()


async def test_committed_write_survives_enqueue_failure(client, harness) -> None:
    notifier = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    harness.runtime.work_notifier = notifier
    created = await create_once(client)
    assert notifier.await_count >= 1
    response = await client.get(f"/api/v1/reminders/{created['reminder_id']}")
    assert response.status_code == 200


@pytest.mark.parametrize(
    "settings_overrides", [{"worker_trigger_token": "trigger-secret-0123456789"}], indirect=True
)
async def test_trigger_fails_for_retry_if_next_wakeup_cannot_be_enqueued(client, harness) -> None:
    harness.runtime.work_notifier = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    # ASGI's test transport surfaces unhandled exceptions; a real server returns 500,
    # which Cloud Tasks retries. Never falsely acknowledge the failed handoff with 2xx.
    with pytest.raises(RuntimeError, match="provider unavailable"):
        await client.post(
            "/internal/worker/run", headers={"X-Worker-Token": "trigger-secret-0123456789"}
        )
