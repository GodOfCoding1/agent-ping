"""Device registration, preferences, receipts and synchronization endpoints."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request, status

from agentping.api.deps import Call, get_runtime, optional_principal
from agentping.api.errors import ERROR_RESPONSES
from agentping.auth.principal import Principal
from agentping.domain.schemas import (
    DeviceActionBatch,
    DeviceActionResult,
    DeviceReceiptBatch,
    DeviceRegister,
    DeviceRegistered,
    DeviceSelfUpdate,
    DeviceUpdate,
    DeviceView,
    PairingCodeCreated,
    ReceiptResult,
    SyncResponse,
)
from agentping.logging import request_id_var
from agentping.services import devices, sync
from agentping.services.runtime import Runtime

router = APIRouter(responses=ERROR_RESPONSES)


@router.post(
    "/devices/pairing-codes",
    response_model=PairingCodeCreated,
    status_code=status.HTTP_201_CREATED,
    tags=["devices"],
    summary="Create a short-lived one-time code to register a phone",
)
async def create_pairing_code(call: Call) -> PairingCodeCreated:
    return await call(devices.create_pairing_code)


@router.post(
    "/devices/register",
    response_model=DeviceRegistered,
    status_code=status.HTTP_201_CREATED,
    tags=["devices"],
    summary="Register (or re-register) this installation and obtain a device credential",
    description="Authenticate either with a pairing code in the body or with a user bearer "
    "token (OIDC). Re-registering the same installation_id rotates its credential.",
)
async def register_device(
    body: DeviceRegister,
    request: Request,
    runtime: Annotated[Runtime, Depends(get_runtime)],
    principal: Annotated[Principal | None, Depends(optional_principal)],
) -> DeviceRegistered:
    if principal is None:
        if runtime.rate_limiter is not None:
            client = request.client.host if request.client else "unknown"
            runtime.rate_limiter.check(f"pairing:{client}")
        return await devices.register_with_pairing_code(
            runtime, body, request_id=request_id_var.get()
        )
    async with runtime.unit(principal, request_id=request_id_var.get()) as ctx:
        return await devices.register_device(ctx, body)


@router.get("/devices", response_model=list[DeviceView], tags=["devices"])
async def list_devices(call: Call, include_inactive: bool = False) -> list[DeviceView]:
    return await call(devices.list_devices, include_inactive=include_inactive)


@router.get("/devices/me", response_model=DeviceView, tags=["devices"])
async def get_self(call: Call) -> DeviceView:
    return await call(devices.get_self)


@router.patch(
    "/devices/me",
    response_model=DeviceView,
    tags=["devices"],
    summary="Update this device: FCM token refresh, capabilities/permissions, timezone",
)
async def update_self(body: DeviceSelfUpdate, call: Call) -> DeviceView:
    return await call(devices.update_self, body)


@router.post("/devices/me/rotate-credential", response_model=DeviceRegistered, tags=["devices"])
async def rotate_credential(call: Call) -> DeviceRegistered:
    return await call(devices.rotate_device_token)


@router.post(
    "/devices/me/receipts",
    response_model=ReceiptResult,
    tags=["devices"],
    summary="Report delivery/scheduling receipts (received, scheduled, displayed, ...)",
)
async def receipts(body: DeviceReceiptBatch, call: Call) -> ReceiptResult:
    return await call(devices.record_receipts, body)


@router.patch(
    "/devices/{device_id}",
    response_model=DeviceView,
    tags=["devices"],
    summary="Rename, make primary, or change per-mode alert settings",
)
async def update_device(device_id: uuid.UUID, body: DeviceUpdate, call: Call) -> DeviceView:
    return await call(devices.update_device, device_id, body)


@router.delete(
    "/devices/{device_id}",
    response_model=DeviceView,
    tags=["devices"],
    summary="Deactivate a device and revoke its credential",
)
async def deactivate_device(device_id: uuid.UUID, call: Call) -> DeviceView:
    return await call(devices.deactivate_device, device_id)


@router.get(
    "/sync",
    response_model=SyncResponse,
    tags=["sync"],
    summary="Incremental schedule sync (omit cursor for a full snapshot)",
)
async def get_sync(
    call: Call,
    cursor: Annotated[int | None, Query(ge=0)] = None,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
) -> SyncResponse:
    return await call(sync.sync, cursor, limit=limit)


@router.post(
    "/sync/actions",
    response_model=list[DeviceActionResult],
    tags=["sync"],
    summary="Replay queued offline acknowledge/snooze/skip actions",
)
async def post_actions(body: DeviceActionBatch, call: Call) -> list[DeviceActionResult]:
    return await call(sync.apply_device_actions, body)
