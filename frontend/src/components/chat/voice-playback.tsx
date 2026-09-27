"use client";

/**
 * 每条助手消息旁的喇叭按钮：文本 → 后端合成 → 播放（可暂停 / 停止）。
 *
 * 几个不显眼但必要的决定：
 *  1. **音频只能经后端拿**。厂商 key 在数据行里（Fernet 加密），浏览器不配持有；
 *     而 `<audio src>` 没法带 Authorization 头，所以走 `POST` + Blob + 对象 URL。
 *     代价是必须自己管生命周期：每个段的对象 URL 在播完/停止/卸载时立刻 revoke，
 *     不然一个长会话下来就是一串没人引用的 ArrayBuffer。
 *  2. **长回复按段合成、按段播放**（`splitTextForSpeech`）。第一段拿到就开播，
 *     用户不必等整条答案合成完；也才可能满足服务端的单次字数上限。
 *  3. **播报的是朗读文本**：先 `plainTextForSpeech` 把 Markdown 洗掉，代码块整段
 *     略过 —— 念 `for i in range(10)` 没有任何信息量，但这句话必须让用户看见，
 *     所以代码-only 的回复给一句明确的中文说明，而不是「点了没反应」。
 *  4. 这里**不用 `AudioContext`**：单个 `<audio>` 元素自带按位置暂停/继续与
 *     `ended` 事件，换成 AudioContext 就得自己 `decodeAudioData` + 记 buffer 偏移，
 *     换来的好处对播报为零。需要 `AudioContext` 的是录音侧的电平表
 *     （见 `voice-input.tsx`，那里在卸载时 close）。
 *
 * 同一时刻只允许一条消息出声：模块作用域记着「当前播放者」的停止函数，
 * 点了 B 就把 A 关掉（两条语音叠在一起播是不可用的产品，不是特性）。
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Pause, Play, Square, Volume2 } from "lucide-react";

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
  audioErrorCodeFromMediaError,
  checkTextBudget,
  isPlaybackActive,
  isTtsAvailable,
  nextPlaybackState,
  plainTextForSpeech,
  splitTextForSpeech,
  speechDisabledHint,
  speechErrorMessage,
  type PlaybackState,
} from "@/lib/voice";

/** 一次播放会话的全部可变状态（每次点喇叭新建一个，卸载/停止即作废）。 */
interface Engine {
  audio: HTMLAudioElement | null;
  url: string | null;
  controller: AbortController | null;
  cancelled: boolean;
  /** 正在 await 的那一段的了结句柄：stop() 必须能立刻唤醒它，否则循环永久挂住。 */
  settle: (() => void) | null;
}

/** 当前出声者的停止函数（见文件头末段：同一时刻只允许一条消息在说话）。 */
let currentOwnerStop: (() => void) | null = null;

export interface VoicePlaybackProps {
  /** 只为「已完成的助手消息」渲染；标在 DOM 上便于测试与排查「是谁在出声」。 */
  messageId: string;
  /** 消息原文（Markdown），由组件内清洗后再切块。 */
  content: string;
  /** 外部禁用（例如流式生成中的消息、或父级正在放另一段音频）。 */
  disabled?: boolean;
  className?: string;
}

function envelopeOf(error: unknown): { code?: string | null; message?: string | null } {
  if (error instanceof ApiError) return { code: error.code, message: error.message };
  return { message: error instanceof Error ? error.message : null };
}

/** 停掉一切：中断在途合成、暂停并脱离音频元素、 revoke 对象 URL、唤醒等待中的循环。 */
function teardownEngine(engine: Engine | null) {
  if (!engine) return;
  engine.cancelled = true;
  engine.controller?.abort();
  engine.controller = null;
  const audio = engine.audio;
  engine.audio = null;
  if (audio) {
    audio.pause();
    // 先摘 src 再 load()：否则部分实现仍持有旧的媒体资源。
    audio.removeAttribute("src");
    audio.load();
  }
  if (engine.url) {
    URL.revokeObjectURL(engine.url);
    engine.url = null;
  }
  const settle = engine.settle;
  engine.settle = null;
  settle?.();
}

export function VoicePlayback({
  messageId,
  content,
  disabled,
  className,
}: VoicePlaybackProps) {
  const { caps, isLoading, failed, retry } = useSpeechCapabilities();
  const [state, setState] = useState<PlaybackState>("idle");
  const [notice, setNotice] = useState<string | null>(null);
  const [segmentCount, setSegmentCount] = useState(0);
  const [segmentIndex, setSegmentIndex] = useState(0);

  const engineRef = useRef<Engine | null>(null);
  const lastMediaErrorRef = useRef<string | null>(null);
  /** 本组件 stop() 的稳定引用（模块级「当前播放者」存的就是它）。 */
  const ownerStopRef = useRef<() => void>(() => {});
  const stopRef = useRef<() => void>(() => {});
  // 播报内容变了（同一条 bubble 被改写）就该停下：不能念着已被替换掉的文本。
  const contentRef = useRef(content);

  const dispatch = useCallback(
    (event: "load" | "ready" | "pause" | "resume" | "stop" | "ended" | "error") => {
      setState((prev) => nextPlaybackState(prev, event));
    },
    [],
  );

  const stopPlayback = useCallback(() => {
    teardownEngine(engineRef.current);
    engineRef.current = null;
    if (currentOwnerStop === ownerStopRef.current) currentOwnerStop = null;
    setSegmentIndex(0);
    setSegmentCount(0);
    setState((prev) => nextPlaybackState(prev, "stop"));
  }, []);
  stopRef.current = stopPlayback;
  ownerStopRef.current = stopPlayback;

  /** 播一个对象 URL，直到播完 / 出错 / 被 stop 唤醒。 */
  const playUrl = useCallback(
    (engine: Engine, url: string) =>
      new Promise<"ended" | "error" | "cancelled">((resolve) => {
        const audio = new Audio(url);
        engine.audio = audio;
        let done = false;
        const finish = (outcome: "ended" | "error" | "cancelled") => {
          if (done) return;
          done = true;
          engine.settle = null;
          if (engine.url) {
            URL.revokeObjectURL(engine.url);
            engine.url = null;
          }
          engine.audio = null;
          resolve(outcome);
        };
        engine.settle = () => finish("cancelled");
        audio.addEventListener("ended", () => finish("ended"));
        audio.addEventListener("error", () => {
          const code = audioErrorCodeFromMediaError(audio.error?.code);
          lastMediaErrorRef.current = code
            ? speechErrorMessage({ code })
            : "音频播放失败，请重试或改用文本阅读";
          finish("error");
        });
        // 自动播放策略下 play() 会被拒；把它当成一次失败，而不是静默无响应。
        audio.play().catch(() => {
          lastMediaErrorRef.current = "浏览器阻止了自动播放，请再点一次播报";
          finish("error");
        });
      }),
    [],
  );

  const start = useCallback(async () => {
    if (failed) {
      retry();
      return;
    }
    const maxChars = caps?.max_text_chars ?? 0;
    if (!isTtsAvailable(caps) || maxChars <= 0) {
      setNotice(speechDisabledHint(caps, "tts") ?? "语音播报当前不可用");
      return;
    }
    const segments = splitTextForSpeech(plainTextForSpeech(content), maxChars);
    if (segments.length === 0) {
      // 纯代码块/纯图片的回复清洗后什么都不剩 —— 必须说清楚，而不是点了没反应。
      setNotice("这条回复没有可朗读的正文（代码块与图片不参与播报）");
      return;
    }
    setNotice(null);
    // 抢占：先让上一条消息闭嘴，再挂上自己。
    stopRef.current();
    const engine: Engine = {
      audio: null,
      url: null,
      controller: null,
      cancelled: false,
      settle: null,
    };
    engineRef.current = engine;
    if (currentOwnerStop) currentOwnerStop();
    currentOwnerStop = ownerStopRef.current;
    setSegmentCount(segments.length);
    dispatch("load");

    try {
      for (let index = 0; index < segments.length; index += 1) {
        if (engine.cancelled) return;
        setSegmentIndex(index);
        // 切块本身保证不超限；这一道防的是服务端把上限调小后仍缓存着旧能力。
        const budget = checkTextBudget(segments[index], maxChars);
        if (!budget.ok) {
          setNotice(budget.message);
          dispatch("error");
          return;
        }
        const controller = new AbortController();
        engine.controller = controller;
        let blob: Blob;
        try {
          blob = await api.synthesizeSpeech(segments[index], controller.signal);
        } catch (error) {
          if (controller.signal.aborted || engine.cancelled) return;
          setNotice(speechErrorMessage(envelopeOf(error)));
          dispatch("error");
          return;
        }
        if (engine.cancelled) return;
        if (!blob || blob.size === 0) {
          setNotice(speechErrorMessage({ code: "speech_provider_empty" }));
          dispatch("error");
          return;
        }
        const url = URL.createObjectURL(blob);
        engine.url = url;
        engine.controller = null;
        dispatch("ready");
        const outcome = await playUrl(engine, url);
        if (outcome !== "ended") {
          if (outcome === "error") {
            setNotice(lastMediaErrorRef.current ?? speechErrorMessage(null));
            dispatch("error");
          }
          return;
        }
      }
      if (!engine.cancelled) dispatch("ended");
    } finally {
      if (engineRef.current === engine) {
        teardownEngine(engine);
        engineRef.current = null;
        if (currentOwnerStop === ownerStopRef.current) currentOwnerStop = null;
      }
    }
  }, [caps, content, dispatch, failed, playUrl, retry]);

  /** 暂停 / 继续：交给 `<audio>` 自己，位置不丢。 */
  const toggle = useCallback(() => {
    if (state === "idle") {
      void start();
      return;
    }
    if (state === "loading") return; // 合成途中由「停止」负责
    const audio = engineRef.current?.audio ?? null;
    if (state === "playing") {
      audio?.pause();
      dispatch("pause");
      return;
    }
    // paused：位置由 `<audio>` 自己记着，继续即从断点续播。
    if (!audio) {
      dispatch("stop");
      return;
    }
    dispatch("resume");
    audio.play().catch(() => {
      setNotice("继续播报失败，请重新点播");
      dispatch("error");
    });
  }, [dispatch, start, state]);

  /** 卸载 / 内容被替换：释放一切，不留幽灵播放。 */
  useEffect(() => {
    if (contentRef.current === content) return;
    contentRef.current = content;
    stopRef.current();
  }, [content]);

  useEffect(
    () => () => {
      teardownEngine(engineRef.current);
      engineRef.current = null;
      if (currentOwnerStop === ownerStopRef.current) currentOwnerStop = null;
    },
    [],
  );

  const maxCharsReady = (caps?.max_text_chars ?? 0) > 0;
  const available = isTtsAvailable(caps) && maxCharsReady && !disabled;
  const tooltip = available
    ? state === "loading"
      ? "正在生成语音…"
      : state === "playing"
        ? "暂停播报"
        : state === "paused"
          ? "继续播报"
          : "播报这条回复"
    : disabled
      ? "这条回复还在生成，稍后再播报"
      : (speechDisabledHint(caps, "tts") ??
        (isLoading ? "正在确认语音能力…" : "语音播报当前不可用"));

  return (
    <span className={cn("inline-flex items-center gap-1", className)}>
      <TooltipProvider delayDuration={300}>
        <Tooltip>
          {/* 禁用态的 button 带 pointer-events-none，hover 得由外层 span 接住。 */}
          <TooltipTrigger asChild>
            <span className="inline-flex">
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="h-7 w-7 text-muted-foreground"
                onClick={toggle}
                // 正在出声时永不禁用：否则父级把消息切到「流式中」会让用户没法停下声音。
                disabled={!available && state === "idle"}
                aria-label={`语音播报这条回复（消息 ${messageId.slice(0, 8)}）`}
                aria-pressed={isPlaybackActive(state)}
              >
                {state === "loading" ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : state === "playing" ? (
                  <Pause className="h-4 w-4" />
                ) : state === "paused" ? (
                  <Play className="h-4 w-4" />
                ) : (
                  <Volume2 className="h-4 w-4" />
                )}
              </Button>
            </span>
          </TooltipTrigger>
          <TooltipContent side="top" className="max-w-[16rem] whitespace-normal">
            {segmentCount > 1 && isPlaybackActive(state)
              ? `${tooltip}（第 ${segmentIndex + 1}/${segmentCount} 段）`
              : tooltip}
          </TooltipContent>
        </Tooltip>
        {isPlaybackActive(state) && (
          <Tooltip>
            <TooltipTrigger asChild>
              <Button
                type="button"
                size="icon"
                variant="ghost"
                className="h-7 w-7 text-muted-foreground"
                onClick={stopRef.current}
                aria-label="停止播报"
              >
                <Square className="h-4 w-4" />
              </Button>
            </TooltipTrigger>
            <TooltipContent side="top">停止播报</TooltipContent>
          </Tooltip>
        )}
      </TooltipProvider>
      {notice && (
        <span
          className="max-w-[16rem] truncate text-[11px] text-destructive"
          role="alert"
          title={notice}
        >
          {notice}
        </span>
      )}
    </span>
  );
}
