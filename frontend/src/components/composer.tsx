"use client";

import { type KeyboardEvent, useRef, useState } from "react";
import { Globe, Paperclip, Send, Square } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { ModelSelector } from "@/components/model-selector";
import type { KnowledgeBase } from "@/lib/types";

interface ComposerProps {
  onSend: (
    content: string,
    opts: { enableTools: boolean; executionMode?: "auto" | "chat" | "agent" }
  ) => void;
  onStop: () => void;
  isStreaming: boolean;
  modelId: string | null;
  onModelChange: (modelId: string | null) => void;
  knowledgeBaseId: string | null;
  onKnowledgeBaseChange: (kbId: string | null) => void;
  knowledgeBases?: KnowledgeBase[];
  className?: string;
}

export function Composer({
  onSend,
  onStop,
  isStreaming,
  modelId,
  onModelChange,
  knowledgeBaseId,
  onKnowledgeBaseChange,
  knowledgeBases,
  className,
}: ComposerProps) {
  const [value, setValue] = useState("");
  const [deepSearch, setDeepSearch] = useState(false);
  // "auto" = native decides; "agent" = force the CrewAI runtime (when enabled).
  const [executionMode, setExecutionMode] = useState<"auto" | "agent">("auto");
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter to send, Shift+Enter for newline. Ignore during IME composition.
    if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      handleSend();
    }
  };

  const handleSend = () => {
    const trimmed = value.trim();
    if (!trimmed || isStreaming) return;
    onSend(trimmed, {
      enableTools: deepSearch,
      executionMode: deepSearch ? executionMode : "auto",
    });
    setValue("");
    // Reset textarea height.
    if (textareaRef.current) {
      textareaRef.current.style.height = "";
    }
  };

  // Auto-grow textarea up to 200px.
  const handleInput = () => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };

  return (
    <div className={cn("border-t bg-background", className)}>
      <div className="mx-auto w-full max-w-3xl px-4 py-3">
        {/* Selectors row */}
        <div className="mb-2 flex flex-wrap items-center gap-2">
          <ModelSelector value={modelId} onChange={onModelChange} />

          {/* Deep search / agent toggle: enables multi-round tool use + steps */}
          <Button
            type="button"
            variant={deepSearch ? "default" : "outline"}
            size="sm"
            className="h-9 gap-1.5 text-sm font-medium"
            onClick={() => setDeepSearch((v) => !v)}
            title="开启后，AI 会多轮调用工具并展示执行过程（需模型支持工具调用）"
          >
            <Globe className="h-4 w-4" />
            深度搜索
          </Button>

          {/* Execution runtime selector — only relevant in deep-search mode.
              "auto" = native loop; "agent" = CrewAI runtime (when enabled). */}
          {deepSearch && (
            <Select
              value={executionMode}
              onValueChange={(v) => setExecutionMode(v as "auto" | "agent")}
            >
              <SelectTrigger className="h-9 w-[120px] text-sm font-medium">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="auto">原生 (auto)</SelectItem>
                <SelectItem value="agent">CrewAI (agent)</SelectItem>
              </SelectContent>
            </Select>
          )}

          {/* Knowledge base selector — only shown if KBs exist */}
          {knowledgeBases && knowledgeBases.length > 0 && (
            <Select
              value={knowledgeBaseId ?? "__none__"}
              onValueChange={(v) =>
                onKnowledgeBaseChange(v === "__none__" ? null : v)
              }
            >
              <SelectTrigger className="h-9 w-[160px] text-sm font-medium">
                <SelectValue placeholder="知识库" />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="__none__">不使用知识库</SelectItem>
                {knowledgeBases.map((kb) => (
                  <SelectItem key={kb.id} value={kb.id}>
                    {kb.name}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
        </div>

        {/* Input row */}
        <div className="flex items-end gap-2 rounded-xl border border-input bg-background p-2 shadow-sm focus-within:ring-2 focus-within:ring-ring focus-within:ring-offset-2">
          {/* Attach button (P1 hook — disabled visually for now) */}
          <Button
            variant="ghost"
            size="icon"
            className="h-8 w-8 shrink-0 text-muted-foreground"
            disabled
            title="附件功能即将上线"
          >
            <Paperclip className="h-4 w-4" />
          </Button>

          <Textarea
            ref={textareaRef}
            value={value}
            onChange={(e) => {
              setValue(e.target.value);
              handleInput();
            }}
            onKeyDown={handleKeyDown}
            placeholder="输入消息... (Enter 发送, Shift+Enter 换行)"
            className="min-h-[40px] flex-1 resize-none border-0 bg-transparent px-1 py-1.5 focus-visible:ring-0 focus-visible:ring-offset-0"
            rows={1}
          />

          {isStreaming ? (
            <Button
              variant="destructive"
              size="icon"
              className="h-8 w-8 shrink-0"
              onClick={onStop}
              title="停止生成"
            >
              <Square className="h-4 w-4" />
            </Button>
          ) : (
            <Button
              size="icon"
              className="h-8 w-8 shrink-0"
              onClick={handleSend}
              disabled={!value.trim()}
              title="发送"
            >
              <Send className="h-4 w-4" />
            </Button>
          )}
        </div>

        <p className="mt-1.5 text-center text-[11px] text-muted-foreground">
          AI 生成的内容可能存在错误，请核实重要信息。
        </p>
      </div>
    </div>
  );
}
