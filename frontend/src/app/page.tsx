"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { NavSuspense } from "@/components/navigation/page-loading";
import { MessageList } from "@/components/message-list";
import { Composer } from "@/components/composer";
import { ApprovalCard } from "@/components/approval-card";
import { ContextPanel } from "@/components/context/context-panel";
import { ContextPanelTrigger } from "@/components/context/context-panel-trigger";
import { AgentPanelTrigger } from "@/components/agents/agent-panel-trigger";
import { ArtifactPreviewPanel } from "@/components/artifacts/artifact-preview-panel";
import { useConversationDetail, useConversations } from "@/hooks/useConversations";
import { useChatStream } from "@/hooks/useChatStream";
import { restoreAgentGraph } from "@/hooks/useAgentRunGraph";
import { useBranchConversation } from "@/hooks/useMessageActions";
import { useModels } from "@/hooks/useModels";
import { api } from "@/lib/api";
import { useChatUiStore } from "@/stores/chat-ui-store";
import { useContextPanelStore } from "@/stores/context-panel-store";
import { useAgentRunStore, selectIsDemo } from "@/stores/agent-run-store";
import { BranchHistory } from "@/components/branch-history";
import type { Citation, KnowledgeBase } from "@/lib/types";

export default function HomePage() {
  return (
    <NavSuspense>
      <AppShell>
        {({ activeConversationId, setActiveConversationId }) => (
          <ChatPanel
            activeConversationId={activeConversationId}
            setActiveConversationId={setActiveConversationId}
          />
        )}
      </AppShell>
    </NavSuspense>
  );
}

function ChatPanel({
  activeConversationId,
  setActiveConversationId,
}: {
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;
}) {
  const { chatModels } = useModels();
  const detail = useConversationDetail(activeConversationId);
  const { create: createConversation } = useConversations();
  const branchConversation = useBranchConversation();

  const kbsQuery = useQuery<KnowledgeBase[]>({
    queryKey: ["knowledge-bases"],
    queryFn: () => api.listKnowledgeBases(),
  });

  const mode = useChatUiStore((s) => s.mode);
  const isDemo = useAgentRunStore(selectIsDemo);

  const [modelId, setModelId] = useState<string | null>(null);
  const [kbIds, setKbIds] = useState<string[]>([]);

  // Default the selector to the conversation's model, else the first chat
  // model. A null modelId ("默认模型") lets the backend choose.
  useEffect(() => {
    if (detail.data?.model_id) setModelId(detail.data.model_id);
    else if (modelId === null && chatModels.length > 0) setModelId(chatModels[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.data?.model_id, chatModels]);

  // Hermes 模式：模型选择器已隐藏，自动锁定 hermes provider 的模型；知识库
  // 同步清空（平台 RAG 不注入 Hermes）。切回其他模式时恢复用户原先的选择。
  const hermesModelId = useMemo(
    () => chatModels.find((m) => m.provider === "hermes")?.id ?? null,
    [chatModels]
  );
  const prevModelRef = useRef<string | null>(null);
  const prevKbRef = useRef<string[]>([]);
  useEffect(() => {
    if (mode === "hermes") {
      if (hermesModelId && modelId !== hermesModelId) {
        if (prevModelRef.current === null) prevModelRef.current = modelId;
        setModelId(hermesModelId);
      }
      if (kbIds.length > 0) {
        if (!prevKbRef.current.length) prevKbRef.current = kbIds;
        setKbIds([]);
      }
    } else if (prevModelRef.current !== null || prevKbRef.current.length) {
      // 离开 hermes 模式：仅当用户没有手动改选时恢复（手动改选会更新
      // modelId，此时恢复旧值反而覆盖用户意图）。
      if (prevModelRef.current !== null && prevModelRef.current !== hermesModelId) {
        setModelId(prevModelRef.current);
      }
      if (prevKbRef.current.length) setKbIds(prevKbRef.current);
      prevModelRef.current = null;
      prevKbRef.current = [];
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, hermesModelId]);

  // Sync the KB picker from the conversation ONLY on a conversation switch.
  // The detail query refetches after every turn; blindly copying its
  // knowledge_base_id here wiped the user's per-turn selection right after
  // the first answer. (The backend now also binds the pick to the
  // conversation, so switching conversations restores it correctly — and a
  // brand-new conversation created by the first send keeps the selection.)
  const kbSyncPrevConvRef = useRef<string | null | undefined>(undefined);
  useEffect(() => {
    const convId = detail.data?.id ?? null;
    const prev = kbSyncPrevConvRef.current;
    kbSyncPrevConvRef.current = convId;
    // Same conversation refetch → keep the user's current selection.
    if (prev !== undefined && prev === convId) return;
    // First send created the conversation (null → id) → the just-made
    // selection applies; don't clobber it with the fresh binding.
    if (prev === null && convId !== null) return;
    setKbIds(detail.data?.knowledge_base_id ? [detail.data.knowledge_base_id] : []);
  }, [detail.data?.id, detail.data?.knowledge_base_id]);

  const chat = useChatStream();
  const lastRestoredRunRef = useRef<string | null>(null);

  // Bumped on every send so MessageList force-scrolls to the newest message
  // (a send is an explicit "jump to the answer", even if the user was
  // scrolled up reading history).
  const [scrollToBottomSignal, setScrollToBottomSignal] = useState(0);
  const bumpScrollSignal = () => setScrollToBottomSignal((n) => n + 1);

  // If a stream surfaces a conversation id while NONE is selected, switch to it.
  // CRITICAL: only react to a NEWLY-streamed id (tracked via the ref below) —
  // never re-activate a stale id merely because activeId became null (e.g. after
  // deleting the active conversation). Otherwise this fights AppShell's
  // invalid-conversation clear (push X → 404 → clear null → push X …) into an
  // infinite push/replace loop and pollutes the back stack.
  const lastStreamedConvRef = useRef<string | null>(null);
  useEffect(() => {
    const streamed = chat.currentConversationId;
    if (!streamed) {
      lastStreamedConvRef.current = null;
      return;
    }
    if (streamed === lastStreamedConvRef.current) return; // already consumed
    lastStreamedConvRef.current = streamed;
    if (!activeConversationId) {
      setActiveConversationId(streamed);
    }
  }, [chat.currentConversationId, activeConversationId, setActiveConversationId]);

  useEffect(() => {
    if (chat.error) toast.error("生成失败", { description: chat.error });
  }, [chat.error]);

  // Restore the multi-agent graph after refresh.
  useEffect(() => {
    if (chat.isStreaming) return;
    const msgs = detail.data?.messages ?? [];
    const lastAssistant = [...msgs].reverse().find((m) => m.role === "assistant");
    const runId = (lastAssistant?.metadata as { run_id?: string } | undefined)?.run_id;
    if (runId && runId !== lastRestoredRunRef.current) {
      lastRestoredRunRef.current = runId;
      void restoreAgentGraph(runId);
    } else if (!runId) {
      lastRestoredRunRef.current = null;
    }
  }, [detail.data?.messages, chat.isStreaming]);

  // Rebuild the replayable last-send from persisted send_params once the
  // conversation detail has loaded (covers the post-refresh case where the
  // in-memory lastSendRef was lost and regenerate/continue went silent).
  useEffect(() => {
    if (detail.data && !chat.isStreaming) chat.rebuildLastSend(activeConversationId);
  }, [detail.data, chat.isStreaming, activeConversationId]);

  // Durable runs: on conversation open / browser refresh, adopt a run that is
  // still executing server-side and resume its live view (no-op otherwise).
  // Deliberately NOT keyed on chat.reattach — its identity churns per render
  // (factory dep), which would probe the API on every keystroke.
  useEffect(() => {
    if (!activeConversationId || chat.isStreaming) return;
    void chat.reattach(activeConversationId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeConversationId, chat.isStreaming]);

  // ---- Context Panel auto-open rules (respect per-run suppression) ----
  // Waiting on a dangerous-tool approval -> open Execution.
  useEffect(() => {
    if (chat.pendingApprovals.length > 0) {
      const runId = useAgentRunStore.getState().active.runId;
      if (runId && !useContextPanelStore.getState().isSuppressed(runId)) {
        useContextPanelStore.getState().openWith("execution");
      }
    }
  }, [chat.pendingApprovals.length]);

  const runStatus = useAgentRunStore((s) => s.active.status);
  useEffect(() => {
    if (runStatus === "failed") {
      const runId = useAgentRunStore.getState().active.runId;
      if (runId && !useContextPanelStore.getState().isSuppressed(runId)) {
        useContextPanelStore.getState().openWith("execution");
      }
    }
  }, [runStatus]);

  // Multi-agent completion: a light confirmation toast (the panel + trigger
  // already reflect status; this is the gentle "it's done" nudge). Fires once
  // per run.
  const lastToastRunRef = useRef<string | null>(null);
  useEffect(() => {
    if (runStatus !== "completed") return;
    const st = useAgentRunStore.getState();
    const runId = st.active.runId;
    if (!runId || lastToastRunRef.current === runId) return;
    if (st.active.nodes.length >= 2) {
      lastToastRunRef.current = runId;
      toast.success(`多 Agent 协作完成 · ${st.active.nodes.length} 个 Agent`);
    }
  }, [runStatus]);

  const ensureConversationId = useCallback(async () => {
    const conv = await createConversation({});
    setActiveConversationId(conv.id);
    return conv.id;
  }, [createConversation, setActiveConversationId]);

  const messages = detail.data?.messages ?? [];
  // Only render the live stream for the conversation it belongs to — otherwise
  // switching conversations mid-stream paints the other conversation's reply.
  // The `activeConversationId == null` clause covers a brand-new chat whose
  // conversation id hasn't resolved yet (the stream starts before the backend
  // mints the id); without it the bubble flickers off for a frame when onMeta
  // sets currentConversationId ahead of the active-id state update.
  const isThisConvStreaming =
    chat.isStreaming &&
    (chat.currentConversationId === activeConversationId ||
      activeConversationId == null);

  const handleSend = (content: string, opts: { mode: typeof mode; attachmentIds: string[] }) => {
    bumpScrollSignal();
    void chat.send(content, {
      conversationId: activeConversationId,
      modelId,
      knowledgeBaseIds: kbIds,
      mode: opts.mode,
      attachmentIds: opts.attachmentIds,
    });
  };

  const handleBranch = async (messageId: string, newContent: string) => {
    if (!activeConversationId) return;
    try {
      const branch = await branchConversation(activeConversationId, messageId, newContent);
      setActiveConversationId(branch.id);
      bumpScrollSignal();
      await chat.send(newContent, {
        conversationId: branch.id,
        modelId,
        knowledgeBaseIds: kbIds,
        mode,
        attachmentIds: [],
      });
    } catch (err) {
      toast.error("编辑分支失败", { description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleSourceClick = useCallback((index: number, cits: Citation[]) => {
    useContextPanelStore.getState().setSources(cits);
    useContextPanelStore.getState().openWith("sources", { sourceIndex: index });
  }, []);

  const handleOpenAttachment = useCallback((attachmentId: string) => {
    useContextPanelStore.getState().openWith("files", { attachmentId });
  }, []);

  const handlePickSuggestion = (prompt: string) => {
    bumpScrollSignal();
    void chat.send(prompt, {
      conversationId: activeConversationId,
      modelId,
      knowledgeBaseIds: kbIds,
      mode,
      attachmentIds: [],
    });
  };

  return (
    <div className="flex min-h-0 flex-1">
      <main className="flex min-w-0 flex-1 flex-col">
        <div className="flex shrink-0 items-center justify-end gap-2 px-4 py-2">
          {activeConversationId && (
            <BranchHistory
              conversationId={activeConversationId}
              activeConversationId={activeConversationId}
              onNavigate={setActiveConversationId}
              className="h-8 gap-1 text-xs text-muted-foreground"
            />
          )}
          <AgentPanelTrigger />
          <ContextPanelTrigger
            conversationId={activeConversationId}
            hasPendingApproval={chat.pendingApprovals.length > 0}
          />
        </div>

        {isDemo && (
          <div
            role="status"
            aria-live="polite"
            className="mx-auto w-full max-w-3xl shrink-0 px-4"
          >
            <div className="flex items-start gap-2 rounded-md border border-amber-500/50 bg-amber-500/10 px-3 py-2 text-xs text-amber-800 dark:text-amber-300">
              <span className="mt-0.5 font-bold" aria-hidden>
                ⚠️
              </span>
              <span>
                演示模式：当前回答由演示执行器生成，<strong>内容非真实模型输出</strong>，
                仅供展示多 Agent 面板与执行流程，请勿作为真实结论使用。
              </span>
            </div>
          </div>
        )}

        <MessageList
          messages={messages}
          streamingText={isThisConvStreaming ? chat.streamingText : undefined}
          isStreaming={isThisConvStreaming}
          streamingCitations={isThisConvStreaming ? chat.citations : undefined}
          streamingSteps={
            isThisConvStreaming
              ? chat.steps.slice(chat.stepsSinceTextFrom)
              : undefined
          }
          canRegenerate={messages.length > 0 && !chat.isStreaming}
          onRegenerate={() => void chat.regenerate()}
          onContinue={() => void chat.continueGeneration()}
          onBranch={(id, content) => void handleBranch(id, content)}
          onSourceClick={handleSourceClick}
          onOpenAttachment={handleOpenAttachment}
          onPickSuggestion={handlePickSuggestion}
          scrollToBottomSignal={scrollToBottomSignal}
        />

        <div className="mx-auto w-full shrink-0 max-w-3xl px-4">
          {chat.pendingApprovals.map((ap) => (
            <ApprovalCard
              key={ap.approvalId}
              approval={ap}
              onApprove={(id) => chat.approveTool(id)}
              onReject={(id) => chat.rejectTool(id)}
            />
          ))}
        </div>

        <Composer
          className="shrink-0"
          onSend={handleSend}
          onStop={chat.stop}
          isStreaming={chat.isStreaming}
          modelId={modelId}
          onModelChange={setModelId}
          knowledgeBaseIds={kbIds}
          onKnowledgeBaseIdsChange={setKbIds}
          knowledgeBases={kbsQuery.data}
          conversationId={activeConversationId}
          ensureConversationId={ensureConversationId}
        />
      </main>

      <ContextPanel conversationId={activeConversationId} />
      <ArtifactPreviewPanel />
    </div>
  );
}
