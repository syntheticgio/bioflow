"""Application error hierarchy and FastAPI exception handlers.

Errors carry a stable machine-readable `code` so the frontend can branch on the
cause rather than parsing prose.
"""

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse


class AppError(Exception):
    """Base for all deliberate application errors."""

    status_code: int = 500
    code: str = "internal_error"

    def __init__(self, message: str, *, details: dict | None = None, code: str | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or {}
        if code:
            self.code = code

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFoundError(AppError):
    status_code = 404
    code = "not_found"


class ConflictError(AppError):
    status_code = 409
    code = "conflict"


class ValidationError(AppError):
    status_code = 422
    code = "validation_error"


class StorageUnavailableError(AppError):
    """BIOINFO_HOME is missing, unwritable, or the sentinel is absent.

    Almost always means the external drive unmounted or was never shared with
    Docker Desktop. Deliberately distinct from a generic 500 so the UI can show
    actionable instructions instead of a stack trace.
    """

    status_code = 503
    code = "storage_unavailable"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


# --- Job handler errors (queue semantics) ---


class RetryableError(AppError):
    """Transient failure. The job goes back to the queue with backoff."""

    code = "retryable"


class PermanentError(AppError):
    """The job can never succeed as specified. Fails immediately, no retry."""

    code = "permanent"


class JobCancelled(Exception):
    """Raised inside a handler when cancellation has been requested."""


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _app_error(_: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict())
