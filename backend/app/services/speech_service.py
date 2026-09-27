"""语音链路（语音输入 ASR + 语音播报 TTS）的服务层。

三条不可妥协的纪律：

1. **供应商密钥永不出后端。** 出站只走 :func:`app.providers.registry.get_provider_for_config`
   —— 也就是 ``ModelConfig`` 行 + Fernet 解密，和聊天完全同一条路。客户端既拿不到
   key 也拿不到 ``api_base_url``，只看到 ``model_name``。这里不新建任何 HTTP 客户端，
   也不从 env 直接读厂商密钥（仓库的约定是「密钥在 ModelConfig 行里」）。
2. **开关与限额都在调用供应商之前判定**：先 503 / 413 / 415 / 402，再花钱。
3. **音频不落盘**：转写的字节只存在于本次请求的栈上，识别文本进响应体即结束；
   合成的音频在响应流完后即丢弃。两边都**不**创建 artifact / attachment 行 ——
   保留音频必须是一次显式的产品决定（用户上传附件是另一条路径），不是副作用。

用量结算复用聊天轮次的同一批原语（:func:`app.core.pricing.normalize_usage` /
:func:`~app.core.pricing.usage_cost` → :meth:`app.quotas.QuotaService.charge_usage`
→ :func:`app.services.credit_service.charge_usage`），**不新建平行账本**。之所以不
直接调 :func:`app.services.chat_service.settle_turn_usage`：那个入口的第一件事就是把
用量写到一个 ``Message`` 行上，而语音请求按设计不产生消息行（转写文本只是进了输入框，
用户可能根本不发送）。复用同一组扣费原语 + 同一个 ``credit_ledger`` 表，只是
``ref_type`` 取 ``"speech"``，幂等仍由 ``uq_credit_ledger_ref`` 唯一部分索引兜住。

一处诚实的能力缺口：仓库 provider 层的 ``transcribe()`` / ``speak()`` **不返回 usage
负载**（``/audio/transcriptions`` 与 ``/audio/speech`` 的 OpenAI 兼容响应里就没有
token 字段），所以这里的用量是**服务端按字节/字符估算**出来的，再交给定价表换算成本。
估算偏保守（宁可多算 token 也不多算钱：见 :func:`estimate_asr_usage` 的时长钳制）。
"""
from __future__ import annotations

import logging
import math
import uuid
from collections.abc import AsyncIterator
from typing import Any

from fastapi import UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import env_flag, get_settings
from app.core.exceptions import AppException
from app.credits import compute_charge, get_credit_policy
from app.models import ModelConfig, User
from app.providers.base import ProviderError
from app.providers.multimodal import ModelCapabilityError
from app.providers.registry import get_provider_for_config
from app.services import credit_service, model_service

logger = logging.getLogger(__name__)

# 唯一事实来源：两个端点 + /capabilities 都读这一份配置。
SPEECH_DISABLED_MESSAGE = (
    "语音功能未开启：请由服务端管理员在 .env 设置 SPEECH_ENABLED=true 并重启后端"
)

# 只有 openai-compatible provider 实现了 transcribe()/speak()（见
# app/providers/openai_compatible.py 的「Multimodal routes」段）。anthropic /
# hermes / mock 没有这两个方法，选中它们只会在调用点抛 AttributeError，
# 所以在挑选阶段就排除，并保留 hasattr 兜底。
_SPEECH_CAPABLE_PROVIDERS = frozenset({"openai-compatible"})

# 允许的输入容器/MIME。键是小写 MIME，值是该格式的**典型**编码码率（bps），
# 用于「字节 → 时长」的计费估算（真实时长无法在本层解码得到）。
_ALLOWED_AUDIO_MIME: dict[str, int] = {
    "audio/webm": 48_000,
    "audio/ogg": 48_000,
    "audio/opus": 48_000,
    "audio/mpeg": 128_000,
    "audio/mp3": 128_000,
    "audio/mp4": 128_000,
    "audio/aac": 128_000,
    "audio/flac": 900_000,
    "audio/wav": 1_411_000,
    "audio/x-wav": 1_411_000,
}

# 扩展名回落（很多浏览器把 MediaRecorder 的 type 报成空串）。
_AUDIO_EXT_MIME: dict[str, str] = {
    ".webm": "audio/webm",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".aac": "audio/aac",
    ".flac": "audio/flac",
}

# 合成响应的 response_format → MIME（浏览器 <audio> 直接可播）。
_FORMAT_MIME: dict[str, str] = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "opus": "audio/opus",
    "aac": "audio/aac",
    "flac": "audio/flac",
    "pcm": "audio/L16",
}

# 各容器的 magic bytes 嗅探。目的是「别把 PDF / 可执行文件转递给厂商」，
# 不是做完整的格式解析（那需要新依赖，明确不做）。
_MAGIC_PREFIXES: tuple[bytes, ...] = (
    b"\x1a\x45\xdf\xa3",  # WebM / Matroska
    b"OggS",              # Ogg
    b"RIFF",              # WAV（下面再校验 WAVE 标记）
    b"fLaC",              # FLAC
    b"\xff\xfb",          # MPEG-1 Layer III ADTS/MP3 frame sync
    b"\xff\xf3",
    b"\xff\xf2",
    b"ID3",               # MP3 with tags
)


def _too_large(message: str) -> AppException:
    return AppException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "speech_payload_too_large", message)


def _bad_audio(message: str) -> AppException:
    return AppException(status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "speech_bad_audio", message)


def _provider_failed(exc: Exception, where: str) -> AppException:
    """厂商侧失败 → 统一的中文 502。

    原始异常只进日志不回显：``ProviderError`` 的文案里带上游 base_url / 响应体，
    直接把 base_url 透给浏览器等于泄露部署拓扑（密钥不会，但地址也不该给）。
    """
    logger.warning("speech %s failed: %s", where, exc, exc_info=True)
    return AppException(
        status.HTTP_502_BAD_GATEWAY,
        "speech_provider_error",
        "语音模型调用失败，请稍后重试或改用文本输入",
    )


def _model_unconfigured(label: str, capability_flag: str) -> AppException:
    """能力位没开 → 中文 503，并说清楚去哪儿开。

    ``ModelCapabilityError`` 是 provider 层在 dispatch 前的自查；能走到这里说明
    配置行的能力位和实际选中的模型不一致（例如手工改过库），报出来而不是 500。
    """
    return AppException(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "speech_model_unconfigured",
        f"尚未配置可用的{label}模型：请在「设置 → 模型」里新增一个 "
        f"OpenAI 兼容端点并勾选 {capability_flag}",
    )


# --------------------------------------------------------------------------- #
# 开关 + 能力探测
# --------------------------------------------------------------------------- #
def speech_enabled() -> bool:
    """总开关。用 :func:`app.core.config.env_flag` 解析（仓库唯一的 env 布尔口径）。"""
    return env_flag(get_settings().SPEECH_ENABLED)


def assert_speech_enabled() -> None:
    if not speech_enabled():
        raise AppException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "speech_disabled", SPEECH_DISABLED_MESSAGE
        )


def _resolve_cfg_by_id(raw: str) -> uuid.UUID | None:
    try:
        return uuid.UUID((raw or "").strip())
    except (ValueError, AttributeError, TypeError):
        return None


def _provider_supports(cfg: ModelConfig, method: str) -> bool:
    """该行能否真的执行 ``method``（transcribe / speak）。

    先过 provider 白名单，再实际构造 provider 验 hasattr —— 构造是纯本地的
    （解密 + 建对象），不发网络请求，所以探测很便宜。
    """
    if (cfg.provider or "").strip().lower() not in _SPEECH_CAPABLE_PROVIDERS:
        return False
    try:
        return hasattr(get_provider_for_config(cfg), method)
    except ProviderError:
        # 未知 provider 类型：能力探测不该把配置列表整个打断。
        return False


# 关键字都没命中时的排序位次（专任语音模型永远排在前面）。
_RANK_NO_KEYWORD = 1_000


def _rank(cfg: ModelConfig, keywords: str) -> int:
    """专任语音模型优先于「顺带支持音频的聊天模型」（数字越小越优）。"""
    name = (cfg.model_name or "").lower()
    for i, kw in enumerate(k.strip().lower() for k in (keywords or "").split(",") if k.strip()):
        if kw in name:
            return i
    return _RANK_NO_KEYWORD


async def _pick_config(
    db: AsyncSession,
    user: User,
    *,
    capability_flag: str,
    provider_method: str,
    model_id_setting: str,
    keywords_setting: str,
) -> ModelConfig | None:
    """在用户可见的 ModelConfig（自有 + 系统级）里挑一个能干活的语言模型。

    排序规则：显式配置的 ``SPEECH_*_MODEL_ID`` > 命中关键字的专任语音模型 > 其余
    带该能力位的行。找不到就返回 None，由调用方决定是报错（端点）还是报 false
    （能力探测）。
    """
    rows = await model_service.list_for_user(db, user.id)
    eligible = [
        cfg
        for cfg in rows
        if bool(getattr(cfg, capability_flag, False)) and _provider_supports(cfg, provider_method)
    ]
    if not eligible:
        return None
    pinned = _resolve_cfg_by_id(model_id_setting)
    if pinned is not None:
        hit = next((cfg for cfg in eligible if cfg.id == pinned), None)
        if hit is not None:
            return hit
        # 显式配了但该行不可见/能力位没开：不静默换一行「差不多」的，
        # 那会让运维以为自己的配置生效了。宁可报未配置。
        return None
    keywords = keywords_setting or ""
    eligible.sort(key=lambda c: (_rank(c, keywords), str(c.created_at or "")))
    return eligible[0]


async def _require_config(
    db: AsyncSession,
    user: User,
    *,
    label: str,
    capability_flag: str,
    provider_method: str,
    model_id_setting: str,
    keywords_setting: str,
) -> ModelConfig:
    cfg = await _pick_config(
        db,
        user,
        capability_flag=capability_flag,
        provider_method=provider_method,
        model_id_setting=model_id_setting,
        keywords_setting=keywords_setting,
    )
    if cfg is None:
        raise _model_unconfigured(label, capability_flag)
    return cfg


async def capabilities(db: AsyncSession, user: User) -> dict[str, Any]:
    """``GET /api/speech/capabilities`` 的载荷 —— 客户端不猜能力，只读这个。

    刻意做成一次 DB 查询 + 零厂商调用（和 ``GET /api/upload-capabilities`` 同一
    定位：服务端把「实际会接受什么」讲清楚，UI 不再抄一份会漂移的常量）。
    """
    s = get_settings()
    on = speech_enabled()
    asr_cfg = await _pick_config(
        db,
        user,
        capability_flag="supports_audio_input",
        provider_method="transcribe",
        model_id_setting=s.SPEECH_ASR_MODEL_ID,
        keywords_setting=s.SPEECH_ASR_MODEL_KEYWORDS,
    )
    tts_cfg = await _pick_config(
        db,
        user,
        capability_flag="supports_audio_output",
        provider_method="speak",
        model_id_setting=s.SPEECH_TTS_MODEL_ID,
        keywords_setting=s.SPEECH_TTS_MODEL_KEYWORDS,
    )
    return {
        "enabled": on,
        "asr_enabled": bool(on and asr_cfg is not None),
        "tts_enabled": bool(on and tts_cfg is not None),
        # 分开报原因，前端才能给出可执行的中文提示（而不是统一的「不可用」）。
        "reason": _capability_reason(on, asr_cfg, tts_cfg),
        "max_audio_mb": s.SPEECH_MAX_AUDIO_MB,
        "max_audio_bytes": s.SPEECH_MAX_AUDIO_MB * 1024 * 1024,
        "max_duration_seconds": s.SPEECH_MAX_DURATION_SECONDS,
        "max_text_chars": s.SPEECH_MAX_TEXT_CHARS,
        "audio_mime_types": sorted(_ALLOWED_AUDIO_MIME),
        "tts_response_format": s.SPEECH_TTS_RESPONSE_FORMAT,
        "tts_mime_type": _FORMAT_MIME.get(s.SPEECH_TTS_RESPONSE_FORMAT, "audio/mpeg"),
        "tts_voice": s.SPEECH_TTS_VOICE,
        "disabled_message": SPEECH_DISABLED_MESSAGE,
    }


def _capability_reason(on: bool, asr_cfg: ModelConfig | None, tts_cfg: ModelConfig | None) -> str | None:
    """开关关 / 模型没配 / 一切就绪 —— 三态里挑一个可展示的原因（None = 无异常）。"""
    if not on:
        return "disabled"
    if asr_cfg is None or tts_cfg is None:
        return "model_unconfigured"
    return None


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _sniff_media_type(data: bytes, declared: str, filename: str | None) -> str:
    """声明的 MIME + 文件名扩展名 + 真实 magic bytes 三者一致才放行。

    浏览器端 ``new Blob(..., {type: "audio/webm"})`` 的 type 经常是空的，所以
    扩展名是必要的回落；但**任何**情况下都要求首字节确实是受支持的音频容器 ——
    否则这个端点就是一个花钱的任意字节转发器。
    """
    mime = (declared or "").split(";")[0].strip().lower()
    if not mime:
        base = (filename or "").lower()
        suffix = f".{base.rsplit('.', 1)[-1]}" if "." in base else ""
        mime = _AUDIO_EXT_MIME.get(suffix, "")
    if mime == "audio/ogg" and data[:4] == b"\x1a\x45\xdf\xa3":
        mime = "audio/webm"  # 容器嗅探优先于声明（同一套字节两种叫法）
    if mime not in _ALLOWED_AUDIO_MIME:
        raise _bad_audio("不支持的音频格式，请使用 webm / ogg / mp3 / wav / m4a / flac 录制")
    head = data[:12]
    known = any(head.startswith(p) for p in _MAGIC_PREFIXES)
    if head[:4] == b"RIFF":
        known = head[8:12] == b"WAVE"
    elif head[4:8] == b"ftyp":
        known = True  # ISO-BMFF（m4a/mp4）
    elif len(head) >= 2 and head[0] == 0xFF and (head[1] & 0xE0) == 0xE0:
        known = True  # 任意 MPEG 帧同步头（mp3 / ADTS aac）
    if not known:
        raise _bad_audio("音频内容无法识别（文件可能已损坏或不是音频）")
    return mime


async def read_capped_audio(file: UploadFile) -> tuple[bytes, str]:
    """读出音频字节并守住体积/格式上限 —— 全部在厂商之前。

    ``file.size`` 先做一次便宜的拒绝（和 artifacts 同款），真正兜底的是
    ``read(max_bytes + 1)``：谎报 Content-Length 的客户端最多只能让我们多持有
    1 字节，超限即 413，不存在「先整份读进内存再看大小」。
    """
    s = get_settings()
    max_bytes = s.SPEECH_MAX_AUDIO_MB * 1024 * 1024
    if file.size is not None and file.size > max_bytes:
        raise _too_large(f"音频过大，最大 {s.SPEECH_MAX_AUDIO_MB}MB")
    data = await file.read(max_bytes + 1)
    try:
        if len(data) > max_bytes:
            raise _too_large(f"音频过大，最大 {s.SPEECH_MAX_AUDIO_MB}MB")
        if not data:
            raise _bad_audio("音频为空，请重新录制")
        return data, _sniff_media_type(data, file.content_type or "", file.filename)
    finally:
        # 句柄必须关掉：UploadFile 背后是临时缓冲文件，泄漏会耗尽 fd。
        try:
            await file.close()
        except Exception:  # pragma: no cover - 关闭失败不该改变请求结果
            logger.debug("speech: failed to close upload handle", exc_info=True)


def normalize_text(text: str, *, limit_setting: str, label: str) -> str:
    """去空白 + 长度上限（按 Python 字符计，中文一个字算一个）。"""
    cleaned = (text or "").strip()
    limit = int(getattr(get_settings(), limit_setting))
    if not cleaned:
        raise AppException(
            status.HTTP_400_BAD_REQUEST, "speech_text_required", f"{label}不能为空"
        )
    if len(cleaned) > limit:
        raise _too_large(f"{label}最长 {limit} 字，当前 {len(cleaned)} 字")
    return cleaned


# --------------------------------------------------------------------------- #
# 用量估算 + 结算
# --------------------------------------------------------------------------- #
def _estimate_text_tokens(text: str, model_name: str) -> int:
    """复用聊天的 token 估算器（tiktoken，失败回落字符启发式）。"""
    from app.services.chat_service import _estimate_tokens

    return int(_estimate_tokens(text or "", model_name) or 0)


def estimate_asr_usage(audio_bytes: int, media_type: str, text: str, model_name: str) -> dict[str, int]:
    """音频 → 估算 usage：时长按容器典型码率折算，再乘 whisper 的 token/分钟。

    时长钳在 ``SPEECH_MAX_DURATION_SECONDS`` 之内 —— 端点本来就只接受不超过该
    时长的录音，所以计费估算不会因为客户端塞进一个高码率大文件而放大到荒谬值。
    没有 usage 字段可读（见模块 docstring），这是**服务端**估算，不接受任何
    客户端提供的数字。
    """
    s = get_settings()
    bitrate = int(_ALLOWED_AUDIO_MIME.get(media_type) or s.SPEECH_AUDIO_BITS_PER_SECOND)
    duration_seconds = (int(audio_bytes) * 8.0) / max(bitrate, 1)
    capped = min(duration_seconds, float(max(1, s.SPEECH_MAX_DURATION_SECONDS)))
    prompt = math.ceil(capped / 60.0 * max(1, s.SPEECH_ASR_TOKENS_PER_MINUTE)) or 1
    completion = _estimate_text_tokens(text, model_name)
    return {
        "prompt_tokens": max(prompt, 1),
        "completion_tokens": completion,
        "total_tokens": max(prompt, 1) + completion,
    }


def estimate_tts_usage(text: str, model_name: str) -> dict[str, int]:
    """合成 → 估算 usage：TTS 厂商按字符计费，这里折成 token 后进同一张定价表。

    ``completion_tokens`` 留 0 —— 合成产出是音频不是文本，把它记成 completion
    会在配额里重复计量输入量。
    """
    return {
        "prompt_tokens": max(1, _estimate_text_tokens(text, model_name)),
        "completion_tokens": 0,
        "total_tokens": max(1, _estimate_text_tokens(text, model_name)),
    }


async def assert_affordable(db: AsyncSession, user: User) -> None:
    """积分准入：与聊天 durable-run 路径同款规则（观察模式下不拦）。

    必须放在厂商调用**之前**：语音请求一旦发出去，钱就花了，再报 402 只是把
    欠费变成既成事实。``CREDITS_ENFORCED=false``（默认）时整段跳过，行为和
    聊天一致。
    """
    if not get_credit_policy().enforced:
        return
    account = await credit_service.read_account(db, user.id)
    if account is None or int(account.balance) <= 0:
        raise AppException(
            status.HTTP_402_PAYMENT_REQUIRED,
            "insufficient_credits",
            "积分不足，请先兑换后再使用语音功能",
            {"balance": int(account.balance) if account else 0},
        )


async def settle_usage(
    db: AsyncSession,
    user: User,
    *,
    kind: str,
    model_name: str,
    usage: dict[str, Any] | None,
    note: str,
) -> None:
    """按聊天的同一套原语结算一次语音请求的成本。

    顺序与 :func:`app.services.chat_service.settle_turn_usage` 一致：
    定价换算 → 配额（Redis，best-effort）→ 积分（数据库账本，权威）。
    配额失败不能影响积分 —— 积分是钱，配额是限流（同 chat_service 的论证）。
    """
    from app.core.pricing import normalize_usage, usage_cost

    normalized = normalize_usage(usage)
    if normalized is None:
        return
    prompt = int(normalized["prompt_tokens"] or 0)
    completion = int(normalized["completion_tokens"] or 0)
    if prompt == 0 and completion == 0:
        return
    cost = usage_cost(model_name, usage)

    from app.quotas import QuotaExceeded, get_quota_service

    svc = get_quota_service()
    if svc.enabled:
        try:
            await svc.charge_usage(
                str(user.id),
                prompt_tokens=prompt,
                completion_tokens=completion,
                cost_usd=float(cost or 0.0),
            )
        except QuotaExceeded:
            # 与聊天同款：本轮已经产生消耗，超额在**下一次**准入时体现，
            # 不把已经花掉钱的请求变成 500。
            logger.warning("quota overage for tenant %s after speech %s charge", user.id, kind)

    amount = compute_charge(cost, int(normalized["total_tokens"] or 0), get_credit_policy())
    try:
        await credit_service.charge_usage(
            db,
            user.id,
            amount=amount,
            ref_type="speech",
            # 一次请求一行流水：ref_id 带 kind 前缀便于对账（<=64 字符）。
            ref_id=f"{kind}:{uuid.uuid4()}"[:64],
            note=note[:200],
        )
    except credit_service.CreditError as exc:
        raise AppException(exc.status_code, exc.code, exc.message) from exc
    # credit_service 只 flush 不 commit（事务边界归调用方）。语音请求没有别的
    # 写入单元，所以这里就是边界。
    await db.commit()


# --------------------------------------------------------------------------- #
# 端点实现
# --------------------------------------------------------------------------- #
async def transcribe(db: AsyncSession, user: User, file: UploadFile) -> dict[str, Any]:
    """语音 → 文本。返回识别文本；音频不落盘、不进 artifact。"""
    assert_speech_enabled()
    s = get_settings()
    await assert_affordable(db, user)
    data, media_type = await read_capped_audio(file)

    cfg = await _require_config(
        db,
        user,
        label="语音转写（ASR）",
        capability_flag="supports_audio_input",
        provider_method="transcribe",
        model_id_setting=s.SPEECH_ASR_MODEL_ID,
        keywords_setting=s.SPEECH_ASR_MODEL_KEYWORDS,
    )
    provider = get_provider_for_config(cfg)
    suffix = (file.filename or "").rsplit(".", 1)[-1].lower() or "webm"
    try:
        text = await provider.transcribe(
            data,
            mime_type=media_type,
            filename=f"voice.{suffix}",
            language=(s.SPEECH_ASR_LANGUAGE or None),
        )
    except ModelCapabilityError as exc:
        raise _model_unconfigured("语音转写（ASR）", "supports_audio_input") from exc
    except ProviderError as exc:
        raise _provider_failed(exc, "transcribe") from exc
    recognized = (text or "").strip()
    if not recognized:
        raise AppException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "speech_no_speech",
            "没有听清内容，请靠近麦克风再说一次",
        )
    await settle_usage(
        db,
        user,
        kind="asr",
        model_name=cfg.model_name,
        usage=estimate_asr_usage(len(data), media_type, recognized, cfg.model_name),
        note=f"语音转写 {len(data)} 字节（{media_type}）",
    )
    # 字节到此为止：随请求上下文释放，不写盘、不入对象存储。
    return {
        "text": recognized,
        "media_type": media_type,
        "audio_bytes": len(data),
        "model_name": cfg.model_name,
    }


async def synthesize(db: AsyncSession, user: User, text: str) -> tuple[AsyncIterator[bytes], str, int]:
    """文本 → 音频流。返回 ``(分块生成器, MIME, 字节数)``。

    所有失败（开关、积分、长度、模型、厂商）都在**首字节之前**发生，因此响应仍是
    干净的 JSON 错误信封；一旦开始流式，状态码就改不了了（与 artifacts 的分块下载
    同纪律）。

    一处能力缺口：provider 的 ``speak()`` 只能整段返回 ``bytes`` —— 仓库的客户端层
    表达不出「上游边合成边吐字节」，所以这里是**有界缓冲 + 分块下发**：体积由
    ``SPEECH_MAX_TEXT_CHARS`` 间接约束，并由 ``SPEECH_MAX_SYNTH_MB`` 直接封顶，
    超过就 502 而不是把超大响应推给浏览器。
    """
    assert_speech_enabled()
    s = get_settings()
    await assert_affordable(db, user)
    cleaned = normalize_text(text, limit_setting="SPEECH_MAX_TEXT_CHARS", label="播报文本")
    fmt = (s.SPEECH_TTS_RESPONSE_FORMAT or "mp3").strip().lower()
    media_type = _FORMAT_MIME.get(fmt)
    if media_type is None:
        raise AppException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "speech_bad_config",
            f"SPEECH_TTS_RESPONSE_FORMAT={fmt!r} 不受支持，可用：" + ", ".join(sorted(_FORMAT_MIME)),
        )

    cfg = await _require_config(
        db,
        user,
        label="语音合成（TTS）",
        capability_flag="supports_audio_output",
        provider_method="speak",
        model_id_setting=s.SPEECH_TTS_MODEL_ID,
        keywords_setting=s.SPEECH_TTS_MODEL_KEYWORDS,
    )
    provider = get_provider_for_config(cfg)
    max_out = s.SPEECH_MAX_SYNTH_MB * 1024 * 1024
    try:
        audio = await provider.speak(
            cleaned, voice=(s.SPEECH_TTS_VOICE or "alloy"), response_format=fmt
        )
    except ModelCapabilityError as exc:
        raise _model_unconfigured("语音播报（TTS）", "supports_audio_output") from exc
    except ProviderError as exc:
        raise _provider_failed(exc, "speak") from exc
    if not audio:
        raise AppException(
            status.HTTP_502_BAD_GATEWAY,
            "speech_provider_empty",
            "语音合成返回空音频，请稍后重试",
        )
    if len(audio) > max_out:
        # 客户端层没有流式上游，所以只能事后丢弃 —— 至少不把它推给浏览器。
        raise AppException(
            status.HTTP_502_BAD_GATEWAY,
            "speech_provider_oversized",
            f"合成音频超过 {s.SPEECH_MAX_SYNTH_MB}MB 上限，请缩短播报文本",
        )

    await settle_usage(
        db,
        user,
        kind="tts",
        model_name=cfg.model_name,
        usage=estimate_tts_usage(cleaned, cfg.model_name),
        note=f"语音播报 {len(cleaned)} 字",
    )

    total = len(audio)

    async def body() -> AsyncIterator[bytes]:
        step = 64 * 1024
        for start in range(0, total, step):
            yield audio[start : start + step]

    return body(), media_type, total


__all__ = [
    "SPEECH_DISABLED_MESSAGE",
    "assert_affordable",
    "assert_speech_enabled",
    "capabilities",
    "estimate_asr_usage",
    "estimate_tts_usage",
    "normalize_text",
    "read_capped_audio",
    "settle_usage",
    "speech_enabled",
    "synthesize",
    "transcribe",
]
