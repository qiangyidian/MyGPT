// 流式对话的纯状态核：`(state, event) -> state`。
//
// 抽出来的理由和 `lib/redeem-batch.ts` 一样：vitest 是 `environment: "node"`
// （`frontend/vitest.config.ts`），hook 里的 fetch / reader / requestAnimationFrame /
// React state 写入全都测不到，而「重试会不会吞掉已收到的半截回复」「终态之后还能不
// 能再入账第二次」恰恰是最容易写错、又只有用户能发现的一组规则。
//
// 分工：这里只放**转移**（一条 SSE 事件如何改变这一轮的状态，以及这一轮结束时该
// 落哪条 assistant 消息）；`hooks/useChatStream.ts` 只负责采集事件、执行副作用
// （React Query 缓存、Zustand 图 store、toast、取消 run），不再自己算任何一个字段。
// 判定规则全部在这一份里，hook 里不留第二条并存的事实来源。
import { userErrorMessage } from "@/lib/api-error";
import { extractWebCitations, mergeCitations } from "@/lib/web-citations";
import { finishReasonToStatus } from "@/lib/types";
import type {
  AgentStep,
  ChatMention,
  Citation,
  FinishReason,
  GenerationStatus,
  Message,
  PendingApproval,
  UserChatMode,
} from "@/lib/types";

/** 流结束的原因：只有 `user_stop` 有权取消后端 run，只有它之外才需要「保留半截」。 */
export type StreamEndReason = "user_stop" | "unmount" | "socket_closed";

/**
 * 这一轮要落的 assistant 消息（还没拼上 `created_at` / `multi_agent` —— 那两个
 * 读取的是 wall clock 与图 store，由 hook 补齐）。`citations` / `steps` 是终态
 * 那一刻的快照引用，之后再来 citations/step 事件也不会追改已入账的消息。
 */
export interface AssistantCommit {
  conversationId: string;
  messageId: string;
  content: string;
  finishReason: FinishReason;
  citations: Citation[];
  steps: AgentStep[];
  runId: string;
}

export interface StreamState {
  /** 后端可能当场建会话，所以它由 `meta` 事件改写。 */
  conversationId: string | null;
  /** 本轮 assistant 消息的 id：`meta` 给的，重放路径靠 `seed` 预置。 */
  assistantMessageId: string;
  runId: string;
  /** 已产出的正文（含被中断时的半截）。 */
  text: string;
  citations: Citation[];
  /** 按 `sequence` 排好序的步骤表；与事件里的 id 一一对应（同 id 覆盖）。 */
  steps: AgentStep[];
  stepsSinceTextFrom: number;
  pendingApprovals: PendingApproval[];
  error: string | null;
  status: GenerationStatus;
  finishReason: FinishReason | null;
  /** 终态闸门：一条消息只提交一次。 */
  terminated: boolean;
  commit: AssistantCommit | null;
  /** 步骤序号计数器（`step_started` 命中已有步骤时也会消耗一个号，与后端事件顺序同源）。 */
  stepSeq: number;
}

export type StreamEvent =
  /** 重放路径预置 id：durable 事件日志里没有 meta 帧。 */
  | { kind: "seed"; conversationId?: string | null; messageId?: string | null }
  | { kind: "meta"; conversationId: string; messageId: string }
  | { kind: "run_started"; runId: string }
  | { kind: "runtime_selected"; runId: string }
  | { kind: "agent_graph"; runId: string }
  /** `plan_created` 与 `research_plan(_updated)` 三帧的正文完全同构。 */
  | { kind: "plan"; summary?: string; steps: { id: string; title: string }[] }
  | { kind: "instruction_received"; instruction: string }
  | { kind: "step_started"; stepId: string; title: string; type?: string }
  | { kind: "step_completed"; stepId: string; status?: string }
  | {
      kind: "tool_call";
      id: string;
      name: string;
      arguments?: Record<string, unknown>;
      dangerous?: boolean;
    }
  | { kind: "tool_result"; id: string; name: string; ok: boolean; result: unknown }
  | { kind: "token"; delta: string }
  | { kind: "citations"; citations: Citation[] | null | undefined }
  | { kind: "approval_required"; approval: PendingApproval }
  | { kind: "approval_resolved"; approvalId: string }
  | { kind: "done"; messageId?: string; finishReason: FinishReason }
  | {
      kind: "error";
      code?: string | null;
      message?: string;
      status?: number;
      detail?: unknown;
    }
  /** `streamChat` 自己抛出来（fetch 层失败），不是后端的 error 帧。 */
  | { kind: "fetch_failed"; error: unknown }
  | { kind: "stream_end"; reason: StreamEndReason };

export function initialStreamState(conversationId: string | null = null): StreamState {
  return {
    conversationId,
    assistantMessageId: "",
    runId: "",
    text: "",
    citations: [],
    steps: [],
    stepsSinceTextFrom: 0,
    pendingApprovals: [],
    error: null,
    status: "complete",
    finishReason: null,
    terminated: false,
    commit: null,
    stepSeq: 0,
  };
}

/** 后端 error 帧的 code → 终态 finish_reason（其余一律按 `error` 收口）。 */
const ERROR_CODE_FINISH: Record<string, FinishReason> = {
  provider_timeout: "timeout",
  stream_disconnected: "stream_disconnected",
  provider_error: "provider_error",
};

export function errorFinishReason(code?: string | null): FinishReason {
  return (code && ERROR_CODE_FINISH[code]) || "error";
}

/**
 * 一条事件进状态核。`now` 注入是为了让「中断时补的假消息 id」「步骤时间戳」在
 * 单测里可钉死；不传就是 `Date.now()`，与直接内联等价。
 */
export function reduce(
  state: StreamState,
  event: StreamEvent,
  now: number = Date.now()
): StreamState {
  switch (event.kind) {
    case "seed": {
      const conversationId = event.conversationId || state.conversationId;
      const assistantMessageId = event.messageId || state.assistantMessageId;
      if (
        conversationId === state.conversationId &&
        assistantMessageId === state.assistantMessageId
      ) {
        return state;
      }
      return { ...state, conversationId, assistantMessageId };
    }

    case "meta":
      return {
        ...state,
        conversationId: event.conversationId,
        assistantMessageId: event.messageId,
      };

    case "run_started":
    case "runtime_selected":
    case "agent_graph":
      return state.runId === event.runId ? state : { ...state, runId: event.runId };

    case "plan": {
      let steps = state.steps;
      let seq = state.stepSeq;
      event.steps.forEach((p, i) => {
        seq += 1;
        steps = replaceStep(steps, {
          id: p.id,
          sequence: seq,
          type: "plan",
          title: p.title,
          summary: i === 0 ? event.summary : undefined,
          status: "pending",
        });
      });
      return { ...state, steps, stepSeq: seq };
    }

    case "instruction_received": {
      const seq = state.stepSeq + 1;
      return withStep(
        state,
        {
          id: `instr-${seq}`,
          sequence: seq,
          type: "approval",
          title: `已接收追加指引：${event.instruction}`,
          status: "done",
        },
        seq
      );
    }

    case "step_started": {
      const seq = state.stepSeq + 1;
      const existing = state.steps.find((s) => s.id === event.stepId);
      const step: AgentStep = existing
        ? {
            ...existing,
            status: "running",
            type: (event.type as AgentStep["type"]) || existing.type,
            title: event.title,
          }
        : {
            id: event.stepId,
            sequence: seq,
            type: (event.type as AgentStep["type"]) || "agent",
            title: event.title,
            status: "running",
            startedAt: new Date(now).toISOString(),
          };
      return withStep(state, step, seq);
    }

    case "step_completed": {
      const existing = state.steps.find((s) => s.id === event.stepId);
      // 步骤没开始过就收到完成：老实现直接丢掉，不凭空造一条（否则会出现没有
      // 起点的步骤，而面板按步骤顺序渲染）。
      if (!existing) return state;
      return {
        ...state,
        steps: replaceStep(state.steps, {
          ...existing,
          status: (event.status as AgentStep["status"]) || "done",
          finishedAt: new Date(now).toISOString(),
        }),
      };
    }

    case "tool_call": {
      const seq = state.stepSeq + 1;
      return withStep(
        state,
        {
          id: event.id,
          sequence: seq,
          type: "tool",
          title: event.name,
          status: "running",
          startedAt: new Date(now).toISOString(),
          tool: {
            name: event.name,
            dangerous: event.dangerous,
            argumentsPreview: event.arguments,
          },
        },
        seq
      );
    }

    case "tool_result": {
      const existing = state.steps.find((s) => s.id === event.id);
      let next = state;
      if (existing) {
        next = {
          ...next,
          steps: replaceStep(next.steps, {
            ...existing,
            status: event.ok ? "done" : "error",
            finishedAt: new Date(now).toISOString(),
            tool: {
              ...(existing.tool ?? { name: event.name }),
              name: event.name,
              ok: event.ok,
              resultPreview: resultPreview(event.result),
            },
          }),
        };
      }
      // 真实网页工具产出升格成「来源」，与 KB 引用并存（同一个合并器，只有一份事实来源）。
      if (event.ok && (event.name === "web_search" || event.name === "http_get")) {
        const web = extractWebCitations(event.name, event.result);
        if (web.length) {
          next = { ...next, citations: mergeCitations(next.citations, web) };
        }
      }
      return next;
    }

    case "token":
      return {
        ...state,
        text: state.text + event.delta,
        // 正文续上就意味着此前那批工具卡片已被读完，下一批从当前长度起算。
        stepsSinceTextFrom: state.steps.length,
      };

    case "citations": {
      const incoming = event.citations;
      if (!Array.isArray(incoming)) return state;
      return { ...state, citations: mergeCitations(state.citations, incoming) };
    }

    case "approval_required":
      return state.pendingApprovals.some((p) => p.approvalId === event.approval.approvalId)
        ? state
        : { ...state, pendingApprovals: [...state.pendingApprovals, event.approval] };

    case "approval_resolved":
      return state.pendingApprovals.some((p) => p.approvalId === event.approvalId)
        ? {
            ...state,
            pendingApprovals: state.pendingApprovals.filter(
              (p) => p.approvalId !== event.approvalId
            ),
          }
        : state;

    case "done": {
      if (state.terminated) return state;
      if (state.conversationId) {
        return commit(state, event.finishReason, event.messageId || state.assistantMessageId);
      }
      return {
        ...state,
        terminated: true,
        finishReason: event.finishReason,
        status: finishReasonToStatus(event.finishReason),
      };
    }

    case "error": {
      if (state.terminated) return state;
      const fr = errorFinishReason(event.code);
      // 服务端自己的中文优先，其次按 status/code 兜底（`lib/api-error.ts`）。
      const message = userErrorMessage(event);
      if (state.conversationId) {
        return { ...commit(state, fr, state.assistantMessageId), error: message };
      }
      return {
        ...state,
        terminated: true,
        error: message,
        finishReason: fr,
        status: finishReasonToStatus(fr),
      };
    }

    case "fetch_failed": {
      if (state.terminated) return state;
      const message = userErrorMessage(event.error);
      if (state.conversationId && state.text) {
        return { ...commit(state, "error", state.assistantMessageId), error: message };
      }
      return {
        ...state,
        terminated: true,
        error: message,
        finishReason: "error",
        status: "error",
      };
    }

    case "stream_end": {
      if (state.terminated) return state;
      switch (event.reason) {
        case "unmount":
          // 卸载只关订阅：后端 durable run 继续跑，回到会话时整段重放补回视图。
          return state;
        case "user_stop":
          if (state.conversationId && state.text) {
            return commit(
              state,
              "cancelled",
              state.assistantMessageId || `cancelled-${now}`
            );
          }
          return { ...state, terminated: true, finishReason: "cancelled", status: "cancelled" };
        case "socket_closed":
          if (state.conversationId && state.text) {
            return {
              ...commit(
                state,
                "stream_disconnected",
                state.assistantMessageId || `interrupted-${now}`
              ),
              error: "连接中断，已保留已生成内容",
            };
          }
          return state;
      }
    }
  }
}

// --------------------------------------------------------------------------- //
// 重放/刷新后的「上一条发送」重建
// --------------------------------------------------------------------------- //

/** 与 hook 的 `SendOptions` 同形（`knowledgeBaseId` 等遗留字段由发送方自带）。 */
export interface RebuiltSend {
  content: string;
  opts: {
    conversationId?: string | null;
    mode?: UserChatMode;
    modelId?: string | null;
    knowledgeBaseIds?: string[];
    mentions?: ChatMention[];
    attachmentIds?: string[];
  };
}

interface SendParamsMeta {
  mode?: string;
  model_id?: string | null;
  knowledge_base_ids?: string[];
  mentions?: ChatMention[];
  attachment_ids?: string[];
}

/**
 * 刷新后从最近一条 user 消息的 `send_params` 还原「可重发」状态。
 *
 * 没有它，页面刷新后的「重新生成 / 继续」会静默变成 no-op —— 用户按了没反应，比
 * 报错更糟。取不到任何 user 消息时返回 null（调用方自己决定要不要提示）。
 */
export function rebuildLastSendFromMessages(
  messages: readonly Message[],
  conversationId: string
): RebuiltSend | null {
  for (let i = messages.length - 1; i >= 0; i--) {
    const m = messages[i];
    if (m.role !== "user") continue;
    const sp = (m.metadata as { send_params?: SendParamsMeta } | undefined)?.send_params;
    return {
      content: m.content,
      opts: {
        conversationId,
        mode: (sp?.mode as UserChatMode | undefined) ?? undefined,
        modelId: sp?.model_id ?? null,
        knowledgeBaseIds: sp?.knowledge_base_ids,
        mentions: sp?.mentions,
        attachmentIds: sp?.attachment_ids,
      },
    };
  }
  return null;
}

// --------------------------------------------------------------------------- //
// 内部：步骤表与终态提交
// --------------------------------------------------------------------------- //

function resultPreview(result: unknown): string | undefined {
  if (typeof result === "string") return result;
  return result != null ? JSON.stringify(result) : undefined;
}

/** 同 id 覆盖、按 `sequence` 排序 —— 与老实现的 map + 重排等价。 */
function replaceStep(steps: AgentStep[], next: AgentStep): AgentStep[] {
  const idx = steps.findIndex((s) => s.id === next.id);
  const out =
    idx === -1 ? [...steps, next] : steps.map((s, i) => (i === idx ? next : s));
  out.sort((a, b) => a.sequence - b.sequence);
  return out;
}

function withStep(
  state: StreamState,
  step: AgentStep | undefined,
  stepSeq: number
): StreamState {
  return {
    ...state,
    stepSeq,
    steps: step ? replaceStep(state.steps, step) : state.steps,
  };
}

/** 提交本轮的 assistant 消息：终态闸门 + finish_reason/status 派生同一处收口。 */
function commit(
  state: StreamState,
  finishReason: FinishReason,
  messageId: string
): StreamState {
  return {
    ...state,
    terminated: true,
    finishReason,
    status: finishReasonToStatus(finishReason),
    commit: {
      conversationId: state.conversationId as string,
      messageId,
      content: state.text,
      finishReason,
      citations: state.citations,
      steps: state.steps,
      runId: state.runId,
    },
  };
}
