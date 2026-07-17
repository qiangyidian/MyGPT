"use client";

import { useState } from "react";
import {
  ChevronDown,
  Check,
  Globe,
  Loader2,
  Search,
  Terminal,
  AlertCircle,
} from "lucide-react";

import { cn } from "@/lib/utils";
import type { ResearchStep } from "@/lib/types";

interface ResearchStepsProps {
  steps: ResearchStep[];
  /** When true, the panel auto-expands (e.g. while the agent is running). */
  defaultOpen?: boolean;
}

const TOOL_META: Record<string, { label: string; icon: typeof Search }> = {
  web_search: { label: "搜索", icon: Search },
  http_get: { label: "读取网页", icon: Globe },
  python_exec: { label: "运行代码", icon: Terminal },
  db_query: { label: "查询数据库", icon: Terminal },
  file_analyze: { label: "分析文件", icon: Globe },
  datetime_now: { label: "获取时间", icon: Globe },
};

function stepTitle(step: ResearchStep): string {
  const meta = TOOL_META[step.name];
  const args = step.arguments ?? {};
  const detail =
    (typeof args.query === "string" && args.query) ||
    (typeof args.url === "string" && args.url) ||
    (typeof args.code === "string" && args.code.slice(0, 60)) ||
    "";
  return `${meta?.label ?? step.name}${detail ? `: ${detail}` : ""}`;
}

export function ResearchSteps({ steps, defaultOpen = true }: ResearchStepsProps) {
  const [open, setOpen] = useState(defaultOpen);
  if (!steps || steps.length === 0) return null;

  const running = steps.filter((s) => s.status === "running").length;

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
          思考与搜索 · {steps.length} 步
          {running > 0 && (
            <span className="ml-2 inline-flex items-center gap-1 text-foreground">
              <Loader2 className="h-3 w-3 animate-spin" /> 进行中
            </span>
          )}
        </span>
      </button>

      {open && (
        <ol className="space-y-1.5 border-t border-border px-3 py-2">
          {steps.map((step, i) => {
            const Icon = TOOL_META[step.name]?.icon ?? Globe;
            return (
              <li key={step.id ?? i} className="flex gap-2">
                <div className="mt-0.5 flex shrink-0 flex-col items-center">
                  <span
                    className={cn(
                      "flex h-5 w-5 items-center justify-center rounded-full",
                      step.status === "running"
                        ? "bg-primary/10 text-primary"
                        : step.status === "error"
                          ? "bg-destructive/10 text-destructive"
                          : "bg-muted text-muted-foreground"
                    )}
                  >
                    {step.status === "running" ? (
                      <Loader2 className="h-3 w-3 animate-spin" />
                    ) : step.status === "error" ? (
                      <AlertCircle className="h-3 w-3" />
                    ) : (
                      <Check className="h-3 w-3" />
                    )}
                  </span>
                </div>
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-1.5 text-xs">
                    <Icon className="h-3 w-3 text-muted-foreground" />
                    <span className="truncate text-foreground">{stepTitle(step)}</span>
                  </div>
                  {step.result && (
                    <div className="mt-0.5 line-clamp-2 break-words text-[11px] text-muted-foreground">
                      {step.result}
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
