"""WeChat Official Account (公众号) protocol helpers.

Pure functions only: signature verification, plaintext XML in/out, and the
DERIVED login code. No I/O, no settings access — the values come in as
arguments so both backends (MyChat and sql2er) can be tested against an
identical golden-vector table.

Why the login code is derived rather than random
------------------------------------------------
MyChat and sql2er share ONE WeChat Official Account, and WeChat's server
config accepts exactly one callback URL per account. A `mirror` directive
delivers the same message to both backends, but WeChat displays only ONE
passive reply — so if each backend invented its own random code, the user
would see one backend's code and be unable to log in to the other.

Deriving the code from (openid, the message's own CreateTime) under a shared
secret makes both backends compute the same value independently, with no shared
storage and no runtime coupling between them.

CreateTime — read out of the message body — rather than server time is
deliberate on two counts:

* nginx `mirror` copies the same XML to both backends, so a clock skew between
  the two servers cannot make them disagree. No bucketing is needed: both sides
  read the identical bytes.
* It makes the code fresh per MESSAGE rather than per time window. WeChat's
  5-second retry re-sends the identical message (same CreateTime) and therefore
  derives the same code, preserving the "reuse the pending code on retry"
  behaviour — while a user who deliberately asks again sends a NEW message whose
  CreateTime differs, and so gets a genuinely new code. Bucketing by time would
  hand back the same code for every request inside the window, which would let a
  code that has already been consumed be brought back to life simply by asking
  again.
"""
from __future__ import annotations

import hashlib
import hmac
from xml.etree import ElementTree as ET

_CODE_MODULUS = 1_000_000


def derive_login_code(openid: str, create_time: int, token: str) -> str:
    """Return the 6-digit login code for ``openid`` from this message.

    Deterministic by construction: the same (openid, CreateTime, token) always
    yields the same code, on any backend. Always 6 characters (zero-padded) so
    what the user reads in WeChat is exactly what they type.
    """
    digest = hmac.new(
        token.encode(),
        f"{openid}:{create_time}".encode(),
        hashlib.sha256,
    ).digest()
    return f"{int.from_bytes(digest, 'big') % _CODE_MODULUS:06d}"


def sha1_signature(token: str, timestamp: str, nonce: str) -> str:
    # WeChat Official Account callback verification REQUIRES SHA-1 over the
    # sorted (token, timestamp, nonce) tuple — protocol-mandated, not a
    # password/credential hash. nosec: this is the documented algorithm.
    values = sorted([token, timestamp, nonce])
    return hashlib.sha1("".join(values).encode()).hexdigest()  # nosec B324


def parse_wechat_xml(body: str) -> dict[str, str]:
    # WeChat callbacks are small text-only XML; parsed for field extraction
    # only. ElementTree does not fetch external entities, but it does expand
    # internal ones (billion-laughs), so the body size is bounded by the caller
    # — app.api.wechat caps it at 64 KB before this function is reached. Keep
    # that bound if the caller ever changes.
    root = ET.fromstring(body)  # nosec B314 B405
    return {child.tag: (child.text or "") for child in root}


def build_text_reply(to_user: str, from_user: str, content: str) -> str:
    """Build a plaintext passive reply. ``to_user`` is the WeChat sender."""
    return (
        "<xml>"
        f"<ToUserName><![CDATA[{to_user}]]></ToUserName>"
        f"<FromUserName><![CDATA[{from_user}]]></FromUserName>"
        "<CreateTime>12345678</CreateTime>"
        "<MsgType><![CDATA[text]]></MsgType>"
        f"<Content><![CDATA[{content}]]></Content>"
        "</xml>"
    )
