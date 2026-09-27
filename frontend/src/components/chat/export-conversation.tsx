"use client";

import { useState } from "react";
import { Download, FileJson, FileText, Loader2 } from "lucide-react";

import { api } from "@/lib/api";
import { filenameFromDisposition, saveBlobToDisk } from "@/lib/download";
import { userErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

type ExportFormat = "markdown" | "json";

/**
 * 整段会话导出（条目 26）。
 *
 * 刻意只做「下载文件」，不做公开分享链接：分享要处理匿名访问、撤销、爬虫和
 * 残留快照，风险远大于「用户备份自己的对话」这个需求本身。
 *
 * 文件名由服务端在 Content-Disposition 里给（中文标题已按 RFC 5987 编码），
 * 这里只解析、不自己拼。错误直接显示在菜单里——项目没有 toast 基础设施。
 */
export function ExportConversation({
  conversationId,
  className,
}: {
  conversationId: string;
  className?: string;
}) {
  const [busy, setBusy] = useState<ExportFormat | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function run(format: ExportFormat) {
    if (busy) return;
    setBusy(format);
    setError(null);
    try {
      const res = await requestExport(conversationId, format);
      saveBlobToDisk(res.blob, res.filename);
    } catch (e) {
      setError(userErrorMessage(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <DropdownMenu onOpenChange={(open) => open && setError(null)}>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className={className}
          disabled={busy !== null}
          aria-label="导出会话"
          title={error ?? "导出会话"}
        >
          {busy ? (
            <Loader2 className="h-4 w-4 shrink-0 animate-spin" />
          ) : (
            <Download className="h-4 w-4 shrink-0" />
          )}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" side="bottom" className="w-[190px]">
        {error && (
          <div className="px-2 py-1.5 text-xs text-destructive">{error}</div>
        )}
        <DropdownMenuItem disabled={busy !== null} onSelect={() => run("markdown")}>
          <FileText className="mr-2 h-4 w-4" />
          导出 Markdown
        </DropdownMenuItem>
        <DropdownMenuItem disabled={busy !== null} onSelect={() => run("json")}>
          <FileJson className="mr-2 h-4 w-4" />
          导出 JSON
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}

// 独立导出便于单测：组件本身只关心「点了→存盘」。
async function requestExport(
  conversationId: string,
  format: ExportFormat
): Promise<{ blob: Blob; filename: string }> {
  const res = await api.exportConversation(conversationId, format);
  const fallback = `conversation-${conversationId.slice(0, 8)}.${
    format === "json" ? "json" : "md"
  }`;
  return { blob: res.blob, filename: filenameFromDisposition(res.contentDisposition) ?? fallback };
}
