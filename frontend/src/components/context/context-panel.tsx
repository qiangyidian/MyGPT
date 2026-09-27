"use client";

import { useEffect, useState } from "react";
import { X } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetTitle,
  SheetDescription,
} from "@/components/ui/sheet";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useAgentRunGraph } from "@/hooks/useAgentRunGraph";
import { useAgentRunStore } from "@/stores/agent-run-store";
import { useContextPanel } from "@/hooks/useContextPanel";
import { useContextPanelStore } from "@/stores/context-panel-store";
import { ExecutionTab } from "@/components/context/execution-tab";
import { SourcesTab } from "@/components/context/sources-tab";
import { FilesTab } from "@/components/context/files-tab";
import type { ContextTab } from "@/lib/types";

function useIsDesktop() {
  const [isDesktop, setIsDesktop] = useState(false);
  useEffect(() => {
    const mql = window.matchMedia("(min-width: 1280px)");
    const onChange = () => setIsDesktop(mql.matches);
    onChange();
    mql.addEventListener("change", onChange);
    return () => mql.removeEventListener("change", onChange);
  }, []);
  return isDesktop;
}

const VISIBLE_TABS: { id: ContextTab; label: string }[] = [
  { id: "execution", label: "执行" },
  { id: "sources", label: "来源" },
  { id: "files", label: "文件" },
];

type RunPoll = ReturnType<typeof useAgentRunGraph>;

function PanelBody({
  conversationId,
  onClose,
  poll,
}: {
  conversationId: string | null;
  onClose: () => void;
  poll: RunPoll;
}) {
  const { tab, setTab } = useContextPanel();
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex items-center justify-between border-b border-border px-3 py-2">
        <span className="text-sm font-medium">上下文</span>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          onClick={onClose}
          aria-label="关闭上下文面板"
        >
          <X className="h-4 w-4" />
        </Button>
      </div>
      {/* 轮询停摆必须说出来：面板上停着的是一个早已过去的时刻，而它看起来和好着
          一样。给一条中文说明 + 一个立刻重试的出口。 */}
      {poll.pollStopped && (
        <div className="flex items-start justify-between gap-2 border-b border-border bg-amber-500/10 px-3 py-2 text-xs text-amber-700 dark:text-amber-400">
          <span>{poll.pollStoppedMessage}</span>
          <Button
            variant="ghost"
            size="sm"
            className="h-6 shrink-0 px-2 text-xs"
            onClick={poll.restartPoll}
          >
            重试
          </Button>
        </div>
      )}
      <Tabs value={tab} onValueChange={(v) => setTab(v as ContextTab)} className="flex min-h-0 flex-1 flex-col">
        <div className="px-3 pt-2">
          <TabsList className="grid w-full grid-cols-3">
            {VISIBLE_TABS.map((t) => (
              <TabsTrigger key={t.id} value={t.id} className="text-xs">
                {t.label}
              </TabsTrigger>
            ))}
          </TabsList>
        </div>
        <TabsContent value="execution" className="m-0 min-h-0 flex-1">
          <ExecutionTab />
        </TabsContent>
        <TabsContent value="sources" className="m-0 min-h-0 flex-1">
          <SourcesTab />
        </TabsContent>
        <TabsContent value="files" className="m-0 min-h-0 flex-1">
          <FilesTab conversationId={conversationId} />
        </TabsContent>
      </Tabs>
    </div>
  );
}

export function ContextPanel({ conversationId }: { conversationId: string | null }) {
  const open = useContextPanelStore((s) => s.open);
  const close = useContextPanelStore((s) => s.close);
  const activeRunId = useAgentRunStore((s) => s.active.runId);
  const isDesktop = useIsDesktop();
  // Drive the shared clock + poll fallback while mounted.
  const poll = useAgentRunGraph();

  const handleClose = () => close(activeRunId ?? undefined);

  if (isDesktop) {
    return (
      <aside
        aria-label="上下文面板"
        className={cn(
          "flex w-[400px] min-w-[340px] max-w-[480px] shrink-0 flex-col border-l border-border bg-card transition-[width,opacity] duration-200",
          open ? "opacity-100" : "w-0 min-w-0 overflow-hidden opacity-0"
        )}
      >
        {open && <PanelBody conversationId={conversationId} onClose={handleClose} poll={poll} />}
      </aside>
    );
  }

  return (
    <Sheet open={open} onOpenChange={(o) => { if (!o) handleClose(); }}>
      <SheetContent side="right" className="w-[88vw] p-0 sm:max-w-[460px]">
        <SheetTitle className="sr-only">上下文</SheetTitle>
        <SheetDescription className="sr-only">
          执行过程、来源与附件。
        </SheetDescription>
        <PanelBody conversationId={conversationId} onClose={handleClose} poll={poll} />
      </SheetContent>
    </Sheet>
  );
}
