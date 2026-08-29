"use client";

import { Database } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ChatModeSelector } from "@/components/chat/chat-mode-selector";
import { AdvancedModelSelector } from "@/components/chat/advanced-model-selector";
import { ReasoningEffortSelector } from "@/components/chat/reasoning-effort-selector";
import { useChatUiStore } from "@/stores/chat-ui-store";
import type { KnowledgeBase } from "@/lib/types";

interface ComposerToolbarProps {
  modelId: string | null;
  onModelChange: (id: string | null) => void;
  knowledgeBaseIds: string[];
  onKnowledgeBaseIdsChange: (ids: string[]) => void;
  knowledgeBases?: KnowledgeBase[];
  /** Attachment mime types — drives modality-aware model filtering. */
  attachmentMimes?: string[];
  className?: string;
}

/**
 * Compact toolbar above the textarea: mode picker (primary), knowledge-base
 * selector (compact), and the advanced model selector (hidden unless opted in).
 * Kept to a single row on mobile by design.
 */
export function ComposerToolbar({
  modelId,
  onModelChange,
  knowledgeBaseIds,
  onKnowledgeBaseIdsChange,
  knowledgeBases,
  attachmentMimes,
  className,
}: ComposerToolbarProps) {
  const hasKbs = !!knowledgeBases && knowledgeBases.length > 0;

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <ChatModeSelector />

      {hasKbs && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="outline" size="sm" className="h-9 gap-1.5 text-sm font-medium">
              <Database className="h-4 w-4" />
              {knowledgeBaseIds.length === 0 ? "知识库" : `知识库 · ${knowledgeBaseIds.length}`}
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-[200px]">
            {knowledgeBases.map((kb) => (
              <DropdownMenuCheckboxItem
                key={kb.id}
                checked={knowledgeBaseIds.includes(kb.id)}
                onCheckedChange={(c) =>
                  onKnowledgeBaseIdsChange(
                    c ? [...knowledgeBaseIds, kb.id] : knowledgeBaseIds.filter((x) => x !== kb.id)
                  )
                }
              >
                {kb.name}
              </DropdownMenuCheckboxItem>
            ))}
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      <AdvancedModelSelector
        value={modelId}
        onChange={onModelChange}
        mimes={attachmentMimes}
      />

      <ReasoningEffortSelectorStoreBridge modelId={modelId} />
    </div>
  );
}

/** Reads/writes reasoning effort from the shared chat-ui store (B6). Only
 *  renders when the selected (or any default) model supports it. */
function ReasoningEffortSelectorStoreBridge({ modelId }: { modelId: string | null }) {
  const effort = useChatUiStore((s) => s.reasoningEffort);
  const setEffort = useChatUiStore((s) => s.setReasoningEffort);
  return (
    <ReasoningEffortSelector
      modelId={modelId}
      value={effort}
      onChange={setEffort}
      className="h-9 gap-1.5 text-sm font-medium"
    />
  );
}
