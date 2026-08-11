"""OpenTelemetry-compatible traces + Prometheus metrics with no-op fallbacks.

The app must boot and run identically whether or not ``opentelemetry`` and
``prometheus_client`` are installed. At import we probe for both packages:

  * if present → real span/counter/histogram adapters backed by the SDK;
  * if absent  → the SAME API surface as pure-Python no-ops.

Callers therefore never branch on availability: ``with span("model.call"): ...``
works unconditionally and silently degrades to a no-op when exporters are
missing. Tests run with ``OTEL_SDK_DISABLED=true`` and without
``prometheus_client``, exercising the no-op path.

Sensitive-data redaction: :func:`sanitize_attributes` is the single chokepoint
that scrubs api keys / secrets / bearer tokens / Fernet ciphertext from any
attributes BEFORE they are handed to a span or a log. Every span/counter/histogram
adapter routes its attributes through it so a leak can't slip through an
unredacted call site.
"""
from __future__ import annotations

import contextvars
import re
import uuid
from contextlib import contextmanager
from typing import Any, Iterator

# --------------------------------------------------------------------------- #
# Optional dependency probes. The API is identical either way.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - exercised by the presence/absence of the package
    import opentelemetry  # noqa: F401
    from opentelemetry import trace as _otel_trace  # type: ignore

    _OTEL_AVAILABLE = True
except Exception:  # noqa: BLE001 — any import failure → no-op
    _OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore

try:  # pragma: no cover
    import prometheus_client  # noqa: F401  — presence probe only

    _PROM_AVAILABLE = True
except Exception:  # noqa: BLE001
    _PROM_AVAILABLE = False


REDACTED = "[redacted]"

# Keys whose presence (case-insensitive, WHOLE-WORD match) marks a value secret.
# Word boundaries are [a-z0-9] so that "tokens" (plural, a count) is preserved
# while "access_token" (a secret) is redacted, and "author" is preserved while
# "auth" / "authorization" are redacted.
_SENSITIVE_KEY_TOKENS = (
    "api_key",
    "apikey",
    "api-key",
    "secret",
    "token",
    "authorization",
    "auth",
    "bearer",
    "password",
    "passwd",
    "pwd",
    "credential",
    "private_key",
    "access_key",
    "refresh_token",
    "client_secret",
)

# A single compiled alternation with non-alphanumeric boundaries on both sides.
# Boundary chars are [a-z0-9]; underscore, dash, dot, and string edges all count
# as boundaries (so "api_key" is one segment bounded by "_").
_SENSITIVE_KEY_RE = re.compile(
    r"(?<![a-z0-9])(" + "|".join(re.escape(t) for t in _SENSITIVE_KEY_TOKENS) + r")(?![a-z0-9])",
    re.IGNORECASE,
)

# Fernet ciphertext: url-safe base64 starting with the version byte 0x80 which
# encodes as "gAAAAA". Match a long-enough tail so ordinary strings don't hit.
_FERNET_RE = re.compile(r"^gAAAAA[A-Za-z0-9_\-]{20,}={0,2}$")

# A "Bearer <token>" / "Basic <token>" header value, even under a neutral key.
_BEARER_RE = re.compile(r"^(bearer|basic|token)\s+\S+$", re.IGNORECASE)

# Raw credential VALUES under neutral keys (defense-in-depth on top of the
# key-name rules — a value that *looks* like a known credential shape is
# redacted regardless of the key it sits under):
#   * OpenAI-compatible API keys: ``sk-...`` (sk-proj-, sk-live-, sk-...).
#     The dominant live-secret shape in the wild; 20+ chars after the prefix.
_OPENAI_KEY_RE = re.compile(r"^sk-[A-Za-z0-9_\-]{20,}$")
#   * JWTs: three base64url segments; the first decodes to a ``{"..."`` JSON
#     header, which base64-encodes to a string starting ``eyJ``. Requiring the
#     first dot rules out ordinary ``eyJ``-prefixed strings.
_JWT_RE = re.compile(r"^eyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")


def _is_sensitive_key(key: str) -> bool:
    return bool(_SENSITIVE_KEY_RE.search(str(key)))


def _is_sensitive_value(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    if _FERNET_RE.match(value):
        return True
    if _BEARER_RE.match(value):
        return True
    if _OPENAI_KEY_RE.match(value):
        return True
    if _JWT_RE.match(value):
        return True
    return False


def sanitize_attributes(attrs: Any) -> dict[str, Any]:
    """Return a copy of ``attrs`` with every sensitive value replaced.

    Redaction rules (defense in depth):
      * any value whose KEY contains a sensitive token (api_key, secret, token,
        authorization, bearer, password, credential, …) → ``"[redacted]"``;
      * any VALUE that looks like Fernet ciphertext (``gAAAAA…``) → redacted,
        so an encrypted API key blob can't leak even under a neutral key;
      * any VALUE that looks like an ``Authorization: Bearer …`` header → redacted;
      * nested dict / list / mapping values are recursed into.

    The input is never mutated.
    """
    return _sanitize(attrs)  # type: ignore[return-value]


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for k, v in value.items():
            if _is_sensitive_key(k):
                out[k] = REDACTED
            else:
                out[k] = _sanitize(v)
        return out
    if isinstance(value, list):
        return [_sanitize(v) for v in value]
    if isinstance(value, tuple):
        return tuple(_sanitize(v) for v in value)
    if _is_sensitive_value(value):
        return REDACTED
    return value


# --------------------------------------------------------------------------- #
# Span / counter / histogram — real when the SDK is present, no-op otherwise.
# --------------------------------------------------------------------------- #
class _NoopSpan:
    """A span that accepts attributes/exceptions/events but records nothing.

    Used both when ``opentelemetry`` is absent AND when the SDK is disabled
    (``OTEL_SDK_DISABLED=true``), so test runs trace the no-op path.
    """

    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        self.name = name
        self._attributes: dict[str, Any] = dict(sanitize_attributes(attributes or {}))

    def set_attribute(self, key: str, value: Any) -> None:
        self._attributes[key] = _sanitize(value)

    def record_exception(self, exc: BaseException) -> None:  # noqa: D401
        # No-op: nothing leaves the process.
        return None

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        return None

    def __enter__(self) -> "_NoopSpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:  # noqa: ANN001
        return None


class _OtelSpan(_NoopSpan):
    """Real OTel span, redacting attributes at the boundary."""

    def __init__(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        super().__init__(name, attributes)
        self._span = _otel_trace.get_tracer(__name__).start_span(  # type: ignore[union-attr]
            name, attributes=self._attributes
        )

    def set_attribute(self, key: str, value: Any) -> None:
        super().set_attribute(key, value)
        try:
            self._span.set_attribute(key, _sanitize(value))
        except Exception:  # noqa: BLE001 — never let tracing crash the call
            pass

    def record_exception(self, exc: BaseException) -> None:
        try:
            self._span.record_exception(exc)
        except Exception:  # noqa: BLE001
            pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        try:
            self._span.add_event(name, sanitize_attributes(attributes or {}))
        except Exception:  # noqa: BLE001
            pass

    def __enter__(self) -> "_OtelSpan":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._span.end()
        except Exception:  # noqa: BLE001
            pass


@contextmanager
def span(
    name: str, attributes: dict[str, Any] | None = None
) -> Iterator[Any]:
    """Open a trace span (OTel when available + enabled, no-op otherwise).

    Attributes are redacted at the boundary. Safe to use unconditionally.
    """
    s = _OtelSpan(name, attributes) if _OTEL_AVAILABLE else _NoopSpan(name, attributes)
    with s as sp:
        yield sp


class _NoopCounter:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description

    def inc(self, amount: float | int = 1, attributes: dict[str, Any] | None = None) -> None:
        return None


class _PromCounter(_NoopCounter):
    def __init__(self, name: str, description: str = "") -> None:
        super().__init__(name, description)
        from prometheus_client import Counter as _Counter  # type: ignore

        # labelnames kept generic; attributes are passed per-inc.
        self._c = _Counter(name, description or name, labelnames=("_attr_hash",))

    def inc(self, amount: float | int = 1, attributes: dict[str, Any] | None = None) -> None:
        try:
            # Collapsing attrs into a stable hash keeps the label cardinality
            # bounded while still letting rich attrs survive in traces/logs.
            key = str(sorted((sanitize_attributes(attributes or {})).items()))
            self._c.labels(_attr_hash=key).inc(amount)
        except Exception:  # noqa: BLE001 — metrics must never break the call
            pass


class _NoopHistogram:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description

    def record(self, amount: float | int = 0, attributes: dict[str, Any] | None = None) -> None:
        return None


class _PromHistogram(_NoopHistogram):
    def __init__(self, name: str, description: str = "") -> None:
        super().__init__(name, description)
        from prometheus_client import Histogram as _Histogram  # type: ignore

        self._h = _Histogram(name, description or name, labelnames=("_attr_hash",))

    def record(self, amount: float | int = 0, attributes: dict[str, Any] | None = None) -> None:
        try:
            key = str(sorted((sanitize_attributes(attributes or {})).items()))
            self._h.labels(_attr_hash=key).observe(amount)
        except Exception:  # noqa: BLE001
            pass


def counter(name: str, description: str = "") -> Any:
    """Return a counter handle (Prometheus when present, no-op otherwise)."""
    return _PromCounter(name, description) if _PROM_AVAILABLE else _NoopCounter(name, description)


def histogram(name: str, description: str = "") -> Any:
    """Return a histogram handle (Prometheus when present, no-op otherwise)."""
    return (
        _PromHistogram(name, description)
        if _PROM_AVAILABLE
        else _NoopHistogram(name, description)
    )


# --------------------------------------------------------------------------- #
# Correlation-ID context (per-request; propagated into logs via structlog
# contextvars, which the logging config already merges).
# --------------------------------------------------------------------------- #
_CORRELATION_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "correlation_id", default=None
)


def new_correlation_id() -> str:
    """Mint a fresh correlation id."""
    return uuid.uuid4().hex


def bind_correlation_id(cid: str | None) -> None:
    """Bind ``cid`` as the current correlation id.

    Also binds it into structlog's contextvars so every structured log line
    rendered downstream includes ``correlation_id`` automatically (the logging
    config chains ``merge_contextvars``).
    """
    _CORRELATION_ID.set(cid)
    try:
        import structlog

        if cid is None:
            structlog.contextvars.unbind_contextvars("correlation_id")
        else:
            structlog.contextvars.bind_contextvars(correlation_id=cid)
    except Exception:  # noqa: BLE001 — structlog optional at this layer
        pass


def clear_correlation_id() -> None:
    bind_correlation_id(None)


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


__all__ = [
    "REDACTED",
    "bind_correlation_id",
    "clear_correlation_id",
    "counter",
    "get_correlation_id",
    "histogram",
    "new_correlation_id",
    "sanitize_attributes",
    "span",
]
