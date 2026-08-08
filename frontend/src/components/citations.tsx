"use client";

import { FileText, Globe, Paperclip } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import type { Citation } from "@/lib/types";

function sourceIcon(c: Citation) {
  const t = c.source_type ?? (c.url ? "web" : "document");
  if (t === "web") return Globe;
  if (t === "attachment") return Paperclip;
  return FileText;
}

/**
 * Qualitative relevance band. Vector/rerank scores are debug/eval metrics and
 * are NOT shown to end users as a confidence percentage — only a label. Web
 * sources carry no rerank score, so they show neutral "相关" (score=0 is the
 * "no score" sentinel for web hits, not a low-relevance signal).
 */
function relevanceLabel(c: Citation): { label: string; tone: string } {
  if (c.source_type === "web") return { label: "相关", tone: "secondary" };
  const score = c.rerank_score ?? c.score;
  if (score == null) return { label: "相关", tone: "secondary" };
  if (score >= 0.75) return { label: "高相关", tone: "default" };
  if (score >= 0.5) return { label: "相关", tone: "secondary" };
  return { label: "一般", tone: "outline" };
}

function CitationChip({
  citation,
  index,
  onSourceClick,
}: {
  citation: Citation;
  index: number;
  onSourceClick?: (index: number) => void;
}) {
  const Icon = sourceIcon(citation);
  const rel = relevanceLabel(citation);

  return (
    <div className="flex items-center gap-2 rounded-lg border border-border/60 bg-transparent px-2.5 py-2 transition-colors hover:border-primary/30 hover:bg-muted/50 focus-within:border-primary/30 focus-within:bg-muted/50">
      <button
        type="button"
        className="flex min-w-0 flex-1 items-center gap-2 text-left"
        onClick={() => onSourceClick?.(index)}
        title="查看来源详情"
      >
        <span className="flex h-5 w-5 shrink-0 items-center justify-center rounded bg-muted text-muted-foreground">
          <Icon className="h-3 w-3" aria-hidden />
        </span>
        <span className="min-w-0 flex-1 truncate text-xs font-medium text-foreground">
          <span className="font-semibold text-indigo-600 dark:text-indigo-400">[{index + 1}]</span>{" "}
          {citation.document_name}
        </span>
      </button>
      <Badge variant={rel.tone as never} className="shrink-0 text-[10px]">{rel.label}</Badge>
    </div>
  );
}

/**
 * Renders citations as compact source chips. Clicking a chip opens the Sources
 * tab of the Context Panel focused on that source (via onSourceClick).
 */
export function Citations({
  citations,
  onSourceClick,
}: {
  citations: Citation[];
  onSourceClick?: (index: number) => void;
}) {
  if (!citations.length) return null;
  return (
    <div className="mt-4 space-y-2">
      <p className="text-[11px] font-semibold text-muted-foreground">引用来源</p>
      <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
        {citations.map((c, i) => (
          <CitationChip
            key={`${c.document_id ?? c.url}-${i}`}
            citation={c}
            index={i}
            onSourceClick={onSourceClick}
          />
        ))}
      </div>
    </div>
  );
}
