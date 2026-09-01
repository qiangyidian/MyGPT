"use client";

/**
 * Online preview of a knowledge-base document.
 *
 * Fetches GET /api/documents/{id}/preview (the SAME parsed text the
 * ingestion pipeline chunked + embedded) and renders it in a dialog:
 * Markdown files render as Markdown, everything else (txt/pdf/docx/…)
 * shows as preformatted text. Load failures surface inline, not as toasts,
 * because the dialog IS the user's focus at that point.
 *
 * Long documents page in via ?offset= ("加载剩余内容" button) instead of a
 * silent server-side cut. The body uses native overflow scrolling — Radix
 * ScrollArea adds custom scrollbar overhead without benefit on long,
 * markup-heavy content.
 */
import { useInfiniteQuery } from "@tanstack/react-query";
import { Download, FileText, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import { formatBytes } from "@/lib/utils";
import { Markdown } from "@/components/markdown";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";

const PAGE_CHARS = 200_000; // must match backend _PREVIEW_PAGE_CHARS

export function DocumentPreviewDialog({
  documentId,
  onClose,
}: {
  documentId: string | null;
  onClose: () => void;
}) {
  const { data, isLoading, error, fetchNextPage, hasNextPage, isFetchingNextPage } =
    useInfiniteQuery({
      queryKey: ["doc-preview", documentId],
      queryFn: ({ pageParam }) =>
        api.previewDocument(documentId as string, pageParam),
      enabled: !!documentId,
      staleTime: 60_000, // parsed text is immutable per document version
      initialPageParam: 0,
      getNextPageParam: (last, all) =>
        last.truncated ? all.reduce((n, p) => n + p.chars, 0) : undefined,
    });

  // Concatenate fetched pages into one continuous document body.
  const content = data?.pages.map((p) => p.content).join("") ?? "";
  const first = data?.pages[0];

  async function handleDownload() {
    if (!first) return;
    const blob = await api.downloadDocument(first.document_id);
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = first.filename;
    a.click();
    URL.revokeObjectURL(url);
  }

  return (
    <Dialog open={!!documentId} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="flex h-[85vh] w-full max-w-3xl flex-col gap-0 overflow-hidden p-0 sm:max-w-3xl">
        <DialogHeader className="flex-none border-b border-border px-6 py-4">
          <DialogTitle className="flex items-center gap-2 pr-6 text-base">
            <FileText className="h-4 w-4 shrink-0 text-muted-foreground" />
            <span className="truncate">{first?.filename ?? "文档预览"}</span>
          </DialogTitle>
          <DialogDescription className="flex items-center gap-2 text-xs">
            {first ? (
              <>
                <span>{first.file_type.replace(".", "").toUpperCase()}</span>
                <span aria-hidden>·</span>
                <span>{formatBytes(first.file_size)}</span>
                <span aria-hidden>·</span>
                <span>
                  {content.length.toLocaleString()} /{" "}
                  {first.total_chars.toLocaleString()} 字符
                </span>
                {first.status === "indexed" && (
                  <>
                    <span aria-hidden>·</span>
                    <span>与向量化内容一致</span>
                  </>
                )}
              </>
            ) : (
              "加载中…"
            )}
          </DialogDescription>
        </DialogHeader>

        <div className="min-h-0 flex-1 overflow-y-auto">
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

          {first && (
            <>
              {first.render_as === "markdown" ? (
                <div className="px-6 py-4">
                  <Markdown content={content} />
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words px-6 py-4 font-mono text-[13px] leading-relaxed">
                  {content}
                </pre>
              )}

              {hasNextPage && (
                <div className="flex justify-center border-t border-border p-4">
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => fetchNextPage()}
                    disabled={isFetchingNextPage}
                  >
                    {isFetchingNextPage && (
                      <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                    )}
                    加载剩余内容（已显示 {content.length.toLocaleString()} /{" "}
                    {first.total_chars.toLocaleString()} 字符）
                  </Button>
                </div>
              )}
            </>
          )}
        </div>

        {first && (
          <div className="flex-none border-t border-border px-6 py-3">
            <Button variant="outline" size="sm" onClick={handleDownload}>
              <Download className="mr-2 h-4 w-4" />
              下载原文件
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
