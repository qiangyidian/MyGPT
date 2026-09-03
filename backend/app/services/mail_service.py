"""SMTP mail sender for one-time verification codes.

Synchronous ``smtplib`` over SSL is wrapped in ``asyncio.to_thread`` so the
event loop never blocks on network IO. Failures raise :class:`MailServiceError`
with a user-safe message (the SMTP conversation detail is logged, not leaked).
"""
from __future__ import annotations

import asyncio
import logging
from email.message import EmailMessage
import smtplib

from app.core.config import get_settings

logger = logging.getLogger(__name__)


class MailServiceError(RuntimeError):
    """User-facing send failure (details logged server-side only)."""


def _build_message(to_email: str, code: str, purpose: str) -> EmailMessage:
    settings = get_settings()
    ttl_minutes = settings.EMAIL_CODE_TTL_SECONDS // 60
    action = "注册" if purpose == "register" else "登录"
    message = EmailMessage()
    message["Subject"] = f"MyChat {action}验证码：{code}"
    message["From"] = settings.MAIL_FROM or settings.MAIL_USERNAME
    message["To"] = to_email
    message.set_content(
        (
            "你好，\n\n"
            f"你正在进行 MyChat {action}验证。\n"
            f"本次验证码是：{code}\n"
            f"验证码 {ttl_minutes} 分钟内有效，请勿泄露给他人。\n\n"
            "如果这不是你本人的操作，请忽略这封邮件。\n"
        ),
        subtype="plain",
        charset="utf-8",
    )
    return message


async def send_verification_email(to_email: str, code: str, purpose: str) -> None:
    """Send a one-time code. Raises MailServiceError on any failure."""
    settings = get_settings()
    if not settings.MAIL_ENABLED:
        # Explicit signal for the caller: dev mode without SMTP configured.
        raise MailServiceError("邮件服务未启用")

    def _send() -> None:
        with smtplib.SMTP_SSL(settings.MAIL_HOST, settings.MAIL_PORT, timeout=15) as client:
            if settings.MAIL_AUTH:
                client.login(settings.MAIL_USERNAME, settings.MAIL_PASSWORD)
            client.send_message(_build_message(to_email, code, purpose))

    try:
        await asyncio.to_thread(_send)
    except MailServiceError:
        raise
    except Exception as exc:  # noqa: BLE001 — normalize, log detail
        logger.warning("verification mail to %s failed: %s", to_email, exc)
        raise MailServiceError("验证码邮件发送失败，请稍后重试") from exc
