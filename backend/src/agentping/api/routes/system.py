"""Health, diagnostics and the request-driven worker trigger."""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import func, select, text

from agentping import __version__
from agentping.api.deps import get_principal, get_runtime
from agentping.api.errors import ERROR_RESPONSES, error_response
from agentping.auth import tokens
from agentping.auth.principal import Principal, Scope
from agentping.db.models import Device, OutboxJob, WorkerHeartbeat
from agentping.domain.enums import DeviceStatus
from agentping.services.runtime import Runtime
from agentping.services.targeting import device_health

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/healthz", tags=["system"], summary="Liveness (does not touch the database)")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": __version__}


@router.get(
    "/readyz",
    tags=["system"],
    summary="Readiness: database reachable and schema migrated",
    responses={503: {"description": "Not ready"}},
)
async def readyz(runtime: Annotated[Runtime, Depends(get_runtime)]) -> JSONResponse:
    try:
        async with runtime.session_factory() as session:
            revision = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
    except Exception as error:
        logger.warning("readiness_failed", extra={"error_type": type(error).__name__})
        return JSONResponse({"status": "unavailable", "database": "unreachable"}, status_code=503)
    return JSONResponse({"status": "ready", "schema_revision": revision})


class Diagnostics(BaseModel):
    version: str
    environment: str
    server_time: str
    principal: dict[str, Any]
    database: dict[str, Any]
    worker: dict[str, Any]
    outbox: dict[str, Any]
    push: dict[str, Any]
    devices: list[dict[str, Any]]


@router.get(
    "/api/v1/diagnostics",
    response_model=Diagnostics,
    tags=["system"],
    responses=ERROR_RESPONSES,
    summary="Operational status for the signed-in user (worker, outbox, push, devices)",
)
async def diagnostics(
    runtime: Annotated[Runtime, Depends(get_runtime)],
    principal: Annotated[Principal, Depends(get_principal)],
) -> Diagnostics:
    principal.require(Scope.DEVICES_READ)
    now = runtime.clock.now()
    async with runtime.session_factory() as session:
        revision = (
            await session.execute(text("SELECT version_num FROM alembic_version"))
        ).scalar_one_or_none()
        heartbeats = list(
            (
                await session.execute(
                    select(WorkerHeartbeat).order_by(WorkerHeartbeat.last_seen_at.desc()).limit(5)
                )
            ).scalars()
        )
        counts = dict(
            (
                await session.execute(
                    select(OutboxJob.status, func.count())
                    .where(OutboxJob.status.in_(["pending", "running", "dead"]))
                    .group_by(OutboxJob.status)
                )
            ).all()
        )
        overdue = (
            await session.execute(
                select(func.count(), func.min(OutboxJob.run_at)).where(
                    OutboxJob.status == "pending", OutboxJob.run_at <= now - timedelta(minutes=1)
                )
            )
        ).one()
        devices = list(
            (
                await session.execute(
                    select(Device).where(
                        Device.user_id == principal.user_id,
                        Device.status == DeviceStatus.ACTIVE.value,
                    )
                )
            ).scalars()
        )
    latest = heartbeats[0] if heartbeats else None
    return Diagnostics(
        version=__version__,
        environment=runtime.settings.app_env.value,
        server_time=now.isoformat(),
        principal={
            "description": principal.describe(),
            "actor_kind": principal.actor_kind.value,
            "auth_method": principal.auth_method,
            "scopes": sorted(principal.scopes),
            "device_id": str(principal.device_id) if principal.device_id else None,
        },
        database={"reachable": True, "schema_revision": revision},
        worker={
            "embedded": runtime.settings.embedded_worker,
            "last_seen_at": latest.last_seen_at.isoformat() if latest else None,
            "last_batch_at": latest.last_batch_at.isoformat()
            if latest and latest.last_batch_at
            else None,
            "workers": [h.worker_id for h in heartbeats],
        },
        outbox={
            "pending": counts.get("pending", 0),
            "running": counts.get("running", 0),
            "dead": counts.get("dead", 0),
            "overdue": overdue[0],
            "oldest_overdue_run_at": overdue[1].isoformat() if overdue[1] else None,
        },
        push=runtime.push.status(),
        devices=[
            {"id": str(d.id), "name": d.name, **device_health(d, now).model_dump(mode="json")}
            for d in devices
        ],
    )


class WorkerRunResult(BaseModel):
    processed: int
    next_run_in_seconds: float


@router.post(
    "/internal/worker/run",
    response_model=WorkerRunResult,
    tags=["system"],
    summary="Process due outbox jobs (for scheduler/event-triggered deployments)",
)
async def run_worker(
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    worker_token: Annotated[str | None, Header(alias="X-Worker-Token")] = None,
) -> Any:
    expected = runtime.settings.worker_trigger_token.get_secret_value()
    if not expected:
        return error_response(404, "not_found", "Not Found")
    if not worker_token or not tokens.constant_time_equals(worker_token, expected):
        return error_response(401, "unauthenticated", "Invalid worker trigger token")
    worker = request.app.state.trigger_worker
    processed = await worker.drain(max_batches=50)
    return WorkerRunResult(processed=processed, next_run_in_seconds=await worker.next_delay())
