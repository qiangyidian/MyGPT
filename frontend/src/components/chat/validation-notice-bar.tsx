"use client";

import { AlertTriangle, ShieldAlert } from "lucide-react";

import { cn } from "@/lib/utils";
import { validationBadge } from "@/lib/validation-notice";
import type { ValidationNotice } from "@/lib/types";

const TONE: Record<"danger" | "warning" | "info", { className: string; Icon: typeof AlertTriangle }> = {
  danger: {
    className: "border-destructive/40 bg-destructive/10 text-destructive",
    Icon: ShieldAlert,
  },
  warning: {
    className: "border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-400",
    Icon: AlertTriangle,
  },
  info: {
    className: "border-border bg-muted/50 text-muted-foreground",
    Icon: AlertTriangle,
  },
};

/**
 * 生成后校验（RAG-13）的结论条。干净的回合**不渲染**（见 `validationBadge`）。
 *
 * 细节（哪几条检查没过）收在折叠里：默认只留一句能读懂的话，想追责的运维/合规同学
 * 点开就有 —— 全展开的话每一条被摘除主张的回答都会变成一面告警墙。
 */
export function ValidationNoticeBar({
  notice,
  className,
}: {
  notice: ValidationNotice | null;
  className?: string;
}) {
  const badge = validationBadge(notice);
  if (!badge) return null;
  const { className: toneClass, Icon } = TONE[badge.tone];
  return (
    <div
      className={cn(
        "mt-1.5 rounded-md border px-3 py-1.5 text-xs",
        toneClass,
        className,
      )}
      title={badge.title}
      data-testid="validation-notice"
      data-validation-status={notice?.status}
    >
      <div className="flex items-center gap-2">
        <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden />
        <span>{badge.label}</span>
        {badge.details.length > 0 && (
          <details className="ml-auto shrink-0">
            <summary className="cursor-pointer select-none underline-offset-2 hover:underline">
              校验详情
            </summary>
            <ul className="mt-1 list-disc space-y-0.5 pl-4">
              {badge.details.map((d) => (
                <li key={d}>{d}</li>
              ))}
            </ul>
          </details>
        )}
      </div>
    </div>
  );
}
