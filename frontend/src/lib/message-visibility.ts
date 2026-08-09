import type { Message } from "@/lib/types";

/**
 * The live stream renders its own assistant bubble. While it is visible, any
 * persisted empty assistant record is only a transport placeholder and must
 * not create a second, blank AI bubble in the conversation.
 */
export function getVisibleMessages(messages: Message[], isStreaming: boolean): Message[] {
  if (!isStreaming) return messages;
  return messages.filter((message) => !(message.role === "assistant" && !message.content));
}
