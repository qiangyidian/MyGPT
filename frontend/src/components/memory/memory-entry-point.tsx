"use client";

import { Brain, ChevronDown } from "lucide-react";

import { useUserMemories } from "@/hooks/useUserMemories";
import { userMemoryIsActive } from "@/lib/memories";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { MemoryManager } from "@/components/memory/memory-manager";

export function MemoryEntryPoint({
  open,
  onOpenChange,
  conversationId,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  conversationId: string | null;
}) {
  const { data: memories = [], isLoading } = useUserMemories();
  const activeCount = memories.filter(userMemoryIsActive).length;
  const candidateCount = memories.filter((memory) => !memory.active).length;

  return (
    <>
      <Button
        type="button"
        variant="outline"
        size="sm"
        className="h-8 gap-1.5 px-2.5 text-xs text-muted-foreground"
        onClick={() => onOpenChange(true)}
        aria-label={`管理长期记忆，已启用 ${activeCount} 条`}
        title="查看哪些长期记忆可用于回答，并管理它们"
      >
        <Brain className="h-3.5 w-3.5" />
        <span>记忆</span>
        {isLoading ? (
          <span className="text-muted-foreground/70">…</span>
        ) : (
          <span className="tabular-nums">{activeCount}</span>
        )}
        {candidateCount > 0 && (
          <span className="rounded-full bg-amber-500/10 px-1.5 py-0.5 text-[10px] text-amber-700 dark:text-amber-400">
            {candidateCount} 待确认
          </span>
        )}
      </Button>

      <Dialog open={open} onOpenChange={onOpenChange}>
        <DialogContent className="max-h-[92dvh] overflow-hidden p-0 sm:max-w-2xl">
          <DialogHeader className="border-b border-border px-5 py-4 pr-12 text-left sm:px-6">
            <DialogTitle className="flex items-center gap-2 text-base">
              <Brain className="h-4 w-4 text-primary" /> 对话记忆
            </DialogTitle>
            <DialogDescription>
              这里管理的记忆会跨对话生效。每条回答下方会标出实际注入的记忆。
            </DialogDescription>
          </DialogHeader>
          <div className="overflow-y-auto px-5 py-5 sm:px-6">
            <MemoryManager conversationId={conversationId} compact />
          </div>
        </DialogContent>
      </Dialog>
    </>
  );
}

interface MemorySnapshot {
  id: string;
  content: string;
  memory_type: string;
}

function memorySnapshots(value: unknown): MemorySnapshot[] {
  if (!Array.isArray(value)) return [];
  return value.filter(
    (entry): entry is MemorySnapshot =>
      !!entry &&
      typeof entry === "object" &&
      typeof (entry as MemorySnapshot).id === "string" &&
      typeof (entry as MemorySnapshot).content === "string" &&
      typeof (entry as MemorySnapshot).memory_type === "string",
  );
}

export function MemoryUsage({
  value,
  onManage,
}: {
  value: unknown;
  onManage: () => void;
}) {
  const memories = memorySnapshots(value);
  if (memories.length === 0) return null;

  return (
    <details className="group mt-3 w-full max-w-2xl rounded-lg border border-border/80 bg-muted/20 text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2.5 text-muted-foreground outline-none marker:hidden focus-visible:ring-2 focus-visible:ring-ring">
        <Brain className="h-3.5 w-3.5 shrink-0 text-primary" />
        <span className="flex-1 font-medium text-foreground">
          本条回答参考了 {memories.length} 条长期记忆
        </span>
        <ChevronDown className="h-3.5 w-3.5 transition-transform group-open:rotate-180" />
      </summary>
      <div className="border-t border-border/70 px-3 py-3">
        <ul className="space-y-2">
          {memories.map((memory) => (
            <li key={memory.id} className="rounded-md bg-background/70 px-2.5 py-2 leading-relaxed text-foreground">
              {memory.content}
            </li>
          ))}
        </ul>
        <p className="mt-2 text-[11px] leading-relaxed text-muted-foreground">
          这是生成本条回答时注入的记忆快照。你可以在记忆管理中修改或停用它们；修改不会改变已经生成的回答。
        </p>
        <Button variant="ghost" size="sm" className="mt-1 h-7 px-2 text-xs" onClick={onManage}>
          管理记忆
        </Button>
      </div>
    </details>
  );
}
