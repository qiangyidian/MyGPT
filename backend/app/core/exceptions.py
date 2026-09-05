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

from fastapi import FastAPI, HTTPException, Request, status
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


def _sanitize_validation_errors(errors: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep useful field diagnostics without reflecting the submitted body.

    Pydantic's error dictionaries can contain the complete request in ``input``
    and arbitrary validator state in ``ctx``. Validation responses are public,
    so allow-list only the stable, non-secret diagnostic fields.
    """
    allowed = ("loc", "msg", "type", "url")
    return [{key: error[key] for key in allowed if key in error} for error in errors]


async def _app_exception_handler(request: Request, exc: AppException) -> JSONResponse:
    return _envelope(exc.code, exc.message, exc.status_code, exc.extra)


async def _validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Collapse pydantic validation errors into a readable message. Safe
    # field-level diagnostics remain under ``details``; submitted input and
    # validator context are intentionally excluded because either can contain
    # credentials.
    errors = _sanitize_validation_errors(exc.errors())
    errors = json.loads(json.dumps(errors, default=str))
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


async def _http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Map Starlette/FastAPI ``HTTPException`` onto the same envelope.

    The routers raise plain ``HTTPException`` in ~80 places; without this
    handler those responses bypassed the envelope and used Starlette's
    ``{"detail": ...}`` shape — the frontend needed two error parsers and the
    machine-readable ``code`` was lost. ``detail`` is echoed inside ``message``
    so no existing client reading ``detail`` breaks.
    """
    detail = exc.detail if isinstance(exc.detail, str) else "error"
    headers = getattr(exc, "headers", None)
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": _code_for_status(exc.status_code), "message": detail, "detail": detail},
        headers=headers,
    )


_STATUS_CODES: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    422: "validation",
    429: "rate_limited",
}


def _code_for_status(status_code: int) -> str:
    return _STATUS_CODES.get(status_code, f"http_{status_code}")


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
    app.add_exception_handler(HTTPException, _http_exception_handler)
    app.add_exception_handler(Exception, _generic_exception_handler)
