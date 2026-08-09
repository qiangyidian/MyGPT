"""Security + operational middleware.

* :class:`SecurityHeadersMiddleware` — sets the baseline HTTP security response
  headers (HSTS / X-Content-Type-Options / X-Frame-Options / Referrer-Policy /
  Permissions-Policy / a conservative CSP). The audit found only CORS mounted
  (``main.py``); a self-hosted instance exposed to the internet had no CSP to
  mitigate markdown/script injection, no clickjacking protection, no HSTS.

Kept dependency-free (pure Starlette). HSTS is only emitted in non-dev so it
can't force-https a localhost http dev session.
"""
from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import get_settings


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
