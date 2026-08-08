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


class ProfileUnresolvedError(AppError):
    """The X-BioFlow-Profile header is missing, malformed, or names no profile.

    Distinct from a generic validation error because the profile picker recovers
    from it rather than reporting it: a profile id remembered in localStorage
    goes stale the moment that profile is deleted, which makes this the expected
    steady-state failure, not a bug. One code covers all three cases on purpose
    -- the picker's branch is "this id is no good, ask again", and it should not
    have to enumerate the ways an id can be no good to take it.
    """

    status_code = 400
    code = "profile_unresolved"


class WrongProfilePasswordError(AppError):
    """The password given when selecting a profile does not match.

    403 rather than 401: a 401 belongs with a `WWW-Authenticate` challenge and
    an authentication scheme, and there is none here to name. Nothing was
    authenticated, so nothing can be re-authenticated -- the request is simply
    refused.

    Read the status as "this is the wrong profile", not "access denied". The
    password is a speed bump that stops someone entering a profile they did
    not mean to open on a shared laptop; it protects nothing, because the API
    stays unauthenticated and any client can put that same profile's id in the
    X-BioFlow-Profile header and read its data without ever being asked. See
    docs/superpowers/specs/2026-07-31-profiles-design.md, "Passwords are a
    speed bump", and keep whatever replaces this from implying otherwise.

    Its own code rather than a bare 403 so the picker can re-prompt on the
    password field it already has open, instead of surfacing a generic
    failure.
    """

    status_code = 403
    code = "wrong_profile_password"


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"


class AgentUnavailableError(AppError):
    """The Pi subprocess could not be spawned or has died.

    Usually means `pi` is not on the PATH inside the api container (the image
    installs it via npm, see backend/Dockerfile) or the process crashed at
    startup. 503 rather than 500 so the drawer can show "agent unavailable"
    as a state, not a bug report.
    """

    status_code = 503
    code = "agent_unavailable"


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
