"""WeChat Official Account callback (公众号扫码登录).

Two things here are protocol-mandated and easy to get wrong:

* The GET handshake must echo `echostr` as **plain text**. FastAPI serializes a
  bare ``str`` return as a QUOTED JSON string, which the WeChat console rejects
  — hence :class:`PlainTextResponse`.
* The POST reply must be **raw XML** with ``text/xml``. An empty body (no reply
  owed) is the correct response to a message we do not act on.

Signature verification runs on BOTH methods. The Token is the only credential
on this public endpoint, so an unverified POST would let anyone forge a message
push for an arbitrary openid and mint a login code for that account.

This router is also what a second deployment mirrors to, so it must be correct
whether or not it is the one WeChat is pointed at.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.core.config import get_settings
from app.core.wechat_mp import build_text_reply, parse_wechat_xml, sha1_signature
from app.services import wechat_mp_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wechat", tags=["wechat"])

# `登录` is accepted alongside the configured keyword: a follower who scanned
# after already subscribing never sees the subscribe event, and "登录" is what
# people actually type.
ALWAYS_ACCEPTED_KEYWORDS = ("登录",)

# WeChat pushes small text-only XML. The cap is not about "enough room" — it
# stops this public endpoint from reading an arbitrarily large body into memory
# (and handing it to an XML parser) before any validation.
MAX_CALLBACK_BODY_BYTES = 64 * 1024


def _require_enabled() -> None:
    if not get_settings().WECHAT_MP_ENABLED:
        raise HTTPException(status_code=503, detail="公众号登录未启用")


def _verify(signature: str, timestamp: str, nonce: str) -> None:
    expected = sha1_signature(get_settings().WECHAT_MP_TOKEN, timestamp, nonce)
    if not signature or signature != expected:
        raise HTTPException(status_code=403, detail="微信回调验签失败")


async def _read_bounded_body(request: Request) -> bytes:
    """Read the callback body with a hard cap (pre-reject, then stream-count)."""
    content_length = request.headers.get("content-length")
    if content_length:
        try:
            if int(content_length) > MAX_CALLBACK_BODY_BYTES:
                raise HTTPException(status_code=413, detail="回调内容过大")
        except ValueError:
            pass
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_CALLBACK_BODY_BYTES:
            raise HTTPException(status_code=413, detail="回调内容过大")
        chunks.append(chunk)
    return b"".join(chunks)


@router.get("/login-info")
async def wechat_login_info() -> dict:
    """Public login-page hints: whether to show a QR guide, and the keyword."""
    settings = get_settings()
    return {
        "data": {
            "configured": bool(settings.WECHAT_MP_LOGIN_QR_URL),
            "qrcode_url": settings.WECHAT_MP_LOGIN_QR_URL,
            "keyword": settings.WECHAT_MP_KEYWORD,
        }
    }


@router.get("/callback")
async def verify_callback(
    signature: str = Query(""),
    timestamp: str = Query(...),
    nonce: str = Query(...),
    echostr: str = Query(...),
) -> PlainTextResponse:
    """WeChat's server-config handshake."""
    _require_enabled()
    _verify(signature, timestamp, nonce)
    # Plain text, NOT JSON — the console compares this byte-for-byte.
    return PlainTextResponse(echostr)


@router.post("/callback")
async def receive_message(
    request: Request,
    signature: str = Query(""),
    timestamp: str = Query(...),
    nonce: str = Query(...),
) -> Response:
    """Message push: subscribe events and keyword texts get a login code."""
    _require_enabled()
    _verify(signature, timestamp, nonce)

    raw = (await _read_bounded_body(request)).decode("utf-8", errors="replace")
    try:
        message = parse_wechat_xml(raw)
    except Exception:
        # Malformed XML is not worth a 500: WeChat would retry the same body.
        logger.warning("wechat callback carried unparseable XML")
        return Response(content="", media_type="text/xml")

    from_user = message.get("FromUserName", "")
    to_user = message.get("ToUserName", "")
    msg_type = message.get("MsgType", "")

    reply_text = ""
    if msg_type == "event" and message.get("Event", "").lower() == "subscribe":
        # Scanning to follow delivers the code without any extra step.
        reply_text = "欢迎关注！" + await _code_reply(from_user, message)
    elif msg_type == "text":
        content = message.get("Content", "").strip()
        if content == get_settings().WECHAT_MP_KEYWORD or content in ALWAYS_ACCEPTED_KEYWORDS:
            reply_text = await _code_reply(from_user, message)

    if not reply_text:
        # Nothing owed: an empty body is the correct reply.
        return Response(content="", media_type="text/xml")
    return Response(
        content=build_text_reply(from_user, to_user, reply_text), media_type="text/xml"
    )


async def _code_reply(openid: str, message: dict[str, str]) -> str:
    """Issue the follower's login code, or the busy notice when withheld."""
    settings = get_settings()
    try:
        create_time = int(message.get("CreateTime", "") or 0)
    except ValueError:
        create_time = 0
    if create_time <= 0:
        # Without the message's own timestamp the two backends cannot agree on
        # a bucket, so refuse rather than emit a code only this side accepts.
        logger.warning("wechat callback without a usable CreateTime")
        return "验证码服务繁忙，请稍后重试。"

    code = await wechat_mp_service.issue_login_code(openid, create_time)
    if code is None:
        # Either the feature is unconfigured or this window's code collided with
        # another follower's. Never hand out a code we did not register.
        return "验证码服务繁忙，请稍后重试。"
    ttl = settings.WECHAT_MP_CODE_TTL_SECONDS
    # Deliberately NAMES NO APPLICATION. The same code signs the follower in to
    # every service sharing this Official Account, so naming one of them would
    # mislead anyone who scanned from the other. It is also not a wording
    # preference: WeChat shows only the primary callback's single passive reply,
    # and the message body carries nothing about which site the user wants — so
    # the text cannot be made application-specific in the first place.
    return f"您的验证码是：{code}，{max(ttl // 60, 1)} 分钟内有效。"
