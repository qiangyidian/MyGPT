"use client";

import { useState } from "react";
import { Download, FileText, Loader2, Trash2 } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { artifactsApi } from "@/lib/api";
import { formatArtifactSize } from "@/lib/artifacts";
import type { ArtifactMeta } from "@/lib/types";

interface ArtifactCardProps {
  artifact: ArtifactMeta;
  /** Compact (inline-in-message) vs. full (settings list) presentation. */
  variant?: "inline" | "full";
  /** When true, shows a delete button (settings list). */
  onDelete?: (id: string) => void;
  className?: string;
}

/**
 * Render an artifact reference: name, media type, size, and an authenticated
 * download. The download goes through `artifactsApi.download` (Bearer auth +
 * refresh-on-401); a raw `<a href>` would miss the Authorization header.
 */
export function ArtifactCard({
  artifact,
  variant = "inline",
  onDelete,
  className,
}: ArtifactCardProps) {
  const [downloading, setDownloading] = useState(false);
  const name = artifact.filename || "artifact";

  const handleDownload = async () => {
    setDownloading(true);
    try {
      const blob = await artifactsApi.download(artifact.id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = name;
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* the toast-free inline card fails silently on click */
    } finally {
      setDownloading(false);
    }
  };

  return (
    <div
      className={cn(
        "flex items-center gap-2 rounded-lg border border-border bg-card text-xs",
        variant === "inline" ? "px-2.5 py-1.5" : "px-3 py-2",
        className,
      )}
      data-artifact-id={artifact.id}
    >
      <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md bg-secondary">
        <FileText className="h-3.5 w-3.5" />
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium" title={name}>
          {name}
        </div>
        <div className="flex items-center gap-1.5 text-muted-foreground">
          <span>{formatArtifactSize(artifact.size)}</span>
          {artifact.media_type && (
            <>
              <span aria-hidden>·</span>
              <Badge variant="outline" className="px-1 py-0 text-[10px] font-normal">
                {artifact.media_type}
              </Badge>
            </>
          )}
          {artifact.source && artifact.source !== "upload" && (
            <Badge variant="outline" className="px-1 py-0 text-[10px] font-normal">
              {artifact.source}
            </Badge>
          )}
        </div>
      </div>
      <Button
        variant="ghost"
        size="icon"
        className="h-7 w-7 shrink-0"
        onClick={(e) => {
          e.preventDefault();
          e.stopPropagation();
          void handleDownload();
        }}
        disabled={downloading}
        title="下载"
        aria-label={`下载 ${name}`}
      >
        {downloading ? (
          <Loader2 className="h-3.5 w-3.5 animate-spin" />
        ) : (
          <Download className="h-3.5 w-3.5" />
        )}
      </Button>
      {variant === "full" && onDelete && (
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7 shrink-0"
          onClick={(e) => {
            e.preventDefault();
            e.stopPropagation();
            onDelete(artifact.id);
          }}
          title="删除"
          aria-label={`删除 ${name}`}
        >
          <Trash2 className="h-3.5 w-3.5 text-destructive" />
        </Button>
      )}
    </div>
  );
}
