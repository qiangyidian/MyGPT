"use client";

import { useCallback, useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { SpeechCapabilities } from "@/lib/types";

/** 全模块共享的一次探测。
 *
 *  每条助手消息都有一个喇叭按钮，一个 60 条消息的窗口就会打出 60 次
 *  ``/api/speech/capabilities`` —— 这个端点每次要做一次模型配置查询，纯属浪费。
 *  成功结果缓存在模块作用域（和页面同生命周期），**失败不缓存**：探测失败往往
 *  是网络抖动，下一次挂载应该重新问一遍，而不是把按钮永久钉成禁用态。
 *  刻意不做 React Query：这两个组件都在消息列表里，多一次 provider 依赖不划算。 */
let cache: Promise<SpeechCapabilities> | null = null;

export function loadSpeechCapabilities(
  force = false,
): Promise<SpeechCapabilities> {
  if (!force && cache) return cache;
  const attempt = api.getSpeechCapabilities().catch((err: unknown) => {
    // 只清掉「这一次」的缓存，避免旧请求的失败覆盖掉后来者成功的结果。
    if (cache === attempt) cache = null;
    throw err;
  });
  cache = attempt;
  return attempt;
}

export interface UseSpeechCapabilities {
  /** 探测成功后的载荷；仍在路上或失败时为 null（组件据此走禁用态，不猜能力）。 */
  caps: SpeechCapabilities | null;
  isLoading: boolean;
  /** 探测失败（网络/未登录）。UI 只表现为「不可用 + 可点的重试」。 */
  failed: boolean;
  retry: () => void;
}

export function useSpeechCapabilities(): UseSpeechCapabilities {
  const [caps, setCaps] = useState<SpeechCapabilities | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setIsLoading(true);
    loadSpeechCapabilities()
      .then((data) => {
        if (cancelled) return;
        setCaps(data);
        setFailed(false);
      })
      .catch(() => {
        if (cancelled) return;
        setCaps(null);
        setFailed(true);
      })
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const retry = useCallback(() => {
    setIsLoading(true);
    loadSpeechCapabilities(true)
      .then((data) => {
        setCaps(data);
        setFailed(false);
      })
      .catch(() => setFailed(true))
      .finally(() => setIsLoading(false));
  }, []);

  return { caps, isLoading, failed, retry };
}
