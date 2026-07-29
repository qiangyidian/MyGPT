"use client";

import { useEffect, useState } from "react";
import { Download, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { formatSize } from "@/components/attachments/attachment-card";
import type { ChatAttachment } from "@/lib/types";

/**
 * Authenticated attachment preview dialog. Images render from a fetched blob;
 * other types show metadata + a download button (bytes are fetched on demand so
 * the stored path is never exposed to the client).
 */
export function AttachmentPreview({
  attachment,
  onOpenChange,
}: {
  attachment: ChatAttachment | null;
  onOpenChange: (open: boolean) => void;
}) {
  const [blobUrl, setBlobUrl] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    setBlobUrl(null);
    if (!attachment) return;
    if (!attachment.mime_type?.startsWith("image/")) return;
    let url: string | null = null;
    let cancelled = false;
    setLoading(true);
    api
      .downloadAttachment(attachment.id)
      .then((blob) => {
        if (cancelled) return;
        url = URL.createObjectURL(blob);
        setBlobUrl(url);
      })
      .catch(() => undefined)
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
      if (url) URL.revokeObjectURL(url);
    };
  }, [attachment]);

  const isImage = attachment?.mime_type?.startsWith("image/");

  return (
    <Dialog open={!!attachment} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle className="truncate pr-8">{attachment?.original_filename}</DialogTitle>
          <DialogDescription>
            {attachment ? `${attachment.mime_type || "文件"} · ${formatSize(attachment.size_bytes)}` : ""}
          </DialogDescription>
        </DialogHeader>
        <div className="max-h-[60vh] overflow-auto rounded-md border border-border bg-muted/30 p-3">
          {isImage ? (
            loading ? (
              <div className="flex h-40 items-center justify-center text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" />
              </div>
            ) : blobUrl ? (
              // eslint-disable-next-line @next/next/no-img-element
              <img src={blobUrl} alt={attachment?.original_filename ?? ""} className="mx-auto max-h-[55vh] rounded" />
            ) : (
              <p className="text-sm text-muted-foreground">无法预览此图片。</p>
            )
          ) : attachment?.preview_metadata ? (
            <pre className="whitespace-pre-wrap break-words text-xs text-muted-foreground">
              {JSON.stringify(attachment.preview_metadata, null, 2)}
            </pre>
          ) : (
            <p className="text-sm text-muted-foreground">
              该类型文件不支持在线预览，可下载后查看。
            </p>
          )}
        </div>
        {attachment && (
          <div className="flex justify-end">
            <Button
              type="button"
              variant="outline"
              size="sm"
              className="gap-1.5"
              onClick={async () => {
                try {
                  const blob = await api.downloadAttachment(attachment.id);
                  const url = URL.createObjectURL(blob);
                  const a = document.createElement("a");
                  a.href = url;
                  a.download = attachment.original_filename;
                  a.click();
                  setTimeout(() => URL.revokeObjectURL(url), 1000);
                } catch {
                  /* ignore */
                }
              }}
            >
              <Download className="h-4 w-4" />
              下载
            </Button>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
