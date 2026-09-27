"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";

import { Button } from "@/components/ui/button";

interface BackLinkProps {
  /** Destination path (already validated/sanitised by the caller). */
  href: string;
  /** Visible + accessible label, e.g. "返回对话". */
  label: string;
  variant?: "ghost" | "outline" | "secondary";
}

/**
 * Presentational "back" affordance: a deterministic internal `<Link>` rendered
 * as a ghost button with an arrow icon. Uses `size="default"` (h-10) so the
 * touch target stays ≥40px on mobile. Pure — no hooks.
 */
export function BackLink({ href, label, variant = "ghost" }: BackLinkProps) {
  return (
    <Button asChild variant={variant} size="default" aria-label={label}>
      <Link href={href}>
        <ArrowLeft />
        <span className="hidden sm:inline">{label}</span>
        <span className="sm:hidden">返回</span>
      </Link>
    </Button>
  );
}
