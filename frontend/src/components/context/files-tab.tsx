"use client";

import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { FileText, Loader2 } from "lucide-react";

import { ScrollArea } from "@/components/ui/scroll-area";
import { AttachmentCard } from "@/components/attachments/attachment-card";
import { AttachmentPreview } from "@/components/attachments/attachment-preview";
import { api } from "@/lib/api";
import { useContextPanelStore } from "@/stores/context-panel-store";
import type { ChatAttachment } from "@/lib/types";

export function FilesTab({ conversationId }: { conversationId: string | null }) {
  const focusId = useContextPanelStore((s) => s.focusAttachmentId);
  const [previewId, setPreviewId] = useState<string | null>(null);

  const query = useQuery<ChatAttachment[]>({
    queryKey: ["chat-attachments", conversationId ?? ""],
    queryFn: () => (conversationId ? api.listChatAttachments(conversationId) : Promise.resolve([])),
    enabled: !!conversationId,
  });

  const attachments = query.data ?? [];

  // Open preview when an external focus (e.g. clicking a message attachment card) arrives.
  useEffect(() => {
    if (focusId) setPreviewId(focusId);
  }, [focusId]);

  const previewing = attachments.find((a) => a.id === previewId) ?? null;

  if (query.isLoading) {
    return (
      <div className="flex items-center justify-center gap-2 py-12 text-xs text-muted-foreground">
        <Loader2 className="h-4 w-4 animate-spin" /> 加载附件…
      </div>
    );
  }

  if (!attachments.length) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-12 text-center text-xs text-muted-foreground">
        <FileText className="h-5 w-5" />
        <span>暂无附件。上传文件后会出现在这里。</span>
      </div>
    );
  }

  return (
    <ScrollArea className="h-full">
      <div className="space-y-2 p-3">
        {attachments.map((a) => (
          <AttachmentCard
            key={a.id}
            attachment={{
              id: a.id,
              filename: a.original_filename,
              mime_type: a.mime_type,
              size_bytes: a.size_bytes,
              status: a.status,
              parse_status: a.parse_status,
              error: a.error_message,
            }}
            onPreview={(id) => setPreviewId(id)}
          />
        ))}
      </div>
      <AttachmentPreview
        attachment={previewing}
        onOpenChange={(o) => !o && setPreviewId(null)}
      />
    </ScrollArea>
  );
}
