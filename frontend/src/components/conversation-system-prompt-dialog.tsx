"use client";

import { useEffect, useState } from "react";
import { Loader2, RotateCcw, Save } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { userErrorMessage } from "@/lib/api-error";
import type { Conversation } from "@/lib/types";
import {
  SYSTEM_PROMPT_MAX_CHARS,
  canRestoreDefaultPrompt,
  countPromptChars,
  systemPromptError,
  systemPromptPatch,
} from "@/lib/system-prompt";

interface ConversationSystemPromptDialogProps {
  open: boolean;
  conversation: Conversation | null;
  onOpenChange: (open: boolean) => void;
  /** PATCHes the conversation; rejects with an ApiError whose message is Chinese. */
  onSave: (conversationId: string, systemPrompt: string | null) => Promise<void>;
}

/**
 * 会话级系统提示词编辑（条目 31）。
 *
 * 放在侧边栏行菜单里，和「重命名 / 置顶 / 归档」同一层：这三件事都是会话属性，
 * 不是某条消息的属性。持久化走已有的 `PATCH /api/conversations/{id}`，成功后由
 * useConversations 同时写回列表缓存和 `["conversation", id]` 详情缓存。
 *
 * 留空 = 把列写回 NULL = 用平台默认提示词（后端 `_build_system_prompt` 正是
 * 这个语义），所以「恢复默认」就是清空后保存，而不是删掉一个字段。
 */
export function ConversationSystemPromptDialog({
  open,
  conversation,
  onOpenChange,
  onSave,
}: ConversationSystemPromptDialogProps) {
  const [draft, setDraft] = useState("");
  const [pending, setPending] = useState(false);

  // 草稿是会话当前值的一份副本：换会话、或重新打开时必须重置，
  // 否则上一次没保存的文本会写进另一个对话。
  useEffect(() => {
    if (open) setDraft(conversation?.system_prompt ?? "");
  }, [open, conversation]);

  if (!conversation) return null;

  const count = countPromptChars(draft);
  const error = systemPromptError(draft);
  const dirty = systemPromptPatch(conversation.system_prompt, draft) !== null;

  const submit = async () => {
    const patch = systemPromptPatch(conversation.system_prompt, draft);
    if (!patch || error || pending) return;
    setPending(true);
    try {
      await onSave(conversation.id, patch.system_prompt);
      onOpenChange(false);
      toast.success("系统提示词已更新", { description: "从下一轮对话开始生效。" });
    } catch (err) {
      toast.error("保存失败", { description: userErrorMessage(err) });
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>系统提示词</DialogTitle>
          <DialogDescription>
            为「{conversation.title || "新对话"}」设定助手的行为。
            只对之后的对话轮次生效 —— 已经发出的消息与回答不会被改写。
          </DialogDescription>
        </DialogHeader>

        <div className="space-y-1.5">
          <Textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            // 服务端没有长度上限（system_prompt 是 Text 列），这里的上限是前端
            // 自己的预算，maxLength 让超额在输入时就发生而不是保存时才报错。
            maxLength={SYSTEM_PROMPT_MAX_CHARS}
            rows={8}
            placeholder="例如：用简体中文回答，面向财务同事，涉及金额时给出计算过程。"
            aria-label="系统提示词"
            className="text-sm"
          />
          <div className="flex items-start justify-between gap-3">
            <p className="text-xs text-muted-foreground">
              留空即使用平台默认提示词。
            </p>
            <p
              className={
                error
                  ? "shrink-0 text-xs tabular-nums text-destructive"
                  : "shrink-0 text-xs tabular-nums text-muted-foreground"
              }
              aria-live="polite"
            >
              {count} / {SYSTEM_PROMPT_MAX_CHARS} 字
            </p>
          </div>
          {error && <p className="text-xs text-destructive">{error}</p>}
        </div>

        <DialogFooter className="items-center gap-2 sm:justify-between">
          <Button
            variant="ghost"
            size="sm"
            className="gap-2"
            disabled={!canRestoreDefaultPrompt(conversation.system_prompt, draft) || pending}
            onClick={() => setDraft("")}
          >
            <RotateCcw className="h-4 w-4" />
            恢复默认
          </Button>
          <span className="flex items-center gap-2">
            <Button variant="ghost" size="sm" onClick={() => onOpenChange(false)} disabled={pending}>
              取消
            </Button>
            <Button size="sm" onClick={() => void submit()} disabled={!dirty || !!error || pending}>
              {pending && <Loader2 className="mr-1 h-4 w-4 animate-spin" />}
              <Save className="mr-1 h-4 w-4" />
              保存
            </Button>
          </span>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
