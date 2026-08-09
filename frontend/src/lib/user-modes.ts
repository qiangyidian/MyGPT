"use client";

import { Brain, Zap, type LucideIcon } from "lucide-react";
import type { UserChatMode } from "@/lib/types";

export interface UserModeMeta {
  value: UserChatMode;
  label: string;
  short: string;
  description: string;
  icon: LucideIcon;
}

/**
 * The single source of truth for the user-facing mode picker. The UI never
 * references internal runtime enums — it only ever sends ``value`` to the
 * backend, and the backend IntentRouter decides the runtime/profile/tools.
 *
 * Only two modes are exposed:
 *   - ``speed``  极速模式: single-agent native answer, NO multi-agent, fastest
 *                first token. The everyday "just answer me" mode.
 *   - ``expert`` 专家模式: multi-agent research crew by default (retrieval +
 *                cross-check + cited deep analysis; parallel when a KB is bound).
 */
export const USER_MODES: UserModeMeta[] = [
  {
    value: "speed",
    label: "极速模式",
    short: "极速",
    description: "快速直接回答，不启动多 Agent，首字最快。适合日常问答。",
    icon: Zap,
  },
  {
    value: "expert",
    label: "专家模式",
    short: "专家",
    description: "默认使用多 Agent 协作：检索、交叉核对、带引用的深入分析。适合复杂或需要溯源的问题。",
    icon: Brain,
  },
];

const MODE_MAP: Record<string, UserModeMeta> = USER_MODES.reduce(
  (acc, m) => {
    acc[m.value] = m;
    return acc;
  },
  {} as Record<string, UserModeMeta>
);

export function getModeMeta(mode: UserChatMode | string | undefined): UserModeMeta {
  return MODE_MAP[(mode as string) ?? "speed"] ?? USER_MODES[0];
}

export function isUserChatMode(v: unknown): v is UserChatMode {
  return typeof v === "string" && v in MODE_MAP;
}

/**
 * Modes that launch the multi-agent pipeline. When active the composer shows an
 * explicit badge so the user always knows they are NOT in ordinary single-agent
 * chat. (专家 = multi-agent; 极速 = never.)
 */
export const SPECIAL_MODES: ReadonlySet<string> = new Set(["expert"]);

export function isSpecialMode(m: UserChatMode | string | undefined): boolean {
  return !!m && (SPECIAL_MODES as Set<string>).has(m);
}
