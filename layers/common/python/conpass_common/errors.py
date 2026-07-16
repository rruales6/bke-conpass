"""Typed application errors mapped to the OpenAPI Error schema + HTTP status."""
from __future__ import annotations

from typing import Any


class AppError(Exception):
    """Base for all handled errors. `code` is the stable machine-readable string."""

    status_code: int = 400
    code: str = "bad_request"

    def __init__(self, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            body["details"] = self.details
        return body


class NotConfigured(AppError):
    """A credentialed integration was invoked but its secret is missing.

    The message tells the operator exactly what to add to secrets.yaml.
    """

    status_code = 503
    code = "not_configured"


class Unauthorized(AppError):
    status_code = 401
    code = "unauthorized"


class Forbidden(AppError):
    status_code = 403
    code = "forbidden"


class NotFound(AppError):
    status_code = 404
    code = "not_found"


class Conflict(AppError):
    status_code = 409
    code = "conflict"


class TierLimit(AppError):
    status_code = 402
    code = "tier_limit"


class RateLimited(AppError):
    status_code = 429
    code = "rate_limited"


class ValidationFailed(AppError):
    status_code = 422
    code = "validation_error"


class NotImplementedYet(AppError):
    """Scaffolded endpoint whose logic lands in a later phase (Phase 3)."""

    status_code = 501
    code = "not_implemented"

    def __init__(self, phase: str = "Phase 3"):
        super().__init__(f"endpoint not implemented yet ({phase})")
