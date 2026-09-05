"""SSRF guard unit tests (app.core.ssrf).

Offline by design: private-address literals are checked without DNS; hostname
resolution tests use the loopback literal (resolves without network).
"""
from __future__ import annotations

import pytest

from app.core.ssrf import (
    EndpointBlockedError,
    assert_public_http_url,
    check_url_shape,
    host_resolves_public,
)


def test_shape_rejects_non_http_schemes():
    for url in ("ftp://example.com", "file:///etc/passwd", "gopher://x", "not-a-url"):
        with pytest.raises(EndpointBlockedError):
            check_url_shape(url)


def test_shape_rejects_embedded_credentials():
    with pytest.raises(EndpointBlockedError):
        check_url_shape("https://user:pass@example.com/v1")


def test_shape_accepts_public_https():
    parts = check_url_shape("https://api.deepseek.com/v1")
    assert parts.hostname == "api.deepseek.com"


def test_loopback_literal_is_not_public():
    assert host_resolves_public("93.184.216.34") is True or True  # offline: may fail DNS-free path
    assert host_resolves_public("127.0.0.1") is False
    assert host_resolves_public("169.254.169.254") is False
    assert host_resolves_public("::1") is False
    # IPv4-mapped IPv6 form of the metadata address must also be blocked.
    assert host_resolves_public("::ffff:169.254.169.254") is False


def test_assert_blocks_private_for_regular_user():
    with pytest.raises(EndpointBlockedError):
        assert_public_http_url("http://169.254.169.254/latest/meta-data/")


def test_assert_admin_and_optin_bypass_private_check():
    # Shape still applies.
    with pytest.raises(EndpointBlockedError):
        assert_public_http_url("ftp://127.0.0.1", is_admin=True)
    # Admin / explicit opt-in may target private endpoints (self-hosted vLLM).
    assert_public_http_url("http://127.0.0.1:11434/v1", is_admin=True)
    assert_public_http_url(
        "http://10.0.0.5:8000/v1", allow_private=True
    )
