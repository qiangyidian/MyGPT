"use client";

import {
  BarChart3,
  Compass,
  FileSearch,
  PenLine,
  Scale,
  Sparkles,
  type LucideIcon,
} from "lucide-react";
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
 */
export const USER_MODES: UserModeMeta[] = [
  {
    value: "auto",
    label: "自动",
    short: "自动",
    description: "由系统根据你的问题自动选择最合适的方式。",
    icon: Sparkles,
  },
  {
    value: "search",
    label: "搜索",
    short: "搜索",
    description: "联网搜索最新资料，多轮检索后给出带来源的回答。",
    icon: Compass,
  },
  {
    value: "deep_research",
    label: "深度研究",
    short: "深度研究",
    description: "多 Agent 协作：检索、核对、撰写，适合复杂或需要溯源的问题。",
    icon: FileSearch,
  },
  {
    value: "create",
    label: "创作",
    short: "创作",
    description: "长文写作、改写、总结，聚焦内容本身，不联网。",
    icon: PenLine,
  },
  {
    value: "data_analysis",
    label: "数据分析",
    short: "数据分析",
    description: "上传表格或文件，进行分析、计算与可视化。",
    icon: BarChart3,
  },
  {
    value: "debate",
    label: "多 Agent 辩论",
    short: "辩论",
    description: "由双方 Agent 独立论证，再由裁判 Agent 按统一标准评估并给出结论。",
    icon: Scale,
  },
];

const MODE_MAP: Record<UserChatMode, UserModeMeta> = USER_MODES.reduce(
  (acc, m) => {
    acc[m.value] = m;
    return acc;
  },
  {} as Record<UserChatMode, UserModeMeta>
);

export function getModeMeta(mode: UserChatMode | string | undefined): UserModeMeta {
  return (
    MODE_MAP[(mode as UserChatMode) ?? "auto"] ??
    USER_MODES[0]
  );
}

export function isUserChatMode(v: unknown): v is UserChatMode {
  return typeof v === "string" && v in MODE_MAP;
}

/**
 * Modes that meaningfully change the answer pipeline (multi-agent research,
 * multi-agent debate, file-backed data analysis). When active the composer
 * shows an explicit, hard-to-miss badge so the user always knows they are NOT
 * in ordinary chat — preventing the "UI says auto, request sends deep_research"
 * class of inconsistency.
 */
export const SPECIAL_MODES: ReadonlySet<UserChatMode> = new Set([
  "deep_research",
  "debate",
  "data_analysis",
]);

export function isSpecialMode(m: UserChatMode | string | undefined): boolean {
  return !!m && (SPECIAL_MODES as Set<string>).has(m);
}
