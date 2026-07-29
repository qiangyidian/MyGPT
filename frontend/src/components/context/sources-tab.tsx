"use client";

import { useEffect, useRef } from "react";
import { ExternalLink, FileText, Globe, Paperclip } from "lucide-react";

import { cn } from "@/lib/utils";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Badge } from "@/components/ui/badge";
import { useContextPanelStore } from "@/stores/context-panel-store";
import type { Citation } from "@/lib/types";

/** Qualitative relevance band — never a raw confidence percentage. */
function relevance(score?: number): { label: string; tone: string } {
  if (score == null) return { label: "相关", tone: "secondary" };
  if (score >= 0.75) return { label: "高相关", tone: "default" };
  if (score >= 0.5) return { label: "相关", tone: "secondary" };
  return { label: "一般", tone: "outline" };
}

function sourceIcon(c: Citation) {
  const t = c.source_type ?? (c.url ? "web" : "document");
  if (t === "web") return Globe;
  if (t === "attachment") return Paperclip;
  return FileText;
}

function domainOf(url?: string | null): string | null {
  if (!url) return null;
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return null;
  }
}

function SourceRow({ c, index, focused }: { c: Citation; index: number; focused: boolean }) {
  const Icon = sourceIcon(c);
  const rel = relevance(c.rerank_score ?? c.score);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (focused) ref.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [focused]);

  return (
    <div
      ref={ref}
      className={cn(
        "rounded-lg border border-border bg-card p-3",
        focused && "ring-2 ring-primary"
      )}
    >
      <div className="mb-1 flex items-center gap-2">
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          <span className="text-muted-foreground">[{index + 1}]</span> {c.document_name || domainOf(c.url) || "来源"}
        </span>
        <Badge variant={rel.tone as never} className="text-[10px]">{rel.label}</Badge>
      </div>
      {c.snippet && (
        <p className="line-clamp-4 text-xs leading-relaxed text-muted-foreground">{c.snippet}</p>
      )}
      <div className="mt-1.5 flex items-center gap-2 text-[11px] text-muted-foreground">
        {c.page_number != null && <span>第 {c.page_number} 页</span>}
        {c.url && (
          <a
            href={c.url}
            target="_blank"
            rel="noopener noreferrer"
            className="inline-flex items-center gap-0.5 hover:text-foreground"
          >
            {domainOf(c.url)} <ExternalLink className="h-3 w-3" />
          </a>
        )}
      </div>
    </div>
  );
}

export function SourcesTab() {
  const sources = useContextPanelStore((s) => s.sources);
  const focusIndex = useContextPanelStore((s) => s.focusSourceIndex);

  if (!sources.length) {
    return (
      <div className="flex flex-col items-center justify-center gap-2 py-12 text-center text-xs text-muted-foreground">
        <FileText className="h-5 w-5" />
        <span>暂无来源。引用知识库或联网搜索后，这里会列出可核实的来源。</span>
      </div>
    );
  }

  return (
    <ScrollArea className="h-full">
      <div className="space-y-2 p-3">
        {sources.map((c, i) => (
          <SourceRow key={`${c.document_id ?? c.url}-${i}`} c={c} index={i} focused={focusIndex === i} />
        ))}
      </div>
    </ScrollArea>
  );
}
