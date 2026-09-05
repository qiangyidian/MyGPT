"""SSRF guard for user-supplied endpoint URLs (model configs, connectors).

The model-provider layer accepts user-configured ``api_base_url`` values and
the server issues POSTs to them. Without a guard, any registered user can
point a config at ``http://169.254.169.254/...`` or an internal
Redis/Postgres/Qdrant port and use the returned error text / completion sample
as a read primitive into the private network.

Policy (see :func:`assert_public_http_url`):

  * scheme must be http/https;
  * no userinfo in the URL (``user:pass@host`` hides the real destination);
  * the host must resolve, and EVERY resolved address must be public —
    private/loopback/link-local/reserved/multicast/unspecified are blocked,
    including IPv4-mapped IPv6 forms;
  * bypasses: dev/test environments (local Ollama/vLLM on localhost is the
    documented workflow) and admin users (self-hosted internal endpoints are
    a legitimate admin deployment), or the explicit ``ALLOW_PRIVATE_MODEL_ENDPOINTS``
    operator opt-in.

This mirrors (and intentionally duplicates the semantics of) the http_get
tool's guard in ``app.tools.builtin`` — the tool layer resolves once and pins
the IP to close the DNS-rebinding TOCTOU window, which a config-time check
cannot do; here we defend the config/creation boundary.
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

_ALLOWED_SCHEMES = ("http", "https")


class EndpointBlockedError(ValueError):
    """Raised when a user-supplied endpoint URL targets a blocked network."""


def _is_private_ip(ip: ipaddress._BaseAddress) -> bool:
    """True for any address an SSRF-safe request must never reach."""
    if (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    ):
        return True
    if ip.version == 6:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None and _is_private_ip(mapped):
            return True
    return False


def check_url_shape(url: str) -> urlsplit.SplitResult:
    """Validate scheme + no-userinfo; returns the parsed split result.

    Raises :class:`EndpointBlockedError` on a malformed or wrong-scheme URL.
    """
    try:
        parts = urlsplit(str(url or "").strip())
    except ValueError as exc:
        raise EndpointBlockedError(f"invalid endpoint URL: {exc}") from exc
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise EndpointBlockedError(
            f"endpoint scheme must be one of {_ALLOWED_SCHEMES} (got {parts.scheme!r})"
        )
    if not parts.hostname:
        raise EndpointBlockedError("endpoint URL has no host")
    if parts.username or parts.password:
        raise EndpointBlockedError(
            "endpoint URL must not embed credentials (use the API key field)"
        )
    return parts


def host_resolves_public(hostname: str) -> bool:
    """True when ``hostname`` resolves and EVERY address is public.

    A host with mixed records (one public, one internal) is treated as
    blocked — the resolver could hand the client the internal address.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, OSError):
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except (ValueError, IndexError):
            return False
        if _is_private_ip(ip):
            return False
    return True


def assert_public_http_url(
    url: str,
    *,
    is_admin: bool = False,
    allow_private: bool = False,
) -> None:
    """Validate a user-supplied endpoint URL or raise :class:`EndpointBlockedError`.

    ``is_admin`` / ``allow_private`` skip the private-address resolution check
    (shape checks always apply) for the admin/self-hosted deployment path.
    """
    parts = check_url_shape(url)
    if is_admin or allow_private:
        return
    if not host_resolves_public(parts.hostname):
        raise EndpointBlockedError(
            "endpoint host is unresolvable or resolves to a private/internal "
            "address, which is not allowed for user-configured endpoints"
        )


def is_private_address(url: str) -> bool:
    """Best-effort: whether ``url``'s host is a private-address literal."""
    try:
        parts = check_url_shape(url)
    except EndpointBlockedError:
        return True
    try:
        return _is_private_ip(ipaddress.ip_address(parts.hostname))
    except ValueError:
        return False
