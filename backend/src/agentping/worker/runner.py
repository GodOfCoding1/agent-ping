"""PostgreSQL-backed durable job runner.

* Jobs are claimed with ``FOR UPDATE SKIP LOCKED`` and a time-limited lease, so concurrent
  workers never process the same job at once; a crashed worker's lease expires and the job
  is reclaimed.
* A handler's database effects and the job's completion commit in one transaction. A job
  whose lease was lost is rolled back rather than completed twice.
* Idle workers sleep until the next known ``run_at`` (bounded by WORKER_IDLE_MAX_SECONDS)
  and wake early on in-process signals or PostgreSQL NOTIFY, avoiding frequent polling.
"""

from __future__ import annotations

import asyncio
import logging
import os
import random
import socket
import uuid
from datetime import datetime, timedelta

import psycopg
from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert

from agentping import __version__
from agentping.db.models import OutboxJob, WorkerHeartbeat
from agentping.domain.enums import ActionSource
from agentping.logging import redact
from agentping.services.runtime import Runtime, system_principal
from agentping.worker.handlers import HANDLERS, Job, RetryLater, on_dead, schedule_maintenance

logger = logging.getLogger(__name__)
NIL_USER = uuid.UUID(int=0)

_CLAIM = text(
    """
    WITH candidates AS (
        SELECT id FROM outbox_jobs
        WHERE (status = 'pending' AND run_at <= :now)
           OR (status = 'running' AND locked_until < :now)
        ORDER BY run_at, id
        LIMIT :limit
        FOR UPDATE SKIP LOCKED
    )
    UPDATE outbox_jobs AS j
    SET status = 'running', locked_by = :worker, locked_until = :lease_until,
        attempts = j.attempts + 1, updated_at = :now
    FROM candidates c
    WHERE j.id = c.id
    RETURNING j.id, j.kind, j.payload, j.user_id, j.attempts, j.max_attempts
    """
)


class LeaseLost(Exception):
    pass


def backoff_seconds(attempt: int) -> float:
    base = min(5 * (2 ** max(attempt - 1, 0)), 3600)
    return float(base * random.uniform(0.8, 1.2))


class Worker:
    def __init__(self, runtime: Runtime, worker_id: str | None = None) -> None:
        self.runtime = runtime
        self.worker_id = worker_id or f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"
        self.succeeded = 0
        self.failed = 0
        self._last_heartbeat: datetime | None = None
        self._started_at = runtime.clock.now()

    async def _claim(self, limit: int) -> list[Job]:
        now = self.runtime.clock.now()
        lease = now + timedelta(seconds=self.runtime.settings.worker_lease_seconds)
        async with self.runtime.session_factory() as session, session.begin():
            rows = await session.execute(
                _CLAIM,
                {"now": now, "limit": limit, "worker": self.worker_id, "lease_until": lease},
            )
            return [
                Job(
                    id=row.id,
                    kind=row.kind,
                    payload=row.payload,
                    user_id=row.user_id,
                    attempts=row.attempts,
                    max_attempts=row.max_attempts,
                )
                for row in rows
            ]

    async def run_batch(self, limit: int | None = None) -> int:
        jobs = await self._claim(limit or self.runtime.settings.worker_batch_size)
        for job in jobs:
            await self._execute(job)
        if jobs:
            await self._heartbeat(batch=True)
        return len(jobs)

    async def drain(self, max_batches: int = 100) -> int:
        """Process all currently due jobs (used by tests and the HTTP trigger)."""
        total = 0
        for _ in range(max_batches):
            processed = await self.run_batch()
            total += processed
            if processed == 0:
                break
        return total

    async def _execute(self, job: Job) -> None:
        handler = HANDLERS.get(job.kind)
        if handler is None:
            await self._fail(job, f"unknown job kind {job.kind}", dead=True)
            return
        principal = system_principal(job.user_id or NIL_USER)
        try:
            async with self.runtime.unit(
                principal, source=ActionSource.SYSTEM, rate_limit=False
            ) as ctx:
                await handler(ctx, job)
                result = await ctx.session.execute(
                    update(OutboxJob)
                    .where(
                        OutboxJob.id == job.id,
                        OutboxJob.locked_by == self.worker_id,
                        OutboxJob.status == "running",
                    )
                    .values(
                        status="succeeded",
                        finished_at=ctx.now,
                        locked_until=None,
                        last_error=None,
                    )
                )
                if result.rowcount != 1:  # type: ignore[attr-defined]
                    raise LeaseLost()
            self.succeeded += 1
        except LeaseLost:
            logger.warning("job_lease_lost", extra={"job_id": job.id, "kind": job.kind})
        except RetryLater as error:
            await self._fail(job, error.detail, delay=error.delay_seconds)
        except Exception as error:
            message = redact(f"{type(error).__name__}: {str(error)[:200]}")
            logger.exception("job_failed", extra={"job_id": job.id, "kind": job.kind})
            await self._fail(job, message)

    async def _fail(
        self, job: Job, error: str, *, delay: float | None = None, dead: bool = False
    ) -> None:
        self.failed += 1
        now = self.runtime.clock.now()
        dead = dead or job.attempts >= job.max_attempts
        principal = system_principal(job.user_id or NIL_USER)
        async with self.runtime.unit(
            principal, source=ActionSource.SYSTEM, rate_limit=False
        ) as ctx:
            values: dict[str, object] = {"last_error": error[:1000], "locked_until": None}
            if dead:
                values.update(status="dead", finished_at=now)
            else:
                retry_at = now + timedelta(seconds=delay or backoff_seconds(job.attempts))
                values.update(status="pending", run_at=retry_at, locked_by=None)
            result = await ctx.session.execute(
                update(OutboxJob)
                .where(OutboxJob.id == job.id, OutboxJob.locked_by == self.worker_id)
                .values(**values)
            )
            if dead and result.rowcount == 1:  # type: ignore[attr-defined]
                logger.error("job_dead", extra={"job_id": job.id, "kind": job.kind})
                await on_dead(ctx, job, error)

    async def next_delay(self) -> float:
        settings = self.runtime.settings
        async with self.runtime.session_factory() as session:
            next_run = (
                await session.execute(
                    select(func.min(OutboxJob.run_at)).where(OutboxJob.status == "pending")
                )
            ).scalar_one_or_none()
            next_lease = (
                await session.execute(
                    select(func.min(OutboxJob.locked_until)).where(OutboxJob.status == "running")
                )
            ).scalar_one_or_none()
        candidates = [t for t in (next_run, next_lease) if t is not None]
        if not candidates:
            return settings.worker_idle_max_seconds
        delta = (min(candidates) - self.runtime.clock.now()).total_seconds()
        return max(settings.worker_idle_min_seconds, min(delta, settings.worker_idle_max_seconds))

    async def ensure_maintenance(self) -> None:
        principal = system_principal(NIL_USER)
        async with self.runtime.unit(
            principal, source=ActionSource.SYSTEM, rate_limit=False
        ) as ctx:
            exists = (
                await ctx.session.execute(
                    select(OutboxJob.id).where(
                        OutboxJob.kind == "maintenance.tick",
                        OutboxJob.status.in_(["pending", "running"]),
                    )
                )
            ).first()
            if exists is None:
                await schedule_maintenance(ctx, ctx.now)

    async def _heartbeat(self, *, batch: bool = False) -> None:
        now = self.runtime.clock.now()
        if not batch and self._last_heartbeat and now - self._last_heartbeat < timedelta(minutes=5):
            return
        self._last_heartbeat = now
        values = {
            "worker_id": self.worker_id,
            "hostname": socket.gethostname(),
            "version": __version__,
            "started_at": self._started_at,
            "last_seen_at": now,
            "jobs_succeeded": self.succeeded,
            "jobs_failed": self.failed,
        }
        if batch:
            values["last_batch_at"] = now
        statement = insert(WorkerHeartbeat).values(**values)
        update_values = {k: v for k, v in values.items() if k != "worker_id"}
        async with self.runtime.session_factory() as session, session.begin():
            await session.execute(
                statement.on_conflict_do_update(index_elements=["worker_id"], set_=update_values)
            )

    async def _listen(self, stop: asyncio.Event, url: str) -> None:
        while not stop.is_set():
            try:
                async with await psycopg.AsyncConnection.connect(url, autocommit=True) as conn:
                    await conn.execute("LISTEN agentping_outbox")
                    logger.info("worker_listening")
                    async for _ in conn.notifies():
                        self.runtime.signal.notify()
                        if stop.is_set():
                            break
            except Exception as error:
                logger.warning("worker_listen_error", extra={"error_type": type(error).__name__})
                await asyncio.sleep(10)

    async def run_forever(self, stop: asyncio.Event, *, listen_url: str | None = None) -> None:
        logger.info("worker_started", extra={"worker_id": self.worker_id})
        await self.ensure_maintenance()
        await self._heartbeat(batch=False)
        listener = asyncio.create_task(self._listen(stop, listen_url)) if listen_url else None
        try:
            while not stop.is_set():
                try:
                    processed = await self.run_batch()
                    if processed:
                        continue
                    delay = await self.next_delay()
                    await self._heartbeat()
                except Exception as error:
                    logger.error("worker_loop_error", extra={"error_type": type(error).__name__})
                    delay = 15.0
                await self.runtime.signal.wait(delay)
        finally:
            if listener:
                listener.cancel()
            logger.info("worker_stopped", extra={"worker_id": self.worker_id})
