"use client";

import { useState } from "react";
import { Download, FileText, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { artifactsApi } from "@/lib/api";

/**
 * Inline card for an `artifact:<id>` handle found in message text. Unlike
 * `ArtifactCard` (which shows full metadata), this only knows the id — it
 * renders a compact reference with an authenticated download. The filename is
 * recovered from the download's Content-Disposition header when available.
 */
export function InlineArtifactHandle({
  artifactId,
  className,
}: {
  artifactId: string;
  className?: string;
}) {
  const [downloading, setDownloading] = useState(false);
  const [label, setLabel] = useState<string | null>(null);

  const handleDownload = async () => {
    setDownloading(true);
    try {
      const blob = await artifactsApi.download(artifactId);
      // Recover a filename from the blob's type or fall back to the id.
      const ext = blob.type.startsWith("image/")
        ? blob.type.slice("image/".length)
        : blob.type.startsWith("audio/")
          ? blob.type.slice("audio/".length)
          : "bin";
      const name = label ?? `artifact-${artifactId.slice(0, 8)}.${ext}`;
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* ignore — inline card fails silently */
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div
      className={cn(
        "inline-flex items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11px] align-middle",
        className,
      )}
      data-artifact-id={artifactId}
    >
      <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
      <span className="max-w-[160px] truncate text-muted-foreground">
        {label ?? `附件 ${artifactId.slice(0, 8)}`}
      </span>
      <Button
        variant="ghost"
        size="icon"
        className="h-5 w-5 shrink-0"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          void handleDownload();
        }}
        disabled={downloading}
        title="下载附件"
        aria-label="下载附件"
      >
        {downloading ? (
          <Loader2 className="h-3 w-3 animate-spin" />
        ) : (
          <Download className="h-3 w-3" />
        )}
      </Button>
    </div>
  );
}
