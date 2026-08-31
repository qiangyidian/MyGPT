"use client";

import { useEffect, useState } from "react";
import { Download, FileText, Loader2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { artifactsApi } from "@/lib/api";
import { formatArtifactSize } from "@/lib/artifacts";
import { openArtifactPreview } from "@/lib/artifact-preview-store";

/**
 * Inline card for an `artifact:<id>` handle found in message text (or listed in
 * the message's persisted `metadata.artifacts`). Fetches lightweight metadata
 * (`GET /api/artifacts/{id}/meta`) so the filename + size render immediately.
 * Clicking the card opens the right-side preview panel; the download button
 * streams the bytes with auth directly.
 */
export function InlineArtifactHandle({
  artifactId,
  className,
}: {
  artifactId: string;
  className?: string;
}) {
  const [downloading, setDownloading] = useState(false);

  // Metadata for the label; stale-forever cache (artifact rows are immutable).
  const { data: meta } = useQuery({
    queryKey: ["artifact-meta", artifactId],
    queryFn: () => artifactsApi.getMeta(artifactId),
    staleTime: Infinity,
    retry: false,
    refetchOnWindowFocus: false,
  });
  // Resolve the label once metadata arrives (filename + size).
  const [label, setLabel] = useState<string | null>(null);
  useEffect(() => {
    if (meta?.filename) {
      setLabel(meta.size ? `${meta.filename} · ${formatArtifactSize(meta.size)}` : meta.filename);
    }
  }, [meta?.filename, meta?.size]);

  const handleDownload = async () => {
    setDownloading(true);
    try {
      const blob = await artifactsApi.download(artifactId);
      const name =
        meta?.filename ||
        (blob.type.startsWith("image/")
          ? `artifact.${blob.type.slice("image/".length)}`
          : blob.type.startsWith("audio/")
            ? `artifact.${blob.type.slice("audio/".length)}`
            : "artifact.bin");
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
        "inline-flex cursor-pointer items-center gap-1.5 rounded-md border border-border bg-card px-2 py-1 text-[11px] align-middle transition-colors hover:border-primary/40 hover:bg-accent",
        className,
      )}
      data-artifact-id={artifactId}
      onClick={() => openArtifactPreview(artifactId)}
      title="点击预览"
      role="button"
      tabIndex={0}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openArtifactPreview(artifactId);
        }
      }}
    >
      <FileText className="h-3 w-3 shrink-0 text-muted-foreground" />
      <span
        className="max-w-[220px] truncate text-muted-foreground"
        title={label ?? undefined}
      >
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
