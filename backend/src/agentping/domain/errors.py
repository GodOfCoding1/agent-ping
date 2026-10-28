"""Domain errors with stable machine-readable codes shared by REST and MCP."""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class ValidationFailed(DomainError):
    code = "validation_failed"
    http_status = 422


class InvalidSchedule(ValidationFailed):
    code = "invalid_schedule"


class AmbiguousDateTime(ValidationFailed):
    code = "ambiguous_datetime"


class NotFound(DomainError):
    code = "not_found"
    http_status = 404


class Unauthenticated(DomainError):
    code = "unauthenticated"
    http_status = 401


class Forbidden(DomainError):
    code = "forbidden"
    http_status = 403


class DeviceNotAccessible(Forbidden):
    code = "device_not_accessible"


class Conflict(DomainError):
    code = "conflict"
    http_status = 409


class RevisionConflict(Conflict):
    code = "revision_conflict"


class IdempotencyConflict(Conflict):
    code = "idempotency_conflict"


class InvalidTransition(Conflict):
    code = "invalid_state_transition"


class LimitExceeded(DomainError):
    code = "limit_exceeded"
    http_status = 429


class RateLimited(LimitExceeded):
    code = "rate_limited"


class PayloadTooLarge(DomainError):
    code = "payload_too_large"
    http_status = 413
