"use client";

import { useEffect, useState } from "react";
import { Download, FileText, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api } from "@/lib/api";
import { Markdown } from "@/components/markdown";
import { formatSize } from "@/components/attachments/attachment-card";
import type { ChatAttachment } from "@/lib/types";

interface AttachmentTextPreview {
  text: string;
  truncated: boolean;
  total_chars: number;
  parse_status: string;
  preview_metadata: Record<string, unknown> | null;
}

/**
 * Authenticated attachment preview dialog.
 *
 * - Images render from a fetched blob.
 * - Documents (PDF/Word/Excel/Markdown/…) fetch the server-parsed text via
 *   ``/text`` and render it — plain text in a <pre>, Markdown rendered (it is
 *   the most common doc type here and stays readable as plain text anyway).
 * - Structured parse metadata (pages / sheets / chars / OCR) shows as chips.
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
  const [textPreview, setTextPreview] = useState<AttachmentTextPreview | null>(null);
  const [textLoading, setTextLoading] = useState(false);

  const isImage = attachment?.mime_type?.startsWith("image/");
  const isAudio = attachment?.mime_type?.startsWith("audio/");
  const isMarkdown = !!attachment?.original_filename?.toLowerCase().match(/\.(md|markdown)$/);

  // Image bytes.
  useEffect(() => {
    setBlobUrl(null);
    if (!attachment || !isImage) return;
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
  }, [attachment, isImage]);

  // Document parsed text.
  useEffect(() => {
    setTextPreview(null);
    if (!attachment || isImage || isAudio) return;
    let cancelled = false;
    setTextLoading(true);
    api
      .getAttachmentText(attachment.id)
      .then((p) => {
        if (!cancelled) setTextPreview(p);
      })
      .catch(() => {
        if (!cancelled) setTextPreview(null);
      })
      .finally(() => {
        if (!cancelled) setTextLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [attachment, isImage, isAudio]);

  const meta = textPreview?.preview_metadata;
  const metaChips: string[] = [];
  if (meta) {
    const m = meta as Record<string, unknown>;
    if (typeof m.pages === "number") metaChips.push(`${m.pages} 页`);
    if (typeof m.sheets === "number") metaChips.push(`${m.sheets} 工作表`);
    if (typeof m.rows === "number") metaChips.push(`${m.rows} 行`);
    if (typeof m.chars === "number") metaChips.push(`${m.chars} 字符`);
    if (m.ocr_used === true) metaChips.push("OCR");
    if (m.rag_indexed === true) metaChips.push("已建索引");
  }

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
          ) : isAudio ? (
            <p className="text-sm text-muted-foreground">
              音频附件不支持在线预览；发送时音频输入模型可直接消费，或下载后收听。
            </p>
          ) : textLoading ? (
            <div className="flex h-40 items-center justify-center text-muted-foreground">
              <Loader2 className="mr-2 h-5 w-5 animate-spin" /> 正在解析文档内容…
            </div>
          ) : textPreview && textPreview.text ? (
            <div className="space-y-2">
              {metaChips.length > 0 && (
                <div className="flex flex-wrap gap-1.5">
                  {metaChips.map((c) => (
                    <span
                      key={c}
                      className="rounded-full bg-secondary px-2 py-0.5 text-[11px] text-secondary-foreground"
                    >
                      {c}
                    </span>
                  ))}
                </div>
              )}
              {isMarkdown ? (
                <div className="text-sm">
                  <Markdown content={textPreview.text} />
                </div>
              ) : (
                <pre className="whitespace-pre-wrap break-words text-xs leading-relaxed">
                  {textPreview.text}
                </pre>
              )}
              {textPreview.truncated && (
                <p className="border-t border-border pt-2 text-[11px] text-muted-foreground">
                  内容较长，仅显示前 {textPreview.text.length.toLocaleString()} 字符（共{" "}
                  {textPreview.total_chars.toLocaleString()}）。完整内容会作为上下文发给模型；如需查看全文请下载。
                </p>
              )}
            </div>
          ) : textPreview && textPreview.parse_status === "parsing" ? (
            <p className="text-sm text-muted-foreground">文档正在解析中，稍后重试预览。</p>
          ) : (
            <div className="flex flex-col items-center gap-2 py-8 text-muted-foreground">
              <FileText className="h-8 w-8" />
              <p className="text-sm">暂无可预览的文本内容，可下载后查看。</p>
            </div>
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
