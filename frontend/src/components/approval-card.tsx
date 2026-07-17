"use client";

import { ShieldAlert, Check, X, Loader2 } from "lucide-react";
import { useState } from "react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import type { PendingApproval } from "@/lib/types";

interface ApprovalCardProps {
  approval: PendingApproval;
  onApprove: (approvalId: string) => Promise<void>;
  onReject: (approvalId: string, reason?: string) => Promise<void>;
}

const RISK_LABEL: Record<string, { text: string; cls: string }> = {
  high: { text: "高风险", cls: "bg-destructive/10 text-destructive" },
  medium: { text: "中风险", cls: "bg-amber-500/10 text-amber-600" },
  low: { text: "低风险", cls: "bg-muted text-muted-foreground" },
};

/** Renders a human-approval gate for a dangerous tool call. */
export function ApprovalCard({ approval, onApprove, onReject }: ApprovalCardProps) {
  const [busy, setBusy] = useState<"approve" | "reject" | null>(null);
  const risk = RISK_LABEL[approval.riskLevel] ?? RISK_LABEL.medium;

  const handle = async (action: "approve" | "reject") => {
    setBusy(action);
    try {
      if (action === "approve") {
        await onApprove(approval.approvalId);
      } else {
        await onReject(approval.approvalId);
      }
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className="mb-2 rounded-lg border border-amber-500/40 bg-amber-500/5 p-3 text-sm">
      <div className="flex items-center gap-2">
        <ShieldAlert className="h-4 w-4 text-amber-600" />
        <span className="font-medium text-foreground">需要你的确认</span>
        <span className={cn("rounded px-1.5 py-0.5 text-[10px] font-medium", risk.cls)}>
          {risk.text}
        </span>
      </div>

      <div className="mt-2 space-y-1 text-xs text-muted-foreground">
        <div>
          <span className="text-foreground">工具：</span>
          <code className="rounded bg-muted px-1 py-0.5">{approval.toolName}</code>
        </div>
        <div>
          <span className="text-foreground">目的：</span>
          {approval.summary}
        </div>
        {Object.keys(approval.argumentsPreview ?? {}).length > 0 && (
          <details className="mt-1">
            <summary className="cursor-pointer select-none text-[11px]">参数预览</summary>
            <pre className="mt-1 max-h-32 overflow-auto rounded bg-muted p-2 text-[10px]">
              {JSON.stringify(approval.argumentsPreview, null, 2)}
            </pre>
          </details>
        )}
      </div>

      <div className="mt-3 flex items-center gap-2">
        <Button
          size="sm"
          variant="destructive"
          className="h-8 gap-1.5"
          onClick={() => handle("reject")}
          disabled={busy !== null}
        >
          {busy === "reject" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <X className="h-3.5 w-3.5" />}
          拒绝
        </Button>
        <Button
          size="sm"
          className="h-8 gap-1.5"
          onClick={() => handle("approve")}
          disabled={busy !== null}
        >
          {busy === "approve" ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5" />}
          允许本次执行
        </Button>
      </div>
    </div>
  );
}
