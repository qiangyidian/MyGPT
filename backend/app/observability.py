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
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

# --------------------------------------------------------------------------- #
# Optional dependency probes. The API is identical either way.
# --------------------------------------------------------------------------- #
try:  # pragma: no cover - exercised by the presence/absence of the package
    import opentelemetry  # noqa: F401
    from opentelemetry import trace as _otel_trace  # type: ignore

    _OTEL_AVAILABLE = True
except Exception:
    _OTEL_AVAILABLE = False
    _otel_trace = None  # type: ignore

try:  # pragma: no cover
    import prometheus_client  # noqa: F401  — presence probe only

    _PROM_AVAILABLE = True
except Exception:
    _PROM_AVAILABLE = False


# --------------------------------------------------------------------------- #
# Emission gates. Package presence is necessary but NOT sufficient: the
# ``OTEL_ENABLED`` / ``PROMETHEUS_ENABLED`` flags are the real switches so an
# operator can fully disable export without uninstalling the SDK. ``get_settings``
# is imported lazily (and failures degrade to no-emit) so this module stays
# import-safe before config is loaded.
# --------------------------------------------------------------------------- #
def _emit_traces() -> bool:
    """True only when OTel is importable AND ``OTEL_ENABLED`` is on."""
    if not _OTEL_AVAILABLE:
        return False
    try:
        from app.core.config import get_settings

        return bool(get_settings().OTEL_ENABLED)
    except Exception:
        return False


def _emit_metrics() -> bool:
    """True only when prometheus_client is importable AND ``PROMETHEUS_ENABLED``."""
    if not _PROM_AVAILABLE:
        return False
    try:
        from app.core.config import get_settings

        return bool(get_settings().PROMETHEUS_ENABLED)
    except Exception:
        return False


# A FIXED, low-cardinality label schema for the Prometheus counters/histograms.
# Only these keys (when present in a counter/histogram attributes dict) become
# Prom labels; the full (still redacted) attributes survive in traces/logs and
# the test recorder. This bounds the series count by the product of distinct
# values per label (each drawn from a small domain: model names, tool names,
# outcomes) — the Task-11 review flagged the previous ``_attr_hash`` (a hash of
# the WHOLE attributes dict) as an unbounded-cardinality risk.
_METRIC_LABEL_KEYS = ("model", "tool", "outcome", "provider", "status", "operation")


def _extract_labels(attributes: dict[str, Any] | None) -> dict[str, str]:
    """Project a (redacted) attributes dict onto the fixed Prom label schema.

    Missing keys default to ``""`` so every series has the full label set
    (Prometheus requires all registered labelnames on every ``labels()`` call).
    """
    a = attributes or {}
    return {k: str(a.get(k, "")) for k in _METRIC_LABEL_KEYS}


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

    def record_exception(self, exc: BaseException) -> None:
        # No-op: nothing leaves the process.
        return None

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        return None

    def __enter__(self) -> _NoopSpan:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
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
        except Exception:
            pass

    def record_exception(self, exc: BaseException) -> None:
        try:
            self._span.record_exception(exc)
        except Exception:
            pass

    def add_event(self, name: str, attributes: dict[str, Any] | None = None) -> None:
        try:
            self._span.add_event(name, sanitize_attributes(attributes or {}))
        except Exception:
            pass

    def __enter__(self) -> _OtelSpan:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._span.end()
        except Exception:
            pass


@contextmanager
def span(
    name: str, attributes: dict[str, Any] | None = None
) -> Iterator[Any]:
    """Open a trace span (OTel when available + enabled, no-op otherwise).

    Attributes are redacted at the boundary. Safe to use unconditionally.
    Emission is gated on :func:`_emit_traces` (``OTEL_ENABLED`` + package
    presence), so an operator can disable export without uninstalling the SDK.
    """
    s = _OtelSpan(name, attributes) if _emit_traces() else _NoopSpan(name, attributes)
    with s as sp:
        yield sp


class _NoopCounter:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description

    def inc(self, amount: float = 1, attributes: dict[str, Any] | None = None) -> None:
        return None


class _PromCounter(_NoopCounter):
    def __init__(self, name: str, description: str = "") -> None:
        super().__init__(name, description)
        from prometheus_client import Counter as _Counter  # type: ignore

        # Fixed labelnames: bounded cardinality (see _METRIC_LABEL_KEYS). The
        # full attributes dict survives in traces/logs + the test recorder; only
        # the small fixed schema becomes Prom labels.
        self._c = _Counter(name, description or name, labelnames=list(_METRIC_LABEL_KEYS))

    def inc(self, amount: float = 1, attributes: dict[str, Any] | None = None) -> None:
        try:
            self._c.labels(**_extract_labels(sanitize_attributes(attributes))).inc(amount)
        except Exception:
            pass


class _NoopHistogram:
    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description

    def record(self, amount: float = 0, attributes: dict[str, Any] | None = None) -> None:
        return None


class _PromHistogram(_NoopHistogram):
    def __init__(self, name: str, description: str = "") -> None:
        super().__init__(name, description)
        from prometheus_client import Histogram as _Histogram  # type: ignore

        self._h = _Histogram(name, description or name, labelnames=list(_METRIC_LABEL_KEYS))

    def record(self, amount: float = 0, attributes: dict[str, Any] | None = None) -> None:
        try:
            self._h.labels(**_extract_labels(sanitize_attributes(attributes))).observe(amount)
        except Exception:
            pass


def counter(name: str, description: str = "") -> Any:
    """Return a counter handle (Prometheus when enabled, no-op otherwise).

    Emission is gated on :func:`_emit_metrics` (``PROMETHEUS_ENABLED`` + package
    presence). The returned handle's ``inc()`` is always safe to call.
    """
    return _PromCounter(name, description) if _emit_metrics() else _NoopCounter(name, description)


def histogram(name: str, description: str = "") -> Any:
    """Return a histogram handle (Prometheus when enabled, no-op otherwise)."""
    return (
        _PromHistogram(name, description)
        if _emit_metrics()
        else _NoopHistogram(name, description)
    )


# --------------------------------------------------------------------------- #
# One-liner instrumentation helpers + test recorders.
#
# ``observe_span`` / ``observe_counter`` / ``observe_histogram`` are the
# call-site conveniences: they sanitize attributes, route to the gated exporter,
# and (when a test recorder is set) record the observation so tests can assert
# instrumentation wiring without a real exporter. The recorders are independent
# of the emission flags — they observe the *call site*; the flags control the
# *exporter*. In production both recorders stay ``None`` so the helpers add only
# the (already cheap, no-op when off) exporter call.
# --------------------------------------------------------------------------- #
_span_recorder: list[dict[str, Any]] | None = None
_metric_recorder: list[dict[str, Any]] | None = None

# Module-level handle caches so a hot path does not rebuild the Prom handle per
# call (Prometheus clients dedupe by name internally too, but caching avoids the
# import + lookup entirely).
_counter_handles: dict[str, Any] = {}
_histogram_handles: dict[str, Any] = {}


def set_span_recorder(recorder: list[dict[str, Any]] | None) -> None:
    """Test injection: capture every ``observe_span`` open/close into ``recorder``.

    Pass ``None`` to clear (production path — zero overhead).
    """
    global _span_recorder
    _span_recorder = recorder


def set_metric_recorder(recorder: list[dict[str, Any]] | None) -> None:
    """Test injection: capture every ``observe_counter`` / ``observe_histogram``
    call into ``recorder``. Pass ``None`` to clear."""
    global _metric_recorder
    _metric_recorder = recorder


def get_span_recorder() -> list[dict[str, Any]] | None:
    return _span_recorder


def get_metric_recorder() -> list[dict[str, Any]] | None:
    return _metric_recorder


def _cached_counter(name: str, description: str = "") -> Any:
    h = _counter_handles.get(name)
    if h is None:
        h = counter(name, description)
        _counter_handles[name] = h
    return h


def _cached_histogram(name: str, description: str = "") -> Any:
    h = _histogram_handles.get(name)
    if h is None:
        h = histogram(name, description)
        _histogram_handles[name] = h
    return h


@contextmanager
def observe_span(name: str, **attributes: Any) -> Iterator[Any]:
    """Open a span named ``name`` with redacted attributes; one-liner call site.

    Records (name, attributes, duration_ms, error) into the span recorder when
    one is set, so tests can assert instrumentation wiring without a real
    exporter. Always closes the span (even on exception) and re-raises.

    Fast path: when neither traces are emitted NOR a recorder is set (the
    production default + the whole test suite without a recorder), this opens a
    bare no-op span with no timing/attribute work — the hot path pays only two
    boolean checks plus the (already cheap) no-op span open/close.
    """
    if not _emit_traces() and _span_recorder is None:
        # Fully inert: open a bare no-op span; the span still redacts internally
        # so passing raw attributes is safe.
        with span(name, attributes) as inert_sp:
            yield inert_sp
        return
    clean = sanitize_attributes(attributes)
    started = time.monotonic()
    rec_entry: dict[str, Any] | None = None
    if _span_recorder is not None:
        rec_entry = {
            "name": name,
            "attributes": dict(clean),
            "duration_ms": None,
            "error": None,
        }
        _span_recorder.append(rec_entry)
    err: BaseException | None = None
    sp: Any = None
    try:
        with span(name, clean) as opened:
            sp = opened
            yield opened
    except BaseException as exc:
        err = exc
        try:
            if sp is not None:
                sp.record_exception(exc)
        except Exception:
            pass
        raise
    finally:
        if rec_entry is not None:
            # Refresh from the span's accumulated attributes so set_attribute
            # calls inside the with-block survive into the recording (callers
            # set the outcome/error attributes after open).
            acc = getattr(sp, "_attributes", None)
            if isinstance(acc, dict):
                rec_entry["attributes"] = dict(acc)
            rec_entry["duration_ms"] = int((time.monotonic() - started) * 1000)
            rec_entry["error"] = type(err).__name__ if err is not None else None


def observe_counter(name: str, value: float = 1, **attributes: Any) -> None:
    """Increment counter ``name`` by ``value`` with redacted attributes.

    Routes to the gated exporter (no-op when ``PROMETHEUS_ENABLED`` is off) and
    records the call when a metric recorder is set. When BOTH are off (the
    production default + the whole test suite), this short-circuits before any
    attribute work so the hot path pays only the cost of two boolean checks.
    """
    if not _emit_metrics() and _metric_recorder is None:
        return
    clean = sanitize_attributes(attributes)
    if _emit_metrics():
        try:
            _cached_counter(name).inc(value, clean)
        except Exception:
            pass
    if _metric_recorder is not None:
        _metric_recorder.append(
            {"kind": "counter", "name": name, "value": value, "attributes": dict(clean)}
        )


def observe_histogram(name: str, value: float, **attributes: Any) -> None:
    """Record ``value`` into histogram ``name`` with redacted attributes."""
    if not _emit_metrics() and _metric_recorder is None:
        return
    clean = sanitize_attributes(attributes)
    if _emit_metrics():
        try:
            _cached_histogram(name).record(value, clean)
        except Exception:
            pass
    if _metric_recorder is not None:
        _metric_recorder.append(
            {"kind": "histogram", "name": name, "value": value, "attributes": dict(clean)}
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
    except Exception:
        pass


def clear_correlation_id() -> None:
    bind_correlation_id(None)


def get_correlation_id() -> str | None:
    return _CORRELATION_ID.get()


__all__ = [
    "REDACTED",
    "_METRIC_LABEL_KEYS",
    "bind_correlation_id",
    "clear_correlation_id",
    "counter",
    "get_correlation_id",
    "get_metric_recorder",
    "get_span_recorder",
    "histogram",
    "new_correlation_id",
    "observe_counter",
    "observe_histogram",
    "observe_span",
    "sanitize_attributes",
    "set_metric_recorder",
    "set_span_recorder",
    "span",
]
