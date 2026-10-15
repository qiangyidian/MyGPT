"use client";

import { Database, X } from "lucide-react";
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

/**
 * 组内每一项：边框与圆角归整个组（见 SETTINGS_GROUP_CLASS），项自己一律不带
 * （border-0 把子组件自带的 outline 外观中性化），否则又是一排各自带框的按钮
 * ——那正是这次要消掉的「乱」。
 *
 * 焦点环用 ring-inset：组的 overflow-hidden 会把外扩的 ring 裁掉，只有内嵌的
 * 看得见。别把 focus-visible 改成只有背景色，键盘用户就找不着焦点了。
 *
 * 窄屏（< sm）三项平分整行宽度，故 flex-1 + min-w-0（min-w-0 是让 truncate
 * 生效的前提）；sm 以上组是 auto 宽，项各自按内容撑开。
 */
const GROUP_ITEM_CLASS =
  "h-9 min-w-0 flex-1 basis-0 gap-1.5 rounded-none border-0 px-2.5 text-sm font-medium focus-visible:ring-inset focus-visible:ring-offset-0 sm:flex-none";

/**
 * 请求设置组：不是三个并列按钮，而是**一个**控件——共用一层边框，项与项之间
 * 一条竖线。视觉上的「一体」即信息：这三项共同描述「这一轮怎么答」。
 *
 * 分隔用 divide-x 而不是 `[&>*+*]:border-l`：后者与子项的 border-0 同特异度，
 * 谁赢取决于 Tailwind 内部发射顺序；divide 生成的选择器带 :not([hidden]) 伪类，
 * 特异度更高，不靠顺序。
 *
 * w-full 是为了窄屏换行后独占一行（父级 flex-wrap）；sm 以上收成 auto 宽。
 */
const SETTINGS_GROUP_CLASS =
  "flex h-9 w-full items-center divide-x divide-input overflow-hidden rounded-lg border border-input bg-background sm:w-auto";

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
 * Compact toolbar above the textarea: mode (the one filled element) + a single
 * grouped control holding the per-request settings (knowledge base, model,
 * reasoning effort).
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
  const kbCount = knowledgeBaseIds.length;
  const kbLabel =
    kbCount === 0
      ? "知识库"
      : kbCount === 1
        ? (selectedNames[0] ?? "已选 1 个")
        : `已选 ${kbCount} 个`;
  const kbTitle =
    kbCount === 0
      ? "为本轮请求绑定知识库（可多选）"
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
      <ChatModeSelector className="shrink-0" />

      {!isHermes && (
        <div role="group" aria-label="本轮请求设置" className={SETTINGS_GROUP_CLASS}>
          {hasKbs && (
            <DropdownMenu>
              <DropdownMenuTrigger asChild>
                <Button
                  variant="ghost"
                  size="sm"
                  className={cn(GROUP_ITEM_CLASS, "max-w-[180px]")}
                  title={kbTitle}
                >
                  <Database className="h-4 w-4 shrink-0" />
                  {/* 没绑库时它是「可以绑」的提示，不是当前值——弱化处理，别让
                      空值和「测试知识库」长得一样重。 */}
                  <span
                    className={cn("truncate", kbCount === 0 && "text-muted-foreground")}
                  >
                    {kbLabel}
                  </span>
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
                    <DropdownMenuItem onSelect={() => onKnowledgeBaseIdsChange([])}>
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

          <AdvancedModelSelector
            value={modelId}
            onChange={onModelChange}
            mimes={attachmentMimes}
            className={cn(GROUP_ITEM_CLASS, "max-w-[200px]")}
          />

          <ReasoningEffortSelectorStoreBridge modelId={modelId} />
        </div>
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
      className={cn(GROUP_ITEM_CLASS, "max-w-[140px]")}
    />
  );
}
