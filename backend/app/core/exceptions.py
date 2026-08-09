"""Application-level exception type + FastAPI handler registration.

Business code raises ``AppException`` with a machine-readable ``code`` and a
human-readable ``message``; the handlers registered here translate that (and the
two framework error classes) into a consistent JSON envelope::

    {"code": "<code>", "message": "<message>"}

Every response uses the same shape so the frontend has one error parser.
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppException(Exception):
    """Domain error carrying an HTTP status, a stable ``code``, and a message.

    Raise this from routers/services for any expected business failure
    (auth, not-found, bad input that pydantic can't catch, quota, etc.) instead
    of building ad-hoc HTTPExceptions.
    """

    def __init__(
        self,
        status_code: int = status.HTTP_400_BAD_REQUEST,
        code: str = "error",
        message: str = "An error occurred",
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra or {}


def _envelope(code: str, message: str, status_code: int, extra: dict[str, Any] | None = None) -> JSONResponse:
    body: dict[str, Any] = {"code": code, "message": message}
    if extra:
        body["details"] = extra
    return JSONResponse(status_code=status_code, content=body)


async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return _envelope(exc.code, exc.message, exc.status_code, exc.extra)


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Collapse pydantic validation errors into a readable message; full detail
    # list is preserved under "details" for clients that want field-level info.
    # Pydantic model validators may put the originating ValueError object in
    # ``ctx.error``; normalize it so the uniform 422 envelope is always JSON-safe.
    errors = json.loads(json.dumps(exc.errors(), default=str))
    first = errors[0] if errors else {}
    loc = ".".join(str(p) for p in first.get("loc", []) if p not in ("body",))
    msg = first.get("msg", "Validation failed")
    message = f"{loc}: {msg}" if loc else msg
    return _envelope(
        "validation",
        message or "Validation failed",
        status.HTTP_422_UNPROCESSABLE_ENTITY,
        {"errors": errors},
    )


async def _generic_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    # Never leak internal tracebacks to clients; the message is intentionally
    # generic. Structured logging elsewhere captures the real exception.
    return _envelope(
        "internal",
        "Internal server error",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Attach all error-response handlers to ``app``. Idempotent."""
    app.add_exception_handler(AppException, _app_exception_handler)
    app.add_exception_handler(RequestValidationError, _validation_exception_handler)
    app.add_exception_handler(Exception, _generic_exception_handler)
