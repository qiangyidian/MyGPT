import { getMessageFinishReason, getMessageStatus, type Message } from "@/lib/types";

export interface MessageTerminationNotice {
  label: string;
  action: "continue" | "retry";
}

function budgetLabel(message: Message): string {
  const budget = message.metadata?.budget;
  const reason = budget && typeof budget === "object" && "reason" in budget
    ? budget.reason
    : message.metadata?.error;
  if (typeof reason !== "string") return "Agent 已达到执行额度上限";
  if (reason.includes("max agent steps")) return "Agent 已达到执行步数上限";
  if (reason.includes("max tool calls")) return "Agent 已达到工具调用次数上限";
  if (reason.includes("max replans")) return "Agent 已达到重新规划次数上限";
  if (reason.includes("time budget")) return "Agent 已达到执行时间上限";
  if (reason.includes("token budget")) return "Agent 已达到总用量上限";
  if (reason.includes("cost budget")) return "Agent 已达到费用上限";
  return "Agent 已达到执行额度上限";
}

/** Empty failures must replay the original request; there is nothing to continue. */
export function getMessageTerminationNotice(message: Message): MessageTerminationNotice | null {
  const status = getMessageStatus(message);
  const hasContent = Boolean(message.content.trim());
  const action = hasContent ? "continue" : "retry";
  const retained = hasContent ? "已保留生成内容" : "尚未生成回答";
  switch (status) {
    case "truncated":
      if (getMessageFinishReason(message) === "budget") {
        return { label: `${budgetLabel(message)}，${retained}`, action };
      }
      return {
        label: hasContent
          ? "输出达到长度上限，内容可能不完整"
          : "模型达到输出长度上限，尚未生成回答",
        action,
      };
    case "interrupted":
      return { label: `连接中断，${retained}`, action };
    case "cancelled":
      return { label: `已停止，${retained}`, action };
    case "error":
      return { label: "生成失败", action: "retry" };
    default:
      return null;
  }
}
