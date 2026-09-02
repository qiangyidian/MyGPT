"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { api, ApiError } from "@/lib/api";
import { attachmentRejectionMessage } from "@/lib/attachment-types";
import { useAttachmentStore, EMPTY_DRAFTS, type AttachmentDraft } from "@/stores/attachment-store";

interface UseChatAttachmentsResult {
  drafts: AttachmentDraft[];
  upload: (files: File[] | FileList) => Promise<void>;
  remove: (id: string) => Promise<void>;
  isUploading: boolean;
  /** True unless some draft is still uploading or has failed. */
  allReady: boolean;
}

/**
 * Manages per-conversation chat attachment drafts in the composer: upload,
 * status polling, remove. On send, the caller passes the draft ids as
 * ``attachment_ids``; binding happens server-side.
 *
 * If no conversation is active, ``ensureConversationId`` is invoked to create
 * one on demand (the first attachment of a brand-new chat).
 */
export function useChatAttachments(
  conversationId: string | null,
  ensureConversationId?: () => Promise<string>
): UseChatAttachmentsResult {
  // Return EMPTY_DRAFTS (a stable singleton) when empty — see that constant's
  // doc comment. A bare `?? []` here triggers an infinite re-render loop under
  // useSyncExternalStore ("Maximum update depth exceeded").
  const drafts = useAttachmentStore((s) => s.drafts[conversationId ?? ""] ?? EMPTY_DRAFTS);
  const addDraft = useAttachmentStore((s) => s.addDraft);
  const updateDraft = useAttachmentStore((s) => s.updateDraft);
  const removeDraft = useAttachmentStore((s) => s.removeDraft);
  const [isUploading, setIsUploading] = useState(false);
  // Temp ids the user removed while an upload was still in flight; the upload's
  // completion must not re-add those (and should delete the orphaned server row).
  const removedRef = useRef<Set<string>>(new Set());

  const upload = useCallback(
    async (files: File[] | FileList) => {
      let list = Array.from(files);
      if (list.length === 0) return;
      // Reject unsupported types up front — a clear toast beats a failed
      // upload after the bytes were already sent. Valid files still proceed.
      const rejected = attachmentRejectionMessage(list);
      if (rejected) {
        toast.error("无法添加附件", { description: rejected });
        list = list.filter((f) => attachmentRejectionMessage([f]) === null);
        if (list.length === 0) return;
      }
      setIsUploading(true);
      try {
        let convId = conversationId;
        if (!convId) {
          if (!ensureConversationId) return;
          convId = await ensureConversationId();
        }
        await Promise.all(
          list.map(async (file) => {
            // Optimistic placeholder so the tray shows progress immediately.
            const tempId = `tmp-${file.name}-${file.size}`;
            addDraft(convId!, {
              id: tempId,
              filename: file.name,
              mime_type: file.type,
              size_bytes: file.size,
              status: "uploading",
              parse_status: "pending",
              uploading: true,
            });
            try {
              const att = await api.uploadChatAttachment(convId!, file);
              // Replace the placeholder with the real row — unless the user
              // removed it while uploading (then delete the orphaned server row).
              removeDraft(convId!, tempId);
              if (removedRef.current.delete(tempId)) {
                try {
                  await api.deleteChatAttachment(att.id);
                } catch {
                  /* orphan cleanup is best-effort */
                }
                return;
              }
              addDraft(convId!, {
                id: att.id,
                filename: att.original_filename,
                mime_type: att.mime_type,
                size_bytes: att.size_bytes,
                status: att.status,
                parse_status: att.parse_status,
                uploading: false,
              });
            } catch (err) {
              const message =
                err instanceof ApiError ? err.message : "上传失败";
              updateDraft(convId!, tempId, {
                status: "failed",
                uploading: false,
                error: message,
              });
            }
          })
        );
      } finally {
        setIsUploading(false);
      }
    },
    [conversationId, ensureConversationId, addDraft, removeDraft, updateDraft]
  );

  const remove = useCallback(
    async (id: string) => {
      const convId = conversationId;
      if (!convId) return;
      // Mark as removed so a concurrent upload's completion doesn't re-add it.
      removedRef.current.add(id);
      removeDraft(convId, id);
      // Best-effort server delete (temp ids aren't real rows).
      if (!id.startsWith("tmp-")) {
        try {
          await api.deleteChatAttachment(id);
        } catch {
          /* row already removed locally; ignore */
        }
      }
    },
    [conversationId, removeDraft]
  );

  const allReady =
    drafts.length === 0 ||
    drafts.every((d) => !d.uploading && d.status !== "failed" && d.status !== "uploading");

  // Status polling (B: comment now true): after upload the server parses the
  // file in the background (parsing → ready/failed). Poll drafts that are
  // still mid-parse every 2.5s so the tray shows the terminal state.
  // The pending id list lives in a ref and the interval only depends on
  // convId — a stable effect. (It previously depended on `drafts`, so every
  // upload-progress tick or poll write tore down and re-created the interval,
  // drifting the 2.5s period.)
  const convId = conversationId ?? "";
  const pendingIdsRef = useRef<string[]>([]);
  pendingIdsRef.current = drafts
    .filter((d) => !d.id.startsWith("tmp-") && (d.parse_status === "parsing" || d.status === "parsing"))
    .map((d) => d.id);
  useEffect(() => {
    const t = setInterval(() => {
      const pendingIds = pendingIdsRef.current;
      if (pendingIds.length === 0) return;
      void (async () => {
        try {
          const all = await api.listChatAttachments(convId);
          const byId = new Map(all.map((a) => [a.id, a]));
          for (const id of pendingIds) {
            const row = byId.get(id);
            if (row && row.parse_status !== "parsing" && row.status !== "parsing") {
              updateDraft(convId, id, {
                status: row.status,
                parse_status: row.parse_status,
                error: row.error_message,
              });
            }
          }
        } catch {
          /* polling is best-effort */
        }
      })();
    }, 2500);
    return () => clearInterval(t);
  }, [convId, updateDraft]);

  return { drafts, upload, remove, isUploading, allReady };
}
