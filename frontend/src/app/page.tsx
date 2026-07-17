"use client";

import { useEffect, useState } from "react";
import { toast } from "sonner";

import { AppShell } from "@/components/app-shell";
import { MessageList } from "@/components/message-list";
import { Composer } from "@/components/composer";
import { ApprovalCard } from "@/components/approval-card";
import { useConversationDetail } from "@/hooks/useConversations";
import { useChatStream } from "@/hooks/useChatStream";
import { useModels } from "@/hooks/useModels";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { KnowledgeBase } from "@/lib/types";

export default function HomePage() {
  return (
    <AppShell>
      {({ activeConversationId, setActiveConversationId }) => (
        <ChatPanel
          activeConversationId={activeConversationId}
          setActiveConversationId={setActiveConversationId}
        />
      )}
    </AppShell>
  );
}

/**
 * Inner panel that holds the message list + composer for the active
 * conversation. Separated from page so that hooks are only called when
 * auth has resolved (i.e. AppShell children render).
 */
function ChatPanel({
  activeConversationId,
  setActiveConversationId,
}: {
  activeConversationId: string | null;
  setActiveConversationId: (id: string | null) => void;
}) {
  const { chatModels } = useModels();
  const detail = useConversationDetail(activeConversationId);

  // Fetch knowledge bases for the composer selector.
  const kbsQuery = useQuery<KnowledgeBase[]>({
    queryKey: ["knowledge-bases"],
    queryFn: () => api.listKnowledgeBases(),
  });

  // Local UI state for model / KB selection.
  const [modelId, setModelId] = useState<string | null>(null);
  const [kbId, setKbId] = useState<string | null>(null);

  // Sync model from the active conversation's saved model_id.
  useEffect(() => {
    if (detail.data?.model_id) {
      setModelId(detail.data.model_id);
    } else if (modelId === null && chatModels.length > 0) {
      setModelId(chatModels[0].id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detail.data?.model_id, chatModels]);

  // Sync KB from the active conversation.
  useEffect(() => {
    if (detail.data?.knowledge_base_id !== undefined) {
      setKbId(detail.data.knowledge_base_id);
    }
  }, [detail.data?.knowledge_base_id]);

  const chat = useChatStream();

  // If a stream created a conversation while NONE was selected (send from the
  // empty state), switch to it so the thread loads. We only do this when
  // activeConversationId is null — otherwise we'd fight the sidebar/"新建对话"
  // selection and keep reverting to the last-streamed conversation.
  useEffect(() => {
    if (!activeConversationId && chat.currentConversationId) {
      setActiveConversationId(chat.currentConversationId);
    }
  }, [chat.currentConversationId, activeConversationId, setActiveConversationId]);

  // Surface stream errors (e.g. upstream 502) instead of silently losing them.
  useEffect(() => {
    if (chat.error) {
      toast.error("生成失败", { description: chat.error });
    }
  }, [chat.error]);

  const messages = detail.data?.messages ?? [];

  const handleSend = (
    content: string,
    opts: { enableTools: boolean; executionMode?: "auto" | "chat" | "agent" }
  ) => {
    void chat.send(content, {
      conversationId: activeConversationId,
      modelId,
      knowledgeBaseId: kbId,
      enableTools: opts.enableTools,
      executionMode: opts.executionMode ?? "auto",
    });
  };

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <MessageList
        messages={messages}
        streamingText={chat.isStreaming ? chat.streamingText : undefined}
        isStreaming={chat.isStreaming}
        streamingCitations={chat.isStreaming ? chat.citations : undefined}
        streamingSteps={chat.isStreaming ? chat.steps : undefined}
        canRegenerate={messages.length > 0 && !chat.isStreaming}
        onRegenerate={() => void chat.regenerate()}
      />

      {/* Human-approval gates for dangerous tools in the live run. */}
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
        knowledgeBaseId={kbId}
        onKnowledgeBaseChange={setKbId}
        knowledgeBases={kbsQuery.data}
      />
    </div>
  );
}
