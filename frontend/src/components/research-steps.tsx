"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Check,
  Globe,
  Search,
  Terminal,
  AlertCircle,
  Database,
  FileText,
  Clock,
  ShieldAlert,
  ListChecks,
  BrainCircuit,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { AgentStep } from "@/lib/types";

interface ResearchStepsProps {
  steps: AgentStep[];
  /** True while the enclosing turn is still streaming (live mode). The panel
   *  only renders for in-flight runs: it shimmers while steps execute and
   *  collapses away once the turn settles (GPT high-reasoning style). */
  live?: boolean;
}

const TOOL_META: Record<string, { label: string; icon: typeof Search }> = {
  web_search: { label: "搜索", icon: Search },
  http_get: { label: "读取网页", icon: Globe },
  python_exec: { label: "运行代码", icon: Terminal },
  db_query: { label: "查询数据库", icon: Database },
  file_analyze: { label: "分析文件", icon: FileText },
  datetime_now: { label: "获取时间", icon: Clock },
  terminal: { label: "终端", icon: Terminal },
  read_file: { label: "读取文件", icon: FileText },
  write_file: { label: "写入文件", icon: FileText },
  browser: { label: "浏览器", icon: Globe },
  memory: { label: "记忆", icon: Database },
};

const TYPE_META: Record<
  AgentStep["type"],
  { label: string; icon: typeof ListChecks }
> = {
  plan: { label: "计划", icon: ListChecks },
  agent: { label: "智能体", icon: BrainCircuit },
  tool: { label: "工具", icon: Search },
  review: { label: "审查", icon: Check },
  approval: { label: "确认", icon: ShieldAlert },
};

/** Hermes tool steps carry the server's own human-readable label + emoji in
 *  tool.argumentsPreview ({label, emoji}); prefer them over the name map. */
function stepTitle(step: AgentStep): string {
  if (step.type === "agent" && step.title === "subagent") {
    return "子代理任务";
  }
  if (step.type === "tool" && step.tool) {
    const args = (step.tool.argumentsPreview ?? {}) as Record<string, unknown>;
    const hermesLabel = typeof args.label === "string" ? args.label.trim() : "";
    if (hermesLabel) return hermesLabel;
    const meta = TOOL_META[step.tool.name];
    const detail =
      (typeof args.query === "string" && args.query) ||
      (typeof args.url === "string" && args.url) ||
      (typeof args.code === "string" && args.code.slice(0, 60)) ||
      (typeof args.sql === "string" && args.sql.slice(0, 60)) ||
      "";
    return `${meta?.label ?? step.tool.name}${detail ? `: ${detail}` : ""}`;
  }
  return step.title;
}

/** Hermes emoji prefix (argumentsPreview.emoji), e.g. "🔎". */
function stepEmoji(step: AgentStep): string {
  if (step.tool) {
    const args = (step.tool.argumentsPreview ?? {}) as Record<string, unknown>;
    const emoji = typeof args.emoji === "string" ? args.emoji.trim() : "";
    if (emoji) return emoji;
  }
  if (step.type === "agent" && step.title === "subagent") return "🧠";
  return "";
}

function stepIcon(step: AgentStep): typeof Search {
  if (step.type === "agent" && step.title === "subagent") return BrainCircuit;
  if (step.type === "tool" && step.tool) {
    return TOOL_META[step.tool.name]?.icon ?? Globe;
  }
  return TYPE_META[step.type]?.icon ?? Globe;
}

/** Human duration between startedAt/finishedAt (or an explicit `duration`
 *  seconds field the backend persists for Hermes subagent steps). */
function stepDuration(step: AgentStep): string | null {
  const meta = step as AgentStep & { duration?: number | string };
  const dur =
    typeof meta.duration === "number"
      ? meta.duration
      : typeof meta.duration === "string"
        ? Number(meta.duration)
        : NaN;
  if (Number.isFinite(dur) && dur >= 0) return formatSeconds(dur);
  if (step.startedAt && step.finishedAt) {
    const ms = new Date(step.finishedAt).getTime() - new Date(step.startedAt).getTime();
    if (Number.isFinite(ms) && ms >= 0) return formatSeconds(ms / 1000);
  }
  return null;
}

function formatSeconds(seconds: number): string {
  if (seconds < 1) return "<1s";
  if (seconds < 60) return `${Math.round(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.round(seconds % 60);
  return s ? `${m}m${s}s` : `${m}m`;
}

/** Delay before unmounting after the run settles — lets the collapse
 *  transition play out instead of the panel vanishing in one frame. */
const COLLAPSE_MS = 350;

export function ResearchSteps({ steps, live = false }: ResearchStepsProps) {
  const [open, setOpen] = useState(true);
  const [hidden, setHidden] = useState(true);
  const everActiveRef = useRef(false);
  const hideTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Auto-scroll: stick to the newest step like the GPT reasoning list, but
  // stop hijacking the scroll the moment the user scrolls up to inspect an
  // earlier step (they re-stick by scrolling back near the bottom).
  const listRef = useRef<HTMLOListElement>(null);
  const stickToBottomRef = useRef(true);

  const runningStep =
    steps?.some((s) => s.status === "running" || s.status === "waiting") ?? false;
  const active = live || runningStep;

  useEffect(() => {
    if (active) {
      if (hideTimerRef.current) {
        clearTimeout(hideTimerRef.current);
        hideTimerRef.current = null;
      }
      everActiveRef.current = true;
      setHidden(false);
    } else if (everActiveRef.current) {
      everActiveRef.current = false;
      hideTimerRef.current = setTimeout(() => {
        hideTimerRef.current = null;
        setHidden(true);
      }, COLLAPSE_MS);
    }
  }, [active]);

  useEffect(
    () => () => {
      if (hideTimerRef.current) clearTimeout(hideTimerRef.current);
    },
    []
  );

  const stepCount = steps?.length ?? 0;

  useEffect(() => {
    const el = listRef.current;
    if (active && open && el && stickToBottomRef.current) {
      el.scrollTop = el.scrollHeight;
    }
  }, [stepCount, active, open]);

  if (!steps || steps.length === 0 || hidden) return null;

  const doneCount = steps.filter((s) => s.status === "done").length;
  const errorCount = steps.filter((s) => s.status === "error").length;
  const pendingCount = Math.max(steps.length - doneCount - errorCount, 0);
  // Between `active` flipping false and the unmount timer, the panel plays
  // its collapse transition instead of cutting off mid-frame.
  const collapsing = !active;

  return (
    <div
      aria-hidden={collapsing}
      className={cn(
        "overflow-hidden rounded-lg border border-border bg-background/60 text-sm transition-all duration-300 ease-out",
        collapsing ? "max-h-0 border-transparent opacity-0" : "mb-2 max-h-[480px] opacity-100"
      )}
    >
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-accent/40"
      >
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform",
            open ? "" : "-rotate-90"
          )}
        />
        <span className="shimmer-text text-xs font-medium">正在执行</span>
        <span className="text-xs text-muted-foreground">
          · {steps.length} 步
          {errorCount > 0 && (
            <span className="ml-1.5 text-destructive">（{errorCount} 失败）</span>
          )}
        </span>
        <span className="ml-auto shrink-0 text-xs tabular-nums text-muted-foreground">
          {pendingCount > 0 ? `${pendingCount} 项进行中` : "进行中"}
        </span>
      </button>

      {open && (
        <ol
          ref={listRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            stickToBottomRef.current =
              el.scrollHeight - el.scrollTop - el.clientHeight < 40;
          }}
          className="max-h-60 space-y-1.5 overflow-y-auto border-t border-border px-3 py-2"
        >
          {steps.map((step, i) => {
            const Icon = stepIcon(step);
            const emoji = stepEmoji(step);
            const duration = stepDuration(step);
            const dangerous = step.type === "tool" && step.tool?.dangerous;
            const isRunning = step.status === "running";
            return (
              <li key={step.id ?? i} className="step-row-in flex gap-2">
                <div className="mt-0.5 flex shrink-0 flex-col items-center">
                  <span
                    className={cn(
                      "flex h-5 w-5 items-center justify-center rounded-full",
                      isRunning
                        ? "bg-primary/10 text-primary"
                        : step.status === "waiting"
                          ? "bg-amber-500/10 text-amber-600"
                          : step.status === "error"
                            ? "bg-destructive/10 text-destructive"
                            : "bg-muted text-muted-foreground"
                    )}
                  >
                    {isRunning ? (
                      // No spinner — the shimmering label below carries the
                      // in-flight signal (GPT reasoning style).
                      <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-primary" />
                    ) : step.status === "error" ? (
                      <AlertCircle className="h-3 w-3" />
                    ) : step.status === "waiting" ? (
                      <ShieldAlert className="h-3 w-3" />
                    ) : (
                      <Check className="h-3 w-3" />
                    )}
                  </span>
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 text-xs">
                    {emoji ? (
                      <span className="text-[13px] leading-none" aria-hidden>
                        {emoji}
                      </span>
                    ) : (
                      <Icon className="h-3 w-3 shrink-0 text-muted-foreground" />
                    )}
                    <span
                      className={cn("truncate", isRunning ? "shimmer-text" : "text-foreground")}
                    >
                      {stepTitle(step)}
                    </span>
                    {duration && (
                      <span className="shrink-0 text-[10px] tabular-nums text-muted-foreground/70">
                        {duration}
                      </span>
                    )}
                    {dangerous && (
                      <span className="inline-flex items-center gap-0.5 rounded bg-amber-500/10 px-1 text-[10px] font-medium text-amber-600">
                        <ShieldAlert className="h-2.5 w-2.5" /> 高风险
                      </span>
                    )}
                  </div>
                  {step.summary && (
                    <div className="mt-0.5 text-[11px] text-muted-foreground">{step.summary}</div>
                  )}
                  {step.tool?.resultPreview && (
                    <div className="mt-0.5 line-clamp-2 break-words text-[11px] text-muted-foreground">
                      {step.tool.resultPreview}
                    </div>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
