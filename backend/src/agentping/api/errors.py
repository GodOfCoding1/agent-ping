"""Stable API error format: {"error": {"code", "message", "details", "request_id"}}."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from agentping.domain.errors import DomainError, Unauthenticated
from agentping.logging import request_id_var

logger = logging.getLogger(__name__)


class ErrorBody(BaseModel):
    code: str
    message: str
    details: dict[str, Any] = {}
    request_id: str | None = None


class ErrorResponse(BaseModel):
    error: ErrorBody


def error_response(
    status: int, code: str, message: str, details: dict[str, Any] | None = None
) -> JSONResponse:
    body = ErrorResponse(
        error=ErrorBody(
            code=code, message=message, details=details or {}, request_id=request_id_var.get()
        )
    )
    headers = {}
    if status == 401:
        headers["WWW-Authenticate"] = 'Bearer realm="agentping"'
    if status == 429 and details and "retry_after_seconds" in details:
        headers["Retry-After"] = str(max(1, int(float(details["retry_after_seconds"]) + 0.5)))
    return JSONResponse(body.model_dump(mode="json"), status_code=status, headers=headers)


def _validation_details(errors: list[Any]) -> dict[str, Any]:
    return {
        "errors": [
            {
                "field": ".".join(str(p) for p in error.get("loc", []) if p != "body"),
                "message": error.get("msg", "invalid"),
                "type": error.get("type", "invalid"),
            }
            for error in errors
        ]
    }


ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    code: {"model": ErrorResponse} for code in (400, 401, 403, 404, 409, 413, 422, 429)
}


_HTTP_CODES = {401: "unauthenticated", 404: "not_found", 405: "method_not_allowed"}


async def http_error(_: Request, error: StarletteHTTPException) -> JSONResponse:
    return error_response(
        error.status_code, _HTTP_CODES.get(error.status_code, "http_error"), str(error.detail)
    )


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def domain_error(_: Request, error: DomainError) -> JSONResponse:
        if isinstance(error, Unauthenticated):
            logger.info("auth_rejected", extra={"code": error.code})
        return error_response(error.http_status, error.code, error.message, error.details)

    @app.exception_handler(RequestValidationError)
    async def request_validation(_: Request, error: RequestValidationError) -> JSONResponse:
        return error_response(
            422,
            "validation_failed",
            "Request validation failed",
            _validation_details(list(error.errors())),
        )

    @app.exception_handler(ValidationError)
    async def model_validation(_: Request, error: ValidationError) -> JSONResponse:
        return error_response(
            422,
            "validation_failed",
            "Request validation failed",
            _validation_details(list(error.errors(include_url=False))),
        )

    app.add_exception_handler(StarletteHTTPException, http_error)  # type: ignore[arg-type]

    @app.exception_handler(Exception)
    async def unexpected(_: Request, error: Exception) -> JSONResponse:
        logger.exception("unhandled_error", extra={"error_type": type(error).__name__})
        return error_response(500, "internal_error", "An unexpected error occurred")
