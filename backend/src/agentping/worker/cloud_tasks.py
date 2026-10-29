"""Schedule the next durable outbox wake-up without a resident worker or idle polling.

Postgres remains authoritative. Task enqueue is deliberately after commit; an hourly
authenticated recovery trigger repairs the commit/enqueue crash window. Tasks only
carry a wake-up signal and never reminder contents or database credentials.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import google.auth
import httpx
from google.auth.transport.requests import Request
from sqlalchemy import case, select

from agentping.db.models import OutboxJob

if TYPE_CHECKING:
    from agentping.services.runtime import Runtime


class CloudTasksWakeup:
    def __init__(self, runtime: Runtime) -> None:
        self.runtime = runtime

    async def notify(self) -> None:
        # Include expired leases: a process can die after claiming a durable job.
        due = case(
            (OutboxJob.status == "pending", OutboxJob.run_at),
            else_=OutboxJob.locked_until,
        ).label("due")
        async with self.runtime.session_factory() as session:
            row = (
                await session.execute(
                    select(OutboxJob.id, OutboxJob.attempts, due)
                    .where(OutboxJob.status.in_(["pending", "running"]))
                    .order_by(due, OutboxJob.id)
                    .limit(1)
                )
            ).first()
        if row is None:
            return
        now = self.runtime.clock.now()
        # Cloud Tasks accepts schedules at most 30 days in the future. A bounded
        # intermediate wake-up also covers unusually distant durable jobs.
        run_at = max(now + timedelta(seconds=1), min(row.due, now + timedelta(days=28)))
        # Deduplicate concurrent transactions selecting the same outbox job. An hourly
        # time bucket allows recovery to recreate a wake-up after a task's tombstone.
        target = (
            self.runtime.settings.worker_base_url or self.runtime.settings.public_base_url
        ).rstrip("/")
        key = (
            f"{target}:{row.id}:{row.attempts}:{row.due.isoformat()}:{int(now.timestamp()) // 3600}"
        )
        task_id = hashlib.sha256(key.encode()).hexdigest()
        await self._enqueue(task_id, run_at)

    async def _enqueue(self, task_id: str, run_at: datetime) -> None:
        settings = self.runtime.settings
        queue = settings.cloud_tasks_queue
        origin = (settings.worker_base_url or settings.public_base_url).rstrip("/")
        payload: dict[str, Any] = {
            "task": {
                "name": f"{queue}/tasks/{task_id}",
                "scheduleTime": run_at.astimezone(UTC).isoformat(),
                "dispatchDeadline": "120s",
                "httpRequest": {
                    "httpMethod": "POST",
                    "url": f"{origin}/internal/worker/run",
                    "headers": {
                        "X-Worker-Token": settings.worker_trigger_token.get_secret_value(),
                    },
                    "oidcToken": {
                        "serviceAccountEmail": settings.cloud_tasks_invoker_email,
                        "audience": origin,
                    },
                },
            }
        }
        access_token = await asyncio.to_thread(self._access_token)
        async with httpx.AsyncClient(timeout=15) as http:
            response = await http.post(
                f"https://cloudtasks.googleapis.com/v2/{queue}/tasks",
                headers={"Authorization": f"Bearer {access_token}"},
                json=payload,
            )
        if response.status_code == 409:
            return
        if response.is_error:
            # API errors may echo request headers. Never propagate response bodies.
            raise RuntimeError(f"Cloud Tasks enqueue failed (HTTP {response.status_code})")

    @staticmethod
    def _access_token() -> str:
        credentials, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/cloud-platform"]
        )
        credentials.refresh(Request())
        return str(credentials.token)
