"""Security + operational middleware.

* :class:`SecurityHeadersMiddleware` — sets the baseline HTTP security response
  headers (HSTS / X-Content-Type-Options / X-Frame-Options / Referrer-Policy /
  Permissions-Policy / a conservative CSP). The audit found only CORS mounted
  (``main.py``); a self-hosted instance exposed to the internet had no CSP to
  mitigate markdown/script injection, no clickjacking protection, no HSTS.

* :class:`RequestMetricsMiddleware` — RED metrics (rate/error/duration) per
  route template, exported through the observability layer (Prometheus when
  ``PROMETHEUS_ENABLED``). Without this the backend had no request-level
  visibility — an operator could not see error rate or latency at all.

Kept dependency-free (pure Starlette). HSTS is only emitted in non-dev so it
can't force-https a localhost http dev session.
"""
from __future__ import annotations

import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings


class ApiV1AliasMiddleware:
    """Expose the current API under a versioned alias: ``/api/v1/*`` → ``/api/*``.

    The routers are mounted on ``/api`` today; the alias gives external clients
    a stable ``/api/v1`` contract from NOW (before a real v2 exists), so a
    future breaking change can move to ``/api/v2`` instead of silently breaking
    every integration. Pure ASGI (no BaseHTTPMiddleware) — a scope-path rewrite
    before routing costs nothing.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            path = scope.get("path", "")
            if path.startswith("/api/v1/"):
                scope = dict(scope)
                scope["path"] = "/api" + path[len("/api/v1"):]
            elif path == "/api/v1":
                scope = dict(scope)
                scope["path"] = "/api"
        await self.app(scope, receive, send)


class RequestMetricsMiddleware(BaseHTTPMiddleware):
    """Count requests / errors and observe latency per route template."""

    async def dispatch(self, request: Request, call_next) -> Response:
        started = time.perf_counter()
        request.state._metrics_started = started
        try:
            response = await call_next(request)
        except Exception:
            self._record(request, 500)
            raise
        self._record(request, response.status_code)
        return response

    @staticmethod
    def _record(request: Request, status_code: int) -> None:
        from app.observability import observe_counter, observe_histogram

        # Route templates ("conversations", "chat") avoid unbounded path
        # cardinality; unmatched paths (404 scans) collapse to "unmatched".
        route = request.scope.get("route")
        template = getattr(route, "path", None) or "unmatched"
        outcome = "error" if status_code >= 500 else (
            "client_error" if status_code >= 400 else "ok"
        )
        try:
            observe_counter(
                "http_requests_total", 1,
                method=request.method, route=template, outcome=outcome,
            )
            observe_histogram(
                "http_request_duration_seconds",
                max(time.perf_counter() - _started(request), 0.0),
                method=request.method, route=template,
            )
        except Exception:
            pass


def _started(request: Request) -> float:
    started = getattr(request.state, "_metrics_started", None)
    if started is None:
        # dispatch() records start inside request.state; missing only if a
        # test drives _record directly.
        started = time.perf_counter()
    return started


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    """Add conservative security response headers to every response."""

    async def dispatch(self, request: Request, call_next) -> Response:
        response = await call_next(request)
        is_dev = get_settings().is_dev
        # HSTS only over https and only outside dev (would otherwise force-https
        # a localhost http session and lock the browser out).
        if not is_dev and request.url.scheme == "https":
            response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = (
            "geolocation=(), microphone=(), camera=(), payment=(), usb=()"
        )
        # Conservative CSP: same-origin by default; allow data:/https images
        # (markdown image rendering + citation favicon cases), inline styles
        # (Tailwind/Next inject style tags), and same-origin XHR/SSE. The OpenAPI
        # docs page needs 'unsafe-inline' for scripts — gated to /docs,/redoc.
        path = request.url.path
        script_src = "'self' 'unsafe-inline'" if path in ("/docs", "/redoc") else "'self'"
        response.headers["Content-Security-Policy"] = (
            f"default-src 'self'; "
            "img-src 'self' data: https:; "
            "media-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; "
            f"script-src {script_src}; "
            "connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        return response
