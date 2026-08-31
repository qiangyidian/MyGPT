"use client";

/**
 * Online preview of a knowledge-base document.
 *
 * Fetches GET /api/documents/{id}/preview (the SAME parsed text the
 * ingestion pipeline chunked + embedded) and renders it in a dialog:
 * Markdown files render as Markdown, everything else (txt/pdf/docx/…)
 * shows as preformatted text. Load failures surface inline, not as toasts,
 * because the dialog IS the user's focus at that point.
 */
import { useQuery } from "@tanstack/react-query";
import { FileText } from "lucide-react";

import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { Markdown } from "@/components/markdown";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Skeleton } from "@/components/ui/skeleton";

export function DocumentPreviewDialog({
  documentId,
  onClose,
}: {
  documentId: string | null;
  onClose: () => void;
}) {
  const { data, isLoading, error } = useQuery({
    queryKey: ["doc-preview", documentId],
    queryFn: () => api.previewDocument(documentId as string),
    enabled: !!documentId,
    staleTime: 60_000, // parsed text is immutable per document version
  });

  return (
    <Dialog open={!!documentId} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex max-h-[85vh] w-full max-w-3xl flex-col gap-0 p-0 sm:max-w-3xl">
        <DialogHeader className="flex-none border-b border-border px-6 py-4">
          <DialogTitle className="flex items-center gap-2 pr-6 text-base">
            <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{data?.filename ?? "文档预览"}</span>
          </DialogTitle>
          <DialogDescription className="text-xs">
            {data
              ? `${data.file_type.replace(".", "").toUpperCase()} · ${formatBytes(data.chars)} 解析文本 · 与向量化内容一致`
              : "加载中…"}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1">
          {isLoading && (
            <div className="space-y-3 p-6">
              <Skeleton className="h-4 w-1/3" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-full" />
              <Skeleton className="h-4 w-2/3" />
              <Skeleton className="h-4 w-full" />
            </div>
          )}

          {error && (
            <div className="p-6 text-sm text-destructive">
              预览加载失败：{error instanceof Error ? error.message : "未知错误"}
            </div>
          )}

          {data && (
            <ScrollArea className="h-full max-h-[65vh]">
              {data.render_as === "markdown" ? (
                <div className="px-6 py-4">
                  <Markdown content={data.content} />
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words px-6 py-4 font-mono text-[13px] leading-relaxed">
                  {data.content}
                </pre>
              )}
            </ScrollArea>
          )}
        </div>
      </DialogContent>
    </Dialog>
  );
}
