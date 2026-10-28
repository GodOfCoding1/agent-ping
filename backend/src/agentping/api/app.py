"""FastAPI application factory: REST API under /api/v1 plus MCP Streamable HTTP at /mcp."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentping import __version__
from agentping.api.errors import http_error, install_error_handlers
from agentping.api.middleware import RequestContextMiddleware
from agentping.api.routes import account, devices, reminders, system
from agentping.auth.oidc import OidcVerifier
from agentping.auth.service import Authenticator
from agentping.bootstrap import build_runtime, listen_url
from agentping.config import AppEnv, AuthMode, Settings, get_settings
from agentping.mcp.server import build_mcp_server, transport_security
from agentping.push.base import close_push
from agentping.services.runtime import Runtime
from agentping.worker.runner import Worker

logger = logging.getLogger(__name__)

API_PREFIX = "/api/v1"


def create_app(
    settings: Settings | None = None,
    *,
    runtime: Runtime | None = None,
    engine: AsyncEngine | None = None,
) -> FastAPI:
    """Build the ASGI app. Tests inject a runtime with a fake clock and push sender."""
    if runtime is None:
        runtime, engine = build_runtime(settings or get_settings())
    settings = runtime.settings
    oidc = OidcVerifier(settings) if settings.auth_mode is AuthMode.OIDC else None
    authenticator = Authenticator(runtime, oidc)
    mcp = build_mcp_server(runtime, authenticator)
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        json_response=True,
        transport_security=transport_security(runtime),
        max_request_body_size=settings.max_request_bytes,
    )
    mcp_app.add_exception_handler(StarletteHTTPException, http_error)  # type: ignore[arg-type]

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        stop = asyncio.Event()
        worker_task: asyncio.Task[None] | None = None
        async with mcp.session_manager.run():
            if settings.embedded_worker:
                worker = Worker(runtime)
                worker_task = asyncio.create_task(
                    worker.run_forever(stop, listen_url=listen_url(settings))
                )
            logger.info(
                "api_started",
                extra={"version": __version__, "environment": settings.app_env.value},
            )
            try:
                yield
            finally:
                stop.set()
                runtime.signal.notify()
                if worker_task is not None:
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.wait_for(worker_task, timeout=30)
        if engine is not None:
            await engine.dispose()
        await close_push(runtime.push)

    production = settings.app_env is AppEnv.PRODUCTION
    app = FastAPI(
        title="AgentPing API",
        version=__version__,
        description="Agent-controlled notifications, reminders and alarms for Android.",
        lifespan=lifespan,
        docs_url=None if production else "/docs",
        redoc_url=None,
        openapi_url=None if production else "/openapi.json",
    )
    app.state.runtime = runtime
    app.state.authenticator = authenticator
    app.state.trigger_worker = Worker(runtime)

    install_error_handlers(app)
    app.include_router(system.router)
    for router in (reminders.router, devices.router, account.router):
        app.include_router(router, prefix=API_PREFIX)

    if settings.cors_allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=settings.cors_allowed_origins,
            allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
            allow_headers=["Authorization", "Content-Type", "Idempotency-Key", "If-Match"],
            expose_headers=["X-Request-ID"],
        )
    app.add_middleware(
        RequestContextMiddleware,
        max_body=settings.max_request_bytes,
        require_https=production,
    )
    app.mount("/", mcp_app)
    return app
