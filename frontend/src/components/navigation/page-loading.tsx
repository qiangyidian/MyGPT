"use client";

import { Suspense, type ReactNode } from "react";

/** Centered, branded loading placeholder reused across navigation surfaces. */
export function CenteredLoading({ label = "加载中…" }: { label?: string }) {
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30">
      <p className="text-sm text-muted-foreground">{label}</p>
    </div>
  );
}

/**
 * Suspense boundary that every page/component using `useSearchParams` must be
 * wrapped in. Next.js 14 fails `next build` ("useSearchParams() should be
 * wrapped in a suspense boundary") otherwise.
 */
export function NavSuspense({
  children,
  fallback,
}: {
  children: ReactNode;
  fallback?: ReactNode;
}) {
  return <Suspense fallback={fallback ?? <CenteredLoading />}>{children}</Suspense>;
}
