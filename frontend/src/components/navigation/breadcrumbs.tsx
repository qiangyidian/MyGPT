"use client";

import Link from "next/link";
import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

export interface BreadcrumbItem {
  label: string;
  /** When omitted the item renders as the current (non-clickable) page. */
  href?: string;
}

/**
 * Accessible breadcrumb trail. The last item is the current page (no link,
 * `aria-current="page"`); earlier items with an `href` are internal links.
 */
export function Breadcrumbs({ items }: { items: BreadcrumbItem[] }) {
  if (!items.length) return null;
  return (
    <nav aria-label="面包屑">
      <ol className="flex flex-wrap items-center gap-0.5 text-sm text-muted-foreground">
        {items.map((item, i) => {
          const last = i === items.length - 1;
          const clickable = !!item.href && !last;
          return (
            <li key={`${item.label}-${i}`} className="flex items-center gap-0.5">
              {clickable ? (
                <Link
                  href={item.href as string}
                  className="rounded px-1.5 py-0.5 hover:bg-accent hover:text-foreground"
                >
                  {item.label}
                </Link>
              ) : (
                <span
                  aria-current={last ? "page" : undefined}
                  className={cn(
                    "rounded px-1.5 py-0.5",
                    last && "font-medium text-foreground"
                  )}
                >
                  {item.label}
                </span>
              )}
              {!last && <ChevronRight className="h-3.5 w-3.5 shrink-0" aria-hidden />}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
