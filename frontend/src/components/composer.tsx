"use client";

import { type KeyboardEvent, useMemo, useRef, useState } from "react";
import { Send, Square } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ComposerToolbar } from "@/components/chat/composer-toolbar";
import { AttachmentPicker } from "@/components/chat/attachment-picker";
import { AttachmentDropzone } from "@/components/attachments/attachment-dropzone";
import { AttachmentList } from "@/components/attachments/attachment-list";
import { useChatAttachments } from "@/hooks/useChatAttachments";
import { useModels } from "@/hooks/useModels";
import { useChatUiStore } from "@/stores/chat-ui-store";
import { getModeMeta } from "@/lib/user-modes";
import {
  filterModelsByModality,
  requiredModalitiesFor,
} from "@/lib/multimodal";
import type { KnowledgeBase, UserChatMode } from "@/lib/types";

export interface ComposerSendOpts {
  mode: UserChatMode;
  attachmentIds: string[];
}

interface ComposerProps {
  onSend: (content: string, opts: ComposerSendOpts) => void;
  onStop: () => void;
  isStreaming: boolean;
  modelId: string | null;
  onModelChange: (modelId: string | null) => void;
  knowledgeBaseIds: string[];
  onKnowledgeBaseIdsChange: (ids: string[]) => void;
  knowledgeBases?: KnowledgeBase[];
  /** Active conversation; attachments bind to it. */
  conversationId: string | null;
  /** Create a conversation on demand when attaching to a brand-new chat. */
  ensureConversationId?: () => Promise<string>;
  className?: string;
}

export function Composer({
  onSend,
  onStop,
  isStreaming,
  modelId,
  onModelChange,
  knowledgeBaseIds,
  onKnowledgeBaseIdsChange,
  knowledgeBases,
  conversationId,
  ensureConversationId,
  className,
}: ComposerProps) {
  const [value, setValue] = useState("");
  const mode = useChatUiStore((s) => s.mode);
  const modeMeta = getModeMeta(mode);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const { drafts, upload, remove, allReady } = useChatAttachments(
    conversationId,
    ensureConversationId
  );
  const { chatModels } = useModels();

  // Derive the modalities required by the current attachments (image/audio/
  // file). This drives modality-aware model filtering in the dropdown below.
  const attachmentMimes = useMemo(
    () => drafts.map((d) => d.mime_type).filter(Boolean),
    [drafts],
  );
  const requiredModalities = useMemo(
    () => requiredModalitiesFor(attachmentMimes),
    [attachmentMimes],
  );
  const capableModels = useMemo(
    () => filterModelsByModality(chatModels, attachmentMimes),
    [chatModels, attachmentMimes],
  );
  // A modality mismatch blocks the send: the selected (or default) model can't
  // accept the attached parts. When the user picks "默认模型" (null) we can't
  // verify capabilities, so we only block an explicit, incapable selection.
  const modelCapable =
    !modelId ||
    capableModels.some((m) => m.id === modelId);
  const modalityBlocked =
    requiredModalities.length > 0 && !modelCapable && capableModels.length === 0;

  // Both modes accept optional attachments; neither requires a file.
  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter to send, Shift+Enter for newline. Ignore during IME composition.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || !allReady) return;
    onSend(trimmed, {
      mode,
      attachmentIds: drafts.map((d) => d.id),
    });
    setValue("");
    if (textareaRef.current) textareaRef.current.style.height = "";
  };

  // Auto-grow textarea up to 200px.
  const handleInput = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  const canSend =
    !!value.trim() && !isStreaming && allReady && !modalityBlocked;

  return (
    <div className={cn("bg-background", className)}>
      <div className="mx-auto w-full max-w-3xl px-4 pb-3 pt-2">
        <ComposerToolbar
          modelId={modelId}
          onModelChange={onModelChange}
          knowledgeBaseIds={knowledgeBaseIds}
          onKnowledgeBaseIdsChange={onKnowledgeBaseIdsChange}
          knowledgeBases={knowledgeBases}
          attachmentMimes={attachmentMimes}
          className="mb-2"
        />

        {/* Attachment tray (composer drafts). */}
        {drafts.length > 0 && (
          <AttachmentList
            attachments={drafts}
            onRemove={(id) => void remove(id)}
            className="mb-2 grid grid-cols-1 gap-1.5 sm:grid-cols-2"
          />
        )}

        <AttachmentDropzone onPick={(f) => void upload(f)} className="rounded-xl">
          <div className="flex items-center gap-2 rounded-xl border border-input bg-background p-2 shadow-sm focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2">
            <AttachmentPicker onPick={(f) => void upload(f)} />

            <Textarea
              ref={textareaRef}
              value={value}
              onChange={(e) => {
                setValue(e.target.value);
                handleInput();
              }}
              onKeyDown={handleKeyDown}
              placeholder={`输入消息…  （${modeMeta.label}）`}
              className="h-10 min-h-10 flex-1 resize-none border-0 bg-transparent px-1 py-2 leading-5 focus-visible:ring-0 focus-visible:ring-offset-0"
              rows={1}
              aria-label="消息输入框"
            />

            {isStreaming ? (
              <Button
                variant="destructive"
                size="icon"
                className="h-8 w-8 shrink-0"
                onClick={onStop}
                title="停止生成"
                aria-label="停止生成"
              >
                <Square className="h-4 w-4" />
              </Button>
            ) : (
              <Button
                size="icon"
                className="h-8 w-8 shrink-0"
                onClick={handleSend}
                disabled={!canSend}
                title="发送"
                aria-label="发送"
              >
                <Send className="h-4 w-4" />
              </Button>
            )}
          </div>
        </AttachmentDropzone>

        <p className="mt-1.5 text-center text-[11px] text-muted-foreground">
          {modalityBlocked
            ? "当前模型不支持所附附件的模态（图片需视觉模型，音频需音频输入模型），请更换模型或移除附件。"
            : "AI 生成的内容可能存在错误，请核实重要信息。可拖拽或粘贴添加附件。"}
        </p>
      </div>
    </div>
  );
}
