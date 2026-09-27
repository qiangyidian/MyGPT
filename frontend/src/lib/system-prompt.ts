// 会话级 system_prompt 的编辑逻辑（条目 31）。纯函数，理由同 conversation-list：
// vitest 没有 jsdom，字数与「改了什么才发请求」这两处最容易出错的地方要能测。
import type { Conversation } from "@/lib/types";

/**
 * 输入框的字数上限。
 *
 * 注意：后端目前**没有**任何长度限制 —— `Conversation.system_prompt` 是
 * Text 列，`ConversationCreate/Update` 也只声明了 `str | None`。这个 4000 是
 * 前端的自我约束（防止把一整本手册粘进每一轮请求的 system 段），不是服务端口
 * 径的镜像；要在服务端落地，应给 schema 加 `max_length` 并让这里读同一个数。
 */
export const SYSTEM_PROMPT_MAX_CHARS = 4000;

/** 按码点计数：`String.length` 会把 emoji 算成 2，中文用户看到的字数会更准。 */
export function countPromptChars(value: string): number {
  return [...value].length;
}

export function systemPromptError(value: string): string | null {
  const n = countPromptChars(value);
  if (n > SYSTEM_PROMPT_MAX_CHARS) {
    return `系统提示词不能超过 ${SYSTEM_PROMPT_MAX_CHARS} 字（当前 ${n} 字）`;
  }
  return null;
}

/**
 * 该发什么 PATCH。返回 `null` 表示没有改动，不用发。
 * 空输入 = `null` = 取消本会话的覆盖，回到平台默认（后端把 NULL 当作默认）。
 */
export function systemPromptPatch(
  current: Conversation["system_prompt"],
  next: string
): { system_prompt: string | null } | null {
  const trimmed = next.trim();
  const previous = (current ?? "").trim();
  if (trimmed === previous) return null;
  return { system_prompt: trimmed || null };
}

/** 「恢复默认」按钮的可用条件：当前确有一个非空的自定义提示词。 */
export function canRestoreDefaultPrompt(
  current: Conversation["system_prompt"],
  draft: string
): boolean {
  return countPromptChars(draft) > 0 || !!current?.trim();
}
