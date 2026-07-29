"use client";

import { AlertCircle, FileText, FileSpreadsheet, Image as ImageIcon, Loader2, X } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

export interface AttachmentCardData {
  id: string;
  filename: string;
  mime_type?: string;
  size_bytes?: number;
  status?: string;
  parse_status?: string;
  uploading?: boolean;
  error?: string | null;
}

function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

function iconFor(a: AttachmentCardData) {
  const ext = extOf(a.filename);
  if (a.mime_type?.startsWith("image/") || [".png", ".jpg", ".jpeg", ".webp", ".gif"].includes(ext)) {
    return ImageIcon;
  }
  if ([".csv", ".xlsx", ".xls"].includes(ext)) return FileSpreadsheet;
  return FileText;
}

export function formatSize(bytes?: number): string {
  if (!bytes && bytes !== 0) return "";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

function statusLabel(a: AttachmentCardData): string | null {
  if (a.uploading || a.status === "uploading") return "上传中…";
  if (a.status === "parsing" || a.parse_status === "parsing") return "正在解析…";
  if (a.status === "failed") return a.error || "处理失败";
  if (a.status === "ready" || a.parse_status === "ready") return "就绪";
  if (a.parse_status === "skipped") return "已就绪";
  return null;
}

export function AttachmentCard({
  attachment,
  onRemove,
  onPreview,
  className,
}: {
  attachment: AttachmentCardData;
  onRemove?: (id: string) => void;
  onPreview?: (id: string) => void;
  className?: string;
}) {
  const Icon = iconFor(attachment);
  const label = statusLabel(attachment);
  const isFailed = attachment.status === "failed";
  const busy = attachment.uploading || attachment.status === "uploading" || attachment.parse_status === "parsing";

  return (
    <div
      className={cn(
        "group flex items-center gap-2.5 rounded-lg border border-border bg-card px-2.5 py-2 text-sm",
        busy && "animate-pulse",
        isFailed && "border-destructive/40 bg-destructive/5",
        className
      )}
    >
      <button
        type="button"
        className="flex min-w-0 flex-1 items-center gap-2.5 text-left"
        onClick={() => !busy && onPreview?.(attachment.id)}
        disabled={busy}
        aria-label={`附件 ${attachment.filename}`}
      >
        <span className="flex h-8 w-8 shrink-0 items-center justify-center rounded-md bg-muted text-muted-foreground">
          {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Icon className="h-4 w-4" />}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-xs font-medium">{attachment.filename}</span>
          <span className="block truncate text-[11px] text-muted-foreground">
            {label ? (
              isFailed ? (
                <span className="inline-flex items-center gap-1 text-destructive">
                  <AlertCircle className="h-3 w-3" /> {label}
                </span>
              ) : (
                label
              )
            ) : (
              formatSize(attachment.size_bytes)
            )}
          </span>
        </span>
      </button>
      {onRemove && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          className="h-6 w-6 shrink-0 text-muted-foreground opacity-60 hover:opacity-100"
          onClick={() => onRemove(attachment.id)}
          aria-label={`移除附件 ${attachment.filename}`}
        >
          <X className="h-3.5 w-3.5" />
        </Button>
      )}
    </div>
  );
}
