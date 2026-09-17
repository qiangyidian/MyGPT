"""Client for the wechat-auth service.

MyChat no longer talks to WeChat at all: it does not receive the callback, does
not write the reply, and does not mint login codes. It asks wechat-auth to
redeem a code the follower typed and gets back an openid. See that service's
docs/design.md for why the split exists.

Everything here is server-to-server over loopback. The app-facing endpoints are
deliberately not exposed publicly, so ``base_url`` should stay a private
address.
"""
from __future__ import annotations

import logging

import httpx

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class WechatAuthError(Exception):
    """The code could not be redeemed (bad/expired/already used/wrong app)."""


class WechatAuthUnavailable(Exception):
    """wechat-auth could not be reached, or rejected our credentials.

    Distinct from :class:`WechatAuthError`: this is an operator problem (the
    service down, or an app secret out of sync), so it must not be shown to the
    user as "your code is wrong".
    """


def _headers() -> dict[str, str]:
    settings = get_settings()
    return {
        "X-App-Id": settings.WECHAT_AUTH_APP_ID,
        "X-App-Secret": settings.WECHAT_AUTH_APP_SECRET,
    }


def _base_url() -> str:
    return get_settings().WECHAT_AUTH_BASE_URL.rstrip("/")


def _timeout() -> float:
    return get_settings().WECHAT_AUTH_TIMEOUT_SECONDS


async def verify_code(code: str) -> str:
    """Redeem ``code`` and return the WeChat openid behind it.

    Raises:
        WechatAuthError: the code is not valid for this application.
        WechatAuthUnavailable: the service is unreachable or refused us.
    """
    url = f"{_base_url()}/api/v1/verify"
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.post(url, json={"code": code}, headers=_headers())
    except httpx.HTTPError as exc:
        # logger.exception, not logger.error: inside an except block the
        # traceback is the useful part.
        logger.exception("wechat-auth unreachable at %s", url)
        raise WechatAuthUnavailable("微信登录服务暂时不可用，请稍后再试") from exc

    if resp.status_code == 200:
        openid = (resp.json() or {}).get("openid")
        if not openid:
            raise WechatAuthUnavailable("微信登录服务返回了异常响应")
        return str(openid)

    # 401 = our credentials are wrong (operator error); 429 = throttled;
    # 400/403 = this code is not redeemable here (user error).
    if resp.status_code in (401, 500, 502, 503):
        logger.error("wechat-auth rejected us: %s %s", resp.status_code, resp.text[:200])
        raise WechatAuthUnavailable("微信登录服务暂时不可用，请稍后再试")
    if resp.status_code == 429:
        raise WechatAuthError("验证码错误次数过多，请稍后再试")
    raise WechatAuthError("公众号验证码错误或已过期")


async def fetch_login_info() -> dict:
    """Login-page hints from wechat-auth, or a safe fallback.

    A failure here must not take the login page down: the caller renders the
    keyword instructions with no QR, which is exactly the degraded state.
    """
    url = f"{_base_url()}/api/v1/apps/{get_settings().WECHAT_AUTH_APP_ID}/login-info"
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code != 200:
            logger.warning("wechat-auth login-info returned %s", resp.status_code)
            return {}
        return resp.json() or {}
    except httpx.HTTPError as exc:
        logger.warning("wechat-auth login-info unreachable: %s", exc)
        return {}


async def fetch_qr_image() -> bytes | None:
    """The parameterized QR image, proxied so the login page stays same-origin."""
    url = f"{_base_url()}/api/v1/apps/{get_settings().WECHAT_AUTH_APP_ID}/qrcode"
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            resp = await client.get(url, headers=_headers())
        if resp.status_code != 200:
            return None
        return resp.content
    except httpx.HTTPError as exc:
        logger.warning("wechat-auth qrcode unreachable: %s", exc)
        return None
