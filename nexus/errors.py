from __future__ import annotations

from typing import Optional


class NexusError(Exception):
    status_code: int = 500
    error_code: str = "NEXUS_ERROR"

    def __init__(
        self,
        message: str = "",
        code: Optional[str] = None,
        status_code: Optional[int] = None,
        details: Optional[dict[str, object]] = None,
    ) -> None:
        self.message = message or self.__class__.__name__
        self.error_code = code or self.error_code
        self.status_code = status_code or self.status_code
        self.details = details or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, object]:
        result: dict[str, object] = {
            "error_code": self.error_code,
            "message": self.message,
        }
        if self.details:
            result["details"] = self.details
        return result


class ConfigError(NexusError):
    status_code = 500
    error_code = "CONFIG_ERROR"


class DatabaseError(NexusError):
    status_code = 500
    error_code = "DATABASE_ERROR"


class AuthError(NexusError):
    status_code = 401
    error_code = "AUTH_ERROR"


class NotFoundError(NexusError):
    status_code = 404
    error_code = "NOT_FOUND"


class ValidationError(NexusError):
    status_code = 422
    error_code = "VALIDATION_ERROR"


class ExternalServiceError(NexusError):
    status_code = 502
    error_code = "EXTERNAL_SERVICE_ERROR"


class RateLimitError(NexusError):
    status_code = 429
    error_code = "RATE_LIMIT_EXCEEDED"


class ForbiddenError(NexusError):
    status_code = 403
    error_code = "FORBIDDEN"


class ConflictError(NexusError):
    status_code = 409
    error_code = "CONFLICT"
