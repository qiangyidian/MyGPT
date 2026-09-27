"use client";

import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Send, Square } from "lucide-react";

import { cn } from "@/lib/utils";
import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { ComposerToolbar } from "@/components/chat/composer-toolbar";
import { AttachmentPicker } from "@/components/chat/attachment-picker";
import { VoiceInput } from "@/components/chat/voice-input";
import {
  MentionPopover,
  useMentionPopover,
} from "@/components/chat/mention-popover";
import { MentionChips } from "@/components/chat/mention-chips";
import { PromptLibraryDialog } from "@/components/chat/prompt-library-dialog";
import { AttachmentList } from "@/components/attachments/attachment-list";
import { useChatAttachments } from "@/hooks/useChatAttachments";
import { useModels } from "@/hooks/useModels";
import { useChatUiStore } from "@/stores/chat-ui-store";
import { getModeMeta } from "@/lib/user-modes";
import { deleteRefAt, removeRef } from "@/lib/inline-refs";
import {
  COMPOSER_MAX_CHARS,
  COMPOSER_NEAR_LIMIT,
  FALLBACK_LIMITS,
  collectMentions,
  droppedNotice,
  insertMention,
  type MentionLimits,
} from "@/lib/mention-insert";
import { promptCreateBody } from "@/lib/prompt-apply";
import {
  applyInsertionToTextarea,
  insertTemplate,
  promptFormFromDraft,
} from "@/lib/prompt-library";
import {
  filterModelsByModality,
  requiredModalitiesFor,
} from "@/lib/multimodal";
import type {
  ChatMention,
  KnowledgeBase,
  UserChatMode,
} from "@/lib/types";
import { toast } from "sonner";

export interface ComposerSendOpts {
  mode: UserChatMode;
  attachmentIds: string[];
  /** 正文里 ``@`` 出来的引用目标（见 lib/inline-refs.ts）。 */
  mentions: ChatMention[];
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
  /**
   * Receives the composer's upload entry point so a page-level drop zone can
   * route dropped/pasted files into the same flow as the paperclip button.
   * Called once on mount with the function, null on unmount.
   */
  onUploadReady?: (upload: ((files: FileList | File[]) => void) | null) => void;
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
  onUploadReady,
}: ComposerProps) {
  const [value, setValue] = useState("");
  /** 服务端随候选下发的上限；没拿到之前用 inline-refs 的同源兜底。 */
  const [limits, setLimits] = useState<MentionLimits>(FALLBACK_LIMITS);
  /** 输入框里的选区：「把选中文字存为模板」要用，别再去读一遍 DOM。 */
  const [selection, setSelection] = useState({ start: 0, end: 0 });
  const [promptLibraryOpen, setPromptLibraryOpen] = useState(false);
  const mode = useChatUiStore((s) => s.mode);
  const modeMeta = getModeMeta(mode);
  const textareaRef = useRef<HTMLTextAreaElement>(null);
  /** 输入法合成中：这段时间的按键与文本变化都不参与 ``@`` 判定。 */
  const composingRef = useRef(false);

  // Re-focus the composer when the user switches/creates a conversation —
  // previously the input stayed blurred and typing went nowhere.
  useEffect(() => {
    textareaRef.current?.focus();
  }, [conversationId]);
  const { drafts, upload, remove, allReady } = useChatAttachments(
    conversationId,
    ensureConversationId
  );
  const { chatModels } = useModels();

  // Publish the upload entry point for the page-level drop zone. The upload fn
  // is unstable (recreated per render by the hook); bridge through a ref so the
  // published callback identity stays stable.
  const uploadFnRef = useRef(upload);
  uploadFnRef.current = upload;
  useEffect(() => {
    onUploadReady?.((files: FileList | File[]) => void uploadFnRef.current(files));
    return () => onUploadReady?.(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps -- mount/unmount only
  }, []);

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
  // A modality mismatch blocks the send: the selected model can't accept the
  // attached parts. When the user picks "默认模型" (null) we can't verify
  // capabilities, so we only block an explicit, incapable selection. (The old
  // `capableModels.length === 0` clause only blocked when NO capable model
  // existed at all — with any vision model in the list, an explicitly selected
  // non-vision model sailed through and the attachment silently never reached
  // the model.)
  const modelCapable =
    !modelId ||
    capableModels.some((m) => m.id === modelId);
  const modalityBlocked =
    requiredModalities.length > 0 && !modelCapable;

  // Both modes accept optional attachments; neither requires a file.

  // ``@`` 类型先行：插入/删除引用都要把光标写回 textarea，而受控组件在
  // render 之后才会更新 DOM，所以把目标光标位存一拍，等 value 落地再对齐。
  const pendingCaret = useRef<number | null>(null);
  const applyText = (next: string, caret: number | null = null) => {
    if (caret !== null) pendingCaret.current = caret;
    setValue(next);
  };

  const mention = useMentionPopover({
    conversationId,
    textareaRef,
    onLimitsChange: setLimits,
    onPick: (target, anchor, caret) => {
      const el = textareaRef.current;
      // 插入 + 上限裁决 + 中文原因全在纯函数里；这里只负责把结果落到受控 value。
      const result = insertMention(
        el?.value ?? value,
        caret,
        target,
        limits,
        anchor,
      );
      if (!result.ok) {
        toast.error(result.reason ?? "无法插入该引用");
        return;
      }
      applyText(result.text, result.caret);
    },
  });

  // 文本/光标一改就重算锚点；但输入法合成期间不能算——候选串会先落进 textarea，
  // 那时读到的「@拼音」并不是用户想引用的东西。合成结束后再补一次判定。
  const syncFromDom = () => {
    if (composingRef.current) return;
    const el = textareaRef.current;
    if (!el) return;
    const start = el.selectionStart ?? el.value.length;
    setSelection({ start, end: el.selectionEnd ?? start });
    mention.sync(el.value, start);
  };

  // 受控 value 落地后才写回光标，否则插入点会停在插入前的位置。
  useEffect(() => {
    if (pendingCaret.current === null) return;
    const caret = pendingCaret.current;
    pendingCaret.current = null;
    const el = textareaRef.current;
    if (!el) return;
    el.focus();
    el.setSelectionRange(caret, caret);
    syncFromDom();
    // eslint-disable-next-line react-hooks/exhaustive-deps -- 只在写回光标这一拍跑
  }, [value]);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // 弹层开着时先让它吃掉方向键/回车/Esc，否则回车会带着半截 ``@查询`` 发出去。
    if (mention.handleKeyDown(e)) return;
    const el = textareaRef.current;
    // 引用是一枚原子记号：整段删掉，而不是留下半截 ``@[label``。
    const caret = el?.selectionStart ?? null;
    if (
      (e.key === "Backspace" || e.key === "Delete") &&
      el &&
      caret !== null &&
      caret === el.selectionEnd &&
      !e.nativeEvent.isComposing
    ) {
      const cut = deleteRefAt(
        el.value,
        caret,
        e.key === "Backspace" ? "backward" : "forward",
      );
      if (cut) {
        e.preventDefault();
        applyText(cut.value, cut.caret);
        mention.sync(cut.value, cut.caret);
        return;
      }
    }
    // Enter to send, Shift+Enter for newline. Ignore during IME composition.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming || !allReady) return;
    // 引用的唯一事实就是正文里的 token：发送时现解，绝不维护第二份列表。
    const collected = collectMentions(trimmed, limits);
    const notice = droppedNotice(collected.dropped);
    if (notice) {
      // 粘贴带进来的引用可能已经越过上限：这时候发出去会被服务端 422，
      // 静默丢掉又不公平，所以停下来把话说清楚。
      toast.error(notice);
      return;
    }
    mention.close();
    onSend(trimmed, {
      mode,
      attachmentIds: drafts.map((d) => d.id),
      mentions: collected.mentions,
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

  /** 语音识别结果只**插进输入框**，绝不自动发送：转写错了用户必须先看到、先能改。
   *  与已有内容之间补一个空格（打字打到一半录音不该把两段黏成一个词）。 */
  const insertRecognizedText = (text: string) => {
    if (!text) return;
    setValue((prev) => (prev.trim() ? `${prev.replace(/\s+$/, "")} ${text}` : text));
    // 自适应高度读的是 DOM，等内容落到 textarea 之后再量一次。
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(handleInput);
    }
    textareaRef.current?.focus();
  };

  /** 提示词库交回的文本插到光标处（有选区就替换选区），并把光标落到第一个占位符。 */
  const insertPromptText = (text: string) => {
    if (!text) return;
    const el = textareaRef.current;
    const current = el?.value ?? value;
    const range = el
      ? { start: el.selectionStart ?? current.length, end: el.selectionEnd ?? current.length }
      : undefined;
    const result = insertTemplate(current, text, range);
    setValue(result.value);
    applyInsertionToTextarea(el, result);
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(handleInput);
    }
  };

  const selectedText = value.slice(
    Math.min(selection.start, selection.end),
    Math.max(selection.start, selection.end),
  );
  /** 只有一行的选中文字当模板没什么价值：要求是多行，用户显然是挑了一整段。 */
  const canSaveSelection = selectedText.includes("\n") && !!selectedText.trim();

  const saveSelection = useMutation({
    mutationFn: async (draft: string) => {
      const form = promptFormFromDraft(draft);
      if (!form.title.trim()) {
        throw new Error("选中的文字里没有可当标题的首行，请先写一行标题。");
      }
      return api.createPrompt(promptCreateBody(form));
    },
    onSuccess: () => toast.success("已存为我的模板，可在「提示词库」里改标题与分类"),
    onError: (err: unknown) =>
      toast.error("存为模板失败", {
        description:
          err instanceof Error && err.message ? err.message : userErrorMessage(err),
      }),
  });

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
          onOpenPromptLibrary={() => setPromptLibraryOpen(true)}
          onSaveSelectionAsTemplate={() => saveSelection.mutate(selectedText)}
          canSaveSelectionAsTemplate={canSaveSelection && !saveSelection.isPending}
          savingSelectionAsTemplate={saveSelection.isPending}
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

        {/* 正文里的 @ 引用一览：删这里等于删正文中那枚 token。 */}
        <MentionChips
          text={value}
          className="mb-2"
          onRemove={(ref) => {
            const next = removeRef(value, ref);
            const el = textareaRef.current;
            const caret = Math.min(el?.selectionStart ?? next.length, next.length);
            applyText(next, caret);
          }}
        />

        <div className="relative flex items-center gap-2 rounded-xl border border-input bg-background p-2 shadow-sm focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2">
          <AttachmentPicker onPick={(f) => void upload(f)} />
          {/* 麦克风：能力探测说可用才亮着（关闭时是带中文说明的禁用态）。 */}
          <VoiceInput
            onTranscript={insertRecognizedText}
            disabled={isStreaming || !allReady}
          />

            <Textarea
              ref={textareaRef}
              autoFocus
              // Mobile软键盘回车键显示"发送"而非"换行"。
              enterKeyHint="send"
              value={value}
              // @ 一枚 token 就占 ~53 字，上限留够：接近上限时下面给中文提醒。
              maxLength={COMPOSER_MAX_CHARS}
              onChange={(e) => {
                setValue(e.target.value);
                handleInput();
                syncFromDom();
              }}
              // 输入法：合成期间的文本变化是候选串，不参与 @ 判定；结束后补一次。
              onCompositionStart={() => {
                composingRef.current = true;
              }}
              onCompositionEnd={() => {
                composingRef.current = false;
                syncFromDom();
              }}
              // 光标移动/选区变化（点击、方向键、Shift+方向键）都会让「正在输入的
              // 那段 @查询」失效，也是「存为模板」唯一可靠的选区来源。
              onClick={syncFromDom}
              onSelect={syncFromDom}
              onKeyDown={handleKeyDown}
              placeholder={`输入消息…  （${modeMeta.label}）输入 @ 可引用知识库/文档/附件`}
              className="h-10 min-h-10 flex-1 resize-none border-0 bg-transparent px-1 py-2 leading-5 focus-visible:ring-0 focus-visible:ring-offset-0"
              rows={1}
              aria-label="消息输入框"
            />

            {isStreaming ? (
              <Button
                variant="destructive"
                size="icon"
                className="h-9 w-9 max-sm:h-11 max-sm:w-11 shrink-0"
                onClick={onStop}
                title="停止生成"
                aria-label="停止生成"
              >
                <Square className="h-4 w-4" />
              </Button>
            ) : (
              <Button
                size="icon"
                className="h-9 w-9 max-sm:h-11 max-sm:w-11 shrink-0"
                onClick={handleSend}
                disabled={!canSend}
                title="发送"
                aria-label="发送"
              >
                <Send className="h-4 w-4" />
              </Button>
            )}
          <MentionPopover state={mention} />
        </div>

        <p className="mt-1.5 text-center text-[11px] text-muted-foreground">
          {modalityBlocked
            ? "当前模型不支持所附附件的模态（图片需视觉模型，音频需音频输入模型），请更换模型或移除附件。"
            : "AI 生成的内容可能存在错误，请核实重要信息。可拖拽或粘贴添加附件，输入 @ 可引用知识库、文档或本对话附件。"}
        </p>
        {COMPOSER_MAX_CHARS - value.length < COMPOSER_NEAR_LIMIT && (
          <p className="mt-1 text-center text-[11px] text-destructive">
            {`已输入 ${value.length} 字，还可输入 ${Math.max(0, COMPOSER_MAX_CHARS - value.length)} 字（上限 ${COMPOSER_MAX_CHARS} 字）：一枚 @ 引用约占 53 字，超出的输入会被截断。`}
          </p>
        )}
      </div>

      <PromptLibraryDialog
        open={promptLibraryOpen}
        onOpenChange={setPromptLibraryOpen}
        onPick={insertPromptText}
      />
    </div>
  );
}
