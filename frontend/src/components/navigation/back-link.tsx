"use client";

import Link from "next/link";
import { ArrowLeft } from "lucide-react";
import { useSearchParams } from "next/navigation";

import { Button } from "@/components/ui/button";
import { resolveReturnTo } from "@/lib/navigation";

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

interface ReturnToLinkProps {
  /** Visible + accessible label. */
  label?: string;
  /** Fallback when no valid `returnTo`/`next` param is present. */
  fallback?: string;
  variant?: "ghost" | "outline" | "secondary";
}

/**
 * "Return to chat" button that resolves its destination from the `returnTo`
 * (then `next`) query param via the shared open-redirect guard. Must be used
 * inside a `<NavSuspense>` boundary because it reads `useSearchParams`.
 */
export function ReturnToLink({
  label = "返回对话",
  fallback = "/",
  variant = "ghost",
}: ReturnToLinkProps) {
  const searchParams = useSearchParams();
  const href = resolveReturnTo(searchParams, fallback);
  return <BackLink href={href} label={label} variant={variant} />;
}
