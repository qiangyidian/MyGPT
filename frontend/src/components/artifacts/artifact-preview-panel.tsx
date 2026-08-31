"use client";

import { useEffect, useMemo, useState } from "react";
import { Download, FileQuestion, Loader2 } from "lucide-react";

import { relativeTime } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { artifactsApi } from "@/lib/api";
import { formatArtifactSize } from "@/lib/artifacts";
import {
  closeArtifactPreview,
  useArtifactPreviewId,
} from "@/lib/artifact-preview-store";
import type { ArtifactMeta } from "@/lib/types";

/**
 * Right-side drawer previewing an artifact. Bytes are fetched as an
 * authenticated Blob and shown via an object URL (never a bare URL — artifact
 * downloads require a Bearer header a plain <iframe src> can't provide).
 *
 * Preview support follows what a browser can natively render: PDF via
 * <iframe>, images via <img>, text-ish via <pre>. Office formats (pptx/docx/
 * xlsx) have no native renderer, so they fall back to a file card + download.
 */

const TEXT_PREVIEW_EXT = new Set([
  "txt", "md", "markdown", "csv", "json", "log", "py", "js", "ts", "tsx",
  "jsx", "html", "css", "sh", "yml", "yaml", "toml", "xml", "sql", "svg",
]);

function isTextPreview(meta: ArtifactMeta): boolean {
  if (meta.media_type.startsWith("text/")) return true;
  if (meta.media_type === "application/json" || meta.media_type === "image/svg+xml")
    return true;
  const ext = (meta.filename ?? "").split(".").pop()?.toLowerCase() ?? "";
  return TEXT_PREVIEW_EXT.has(ext);
}

function previewKind(meta: ArtifactMeta): "pdf" | "image" | "text" | "none" {
  if (meta.media_type === "application/pdf") return "pdf";
  if (meta.media_type.startsWith("image/")) return "image";
  if (isTextPreview(meta)) return "text";
  return "none";
}

export function ArtifactPreviewPanel() {
  const artifactId = useArtifactPreviewId();
  const open = artifactId !== null;

  const [meta, setMeta] = useState<ArtifactMeta | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [textContent, setTextContent] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // Reset cached content whenever the target artifact changes.
  useEffect(() => {
    setMeta(null);
    setObjectUrl(null);
    setTextContent(null);
    setError(null);
    if (!artifactId) return;

    let cancelled = false;
    let createdUrl: string | null = null;
    setLoading(true);
    (async () => {
      try {
        const m = await artifactsApi.getMeta(artifactId);
        if (cancelled) return;
        setMeta(m);
        const blob = await artifactsApi.download(artifactId);
        if (cancelled) return;
        if (previewKind(m) === "text") {
          setTextContent(await blob.text());
        } else {
          createdUrl = URL.createObjectURL(blob);
          setObjectUrl(createdUrl);
        }
      } catch {
        if (!cancelled) setError("加载预览失败，请尝试下载");
      } finally {
        if (!cancelled) setLoading(false);
      }
    })();
    return () => {
      cancelled = true;
      if (createdUrl) URL.revokeObjectURL(createdUrl);
    };
  }, [artifactId]);

  const kind = useMemo(() => (meta ? previewKind(meta) : "none"), [meta]);

  const handleDownload = async () => {
    if (!artifactId || !meta) return;
    try {
      const blob = await artifactsApi.download(artifactId);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url;
      a.download = meta.filename || "artifact";
      a.click();
      URL.revokeObjectURL(url);
    } catch {
      /* ignore */
    }
  };

  return (
    <Sheet open={open} onOpenChange={(o) => !o && closeArtifactPreview()}>
      <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-xl">
        <SheetHeader className="border-b border-border px-4 py-3">
          <SheetTitle className="flex min-w-0 items-center justify-between gap-2 pr-8 text-sm font-medium">
            <span className="min-w-0 truncate" title={meta?.filename ?? undefined}>
              {meta?.filename ?? "文件预览"}
            </span>
          </SheetTitle>
        </SheetHeader>

        <div className="flex items-center justify-between gap-2 border-b border-border px-4 py-2 text-xs text-muted-foreground">
          <span className="min-w-0 truncate">
            {meta
              ? `${formatArtifactSize(meta.size)} · ${meta.media_type}${
                  meta.created_at ? ` · ${relativeTime(meta.created_at)}` : ""
                }`
              : "加载中…"}
          </span>
          <Button
            variant="ghost"
            size="sm"
            className="h-7 gap-1.5 px-2"
            onClick={() => void handleDownload()}
            disabled={!meta}
          >
            <Download className="h-3.5 w-3.5" /> 下载
          </Button>
        </div>

        <div className="min-h-0 flex-1 overflow-auto bg-muted/30">
          {loading && (
            <div className="flex h-full items-center justify-center text-muted-foreground">
              <Loader2 className="h-6 w-6 animate-spin" />
            </div>
          )}
          {!loading && error && (
            <div className="flex h-full flex-col items-center justify-center gap-2 p-6 text-center text-sm text-muted-foreground">
              <FileQuestion className="h-8 w-8" />
              {error}
            </div>
          )}
          {!loading && !error && meta && kind === "pdf" && objectUrl && (
            <iframe
              src={objectUrl}
              title={meta.filename ?? "PDF 预览"}
              className="h-full w-full border-0 bg-white"
            />
          )}
          {!loading && !error && meta && kind === "image" && objectUrl && (
            <div className="flex h-full items-center justify-center p-4">
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={objectUrl}
                alt={meta.filename ?? "图片预览"}
                className="max-h-full max-w-full rounded-md object-contain shadow-sm"
              />
            </div>
          )}
          {!loading && !error && meta && kind === "text" && (
            <pre className="h-full overflow-auto whitespace-pre-wrap break-words p-4 text-xs leading-relaxed">
              {textContent}
            </pre>
          )}
          {!loading && !error && meta && kind === "none" && (
            <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
              <FileQuestion className="h-10 w-10 text-muted-foreground" />
              <p className="text-sm text-muted-foreground">
                该格式暂不支持在线预览，可下载后查看
              </p>
              <Button size="sm" className="gap-1.5" onClick={() => void handleDownload()}>
                <Download className="h-4 w-4" /> 下载 {meta.filename}
              </Button>
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
}
