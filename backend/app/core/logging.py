"""Structured logging via structlog. Configure once at import.

Task 11 adds two concerns:

  * **Sensitive-data redaction** — a structlog processor scrubs any event key
    whose name looks secret (api_key, secret, token, authorization, password,
    …) or any value that looks like Fernet ciphertext / a Bearer header. This
    is defense-in-depth on top of :func:`app.observability.sanitize_attributes`
    (which guards trace attrs): a call site that accidentally logs a credential
    is redacted before it reaches the renderer.

  * **Correlation-ID binding** — every structured log line merges structlog
    contextvars, so a correlation id bound via
    :func:`app.observability.bind_correlation_id` (set per request by the
    CorrelationIdMiddleware) lands in every line automatically.
"""
from __future__ import annotations

import logging
import sys

import structlog

from app.core.config import get_settings


def _redact_processor(logger, method_name, event_dict):
    """structlog processor: redact sensitive keys/values before rendering.

    Routes the event dict through :func:`sanitize_attributes` so a leaked
    credential in a log call is replaced with ``"[redacted]"``. Cheap (single
    dict walk) and never raises — logging must not crash the caller.
    """
    try:
        from app.observability import sanitize_attributes

        return sanitize_attributes(event_dict)
    except Exception:
        return event_dict


def configure_logging(level: str = "INFO", *, json_output: bool | None = None) -> None:
    """Configure structlog for the API, worker and recovery processes.

    ``json_output`` defaults to "on in non-dev": human-readable console output
    in development, machine-parseable JSON in production (the previous
    unconditional ConsoleRenderer left production logs in three different
    plain-text formats across the three processes — uncollectable/uncorrelatable).
    """
    if json_output is None:
        json_output = not get_settings().is_dev
    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer()
    )
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            _redact_processor,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level, logging.INFO)),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None):
    return structlog.get_logger(name)
