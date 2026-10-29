"""ASGI middleware: request IDs, body size limits, HTTPS enforcement, security headers."""

from __future__ import annotations

import json
import logging
import time
import uuid

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from agentping.logging import request_id_var

logger = logging.getLogger("agentping.access")


async def _send_error(send: Send, status: int, code: str, message: str) -> None:
    body = json.dumps(
        {
            "error": {
                "code": code,
                "message": message,
                "details": {},
                "request_id": request_id_var.get(),
            }
        }
    ).encode()
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class RequestContextMiddleware:
    """Assigns a request ID, enforces body limits/HTTPS and logs one line per request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        max_body: int,
        require_https: bool,
        application_token_header: str = "Authorization",
    ) -> None:
        self.app = app
        self.max_body = max_body
        self.require_https = require_https
        self.application_token_header = application_token_header.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        if self.application_token_header != "authorization":
            # IAM consumes its bearer token at Google's frontend. Keep the application's
            # independently verified credential in a separate header, for REST and MCP.
            app_token = headers.get(self.application_token_header)
            scope = dict(scope)
            scope["headers"] = [
                (k, v) for k, v in scope.get("headers", []) if k.lower() != b"authorization"
            ]
            if app_token:
                scope["headers"].append((b"authorization", app_token.encode()))
        incoming = headers.get("x-request-id", "")
        request_id = (
            incoming if 8 <= len(incoming) <= 64 and incoming.isascii() else uuid.uuid4().hex
        )
        token = request_id_var.set(request_id)
        started = time.perf_counter()
        status_holder = {"status": 500}
        path = scope.get("path", "")
        try:
            if self.require_https and path not in ("/health", "/healthz", "/readyz"):
                proto = headers.get("x-forwarded-proto", scope.get("scheme", "http"))
                if proto != "https":
                    status_holder["status"] = 403
                    await _send_error(send, 403, "https_required", "HTTPS is required")
                    return
            length = headers.get("content-length")
            if length is not None and length.isdigit() and int(length) > self.max_body:
                status_holder["status"] = 413
                await _send_error(send, 413, "payload_too_large", "Request body is too large")
                return
            received = 0

            async def limited_receive() -> Message:
                nonlocal received
                message = await receive()
                if message["type"] == "http.request":
                    received += len(message.get("body", b""))
                    if received > self.max_body:
                        raise BodyTooLarge()
                return message

            async def wrapped_send(message: Message) -> None:
                if message["type"] == "http.response.start":
                    status_holder["status"] = message["status"]
                    extra = [
                        (b"x-request-id", request_id.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"cache-control", b"no-store"),
                    ]
                    response_headers = list(message.get("headers", []))
                    if not any(k.lower() == b"referrer-policy" for k, _ in response_headers):
                        extra.append((b"referrer-policy", b"no-referrer"))
                    if self.require_https:
                        extra.append((b"strict-transport-security", b"max-age=31536000"))
                    message["headers"] = response_headers + extra
                await send(message)

            try:
                await self.app(scope, limited_receive, wrapped_send)
            except BodyTooLarge:
                status_holder["status"] = 413
                await _send_error(send, 413, "payload_too_large", "Request body is too large")
        finally:
            if path not in ("/health", "/healthz"):
                logger.info(
                    "request",
                    extra={
                        "method": scope.get("method"),
                        "path": path,
                        "status": status_holder["status"],
                        "duration_ms": round((time.perf_counter() - started) * 1000, 1),
                    },
                )
            request_id_var.reset(token)


class BodyTooLarge(Exception):
    pass
