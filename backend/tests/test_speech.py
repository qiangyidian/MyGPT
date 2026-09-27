"""语音端点（ASR / TTS）的开关联动与输入上限。

只测「花钱之前」的那一段：开关、鉴权、体积/长度/格式上限、模型未配置。
厂商调用本身不在这儿测（离线套件不打网络，provider 层的 transcribe/speak 由
tests/test_model_capabilities.py 那类契约测试覆盖）。
"""
from __future__ import annotations

from app.core.config import get_settings
from app.services import model_service, speech_service
from tests.conftest import auth_headers

# 一段「看起来像 WebM」的字节：EBML magic + 填充。够让格式嗅探放行，
# 又不至于真是一个可解码的音频（这些请求根本不该走到解码）。
WEBM_LIKE = b"\x1a\x45\xdf\xa3" + b"\x00" * 512
WAV_LIKE = b"RIFF" + b"\x00\x00\x00\x00" + b"WAVE" + b"\x00" * 512


def _enable(monkeypatch, **overrides) -> None:
    """打开总开关 + 按需压小额度（``get_settings()`` 是进程级单例）。"""
    s = get_settings()
    monkeypatch.setattr(s, "SPEECH_ENABLED", True)
    for key, value in overrides.items():
        monkeypatch.setattr(s, key, value)


def _no_audio_model(monkeypatch) -> None:
    """让候选集为空：模拟「用户没有任何带音频能力位的 ModelConfig 行」。

    走真实代码路径（``_pick_config`` 过滤一个空列表），而不是把 ``_pick_config``
    本身打桩；并且不依赖共享的 session 级测试库里是否残留了别的模型行
    （``tests/test_models_api.py`` 就会插入一条 supports_audio_input=True 的配置）。
    """
    async def _empty(db, user_id):
        return []

    monkeypatch.setattr(model_service, "list_for_user", _empty)


# --------------------------------------------------------------------------- #
# 开关：默认关闭 → 中文 503
# --------------------------------------------------------------------------- #
async def test_transcribe_is_503_when_disabled(client):
    r = await client.post(
        "/api/speech/transcribe",
        headers=auth_headers(),
        files={"file": ("voice.webm", WEBM_LIKE, "audio/webm")},
    )
    assert r.status_code == 503
    body = r.json()
    assert body["code"] == "speech_disabled"
    assert "语音功能未开启" in body["message"]


async def test_synthesize_is_503_when_disabled(client):
    r = await client.post(
        "/api/speech/synthesize",
        headers=auth_headers(),
        json={"text": "你好，世界"},
    )
    assert r.status_code == 503
    assert r.json()["code"] == "speech_disabled"
    assert "SPEECH_ENABLED" in r.json()["message"]


async def test_disabled_gate_wins_over_input_limits(client, monkeypatch):
    """开关判定在限额之前：一个超限的大文件也只会拿到 503，不会先花 413 的力气。

    顺序即成本：功能关着时，请求必须停在最外层。
    """
    monkeypatch.setattr(get_settings(), "SPEECH_MAX_AUDIO_MB", 1)
    r = await client.post(
        "/api/speech/transcribe",
        headers=auth_headers(),
        files={"file": ("big.webm", WEBM_LIKE + b"\x00" * (2 * 1024 * 1024), "audio/webm")},
    )
    assert r.status_code == 503
    assert r.json()["code"] == "speech_disabled"


# --------------------------------------------------------------------------- #
# 能力探测：客户端不猜
# --------------------------------------------------------------------------- #
async def test_capabilities_reports_the_off_state(client):
    r = await client.get("/api/speech/capabilities", headers=auth_headers())
    assert r.status_code == 200
    body = r.json()
    assert body["enabled"] is False
    assert body["asr_enabled"] is False
    assert body["tts_enabled"] is False
    assert body["reason"] == "disabled"
    # 上限一并透出，前端才有办法做录音时长/字数的本地约束。
    assert body["max_audio_mb"] == get_settings().SPEECH_MAX_AUDIO_MB
    assert body["max_text_chars"] == get_settings().SPEECH_MAX_TEXT_CHARS
    assert "audio/webm" in body["audio_mime_types"]


async def test_capabilities_reports_model_unconfigured_when_on(client, monkeypatch):
    _enable(monkeypatch)
    _no_audio_model(monkeypatch)
    body = (await client.get("/api/speech/capabilities", headers=auth_headers())).json()
    assert body["enabled"] is True
    assert body["asr_enabled"] is False
    assert body["tts_enabled"] is False
    assert body["reason"] == "model_unconfigured"


# --------------------------------------------------------------------------- #
# 鉴权
# --------------------------------------------------------------------------- #
async def test_speech_endpoints_require_auth(client):
    for method, path, kwargs in (
        ("GET", "/api/speech/capabilities", {}),
        ("POST", "/api/speech/transcribe", {"files": {"file": ("v.webm", WEBM_LIKE, "audio/webm")}}),
        ("POST", "/api/speech/synthesize", {"json": {"text": "你好"}}),
    ):
        r = await client.request(method, path, **kwargs)
        assert r.status_code == 401, path


# --------------------------------------------------------------------------- #
# 输入上限（开关已打开）
# --------------------------------------------------------------------------- #
async def test_transcribe_rejects_oversized_audio(client, monkeypatch):
    _enable(monkeypatch, SPEECH_MAX_AUDIO_MB=1)
    payload = WEBM_LIKE + b"\x00" * (2 * 1024 * 1024)
    r = await client.post(
        "/api/speech/transcribe",
        headers=auth_headers(),
        files={"file": ("big.webm", payload, "audio/webm")},
    )
    assert r.status_code == 413
    assert r.json()["code"] == "speech_payload_too_large"
    assert "音频过大" in r.json()["message"]


async def test_transcribe_rejects_non_audio_bytes_claiming_audio(client, monkeypatch):
    """声明 audio/webm 但首字节是 PDF：415，不能把任意字节转发给厂商。"""
    _enable(monkeypatch)
    r = await client.post(
        "/api/speech/transcribe",
        headers=auth_headers(),
        files={"file": ("invoice.webm", b"%PDF-1.7\n" + b"\x00" * 200, "audio/webm")},
    )
    assert r.status_code == 415
    assert r.json()["code"] == "speech_bad_audio"


async def test_transcribe_rejects_unsupported_audio_container(client, monkeypatch):
    """首字节确实是音频（WAVE）但容器不在白名单外也不行 —— 这里用 MIME 拒绝。"""
    _enable(monkeypatch)
    r = await client.post(
        "/api/speech/transcribe",
        headers=auth_headers(),
        files={"file": ("voice.aiff", b"FORM\x00\x00\x00\x00AIFF" + b"\x00" * 64, "audio/aiff")},
    )
    assert r.status_code == 415
    assert r.json()["code"] == "speech_bad_audio"


async def test_transcribe_rejects_empty_upload(client, monkeypatch):
    _enable(monkeypatch)
    r = await client.post(
        "/api/speech/transcribe",
        headers=auth_headers(),
        files={"file": ("empty.webm", b"", "audio/webm")},
    )
    assert r.status_code in (400, 415)


async def test_transcribe_stops_at_model_gate_before_vendor(client, monkeypatch):
    """开关开 + 音频合法，但没配语音模型 → 中文 503（且不是 500）。

    两种容器都验一遍：webm（MediaRecorder 的默认产物）和 wav（附件白名单里的）。
    """
    _enable(monkeypatch)
    _no_audio_model(monkeypatch)
    for name, blob, mime in (
        ("voice.webm", WEBM_LIKE, "audio/webm"),
        ("voice.wav", WAV_LIKE, "audio/wav"),
    ):
        r = await client.post(
            "/api/speech/transcribe",
            headers=auth_headers(),
            files={"file": (name, blob, mime)},
        )
        assert r.status_code == 503, name
        body = r.json()
        assert body["code"] == "speech_model_unconfigured", name
        assert "语音转写" in body["message"], name


async def test_synthesize_rejects_blank_text(client, monkeypatch):
    _enable(monkeypatch)
    r = await client.post(
        "/api/speech/synthesize",
        headers=auth_headers(),
        json={"text": "   \n  "},
    )
    assert r.status_code == 400
    assert r.json()["code"] == "speech_text_required"


async def test_synthesize_rejects_over_length_text(client, monkeypatch):
    _enable(monkeypatch, SPEECH_MAX_TEXT_CHARS=10)
    text = "语" * 11
    r = await client.post(
        "/api/speech/synthesize",
        headers=auth_headers(),
        json={"text": text},
    )
    assert r.status_code == 413
    body = r.json()
    assert body["code"] == "speech_payload_too_large"
    assert "最长 10 字" in body["message"]


async def test_synthesize_text_limit_is_checked_before_model_resolution(
    client, monkeypatch
):
    """超限的文本连模型都不用挑：不花厂商一分钱。

    做法是同时「没有语音模型」+ 「文本超限」——如果顺序反了会先拿到 503。
    """
    _enable(monkeypatch, SPEECH_MAX_TEXT_CHARS=5)
    _no_audio_model(monkeypatch)
    r = await client.post(
        "/api/speech/synthesize",
        headers=auth_headers(),
        json={"text": "语" * 50},
    )
    assert r.status_code == 413
    assert r.json()["code"] == "speech_payload_too_large"


# --------------------------------------------------------------------------- #
# 估算函数（纯函数，不碰网络/数据库）
# --------------------------------------------------------------------------- #
def test_estimate_asr_usage_clamps_duration_to_the_cap():
    """塞进来一个高码率大文件也不能把计费时长放大到荒谬值。"""
    s = get_settings()
    small = speech_service.estimate_asr_usage(48_000 * 2, "audio/webm", "你好", "whisper-1")
    huge = speech_service.estimate_asr_usage(500 * 1024 * 1024, "audio/webm", "你好", "whisper-1")
    assert small["prompt_tokens"] < huge["prompt_tokens"]
    ceiling = (s.SPEECH_MAX_DURATION_SECONDS / 60.0) * s.SPEECH_ASR_TOKENS_PER_MINUTE
    assert huge["prompt_tokens"] <= ceiling + 1
    assert small["total_tokens"] == small["prompt_tokens"] + small["completion_tokens"]


def test_estimate_tts_usage_never_charges_zero_for_real_text():
    usage = speech_service.estimate_tts_usage("把这段话念出来", "tts-1")
    assert usage["prompt_tokens"] >= 1
    assert usage["completion_tokens"] == 0
    assert usage["total_tokens"] == usage["prompt_tokens"]
