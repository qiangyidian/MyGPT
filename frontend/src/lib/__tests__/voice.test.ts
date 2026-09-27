// 语音链路的纯逻辑（录音容器选择 / 三项预算 / 播报状态机 / 错误码文案）。
// 这些规则一旦和后端的 speech_service 漂移，用户看到的就是「点了没反应」，
// 或者一个本该在前端拦下的请求真的花掉了钱，所以每条都在 node 里钉死。
import { describe, expect, it } from "vitest";

import {
  MIC_ERROR_CODE_BY_NAME,
  RECORDER_MIME_CANDIDATES,
  SERVER_AUDIO_MIME_FALLBACK,
  SPEECH_ERROR_ZH,
  audioErrorCodeFromMediaError,
  audioFileExtension,
  baseMimeType,
  canTransition,
  charLength,
  checkByteBudget,
  checkDurationBudget,
  checkTextBudget,
  clipToCharBudget,
  formatMb,
  formatRecordingClock,
  isAsrAvailable,
  isAtDurationCap,
  isEmptyRecording,
  isPlaybackActive,
  isTtsAvailable,
  levelFromByteDomain,
  levelFromSampleDomain,
  maxRecordingSeconds,
  micErrorCodeFromName,
  nextPlaybackState,
  pickRecorderMime,
  plainTextForSpeech,
  recorderFilename,
  remainingRecordingSeconds,
  smoothLevel,
  splitTextForSpeech,
  speechDisabledHint,
  speechErrorMessage,
  totalBytes,
  type PlaybackEvent,
  type PlaybackState,
} from "@/lib/voice";
import type { SpeechCapabilities } from "@/lib/types";

/** 一次能力探测的真实载荷（后端 capabilities() 的每个键都在）。 */
function caps(overrides: Partial<SpeechCapabilities> = {}): SpeechCapabilities {
  return {
    enabled: true,
    asr_enabled: true,
    tts_enabled: true,
    reason: null,
    max_audio_mb: 10,
    max_audio_bytes: 10 * 1024 * 1024,
    max_duration_seconds: 120,
    max_text_chars: 4000,
    audio_mime_types: [...SERVER_AUDIO_MIME_FALLBACK].sort(),
    tts_response_format: "mp3",
    tts_mime_type: "audio/mpeg",
    tts_voice: "alloy",
    disabled_message: "语音功能未开启：请由服务端管理员在 .env 设置 SPEECH_ENABLED=true 并重启后端",
    ...overrides,
  };
}

const CHROME = (mime: string) =>
  mime === "audio/webm;codecs=opus" || mime === "audio/webm";
const SAFARI = (mime: string) => mime === "audio/mp4" || mime === "audio/webm";
const NONE = () => false;

describe("baseMimeType", () => {
  it("strips codecs and normalizes case", () => {
    expect(baseMimeType("AUDIO/WebM; CODECS=opus")).toBe("audio/webm");
    expect(baseMimeType("audio/ogg;codecs=opus")).toBe("audio/ogg");
    expect(baseMimeType("")).toBe("");
  });
});

describe("pickRecorderMime", () => {
  it("prefers opus-in-webm on Chrome", () => {
    expect(pickRecorderMime(CHROME)).toBe("audio/webm;codecs=opus");
  });

  it("falls back to a container Safari can actually write", () => {
    expect(pickRecorderMime(SAFARI)).toBe("audio/mp4");
  });

  it("honours a browser that only supports plain webm", () => {
    expect(pickRecorderMime((m) => m === "audio/webm")).toBe("audio/webm");
  });

  it("returns null when nothing is supported (button must self-disable)", () => {
    expect(pickRecorderMime(NONE)).toBeNull();
  });

  it("never picks a container the server would 415", () => {
    // Android Chrome 有时只给 3gpp —— 它不在候选里，也不在白名单里。
    expect(pickRecorderMime((m) => m === "audio/3gpp")).toBeNull();
    // 服务端白名单收窄到 mp4 时，即便浏览器更想录 webm 也必须换容器。
    expect(pickRecorderMime(CHROME, ["audio/mp4"])).toBeNull();
    expect(pickRecorderMime((m) => CHROME(m) || m === "audio/mp4", ["audio/mp4"])).toBe(
      "audio/mp4",
    );
  });

  it("uses the candidate preference order, not the server's alphabetical one", () => {
    // 服务端返回的是 sorted(_ALLOWED_AUDIO_MIME)：字母序会把 aac 排到 webm 前。
    expect(pickRecorderMime((m) => m === "audio/aac" || m === "audio/webm")).toBe(
      "audio/webm",
    );
  });

  it("compares the bare type when matching a codec-bearing candidate", () => {
    const allowed = ["audio/webm"];
    expect(pickRecorderMime(() => true, allowed)).toBe("audio/webm;codecs=opus");
  });

  it("keeps every candidate inside the fallback whitelist's bare types", () => {
    const allowed = new Set(SERVER_AUDIO_MIME_FALLBACK);
    for (const candidate of RECORDER_MIME_CANDIDATES) {
      expect(allowed.has(baseMimeType(candidate))).toBe(true);
    }
  });
});

describe("recorderFilename", () => {
  it("maps each container to an extension the backend recognizes", () => {
    expect(recorderFilename("audio/webm;codecs=opus")).toBe("voice.webm");
    expect(recorderFilename("audio/mp4")).toBe("voice.m4a");
    expect(recorderFilename("audio/mpeg")).toBe("voice.mp3");
    expect(recorderFilename("audio/x-wav")).toBe("voice.wav");
    expect(audioFileExtension("audio/opus")).toBe("ogg");
  });

  it("defaults to webm when the browser reports no type at all", () => {
    expect(recorderFilename(null)).toBe("voice.webm");
    expect(audioFileExtension("audio/3gpp")).toBe("webm");
  });
});

describe("totalBytes / budget formatting", () => {
  it("sums chunk sizes and ignores junk", () => {
    expect(totalBytes([1, 2, 3])).toBe(6);
    expect(totalBytes([])).toBe(0);
    expect(totalBytes([5, -1, Number.NaN, 2])).toBe(7);
  });

  it("renders whole MB without a decimal and fractional MB with one", () => {
    expect(formatMb(10 * 1024 * 1024)).toBe("10MB");
    expect(formatMb(1.5 * 1024 * 1024)).toBe("1.5MB");
    expect(formatMb(0)).toBe("0MB");
    expect(formatMb(-100)).toBe("0MB");
  });
});

describe("checkByteBudget", () => {
  it("accepts up to and including the cap, matching the server's '>' test", () => {
    const max = 10 * 1024 * 1024;
    expect(checkByteBudget(max - 1, max).ok).toBe(true);
    expect(checkByteBudget(max, max).ok).toBe(true);
    expect(checkByteBudget(max + 1, max).ok).toBe(false);
  });

  it("states the cap in Chinese", () => {
    const { message } = checkByteBudget(20 * 1024 * 1024, 10 * 1024 * 1024);
    expect(message).toContain("10MB");
    expect(message).toContain("上限");
  });

  it("carries no message when it passes", () => {
    expect(checkByteBudget(1, 10).message).toBeNull();
  });
});

describe("duration cap", () => {
  it("only fails past the cap", () => {
    expect(checkDurationBudget(120, 120).ok).toBe(true);
    expect(checkDurationBudget(121, 120).ok).toBe(false);
    expect(checkDurationBudget(121, 120).message).toContain("120");
  });

  it("counts down and clamps at zero", () => {
    expect(remainingRecordingSeconds(110, 120)).toBe(10);
    expect(remainingRecordingSeconds(130, 120)).toBe(0);
    expect(remainingRecordingSeconds(-5, 120)).toBe(120);
  });

  it("treats reaching the cap as 'stop now'", () => {
    expect(isAtDurationCap(120, 120)).toBe(true);
    expect(isAtDurationCap(119.9, 120)).toBe(false);
    // 没拿到能力（上限 0）时不做时长判断，交给调用方先禁用按钮。
    expect(isAtDurationCap(5, 0)).toBe(false);
  });

  it("flags a zero-byte recording", () => {
    expect(isEmptyRecording(0)).toBe(true);
    expect(isEmptyRecording(1)).toBe(false);
  });
});

describe("formatRecordingClock", () => {
  it("renders m:ss with a zero-padded second", () => {
    expect(formatRecordingClock(0)).toBe("0:00");
    expect(formatRecordingClock(5)).toBe("0:05");
    expect(formatRecordingClock(65)).toBe("1:05");
    expect(formatRecordingClock(120)).toBe("2:00");
  });

  it("floors a fractional elapsed value", () => {
    expect(formatRecordingClock(3.9)).toBe("0:03");
  });

  it("switches to h:mm:ss only past an hour", () => {
    expect(formatRecordingClock(3599)).toBe("59:59");
    expect(formatRecordingClock(3600)).toBe("1:00:00");
  });

  it("normalizes junk instead of leaking NaN into the UI", () => {
    expect(formatRecordingClock(-1)).toBe("0:00");
    expect(formatRecordingClock(Number.NaN)).toBe("0:00");
    expect(formatRecordingClock(Number.POSITIVE_INFINITY)).toBe("0:00");
  });
});

describe("micErrorCodeFromName", () => {
  it("maps the permission-shaped names to mic_denied", () => {
    expect(micErrorCodeFromName("NotAllowedError")).toBe("mic_denied");
    expect(micErrorCodeFromName("PermissionDeniedError")).toBe("mic_denied");
  });

  it("maps the device-shaped names to mic_unavailable", () => {
    for (const name of [
      "NotFoundError",
      "DevicesNotFoundError",
      "OverconstrainedError",
      "NotReadableError",
    ]) {
      expect(micErrorCodeFromName(name)).toBe("mic_unavailable");
    }
  });

  it("keeps the two special names distinct", () => {
    // 非 HTTPS 的站点上 Chrome 抛的就是 SecurityError，混进「拒绝」里就查不出来了。
    expect(micErrorCodeFromName("SecurityError")).toBe("mic_insecure_origin");
    // 自己 abort 的录音归到 recorder_failed，而不是「没麦克风」。
    expect(micErrorCodeFromName("AbortError")).toBe("recorder_failed");
  });

  it("returns null for blank or unmapped names so the caller keeps its own default", () => {
    expect(micErrorCodeFromName("SomeWeirdError")).toBeNull();
    expect(micErrorCodeFromName("")).toBeNull();
    expect(micErrorCodeFromName(null)).toBeNull();
    expect(micErrorCodeFromName(undefined)).toBeNull();
  });

  it("only ever produces codes the copy table can translate", () => {
    for (const code of Object.values(MIC_ERROR_CODE_BY_NAME)) {
      expect(SPEECH_ERROR_ZH[code]).toBeTruthy();
      expect(/[一-鿿]/.test(SPEECH_ERROR_ZH[code])).toBe(true);
    }
  });
});

describe("plainTextForSpeech", () => {
  it("drops fenced code entirely rather than reading it out", () => {
    expect(plainTextForSpeech("```py\nprint(1)\n```")).toBe("");
    const out = plainTextForSpeech("看这里\n\n```js\nconst answer = 42\n```\n\n结束");
    expect(out).toContain("看这里");
    expect(out).toContain("结束");
    expect(out).not.toContain("const");
  });

  it("keeps link text and image alt, drops the URLs", () => {
    expect(plainTextForSpeech("[官方文档](https://example.com/a)")).toBe("官方文档");
    expect(plainTextForSpeech("![架构图](https://example.com/p.png)")).toBe("架构图");
  });

  it("strips block prefixes but keeps the content", () => {
    expect(plainTextForSpeech("## 小结\n内容")).toBe("小结\n内容");
    expect(plainTextForSpeech("- 甲\n- 乙")).toBe("甲\n乙");
    expect(plainTextForSpeech("1. 第一步\n2. 第二步")).toBe("第一步\n第二步");
    expect(plainTextForSpeech("> 引用")).toBe("引用");
  });

  it("unwraps inline code and emphasis", () => {
    expect(plainTextForSpeech("运行 `npm i` 即可")).toBe("运行 npm i 即可");
    expect(plainTextForSpeech("**重点**说明")).toBe("重点说明");
  });

  it("removes citation markers, tags and table pipes", () => {
    expect(plainTextForSpeech("结论[1]与 [source 2]")).toBe("结论 与");
    expect(plainTextForSpeech("见 <b>粗</b> 标签")).toBe("见 粗 标签");
    const table = plainTextForSpeech("| A | B |\n| 1 | 2 |");
    expect(table).not.toContain("|");
    expect(table).toContain("A");
  });

  it("leaves prose untouched and blank input blank", () => {
    expect(plainTextForSpeech("今天天气不错")).toBe("今天天气不错");
    expect(plainTextForSpeech("")).toBe("");
    expect(plainTextForSpeech("   \n  ")).toBe("");
  });

  it("must run before the char budget (server counts what we send)", () => {
    const markdown = "## 标题\n\n```py\nx = 1\n```\n\n正文内容";
    const speakable = plainTextForSpeech(markdown);
    expect(charLength(speakable)).toBeLessThan(charLength(markdown));
    // 已经在上限内时原样返回（不吞省略号）。
    expect(clipToCharBudget(speakable, 100)).toBe(speakable);
    expect(charLength(clipToCharBudget(speakable, 4))).toBe(4);
  });
});

describe("charLength / text budget", () => {
  it("counts code points like Python len(), not UTF-16 units", () => {
    expect(charLength("hello")).toBe(5);
    expect(charLength("你好世界")).toBe(4);
    expect(charLength("😀x")).toBe(2);
    // 这条就是为什么不能用 string.length：它是 3。
    expect("😀x".length).toBe(3);
  });

  it("trims before measuring, like the backend's normalize_text", () => {
    expect(checkTextBudget("   你好   ", 2).ok).toBe(true);
  });

  it("rejects blank text with a Chinese message", () => {
    const bad = checkTextBudget("   \n ", 100);
    expect(bad.ok).toBe(false);
    expect(bad.message).toContain("不能为空");
  });

  it("reports the overage with both numbers", () => {
    const bad = checkTextBudget("一二三四五", 3);
    expect(bad.ok).toBe(false);
    expect(bad.message).toContain("3");
    expect(bad.message).toContain("5");
  });

  it("clips inside the budget without splitting a surrogate pair", () => {
    expect(clipToCharBudget("你好世界", 4)).toBe("你好世界");
    expect(clipToCharBudget("你好世界", 3)).toBe("你好…");
    expect(charLength(clipToCharBudget("一二三四五", 3))).toBe(3);
    // 边界上正好是 emoji：不能留下半个代理对。
    const clipped = clipToCharBudget("😀😀😀", 2);
    expect(charLength(clipped)).toBe(2);
    expect(clipped).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
    expect(clipToCharBudget("  spaced  ", 100)).toBe("spaced");
    expect(clipToCharBudget("anything", 0)).toBe("");
  });
});

describe("splitTextForSpeech", () => {
  it("returns the whole text when it already fits", () => {
    expect(splitTextForSpeech("短句。", 10)).toEqual(["短句。"]);
    expect(splitTextForSpeech("  两边有空  ", 10)).toEqual(["两边有空"]);
  });

  it("returns nothing for blank text or a non-positive cap", () => {
    expect(splitTextForSpeech("", 10)).toEqual([]);
    expect(splitTextForSpeech("   ", 10)).toEqual([]);
    expect(splitTextForSpeech("有内容", 0)).toEqual([]);
    expect(splitTextForSpeech("有内容", -3)).toEqual([]);
  });

  it("packs whole sentences up to the cap without breaking them", () => {
    // 一句都不能被切断（听感），除非它本身超限 —— 半句被切比多一次请求更难接受。
    expect(splitTextForSpeech("第一句话。第二句话。第三句很长很长的话。", 10)).toEqual([
      "第一句话。第二句话。",
      "第三句很长很长的话。",
    ]);
  });

  it("hard-cuts a single sentence longer than the cap", () => {
    expect(splitTextForSpeech("没有句读的很长很长一段话", 5)).toEqual([
      "没有句读的",
      "很长很长一",
      "段话",
    ]);
  });

  it("keeps every code point and never exceeds the cap", () => {
    const text = "甲。乙。" + "丙".repeat(37) + "。丁。";
    const parts = splitTextForSpeech(text, 6);
    for (const part of parts) expect(charLength(part)).toBeLessThanOrEqual(6);
    expect(parts.join("")).toBe(text);
  });

  it("never emits an empty or whitespace-only segment", () => {
    const parts = splitTextForSpeech("。\n\n\t。甲乙丙丁戊己庚辛", 4);
    expect(parts.every((part) => part.trim().length > 0)).toBe(true);
    expect(parts).toEqual(["。", "。", "甲乙丙丁", "戊己庚辛"]);
  });

  it("counts code points, so an emoji is never cut in half", () => {
    const parts = splitTextForSpeech("😀😀😀😀😀", 3);
    expect(parts).toEqual(["😀😀😀", "😀😀"]);
    for (const part of parts) {
      expect(charLength(part)).toBeLessThanOrEqual(3);
      expect(part).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
    }
  });

  it("runs after plainTextForSpeech: what we count is what the server counts", () => {
    // 代码-only 的回复清洗后什么都不剩 —— 组件据此给出「没有可朗读正文」而不是发一个
    // 必然被 400 拒掉的请求。
    expect(splitTextForSpeech(plainTextForSpeech("```py\nx = 1\n```"), 4000)).toEqual([]);
  });
});

describe("level meter", () => {
  it("reads silence as zero in the byte domain", () => {
    expect(levelFromByteDomain([128, 128, 128])).toBe(0);
    expect(levelFromByteDomain([])).toBe(0);
  });

  it("reads full scale as one and clamps beyond", () => {
    expect(levelFromByteDomain([0, 0])).toBe(1);
    expect(levelFromSampleDomain([1, -1])).toBe(1);
    expect(levelFromSampleDomain([2])).toBe(1);
  });

  it("is the RMS, not the peak", () => {
    // 一半静音一半满幅 → 0.707，而不是 1。
    expect(levelFromByteDomain([128, 0])).toBeCloseTo(0.7071, 3);
    expect(levelFromSampleDomain([0.5])).toBeCloseTo(0.5, 5);
  });

  it("smooths toward the target and clamps its inputs", () => {
    expect(smoothLevel(0, 1, 0.3)).toBeCloseTo(0.3, 5);
    expect(smoothLevel(0.5, 0.5, 0.3)).toBe(0.5);
    expect(smoothLevel(0, 5, 0.3)).toBeCloseTo(0.3, 5);
    expect(smoothLevel(0, -1, 2)).toBe(0);
    // factor 越界钳到 1 时直接到位。
    expect(smoothLevel(0, 1, 9)).toBe(1);
  });
});

const STATES: PlaybackState[] = ["idle", "loading", "playing", "paused"];
const EVENTS: PlaybackEvent["type"][] = [
  "load",
  "ready",
  "pause",
  "resume",
  "stop",
  "ended",
  "error",
];

describe("playback state machine", () => {
  it("walks the happy path", () => {
    expect(nextPlaybackState("idle", "load")).toBe("loading");
    expect(nextPlaybackState("loading", "ready")).toBe("playing");
    expect(nextPlaybackState("playing", "pause")).toBe("paused");
    expect(nextPlaybackState("paused", "resume")).toBe("playing");
    expect(nextPlaybackState("playing", "ended")).toBe("idle");
  });

  it("allows switching to another clip from any audible state", () => {
    for (const state of STATES) {
      expect(canTransition(state, "load")).toBe(true);
      expect(nextPlaybackState(state, "load")).toBe("loading");
    }
  });

  it("treats stop and error as universal escapes", () => {
    for (const state of STATES) {
      expect(nextPlaybackState(state, "stop")).toBe("idle");
      expect(nextPlaybackState(state, "error")).toBe("idle");
    }
  });

  it("rejects transitions that would corrupt the UI", () => {
    // 还没出声不能暂停；没在加载不会 ready；idle 没有「播完」。
    expect(canTransition("idle", "pause")).toBe(false);
    expect(canTransition("idle", "ready")).toBe(false);
    expect(canTransition("idle", "ended")).toBe(false);
    expect(canTransition("idle", "resume")).toBe(false);
    expect(canTransition("loading", "pause")).toBe(false);
    expect(canTransition("playing", "resume")).toBe(false);
    expect(canTransition("paused", "pause")).toBe(false);
    expect(canTransition("paused", "ready")).toBe(false);
  });

  it("leaves the state untouched on an illegal event instead of throwing", () => {
    // 迟到的 onended 不能把「正在加载下一条」的界面打回 idle。
    expect(nextPlaybackState("loading", "ended")).toBe("loading");
    expect(nextPlaybackState("idle", "pause")).toBe("idle");
  });

  it("always lands on a known state for every combination", () => {
    for (const state of STATES) {
      for (const event of EVENTS) {
        expect(STATES).toContain(nextPlaybackState(state, event));
      }
    }
  });

  it("reports occupancy so only one bubble plays at a time", () => {
    expect(isPlaybackActive("idle")).toBe(false);
    expect(isPlaybackActive("loading")).toBe(true);
    expect(isPlaybackActive("playing")).toBe(true);
    expect(isPlaybackActive("paused")).toBe(true);
  });
});

describe("speechErrorMessage", () => {
  it("prefers the backend's own Chinese message", () => {
    expect(
      speechErrorMessage({
        code: "speech_no_speech",
        message: "没有听清内容，请靠近麦克风再说一次",
      }),
    ).toBe("没有听清内容，请靠近麦克风再说一次");
  });

  it("covers every code the backend can raise", () => {
    const backendCodes = [
      "speech_disabled",
      "speech_model_unconfigured",
      "speech_bad_config",
      "speech_payload_too_large",
      "speech_bad_audio",
      "speech_text_required",
      "speech_no_speech",
      "speech_provider_error",
      "speech_provider_empty",
      "speech_provider_oversized",
      "insufficient_credits",
    ];
    expect(Object.keys(SPEECH_ERROR_ZH)).toEqual(
      expect.arrayContaining(backendCodes),
    );
    for (const code of backendCodes) {
      const copy = SPEECH_ERROR_ZH[code];
      // 表里有码，就必须能在没有 message 时翻译出一句中文。
      expect(typeof copy === "string" && copy.length > 0).toBe(true);
      expect(/[一-鿿]/.test(copy)).toBe(true);
      expect(speechErrorMessage({ code })).toBe(copy);
    }
  });

  it("falls back to the table when a proxy replaces the body with English", () => {
    expect(
      speechErrorMessage({ code: "speech_provider_error", message: "Bad Gateway" }),
    ).toBe(SPEECH_ERROR_ZH.speech_provider_error);
  });

  it("keeps an English message readable instead of dropping it", () => {
    expect(speechErrorMessage({ code: "unknown", message: "upstream timeout" })).toContain(
      "upstream timeout",
    );
  });

  it("never returns an empty string", () => {
    expect(speechErrorMessage(null)).toBe("语音请求失败，请稍后重试");
    expect(speechErrorMessage({})).toBe("语音请求失败，请稍后重试");
    expect(speechErrorMessage(undefined, "自定义")).toBe("自定义");
  });
});

describe("audioErrorCodeFromMediaError", () => {
  it("maps all four MEDIA_ERR codes onto the shared copy table", () => {
    // 播报侧的失败只有这四个枚举值；漏一个就会让用户看到「未知错误」。
    for (const code of [1, 2, 3, 4]) {
      const mapped = audioErrorCodeFromMediaError(code);
      expect(mapped).toBeTruthy();
      const copy = SPEECH_ERROR_ZH[mapped as string];
      expect(copy).toBeTruthy();
      expect(/[一-鿿]/.test(copy)).toBe(true);
      expect(speechErrorMessage({ code: mapped })).toBe(copy);
    }
  });

  it("returns null for 'no error' and for junk", () => {
    expect(audioErrorCodeFromMediaError(0)).toBeNull();
    expect(audioErrorCodeFromMediaError(null)).toBeNull();
    expect(audioErrorCodeFromMediaError(undefined)).toBeNull();
    expect(audioErrorCodeFromMediaError(Number.NaN)).toBeNull();
    expect(audioErrorCodeFromMediaError(9)).toBeNull();
  });
});

describe("capability gating", () => {
  it("reads both directions as available when the server says so", () => {
    expect(isAsrAvailable(caps())).toBe(true);
    expect(isTtsAvailable(caps())).toBe(true);
    expect(speechDisabledHint(caps(), "asr")).toBeNull();
    expect(speechDisabledHint(caps(), "tts")).toBeNull();
  });

  it("shows the server's own 503 wording when the flag is off", () => {
    const off = caps({ enabled: false, asr_enabled: false, tts_enabled: false, reason: "disabled" });
    expect(isAsrAvailable(off)).toBe(false);
    expect(speechDisabledHint(off, "asr")).toContain("SPEECH_ENABLED");
  });

  it("prefers a populated disabled_message over the local copy", () => {
    const off = caps({
      enabled: false,
      asr_enabled: false,
      reason: "disabled",
      disabled_message: "语音已停用",
    });
    expect(speechDisabledHint(off, "tts")).toBe("语音已停用");
  });

  it("survives an empty disabled_message", () => {
    const off = caps({ enabled: false, asr_enabled: false, disabled_message: "  " });
    expect(speechDisabledHint(off, "asr")).toContain("语音功能未开启");
  });

  it("tells the two half-broken cases apart", () => {
    const noAsr = caps({ asr_enabled: false, reason: "model_unconfigured" });
    expect(isAsrAvailable(noAsr)).toBe(false);
    expect(isTtsAvailable(noAsr)).toBe(true);
    expect(speechDisabledHint(noAsr, "asr")).toContain("音频输入");
    expect(speechDisabledHint(noAsr, "tts")).toBeNull();

    const noTts = caps({ tts_enabled: false, reason: "model_unconfigured" });
    expect(speechDisabledHint(noTts, "tts")).toContain("音频输出");
    expect(speechDisabledHint(noTts, "asr")).toBeNull();
  });

  it("treats a failed probe as unavailable rather than crashing the composer", () => {
    expect(isAsrAvailable(null)).toBe(false);
    expect(isTtsAvailable(undefined)).toBe(false);
    expect(speechDisabledHint(null, "asr")).toContain("未能读取");
    expect(maxRecordingSeconds(null)).toBe(0);
  });
});

describe("maxRecordingSeconds", () => {
  it("floors a sane server value", () => {
    expect(maxRecordingSeconds(caps({ max_duration_seconds: 120.7 }))).toBe(120);
  });

  it("normalizes junk to zero so callers can disable the button", () => {
    expect(maxRecordingSeconds(caps({ max_duration_seconds: 0 }))).toBe(0);
    expect(maxRecordingSeconds(caps({ max_duration_seconds: -5 }))).toBe(0);
    expect(maxRecordingSeconds(caps({ max_duration_seconds: Number.NaN }))).toBe(0);
  });
});
