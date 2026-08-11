"use client";

import { useState } from "react";
import { Check, ClipboardList, Loader2, Pencil, X } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

/** A plan step shown in the plan-review card. */
export interface ReviewablePlanStep {
  id: string;
  title: string;
  description?: string;
  sources?: string[];
}

interface PlanReviewProps {
  runId: string;
  summary: string;
  steps: ReviewablePlanStep[];
  /** Acceptance criteria surfaced to the user before they approve. */
  acceptanceCriteria?: string[];
  /** Current plan status (e.g. "proposed" | "confirmed" | "updated"). */
  status?: string;
  onApprove?: (runId: string) => Promise<unknown>;
  onRevise?: (runId: string, revision: { summary?: string }) => Promise<unknown>;
  className?: string;
}

/**
 * Render an agent plan with its acceptance criteria and approve / revise
 * controls. Calls the durable run-control endpoints
 * (`POST /api/agent-runs/{id}/plan/confirm` and `/plan/update`).
 */
export function PlanReview({
  runId,
  summary,
  steps,
  acceptanceCriteria,
  status,
  onApprove,
  onRevise,
  className,
}: PlanReviewProps) {
  const [busy, setBusy] = useState<"approve" | "revise" | null>(null);
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(summary);
  const decided = status === "confirmed" || status === "updated";

  const handleApprove = async () => {
    if (!onApprove) return;
    setBusy("approve");
    try {
      await onApprove(runId);
    } finally {
      setBusy(null);
    }
  };

  const handleRevise = async () => {
    if (!onRevise) return;
    setBusy("revise");
    try {
      await onRevise(runId, { summary: draft.trim() || undefined });
      setEditing(false);
    } finally {
      setBusy(null);
    }
  };

  return (
    <div
      className={cn(
        "rounded-lg border border-border bg-card p-3 text-sm",
        className,
      )}
      data-run-id={runId}
    >
      <div className="flex items-center gap-2">
        <ClipboardList className="h-4 w-4 text-muted-foreground" />
        <span className="font-medium text-foreground">执行计划</span>
        {status && (
          <span className="rounded bg-muted px-1.5 py-0.5 text-[10px] text-muted-foreground">
            {status}
          </span>
        )}
      </div>

      {editing ? (
        <div className="mt-2 space-y-2">
          <Textarea
            autoFocus
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            className="min-h-[64px] bg-background text-xs"
          />
          <div className="flex justify-end gap-1">
            <Button
              variant="ghost"
              size="sm"
              className="h-7 gap-1 text-xs"
              onClick={() => {
                setDraft(summary);
                setEditing(false);
              }}
            >
              <X className="h-3 w-3" /> 取消
            </Button>
            <Button
              size="sm"
              className="h-7 gap-1 text-xs"
              onClick={handleRevise}
              disabled={busy !== null}
            >
              {busy === "revise" ? (
                <Loader2 className="h-3 w-3 animate-spin" />
              ) : (
                <Check className="h-3 w-3" />
              )}
              提交修改
            </Button>
          </div>
        </div>
      ) : (
        <p className="mt-1.5 text-xs text-muted-foreground">{summary}</p>
      )}

      {acceptanceCriteria && acceptanceCriteria.length > 0 && (
        <div className="mt-2">
          <div className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
            验收标准
          </div>
          <ul className="mt-1 list-inside list-disc space-y-0.5 text-xs text-muted-foreground">
            {acceptanceCriteria.map((c, i) => (
              <li key={i}>{c}</li>
            ))}
          </ul>
        </div>
      )}

      {steps.length > 0 && (
        <ol className="mt-2 space-y-1">
          {steps.map((s, i) => (
            <li key={s.id} className="flex gap-2 text-xs">
              <span className="flex h-4 w-4 shrink-0 items-center justify-center rounded-full bg-secondary text-[10px] text-muted-foreground">
                {i + 1}
              </span>
              <div className="min-w-0">
                <div className="truncate text-foreground">{s.title}</div>
                {s.description && (
                  <div className="truncate text-muted-foreground">
                    {s.description}
                  </div>
                )}
              </div>
            </li>
          ))}
        </ol>
      )}

      {(onApprove || onRevise) && !decided && (
        <div className="mt-3 flex items-center gap-2">
          {onRevise && !editing && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 gap-1.5"
              onClick={() => setEditing(true)}
              disabled={busy !== null}
            >
              <Pencil className="h-3.5 w-3.5" /> 修改
            </Button>
          )}
          {onApprove && (
            <Button
              size="sm"
              className="h-8 gap-1.5"
              onClick={handleApprove}
              disabled={busy !== null || editing}
            >
              {busy === "approve" ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <Check className="h-3.5 w-3.5" />
              )}
              确认执行
            </Button>
          )}
        </div>
      )}
    </div>
  );
}
