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
 * are NOT shown to end users as a confidence percentage — only a label.
 */
function relevanceLabel(score?: number): { label: string; tone: string } {
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
  const rel = relevanceLabel(citation.rerank_score ?? citation.score);

  return (
    <div className="flex items-center gap-1.5 rounded-md border border-border bg-card px-2.5 py-1.5">
      <button
        type="button"
        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
        onClick={() => onSourceClick?.(index)}
        title="查看来源详情"
      >
        <Icon className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-xs font-medium">
          <span className="text-muted-foreground">[{index + 1}]</span> {citation.document_name}
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
    <div className="mt-3 space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground">引用来源</p>
      <div className="grid grid-cols-1 gap-1.5 sm:grid-cols-2">
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
