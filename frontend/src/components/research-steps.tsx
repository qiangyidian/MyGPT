"use client";

import { useEffect, useRef, useState } from "react";
import {
  ChevronDown,
  Check,
  Globe,
  Loader2,
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
  /** When true, the panel auto-expands (e.g. while the agent is running). */
  defaultOpen?: boolean;
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

export function ResearchSteps({ steps, defaultOpen = true }: ResearchStepsProps) {
  const [open, setOpen] = useState(defaultOpen);
  const wasRunningRef = useRef(false);

  const running =
    steps?.some((s) => s.status === "running" || s.status === "waiting") ?? false;

  // Agent-UX: auto-expand while work is in flight; once every step settles,
  // collapse to the one-line summary so the answer text takes over. The user
  // can still reopen — this only flips the default once per run.
  useEffect(() => {
    if (running) {
      wasRunningRef.current = true;
      setOpen(true);
    } else if (wasRunningRef.current) {
      wasRunningRef.current = false;
      setOpen(false);
    }
  }, [running]);

  if (!steps || steps.length === 0) return null;

  const doneCount = steps.filter((s) => s.status === "done").length;
  const errorCount = steps.filter((s) => s.status === "error").length;

  // Total wall time: earliest start → latest finish across steps.
  let totalMs = 0;
  const starts = steps
    .map((s) => (s.startedAt ? new Date(s.startedAt).getTime() : NaN))
    .filter(Number.isFinite);
  const ends = steps
    .map((s) => (s.finishedAt ? new Date(s.finishedAt).getTime() : NaN))
    .filter(Number.isFinite);
  if (starts.length && ends.length) {
    totalMs = Math.max(...ends) - Math.min(...starts);
  }

  return (
    <div className="mb-2 rounded-lg border border-border bg-background/60 text-sm">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        <ChevronDown
          className={cn("h-3.5 w-3.5 transition-transform", open ? "" : "-rotate-90")}
        />
        <span>
          {running ? "正在执行" : "已完成"} · {steps.length} 步
          {errorCount > 0 && (
            <span className="ml-1.5 text-destructive">（{errorCount} 失败）</span>
          )}
          {!running && totalMs > 0 && (
            <span className="ml-1.5 text-muted-foreground/80">
              · {formatSeconds(totalMs / 1000)}
            </span>
          )}
        </span>
        {running && (
          <span className="inline-flex items-center gap-1 text-foreground">
            <Loader2 className="h-3 w-3 animate-spin" />
            <span className="text-muted-foreground">
              {steps.length - doneCount - errorCount > 0
                ? `${steps.length - doneCount - errorCount} 项进行中`
                : "进行中"}
            </span>
          </span>
        )}
      </button>

      {open && (
        <ol className="space-y-1.5 border-t border-border px-3 py-2">
          {steps.map((step, i) => {
            const Icon = stepIcon(step);
            const emoji = stepEmoji(step);
            const duration = stepDuration(step);
            const dangerous = step.type === "tool" && step.tool?.dangerous;
            return (
              <li key={step.id ?? i} className="flex gap-2">
                <div className="mt-0.5 flex shrink-0 flex-col items-center">
                  <span
                    className={cn(
                      "flex h-5 w-5 items-center justify-center rounded-full",
                      step.status === "running"
                        ? "bg-primary/10 text-primary"
                        : step.status === "waiting"
                          ? "bg-amber-500/10 text-amber-600"
                          : step.status === "error"
                            ? "bg-destructive/10 text-destructive"
                            : "bg-muted text-muted-foreground"
                    )}
                  >
                    {step.status === "running" ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
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
                      <Icon className="h-3 w-3 text-muted-foreground" />
                    )}
                    <span className="truncate text-foreground">{stepTitle(step)}</span>
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
