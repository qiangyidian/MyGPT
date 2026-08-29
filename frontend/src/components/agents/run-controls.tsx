"use client";

import { useState } from "react";
import {
  AlertTriangle,
  Ban,
  Loader2,
  Octagon,
  Play,
  Send,
  ShieldQuestion,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { DurableRunState } from "@/lib/run-events";

interface RunControlsProps {
  state: DurableRunState;
  onPause?: (runId: string) => Promise<unknown>;
  onResume?: (runId: string) => Promise<unknown>;
  onCancel?: (runId: string) => Promise<unknown>;
  onInstruction?: (runId: string, instruction: string) => Promise<unknown>;
  className?: string;
}

/**
 * Durable run control surface: pause / resume / cancel / append-instruction,
 * plus status banners for the paused, blocked (approval/plan), recovery, and
 * budget states. Shown in the Execution tab. These endpoints never execute
 * the run — they only signal the durable workflow.
 */
export function RunControls({
  state,
  onPause,
  onResume,
  onCancel,
  onInstruction,
  className,
}: RunControlsProps) {
  const [busy, setBusy] = useState<
    "pause" | "resume" | "cancel" | "instruction" | null
  >(null);
  const [instructionOpen, setInstructionOpen] = useState(false);
  const [draft, setDraft] = useState("");

  const terminal = ["completed", "failed", "cancelled"].includes(state.runStatus);

  const run = async (
    action: "pause" | "resume" | "cancel" | "instruction",
    fn: () => Promise<unknown>,
  ) => {
    setBusy(action);
    try {
      await fn();
    } finally {
      setBusy(null);
    }
  };

  const statusBanner = (() => {
    if (state.runStatus === "paused") {
      return {
        text: "运行已暂停，恢复后继续。",
        cls: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
        Icon: Octagon,
      };
    }
    if (state.runStatus === "failed") {
      return {
        text: state.error ? `运行失败：${state.error}` : "运行失败",
        cls: "border-destructive/40 bg-destructive/10 text-destructive",
        Icon: AlertTriangle,
      };
    }
    if (state.blocked) {
      return {
        text: "等待你的确认（工具审批 / 计划确认）。",
        cls: "border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-400",
        Icon: ShieldQuestion,
      };
    }
    if (state.runStatus === "pending") {
      return {
        text: "计划已发布，等待确认后开始执行。",
        cls: "border-blue-500/40 bg-blue-500/10 text-blue-700 dark:text-blue-400",
        Icon: ShieldQuestion,
      };
    }
    return null;
  })();

  return (
    <div className={cn("space-y-2", className)}>
      {statusBanner && (
        <div
          className={cn(
            "flex items-center gap-2 rounded-md border px-2.5 py-1.5 text-[11px]",
            statusBanner.cls,
          )}
        >
          <statusBanner.Icon className="h-3.5 w-3.5 shrink-0" />
          <span className="truncate">{statusBanner.text}</span>
        </div>
      )}

      <div className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <span>
          状态：<span className="font-medium text-foreground">{state.runStatus}</span>
        </span>
        <span aria-hidden>·</span>
        <span>订阅：{state.subscriptionStatus}</span>
        <span aria-hidden>·</span>
        <span>事件：{state.events.length}</span>
      </div>

      {!terminal && (onPause || onResume || onCancel || onInstruction) && (
        <div className="flex flex-wrap items-center gap-1.5">
          {state.runStatus === "paused" && onResume && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 gap-1 text-[11px]"
              onClick={() => run("resume", () => onResume!(state.runId))}
              disabled={busy !== null}
            >
              {busy === "resume" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Play className="h-3 w-3" />
              )}
              恢复
            </Button>
          )}
          {state.runStatus !== "paused" && onPause && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 gap-1 text-[11px]"
              onClick={() => run("pause", () => onPause!(state.runId))}
              disabled={busy !== null}
            >
              {busy === "pause" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Octagon className="h-3 w-3" />
              )}
              暂停
            </Button>
          )}
          {onInstruction && (
            <Button
              size="sm"
              variant="outline"
              className="h-7 gap-1 text-[11px]"
              onClick={() => setInstructionOpen((v) => !v)}
              disabled={busy !== null}
            >
              <Send className="h-3 w-3" />
              追加指令
            </Button>
          )}
          {onCancel && (
            <Button
              size="sm"
              variant="destructive"
              className="h-7 gap-1 text-[11px]"
              onClick={() => {
                if (confirm("取消本次运行？")) {
                  run("cancel", () => onCancel!(state.runId));
                }
              }}
              disabled={busy !== null}
            >
              {busy === "cancel" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Ban className="h-3 w-3" />
              )}
              取消
            </Button>
          )}
        </div>
      )}

      {instructionOpen && onInstruction && (
        <div className="space-y-1.5">
          <Textarea
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="min-h-[56px] bg-background text-xs"
            placeholder="追加运行中的指引（例如：聚焦 X、跳过 Y）"
          />
          <div className="flex justify-end gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 text-[11px]"
              onClick={() => {
                setDraft("");
                setInstructionOpen(false);
              }}
            >
              取消
            </Button>
            <Button
              size="sm"
              className="h-7 gap-1 text-[11px]"
              disabled={!draft.trim() || busy !== null}
              onClick={() =>
                run("instruction", async () => {
                  await onInstruction!(state.runId, draft.trim());
                  setDraft("");
                  setInstructionOpen(false);
                })
              }
            >
              {busy === "instruction" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Send className="h-3 w-3" />
              )}
              发送
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
