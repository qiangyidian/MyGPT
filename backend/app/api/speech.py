"""语音路由：语音输入（ASR 转写）+ 语音播报（TTS 合成）。

端点：

* ``GET  /api/speech/capabilities`` —— 廉价能力探测（零厂商调用）。前端**不猜**：
  麦克风/喇叭按钮的可用性、体积与时长上限、可接受的 MIME 全部以此为准。
* ``POST /api/speech/transcribe``  —— multipart 音频进 → 识别文本出（JSON）。
* ``POST /api/speech/synthesize``  —— 文本进 → **分块流式**音频出。

设计约束（与 :mod:`app.api.artifacts` 同纪律）：鉴权、开关、限额、积分准入全部
在**首字节之前**完成，所以错误永远是干净的 JSON 信封；音频既不进磁盘也不进
对象存储，响应返回即丢弃。供应商密钥只在后端 ``ModelConfig`` 行里（Fernet 加密），
浏览器只看到 model_name —— 任何模型调用都由后端代发。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, File, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_current_user
from app.core.rate_limit import rate_limit_user
from app.db import get_db
from app.models import User
from app.services import speech_service

router = APIRouter(prefix="/api/speech", tags=["speech"])


class SynthesizeRequest(BaseModel):
    """``POST /api/speech/synthesize`` 的请求体。

    ``text`` 的长度上限由服务端 :func:`speech_service.normalize_text` 判定
    （``SPEECH_MAX_TEXT_CHARS``）；这里刻意不设 pydantic ``max_length``，
    这样超限的响应是带中文说明的 413 业务信封，而不是 pydantic 的 422 校验噪声。
    """

    text: str = Field(default="", description="要播报的文本")


def _gate_enabled() -> None:
    """路由级开关依赖 —— 让「功能关闭」真的零成本。

    FastAPI 会先解析 ``dependencies`` 再读请求体，所以关掉开关时一个
    multipart 上传连**落临时文件**都不会发生（服务层里的
    :func:`speech_service.assert_speech_enabled` 是第二道，防的是绕过本路由的调用）。
    """
    speech_service.assert_speech_enabled()


@router.get("/capabilities")
async def get_speech_capabilities(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """当前账号**真正能用到**的语音能力（含关闭原因），供前端置灰按钮。

    这个端点自己**不受开关拦截** —— 前端必须先能问到「是关的」，才能把按钮渲染成
    带中文说明的禁用态，而不是 503 之后靠猜。
    """
    return await speech_service.capabilities(db, user)


@router.post(
    "/transcribe",
    dependencies=[Depends(_gate_enabled), Depends(rate_limit_user(20, 60, "speech-asr"))],
)
async def transcribe(
    file: UploadFile = File(...),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """语音 → 文本。返回的文本由前端写进输入框，**不自动发送**。"""
    return await speech_service.transcribe(db, user, file)


@router.post(
    "/synthesize",
    dependencies=[Depends(_gate_enabled), Depends(rate_limit_user(10, 60, "speech-tts"))],
)
async def synthesize(
    payload: SynthesizeRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """文本 → 音频（分块下发）。"""
    body, media_type, size = await speech_service.synthesize(db, user, payload.text)
    return StreamingResponse(
        body,
        media_type=media_type,
        status_code=status.HTTP_200_OK,
        headers={
            "Cache-Control": "private, no-store",
            "Content-Length": str(size),
        },
    )
