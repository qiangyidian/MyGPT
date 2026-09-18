"""Task 11 — observability redaction + no-op fallback + correlation IDs.

The app must boot and trace/metric identically whether or not the
``opentelemetry`` / ``prometheus_client`` packages are installed. These tests
exercise the no-op path (tests run with ``OTEL_SDK_DISABLED=true`` and
``prometheus_client`` is NOT in the test deps), so every call must succeed and
every sensitive attribute must be redacted before it could ever reach an
exporter.
"""
from __future__ import annotations

from app.observability import (
    bind_correlation_id,
    clear_correlation_id,
    counter,
    get_correlation_id,
    histogram,
    new_correlation_id,
    sanitize_attributes,
    scrub_text,
    span,
)


# --------------------------------------------------------------------------- #
# sanitize_attributes — the single redaction chokepoint for trace + log attrs.
# --------------------------------------------------------------------------- #
def test_trace_attributes_redact_api_keys():
    out = sanitize_attributes({"api_key": "secret"})
    assert "secret" not in out.values()
    assert out["api_key"] == "[redacted]"


def test_redacts_secret_token_authorization_bearer_password_keys():
    out = sanitize_attributes(
        {
            "client_secret": "s",
            "access_token": "t",
            "authorization": "Bearer abc",
            "bearer_token": "y",
            "user_password": "p",
            "x_apikey": "k",
        }
    )
    # Every sensitive value must be replaced — none of the originals survive.
    for v in out.values():
        assert v == "[redacted]"


def test_redacts_fernet_ciphertext():
    # Fernet tokens are url-safe base64 and start with "gAAAAA" (version byte 0x80).
    fernet = "gAAAAABmY2Fkc2pm" + "A" * 40 + "=="
    out = sanitize_attributes({"payload": fernet, "api_key": fernet})
    assert out["payload"] == "[redacted]"
    assert out["api_key"] == "[redacted]"


def test_redacts_authorization_header_value_even_for_neutral_key():
    # A bearer-token string is sensitive regardless of the key it sits under.
    out = sanitize_attributes({"header": "Bearer dGhpcyBpcyBhIHRva2Vu"})
    assert out["header"] == "[redacted]"


def test_redacts_openai_key_value_under_neutral_key():
    # The dominant OpenAI-compatible key shape (sk-...) is redacted even when
    # the key name gives no hint ("config", "model", …).
    out = sanitize_attributes({"config": "sk-proj-live-" + "x" * 30})
    assert out["config"] == "[redacted]"
    out2 = sanitize_attributes({"model": "sk-" + "A" * 24})
    assert out2["model"] == "[redacted]"


def test_redacts_jwt_value_under_neutral_key():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.SflKxwRJSMeKKF2QT4f"
    out = sanitize_attributes({"payload": jwt})
    assert out["payload"] == "[redacted]"


def test_keeps_non_sensitive_values_untouched():
    out = sanitize_attributes({"model": "gpt-4o", "tokens": 42, "latency_ms": 12.5})
    assert out == {"model": "gpt-4o", "tokens": 42, "latency_ms": 12.5}


def test_redacts_nested_dict_values():
    out = sanitize_attributes({"ctx": {"api_key": "k", "model": "x"}, "list": [{"secret": "s"}]})
    assert out["ctx"]["api_key"] == "[redacted]"
    assert out["ctx"]["model"] == "x"  # non-sensitive preserved
    assert out["list"][0]["secret"] == "[redacted]"


def test_sanitize_is_a_new_dict_does_not_mutate_input():
    src = {"api_key": "secret", "model": "x"}
    out = sanitize_attributes(src)
    assert src["api_key"] == "secret"  # input untouched
    assert out["api_key"] == "[redacted]"


# --------------------------------------------------------------------------- #
# No-op-safe span / counter / histogram — must not raise without exporters.
# --------------------------------------------------------------------------- #
def test_span_is_usable_without_otel_exporters():
    # OTEL_SDK_DISABLED=true is set in conftest → the no-op path runs. The span
    # context manager must not raise and must accept attribute + set_attribute.
    with span("model.call", {"model": "gpt-4o"}) as s:
        s.set_attribute("tokens", 10)
        s.record_exception(ValueError("boom"))  # must not raise either


def test_counter_inc_noop_safe():
    c = counter("model.calls", "model invocations")
    c.inc(1, {"model": "gpt-4o"})
    c.inc()  # default increment
    c.inc(2.5)  # float ok


def test_histogram_record_noop_safe():
    h = histogram("model.latency_ms", "model latency in ms")
    h.record(12.5, {"model": "gpt-4o"})
    h.record(0)


# --------------------------------------------------------------------------- #
# Correlation-ID context — bind per request, propagate into logs.
# --------------------------------------------------------------------------- #
def test_correlation_id_bind_and_get():
    cid = new_correlation_id()
    assert isinstance(cid, str) and len(cid) > 0
    bind_correlation_id(cid)
    try:
        assert get_correlation_id() == cid
    finally:
        clear_correlation_id()
    assert get_correlation_id() is None


def test_correlation_id_propagates_into_logs(capsys):
    # structlog's merge_contextvars processor (already in the logging chain)
    # folds the bound correlation id into every rendered log line.
    from app.core.logging import configure_logging, get_logger

    configure_logging("DEBUG")
    cid = new_correlation_id()
    bind_correlation_id(cid)
    try:
        get_logger("test.obs").info("hello world", actor="test")
    finally:
        clear_correlation_id()
    captured = capsys.readouterr().out + capsys.readouterr().err
    # The bound correlation id must appear in the rendered log line.
    assert cid in captured


# --------------------------------------------------------------------------- #
# scrub_text — the STRING-level counterpart of sanitize_attributes.
#
# sanitize_attributes answers "is this whole value a credential?" (its value
# regexes are ^...$ anchored). An upstream HTTP error body is free-form text,
# so a credential can sit in the MIDDLE of it and survive the anchored check.
# scrub_text exists for exactly that: provider error details.
# --------------------------------------------------------------------------- #
def test_scrub_text_redacts_embedded_openai_key():
    out = scrub_text("Invalid API key: sk-abcdefghijklmnopqrstuvwxyz1234")
    assert "sk-abcdefghijklmnopqrstuvwxyz1234" not in out
    assert "[redacted]" in out
    # The useful part of the message survives — that is the whole point.
    assert "Invalid API key" in out


def test_scrub_text_redacts_embedded_fernet_ciphertext():
    blob = "gAAAAABm" + "A" * 40
    out = scrub_text(f"decrypt failed for {blob} in request")
    assert blob not in out
    assert "[redacted]" in out


def test_scrub_text_redacts_bearer_header_inside_text():
    out = scrub_text("upstream rejected Authorization: Bearer abc123def456ghi789")
    assert "abc123def456ghi789" not in out
    assert "[redacted]" in out


def test_scrub_text_redacts_embedded_jwt():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1g"
    out = scrub_text(f"token {jwt} rejected")
    assert jwt not in out
    assert "[redacted]" in out


def test_scrub_text_truncates_and_bounds_length():
    out = scrub_text("x" * 5000, max_chars=300)
    assert len(out) <= 301  # 300 chars + the ellipsis marker
    assert out.startswith("xxx")


def test_scrub_text_is_safe_on_non_string_input():
    assert scrub_text(None) == ""
    assert scrub_text(12345) == ""


def test_scrub_text_leaves_ordinary_text_alone():
    msg = "Model deepseek-v4.1-flash is not supported"
    assert scrub_text(msg) == msg
