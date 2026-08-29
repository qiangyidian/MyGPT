"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { FileText, Loader2 } from "lucide-react";
import { toast } from "sonner";

import { ScrollArea } from "@/components/ui/scroll-area";
import { AttachmentCard } from "@/components/attachments/attachment-card";
import { AttachmentPreview } from "@/components/attachments/attachment-preview";
import { api, ApiError } from "@/lib/api";
import { useContextPanelStore } from "@/stores/context-panel-store";
import type { ChatAttachment, KnowledgeBase } from "@/lib/types";
import {
  Dialog,
  DialogContent,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

export function FilesTab({ conversationId }: { conversationId: string | null }) {
  const focusId = useContextPanelStore((s) => s.focusAttachmentId);
  const [previewId, setPreviewId] = useState<string | null>(null);
  // Save-to-KB flow: which attachment, target KB picker.
  const [saveTarget, setSaveTarget] = useState<string | null>(null);
  const [saveKbId, setSaveKbId] = useState<string>("");
  const [savedIds, setSavedIds] = useState<Set<string>>(new Set());

  const query = useQuery<ChatAttachment[]>({
    queryKey: ["chat-attachments", conversationId ?? ""],
    queryFn: () => (conversationId ? api.listChatAttachments(conversationId) : Promise.resolve([])),
    enabled: !!conversationId,
  });

  const attachments = query.data ?? [];

  // KBs the user may save into.
  const kbsQ = useQuery<KnowledgeBase[]>({
    queryKey: ["knowledge-bases"],
    queryFn: () => api.listKnowledgeBases(),
    enabled: !!saveTarget,
  });

  // Open preview when an external focus (e.g. clicking a message attachment card) arrives.
  useEffect(() => {
    if (focusId) setPreviewId(focusId);
  }, [focusId]);

  const previewing = attachments.find((a) => a.id === previewId) ?? null;

  const saveMut = useMutation({
    mutationFn: ({ id, kbId }: { id: string; kbId: string }) =>
      api.saveAttachmentToKb(id, kbId),
    onSuccess: (_data, vars) => {
      toast.success("已存入知识库，开始解析索引");
      setSavedIds((s) => new Set(s).add(vars.id));
      setSaveTarget(null);
    },
    onError: (e) =>
      toast.error(e instanceof ApiError ? e.message : "存入知识库失败"),
  });

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
            onSaveToKb={(id) => {
              setSaveTarget(id);
              setSaveKbId("");
            }}
            saveToKbState={
              saveMut.isPending && saveMut.variables?.id === a.id
                ? "saving"
                : savedIds.has(a.id)
                  ? "saved"
                  : saveMut.isError && saveMut.variables?.id === a.id
                    ? "error"
                    : undefined
            }
          />
        ))}
      </div>
      <AttachmentPreview
        attachment={previewing}
        onOpenChange={(o) => !o && setPreviewId(null)}
      />

      {/* Save-to-KB target picker */}
      <Dialog open={!!saveTarget} onOpenChange={(o) => !o && setSaveTarget(null)}>
        <DialogContent className="max-w-sm">
          <DialogHeader>
            <DialogTitle>存入知识库</DialogTitle>
          </DialogHeader>
          <div className="grid gap-2 py-1">
            {kbsQ.isLoading ? (
              <p className="text-sm text-muted-foreground">加载知识库…</p>
            ) : !kbsQ.data?.length ? (
              <p className="text-sm text-muted-foreground">
                还没有知识库。请先在「知识库」页面创建一个。
              </p>
            ) : (
              <Select value={saveKbId} onValueChange={setSaveKbId}>
                <SelectTrigger>
                  <SelectValue placeholder="选择目标知识库" />
                </SelectTrigger>
                <SelectContent>
                  {kbsQ.data.map((kb) => (
                    <SelectItem key={kb.id} value={kb.id}>
                      {kb.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setSaveTarget(null)}>
              取消
            </Button>
            <Button
              disabled={!saveKbId || saveMut.isPending}
              onClick={() =>
                saveTarget && saveKbId && saveMut.mutate({ id: saveTarget, kbId: saveKbId })
              }
            >
              {saveMut.isPending && <Loader2 className="h-4 w-4 animate-spin" />}
              保存
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </ScrollArea>
  );
}
