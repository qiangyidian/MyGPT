"""Login-page support for WeChat scan login.

MyChat no longer hosts the WeChat callback. The ``wechat-auth`` service owns it
(because WeChat allows exactly one callback URL per Official Account, and every
product here shares one account). What remains here is what the browser needs:

* :func:`wechat_login_info` — public, no credentials, feeds the login panel.
* :func:`wechat_qrcode` — the QR image, proxied so the page stays same-origin
  and never shows another product's hostname.

The verification call lives in ``app.api.auth`` (``/api/auth/login/wechat``).
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Response, status

from app.core.config import get_settings
from app.services import wechat_auth_client

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/wechat", tags=["wechat"])


def _require_enabled() -> None:
    if not get_settings().WECHAT_AUTH_ENABLED:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "公众号登录未启用")


@router.get("/login-info")
async def wechat_login_info() -> dict:
    """Hints for the login page.

    Deliberately public and credential-free: the login page is unauthenticated.

    Two shapes, decided by the upstream service:

    * ``mode == "qr"`` — a per-application parameterized QR exists; the page
      shows it and polling is not needed (the code arrives via WeChat).
    * ``mode == "keyword"`` — no AppID/AppSecret upstream; the page shows the
      Official Account's static QR plus "send <keyword>".
    """
    settings = get_settings()
    if not settings.WECHAT_AUTH_ENABLED:
        return {"data": {"configured": False, "qrcode_url": "", "keyword": ""}}

    info = await wechat_auth_client.fetch_login_info()
    mode = str(info.get("mode") or "")
    keyword = str(info.get("keyword") or "")

    if mode == "qr":
        return {
            "data": {
                "configured": True,
                "qrcode_url": "/api/wechat/qrcode",
                "keyword": keyword,
                "mode": "qr",
                "display_name": info.get("display_name", ""),
            }
        }

    # Keyword mode: the static account QR is MyChat's own published asset, so
    # the page keeps working even if wechat-auth is unreachable.
    qr_url = settings.WECHAT_AUTH_LOGIN_QR_URL
    return {
        "data": {
            "configured": bool(qr_url),
            "qrcode_url": qr_url,
            "keyword": keyword or settings.WECHAT_AUTH_DEFAULT_KEYWORD,
            "mode": "keyword",
            "display_name": info.get("display_name", ""),
        }
    }


@router.get("/qrcode")
async def wechat_qrcode() -> Response:
    """The parameterized QR image, re-served from MyChat's own origin."""
    _require_enabled()
    image = await wechat_auth_client.fetch_qr_image()
    if not image:
        # The login page falls back to the keyword instructions on a non-200.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "二维码暂不可用")
    return Response(
        content=image,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=3600"},
    )
