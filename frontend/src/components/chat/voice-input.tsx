"use client";

/**
 * 语音输入（ASR）按钮：录音 → 上传 → 把识别出的文本交回输入框。
 *
 * 三个刻意的决定：
 *  1. **绝不自动发送**。识别一定有错（同音字、标点、被切掉的尾音），自动发出去等于
 *     把一次纠错成本转嫁成一轮真实的模型调用（真花钱）。文本只落到输入框，发不发由用户。
 *  2. **音频不落盘、不留对象 URL**。录到的 Blob 只活过一次请求的生命周期，成功失败都
 *     丢引用；后端同样不写对象存储（`app/services/speech_service.py` 的 transcribe）。
 *  3. **上限以服务端为准**。时长 / 体积 / 容器全部来自
 *     `GET /api/speech/capabilities`，前端这一道只是「提前拦一下省一次上行」，
 *     真正的拒绝永远在服务端（组件不构成放行依据）。
 *
 * 浏览器 API 集中在本文件（`@/lib/voice` 保持纯函数才能在 node 里测）：
 * `getUserMedia` + `MediaRecorder` + `AudioContext`/`AnalyserNode`。卸载时必须
 * 关掉 AudioContext 并停掉麦克风轨道，否则切走页面后标签页仍亮着录音指示灯。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Mic, Square, X } from "lucide-react";

import { api, ApiError } from "@/lib/api";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";
import { useSpeechCapabilities } from "@/hooks/useSpeechCapabilities";
import {
  baseMimeType,
  checkByteBudget,
  checkDurationBudget,
  formatRecordingClock,
  isAsrAvailable,
  isAtDurationCap,
  isEmptyRecording,
  levelFromByteDomain,
  maxRecordingSeconds,
  micErrorCodeFromName,
  pickRecorderMime,
  recorderFilename,
  smoothLevel,
  speechDisabledHint,
  speechErrorMessage,
  totalBytes,
} from "@/lib/voice";

/** 电平/计时采样：10Hz 够画电平表，也不会在说话时抢主线程。 */
const TICK_MS = 100;
/** `MediaRecorder.start(timeslice)`：切太碎是几百个小对象，太大则中途崩溃丢更多音频。 */
const TIMESLICE_MS = 500;
/** 电平条格数。 */
const BAR_COUNT = 5;

type Phase = "idle" | "starting" | "recording" | "uploading";

export interface VoiceInputProps {
  /** 识别出的文本（已 trim）；插入位置由输入框决定。 */
  onTranscript: (text: string) => void;
  /** 外部禁用（正在流式生成时不该同时开麦）。 */
  disabled?: boolean;
  className?: string;
}

/** `speechErrorMessage` 只读这两个字段，这里把各种失败整形成它。 */
interface SpeechEnvelope {
  code?: string | null;
  message?: string | null;
}

/**
 * 把任意失败翻译成后端信封的形状。
 *
 * `ApiError` 自带 code（服务端本来就是中文 message，直接用）；`getUserMedia` 抛的
 * `DOMException` 各家浏览器 message 都是英文且措辞不一，所以翻成 code 交给同一张
 * 表 —— 组件里不出现裸文案，麦克风失败与后端失败共用一条链路。认不出的错误只带
 * message，交给 `speechErrorMessage` 的通用兜底。
 */
function envelopeOf(error: unknown): SpeechEnvelope {
  if (error instanceof ApiError) return { code: error.code, message: error.message };
  const message = error instanceof Error ? error.message : null;
  const code = micErrorCodeFromName((error as DOMException | null | undefined)?.name);
  if (code) return { code, message };
  return { message };
}

export function VoiceInput({ onTranscript, disabled, className }: VoiceInputProps) {
  const { caps, isLoading, failed, retry } = useSpeechCapabilities();
  const [phase, setPhase] = useState<Phase>("idle");
  const [level, setLevel] = useState(0);
  const [elapsed, setElapsed] = useState(0);
  const [notice, setNotice] = useState<string | null>(null);

  const chunksRef = useRef<Blob[]>([]);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const streamRef = useRef<MediaStream | null>(null);
  const audioCtxRef = useRef<AudioContext | null>(null);
  const timerRef = useRef<number | null>(null);
  const startedAtRef = useRef(0);
  /** 协商出来的容器：个别实现的 `recorder.mimeType` 会返回空串，用它兜底。 */
  const mimeRef = useRef("");
  const inflightRef = useRef<AbortController | null>(null);
  /** 组件已卸载：在途请求的识别结果不能再写回（父级输入框已经没了）。 */
  const discardedRef = useRef(false);
  // props 经 ref 转发：父级回调每次渲染都是新函数，不进依赖数组免得录音链路被重建。
  const onTranscriptRef = useRef(onTranscript);
  onTranscriptRef.current = onTranscript;

  const clearTimer = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearInterval(timerRef.current);
      timerRef.current = null;
    }
  }, []);

  /** 放掉麦克风轨道与音频图：这两件事泄漏的代价是「录音指示灯常亮」和「白烧 CPU」。 */
  const releaseHardware = useCallback(() => {
    clearTimer();
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
    const ctx = audioCtxRef.current;
    audioCtxRef.current = null;
    if (ctx && ctx.state !== "closed") void ctx.close().catch(() => undefined);
    setLevel(0);
  }, [clearTimer]);

  /** 分片 → Blob → 后端 → 文本。所有失败都在这里变成一句中文，不抛给调用方。 */
  const transcribe = useCallback(
    async (chunks: Blob[], mimeType: string) => {
      // 字节数由 `@/lib/voice` 汇总（那层不碰 Blob 类型，才能在 node 里测）。
      const bytes = totalBytes(chunks.map((chunk) => chunk.size));
      if (isEmptyRecording(bytes)) {
        setNotice("没有录到声音，请重试");
        setPhase("idle");
        return;
      }
      // caps 中途被刷新掉时不做本地体积判断（服务端仍会 413），免得误拒一次已完成的录音。
      const budget = checkByteBudget(bytes, caps?.max_audio_bytes ?? Number.MAX_SAFE_INTEGER);
      if (!budget.ok) {
        setNotice(budget.message);
        setPhase("idle");
        return;
      }
      // 只有真要上传时才拼 Blob：拒绝路径上少一次内存拷贝。
      const blob = new Blob(chunks, { type: baseMimeType(mimeType) });
      const controller = new AbortController();
      inflightRef.current = controller;
      try {
        const result = await api.transcribeAudio(
          blob,
          recorderFilename(mimeType),
          controller.signal,
        );
        if (discardedRef.current) return;
        const text = (result.text || "").trim();
        if (text) {
          setNotice(null);
          onTranscriptRef.current(text);
        } else {
          // 200 却什么都没有：不算失败，但要告诉用户这次没识别出来。
          setNotice(speechErrorMessage({ code: "speech_no_speech" }));
        }
      } catch (error) {
        if (controller.signal.aborted || discardedRef.current) return;
        setNotice(speechErrorMessage(envelopeOf(error)));
      } finally {
        inflightRef.current = null;
        if (!discardedRef.current) setPhase("idle");
      }
    },
    [caps],
  );

  /** 停止录音并交给识别。最后一片 `dataavailable` 必在 `onstop` 前到，所以在那里拼 Blob。 */
  const stopRecording = useCallback(() => {
    clearTimer();
    const recorder = recorderRef.current;
    recorderRef.current = null;
    setElapsed(0);
    if (!recorder || recorder.state === "inactive") {
      releaseHardware();
      setPhase("idle");
      return;
    }
    const effectiveMime = recorder.mimeType || "";
    setPhase("uploading");
    recorder.onstop = () => {
      // 最后一片 `dataavailable` 一定在 `onstop` 之前到，所以这里拼才是完整的。
      const chunks = chunksRef.current;
      chunksRef.current = [];
      releaseHardware();
      void transcribe(chunks, effectiveMime || mimeRef.current);
    };
    try {
      recorder.stop();
    } catch {
      // 设备掉线 / 已经停止：丢掉这次录音，别把 UI 卡在识别中。
      chunksRef.current = [];
      recorder.onstop = null;
      releaseHardware();
      setNotice(speechErrorMessage({ code: "recorder_failed" }));
      setPhase("idle");
    }
  }, [clearTimer, releaseHardware, transcribe]);

  /** 放弃：不上传、不识别，把已收到的分片原地丢弃。 */
  const cancelRecording = useCallback(() => {
    clearTimer();
    const recorder = recorderRef.current;
    recorderRef.current = null;
    chunksRef.current = [];
    if (recorder) {
      recorder.onstop = null;
      try {
        if (recorder.state !== "inactive") recorder.stop();
      } catch {
        /* 释放失败也无所谓：轨道在下面统一停掉 */
      }
    }
    releaseHardware();
    setPhase("idle");
    setNotice(null);
    setElapsed(0);
  }, [clearTimer, releaseHardware]);

  const startRecording = useCallback(async () => {
    if (phase !== "idle") return;
    setNotice(null);
    if (failed) {
      // 探测失败时按钮本身就是禁用样式，但用户还是会点：把「点一次」变成「重试一次」。
      retry();
      return;
    }
    if (!isAsrAvailable(caps)) {
      setNotice(speechDisabledHint(caps, "asr") ?? "语音输入当前不可用");
      return;
    }
    if (typeof window === "undefined" || typeof MediaRecorder === "undefined") {
      setNotice(speechErrorMessage({ code: "mic_unsupported" }));
      return;
    }
    // 非安全上下文（http 且非 localhost）里 `navigator.mediaDevices` 直接是
    // undefined：不先判一下就会以「Cannot read properties of undefined」的面目
    // 出现，用户完全看不出是部署方式的问题。
    const media = window.navigator.mediaDevices;
    if (!media || typeof media.getUserMedia !== "function") {
      setNotice(
        speechErrorMessage({
          code: window.isSecureContext === false ? "mic_insecure_origin" : "mic_unsupported",
        }),
      );
      return;
    }
    setPhase("starting");
    discardedRef.current = false;

    let stream: MediaStream;
    try {
      stream = await media.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        video: false,
      });
    } catch (error) {
      setPhase("idle");
      setNotice(speechErrorMessage(envelopeOf(error)));
      return;
    }
    if (discardedRef.current) {
      stream.getTracks().forEach((track) => track.stop());
      setPhase("idle");
      return;
    }

    // 容器必须同时满足「浏览器真能写」和「服务端白名单里有」，缺一头就是 415 或静音。
    const mime = pickRecorderMime(
      (candidate) => {
        try {
          return MediaRecorder.isTypeSupported(candidate);
        } catch {
          // 部分实现对带 codecs 的串直接抛错，而不是返回 false。
          return false;
        }
      },
      caps?.audio_mime_types ?? [],
    );
    if (!mime) {
      stream.getTracks().forEach((track) => track.stop());
      setPhase("idle");
      setNotice(speechErrorMessage({ code: "mic_unsupported" }));
      return;
    }

    let recorder: MediaRecorder;
    try {
      recorder = new MediaRecorder(stream, { mimeType: mime });
    } catch {
      // 极少数实现 isTypeSupported 报 true 却仍拒绝显式构造；退回默认容器，
      // 真实格式从 recorder.mimeType 读，扩展名跟着走，不猜。
      recorder = new MediaRecorder(stream);
    }

    chunksRef.current = [];
    mimeRef.current = recorder.mimeType || mime;
    recorder.ondataavailable = (event) => {
      if (event.data && event.data.size > 0) chunksRef.current.push(event.data);
    };
    recorder.onerror = () => {
      setNotice(speechErrorMessage({ code: "recorder_failed" }));
      cancelRecording();
    };
    recorderRef.current = recorder;
    streamRef.current = stream;
    recorder.start(TIMESLICE_MS);

    // 电平表挂在麦克风的 MediaStream 上（不是 recorder），采集与编码互不干扰。
    let analyser: AnalyserNode | null = null;
    let bytes: Uint8Array<ArrayBuffer> | null = null;
    try {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext?: typeof AudioContext })
          .webkitAudioContext;
      if (Ctor) {
        const ctx = new Ctor();
        audioCtxRef.current = ctx;
        const node = ctx.createAnalyser();
        node.fftSize = 1024;
        // 浏览器自带的平滑只留一点点：跳变由 smoothLevel 负责，两处都平会糊成一条线。
        node.smoothingTimeConstant = 0.4;
        ctx.createMediaStreamSource(stream).connect(node);
        analyser = node;
        // 缓冲区在这里分配一次（不是每帧新建），类型由推导决定，避开 TS 5.7 的
        // Uint8Array<ArrayBufferLike> 与 DOM 签名不兼容的问题。
        bytes = new Uint8Array(node.fftSize);
      }
    } catch {
      // 电平表是装饰性的：拿不到音频图也要能录音。
      analyser = null;
      bytes = null;
    }

    const capSeconds = maxRecordingSeconds(caps);
    startedAtRef.current = Date.now();
    setElapsed(0);
    setPhase("recording");
    timerRef.current = window.setInterval(() => {
      if (analyser && bytes) {
        analyser.getByteTimeDomainData(bytes);
        const sampled = levelFromByteDomain(bytes);
        setLevel((prev) => smoothLevel(prev, sampled, 0.35));
      }
      const seconds = (Date.now() - startedAtRef.current) / 1000;
      setElapsed(seconds);
      if (isAtDurationCap(seconds, capSeconds)) {
        // 到点自动收尾：仍走一次识别（用户可能正好说完），并把原因说清楚。
        stopRecording();
        setNotice(
          checkDurationBudget(seconds, capSeconds).message ??
            `录音已达 ${capSeconds} 秒上限，已自动停止`,
        );
      }
    }, TICK_MS);
  }, [
    cancelRecording,
    caps,
    failed,
    phase,
    retry,
    stopRecording,
  ]);

  /** 卸载收尾：在途请求作废 + 释放一切浏览器资源（含 AudioContext）。 */
  useEffect(() => {
    discardedRef.current = false;
    return () => {
      discardedRef.current = true;
      inflightRef.current?.abort();
      const recorder = recorderRef.current;
      recorderRef.current = null;
      if (recorder) {
        recorder.onstop = null;
        try {
          if (recorder.state !== "inactive") recorder.stop();
        } catch {
          /* ignore */
        }
      }
      chunksRef.current = [];
      releaseHardware();
    };
    // 只装配一次：释放逻辑全在 ref 上，不需要跟着渲染走。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const capSeconds = maxRecordingSeconds(caps);
  const available = isAsrAvailable(caps) && !disabled;
  const tooltip = available
    ? capSeconds > 0
      ? `语音输入（最长 ${formatRecordingClock(capSeconds)}）`
      : "语音输入"
    : disabled
      ? "AI 正在回复，稍后再用语音输入"
      : (speechDisabledHint(caps, "asr") ??
        (isLoading ? "正在确认语音能力…" : "语音输入当前不可用"));

  if (phase !== "idle") {
    const lit = Math.min(BAR_COUNT, Math.max(0, Math.round(level * BAR_COUNT)));
    return (
      <div className={cn("flex shrink-0 items-center gap-1", className)}>
        {phase === "recording" ? (
          <>
            <span className="flex h-4 items-end gap-0.5" aria-hidden="true">
              {Array.from({ length: BAR_COUNT }, (_unused, index) => (
                <span
                  key={index}
                  className={cn(
                    "w-0.5 rounded-full transition-colors",
                    index < lit ? "bg-primary" : "bg-muted-foreground/25",
                  )}
                  style={{ height: `${6 + index * 3}px` }}
                />
              ))}
            </span>
            <span
              className="text-[11px] tabular-nums text-muted-foreground"
              role="status"
              aria-live="polite"
            >
              {formatRecordingClock(elapsed)}
              {capSeconds > 0 ? ` / ${formatRecordingClock(capSeconds)}` : ""}
            </span>
          </>
        ) : (
          <span
            className="flex items-center gap-1 text-[11px] text-muted-foreground"
            role="status"
          >
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
            {phase === "starting" ? "开启麦克风…" : "识别中…"}
          </span>
        )}
        {phase === "recording" && (
          <Button
            type="button"
            size="icon"
            variant="ghost"
            className="h-8 w-8 shrink-0"
            onClick={stopRecording}
            title="停止并识别"
            aria-label="停止录音并识别"
          >
            <Square className="h-4 w-4" />
          </Button>
        )}
        <Button
          type="button"
          size="icon"
          variant="ghost"
          className="h-8 w-8 shrink-0 text-muted-foreground"
          onClick={cancelRecording}
          title="放弃这次录音"
          aria-label="取消录音"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
    );
  }

  return (
    <span className={cn("inline-flex shrink-0 items-center gap-1", className)}>
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          {/* 禁用态的 button 带 pointer-events-none，hover 必须由外层 span 承接，
              否则「为什么不能点」这句中文提示永远出不来。 */}
          <TooltipTrigger asChild>
            <span className="inline-flex shrink-0">
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8 text-muted-foreground"
                onClick={() => void startRecording()}
                disabled={!available}
                aria-label="语音输入"
              >
                <Mic className="h-4 w-4" />
              </Button>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-[16rem] whitespace-normal">
            {tooltip}
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
      {notice && (
        <span
          className="max-w-[14rem] truncate text-[11px] text-destructive"
          role="alert"
          title={notice}
        >
          {notice}
        </span>
      )}
    </span>
  );
}
