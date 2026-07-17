"use client";

import { useState } from "react";
import { ChevronDown, FileText } from "lucide-react";

import { cn } from "@/lib/utils";
import type { Citation } from "@/lib/types";

function CitationChip({ citation, index }: { citation: Citation; index: number }) {
  const [expanded, setExpanded] = useState(false);

  const scorePct = Math.round((citation.score ?? 0) * 100);

  return (
    <div className="rounded-md border border-border bg-card text-card-foreground">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-accent/50"
        onClick={() => setExpanded((v) => !v)}
      >
        <FileText className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate font-medium">
          <span className="text-muted-foreground">[{index + 1}]</span>{" "}
          {citation.document_name}
        </span>
        <span className="shrink-0 text-muted-foreground">{scorePct}%</span>
        <ChevronDown
          className={cn(
            "h-3.5 w-3.5 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-180"
          )}
        />
      </button>
      {expanded && (
        <div className="border-t border-border px-3 py-2">
          <p className="text-xs leading-relaxed text-muted-foreground">
            {citation.snippet}
          </p>
        </div>
      )}
    </div>
  );
}

/**
 * Renders an array of Citation objects as expandable source chips.
 * Each chip shows the document name, relevance score, and an expandable
 * snippet of the matched text.
 */
export function Citations({ citations }: { citations: Citation[] }) {
  if (!citations.length) return null;

  return (
    <div className="mt-3 space-y-1.5">
      <p className="text-xs font-medium text-muted-foreground">引用来源</p>
      <div className="space-y-1.5">
        {citations.map((c, i) => (
          <CitationChip key={`${c.document_id}-${c.chunk_index}-${i}`} citation={c} index={i} />
        ))}
      </div>
    </div>
  );
}
