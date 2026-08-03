"use client";

import { useEffect, useState, type ReactNode } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";

import { cn } from "@/lib/utils";
import { useAuth } from "@/hooks/useAuth";
import { buildLoginUrl, buildReturnTo, resolveChatHome, resolveReturnTo } from "@/lib/navigation";
import { PageHeader, type BreadcrumbItem } from "./page-header";
import { CenteredLoading } from "./page-loading";

export type { BreadcrumbItem };

export interface AppPageShellProps {
  title: string;
  description?: string;
  breadcrumbs?: BreadcrumbItem[];
  actions?: ReactNode;
  /** Secondary deterministic back link, e.g. { href, label: "返回知识库" }. */
  secondaryBack?: { href: string; label: string };
  returnLabel?: string;
  /** When true, authenticated non-admins are redirected to a safe internal page. */
  requireAdmin?: boolean;
  /** Tailwind max-width class for the content column (default `max-w-6xl`). */
  maxWidthClassName?: string;
  /** Extra classes for the body wrapper. */
  bodyClassName?: string;
  children: ReactNode;
}

/**
 * Unified standalone-page shell: consistent background, max width, responsive
 * padding, a deterministic "返回对话" affordance, breadcrumbs/title/actions, and
 * — critically — a single auth gate (loading → not-logged-in → optional admin
 * check) so no page re-implements login/role redirects.
 *
 * Auth uses `useAuth` (the canonical `["auth","me"]` cache), not a divergent
 * `["me"]` query. A `mounted` gate is preserved to avoid `useAuth`'s
 * client-only token check causing SSR/client hydration mismatches.
 *
 * Because it reads `useSearchParams`, every page rendering it must wrap it in
 * `<NavSuspense>`.
 */
export function AppPageShell({
  title,
  description,
  breadcrumbs,
  actions,
  secondaryBack,
  returnLabel,
  requireAdmin = false,
  maxWidthClassName = "max-w-6xl",
  bodyClassName,
  children,
}: AppPageShellProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { user, isLoading } = useAuth();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // "返回对话" always targets the chat home (preserving the conversation id when
  // the return target is home-based), so it can never land on a non-existent
  // route (404).
  const returnHref = resolveChatHome(searchParams);

  // Not logged in → /login, carrying current location as `next`.
  useEffect(() => {
    if (!mounted || isLoading) return;
    if (!user) {
      router.replace(buildLoginUrl(buildReturnTo(pathname, searchParams)));
    }
  }, [mounted, isLoading, user, pathname, searchParams, router]);

  // Logged in but lacking the required admin role → safe internal fallback.
  useEffect(() => {
    if (!mounted || isLoading || !user || !requireAdmin) return;
    if (user.role !== "admin") {
      let target = resolveReturnTo(searchParams, "/");
      // Never bounce to the very page we're on (would loop).
      if (target === pathname) target = "/";
      router.replace(target);
    }
  }, [mounted, isLoading, user, requireAdmin, searchParams, pathname, router]);

  if (!mounted || isLoading || !user) {
    return <CenteredLoading />;
  }
  if (requireAdmin && user.role !== "admin") {
    return <CenteredLoading />;
  }

  return (
    <div className="min-h-screen bg-muted/30">
      <div
        className={cn(
          "container mx-auto flex flex-col gap-6 px-4 py-6 md:py-8",
          maxWidthClassName
        )}
      >
        <PageHeader
          title={title}
          description={description}
          breadcrumbs={breadcrumbs}
          actions={actions}
          returnHref={returnHref}
          returnLabel={returnLabel}
          secondaryBack={secondaryBack}
        />
        <div className={cn("min-w-0 flex-1", bodyClassName)}>{children}</div>
      </div>
    </div>
  );
}
