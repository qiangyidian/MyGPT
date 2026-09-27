"use client";

import { Database, Save, Sparkles, X } from "lucide-react";
import { toast } from "sonner";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ChatModeSelector } from "@/components/chat/chat-mode-selector";
import { AdvancedModelSelector } from "@/components/chat/advanced-model-selector";
import { ReasoningEffortSelector } from "@/components/chat/reasoning-effort-selector";
import { useChatUiStore } from "@/stores/chat-ui-store";
import { MAX_KB_PER_REQUEST, toggleKnowledgeBase } from "@/lib/inline-refs";
import type { KnowledgeBase } from "@/lib/types";

interface ComposerToolbarProps {
  modelId: string | null;
  onModelChange: (id: string | null) => void;
  knowledgeBaseIds: string[];
  onKnowledgeBaseIdsChange: (ids: string[]) => void;
  knowledgeBases?: KnowledgeBase[];
  /** Attachment mime types — drives modality-aware model filtering. */
  attachmentMimes?: string[];
  /** 提示词库：打开选择器（选中的模板由宿主插到输入框光标处）。 */
  onOpenPromptLibrary?: () => void;
  /** 把输入框里选中的多行文字存成我的模板。 */
  onSaveSelectionAsTemplate?: () => void;
  /** 选区是否够格当模板（多行且非空）；不勾上就别让用户点了没反应。 */
  canSaveSelectionAsTemplate?: boolean;
  savingSelectionAsTemplate?: boolean;
  className?: string;
}

/**
 * Compact toolbar above the textarea: mode picker (primary), knowledge-base
 * selector (compact), and the advanced model selector (hidden unless opted in).
 * Kept to a single row on mobile by design.
 *
 * 知识库选择是真多选：勾一个不顶掉已勾的、菜单不收起，触发器上写清「已选 N
 * 个」并把名字放在 title 里；上限与服务端同源（inline-refs 的
 * MAX_KB_PER_REQUEST），超了给一句中文提示而不是静默丢。
 *
 * Hermes 模式下只保留模式选择器：知识库（平台 RAG 对 Hermes 不生效——它有
 * 自己的服务端记忆与检索）、模型选择（由页面自动锁定 hermes provider 的
 * 模型）与推理力度（Hermes 不消费）全部隐藏，避免无效选项误导。
 */
export function ComposerToolbar({
  modelId,
  onModelChange,
  knowledgeBaseIds,
  onKnowledgeBaseIdsChange,
  knowledgeBases,
  attachmentMimes,
  onOpenPromptLibrary,
  onSaveSelectionAsTemplate,
  canSaveSelectionAsTemplate,
  savingSelectionAsTemplate,
  className,
}: ComposerToolbarProps) {
  const hasKbs = !!knowledgeBases && knowledgeBases.length > 0;
  const mode = useChatUiStore((s) => s.mode);
  const isHermes = mode === "hermes";

  // 多选：按钮上要看得出「选了几个、是哪几个」，而不是只显示第一个名字。
  const kbById = new Map((knowledgeBases ?? []).map((kb) => [kb.id, kb]));
  const selectedNames = knowledgeBaseIds
    .map((id) => kbById.get(id)?.name)
    .filter((n): n is string => !!n);
  // 选中项被别处删掉时不能假装没发生：计数照实，名字标成不可用。
  const missingCount = knowledgeBaseIds.length - selectedNames.length;
  const kbLabel =
    selectedNames.length === 0 && missingCount === 0
      ? "知识库"
      : knowledgeBaseIds.length === 1
        ? (selectedNames[0] ?? "已选 1 个")
        : `知识库 · 已选 ${knowledgeBaseIds.length} 个`;
  const kbTitle =
    knowledgeBaseIds.length === 0
      ? undefined
      : `${selectedNames.join("、")}${missingCount > 0 ? `（${missingCount} 个已不可用）` : ""}`;

  const toggleKb = (id: string) => {
    const result = toggleKnowledgeBase(knowledgeBaseIds, id);
    if (!result.ok) {
      // 满了就直说，不静默丢、也不挤掉最早那个——两种都会让用户以为选上了。
      toast.error(result.reason ?? "已达知识库选择上限");
      return;
    }
    onKnowledgeBaseIdsChange(result.ids);
  };

  return (
    <div className={cn("flex flex-wrap items-center gap-2", className)}>
      <ChatModeSelector />

      {!isHermes && hasKbs && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              className="h-9 max-w-[220px] gap-1.5 text-sm font-medium"
              title={kbTitle}
            >
              <Database className="h-4 w-4 shrink-0" />
              <span className="truncate">{kbLabel}</span>
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" side="top" className="w-[260px]">
            {knowledgeBases.map((kb) => (
              <DropdownMenuCheckboxItem
                key={kb.id}
                checked={knowledgeBaseIds.includes(kb.id)}
                // 勾一个就收起的话多选等于没有：连着勾完再关。
                onSelect={(e) => e.preventDefault()}
                onCheckedChange={() => toggleKb(kb.id)}
                className="items-start"
              >
                <span className="min-w-0">
                  <span className="block truncate">{kb.name}</span>
                  {/* 每库自己的检索参数（条目 23）：多库查询里不存在一个全局 k。 */}
                  <span className="block truncate text-[11px] font-normal text-muted-foreground">
                    {kbRetrievalHint(kb)}
                  </span>
                </span>
              </DropdownMenuCheckboxItem>
            ))}
            {knowledgeBaseIds.length > 0 && (
              <>
                <DropdownMenuSeparator />
                <DropdownMenuItem
                  onSelect={() => onKnowledgeBaseIdsChange([])}
                >
                  <X className="mr-2 h-4 w-4" />
                  清空已选（{knowledgeBaseIds.length}）
                </DropdownMenuItem>
              </>
            )}
            <p className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
              {`最多同时选 ${MAX_KB_PER_REQUEST} 个。每库按自己的参数召回，再合并成一份全局上下文。`}
            </p>
          </DropdownMenuContent>
        </DropdownMenu>
      )}

      {!isHermes && (
        <AdvancedModelSelector
          value={modelId}
          onChange={onModelChange}
          mimes={attachmentMimes}
        />
      )}

      {!isHermes && <ReasoningEffortSelectorStoreBridge modelId={modelId} />}

      {/* 提示词库和模式无关（Hermes 也要能插模板），所以不参与 isHermes 的隐藏。 */}
      {onOpenPromptLibrary && (
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="outline"
              size="sm"
              className="h-9 gap-1.5 text-sm font-medium"
              title="从提示词库挑一个模板插入输入框，或把选中文字存为模板"
            >
              <Sparkles className="h-4 w-4 shrink-0" />
              提示词库
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" side="top" className="w-[240px]">
            <DropdownMenuItem onSelect={() => onOpenPromptLibrary()}>
              <Sparkles className="mr-2 h-4 w-4" />
              插入模板…
            </DropdownMenuItem>
            <DropdownMenuItem
              disabled={!canSaveSelectionAsTemplate || savingSelectionAsTemplate}
              onSelect={() => onSaveSelectionAsTemplate?.()}
            >
              <Save className="mr-2 h-4 w-4" />
              {savingSelectionAsTemplate ? "保存中…" : "把选中文字存为模板"}
            </DropdownMenuItem>
            <p className="px-2 py-1.5 text-[11px] leading-4 text-muted-foreground">
              {canSaveSelectionAsTemplate
                ? "将把输入框里选中的多行文字存为你的模板。"
                : "先在输入框里选中多行文字，才能存为模板。"}
            </p>
          </DropdownMenuContent>
        </DropdownMenu>
      )}
    </div>
  );
}

/** 这个库这一轮按什么参数召回。多库查询里每库各算各的（服务端按库解析
 *  NULL = 沿用平台默认），所以这里绝不明示一个「全局 top_k」。 */
function kbRetrievalHint(kb: KnowledgeBase): string {
  const parts = [
    kb.document_count === 0 ? "暂无文档" : `${kb.document_count} 个文档`,
    kb.top_k ? `召回 ${kb.top_k} 条` : "召回沿用默认",
  ];
  if (kb.score_threshold != null) parts.push(`阈值 ${kb.score_threshold}`);
  if (kb.rerank_enabled != null) {
    parts.push(kb.rerank_enabled ? "参与重排" : "不重排");
  }
  return parts.join(" · ");
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
