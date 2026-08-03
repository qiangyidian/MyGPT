"use client";

import Link from "next/link";
import { usePathname, useSearchParams, useRouter } from "next/navigation";
import { Suspense, useEffect, useState, type ReactNode } from "react";
import { Boxes, Cpu } from "lucide-react";

import { cn } from "@/lib/utils";
import { buildLoginUrl, buildReturnTo, resolveChatHome, withReturnTo } from "@/lib/navigation";
import { useAuth } from "@/hooks/useAuth";
import { BackLink } from "@/components/navigation/back-link";
import { CenteredLoading } from "@/components/navigation/page-loading";

// Settings sections. Currently only "Models" is wired up; the rest are
// placeholders so the nav reflects the intended structure.
const NAV = [
  {
    label: "模型配置",
    href: "/settings/models",
    icon: Cpu,
    description: "管理 OpenAI 兼容 / Mock 模型",
    enabled: true,
  },
  {
    label: "知识库",
    href: "/settings/knowledge",
    icon: Boxes,
    description: "管理向量知识库与文档",
    enabled: false,
  },
] as const;

export default function SettingsLayout({ children }: { children: ReactNode }) {
  // useSearchParams (used below for returnTo) requires a Suspense boundary for
  // next build — wrap the whole gated shell so both the auth gate and the
  // sidebar are covered.
  return (
    <Suspense fallback={<CenteredLoading />}>
      <SettingsLayoutInner>{children}</SettingsLayoutInner>
    </Suspense>
  );
}

function SettingsLayoutInner({ children }: { children: ReactNode }) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const { user, isLoading } = useAuth();
  const [mounted, setMounted] = useState(false);
  useEffect(() => setMounted(true), []);

  // Auth gate (consistent with KB/admin via AppPageShell): unauthenticated users
  // are sent to /login carrying the current location as `next`. The mounted gate
  // avoids useAuth's client-only token check causing a hydration mismatch.
  useEffect(() => {
    if (!mounted || isLoading) return;
    if (!user) router.replace(buildLoginUrl(buildReturnTo(pathname, searchParams)));
  }, [mounted, isLoading, user, pathname, searchParams, router]);

  if (!mounted || isLoading || !user) {
    return <CenteredLoading />;
  }

  return (
    <div className="min-h-screen bg-muted/30">
      <div className="container flex max-w-6xl flex-col gap-6 px-4 py-6 md:py-8 lg:flex-row">
        {/* Sidebar */}
        <aside className="lg:w-64 lg:shrink-0">
          <div className="sticky top-8 space-y-4">
            <SettingsSidebar pathname={pathname} searchParams={searchParams} />
          </div>
        </aside>

        {/* Content */}
        <main className="min-w-0 flex-1">{children}</main>
      </div>
    </div>
  );
}

function SettingsSidebar({
  pathname,
  searchParams,
}: {
  pathname: string;
  searchParams: URLSearchParams;
}) {
  // "返回对话" + forwarded returnTo always target the chat home (preserving the
  // conversation id), so they can never land on a non-existent route (404).
  const returnTo = resolveChatHome(searchParams);

  return (
    <>
      <div className="flex items-center justify-between gap-2">
        <h2 className="text-lg font-semibold tracking-tight">设置</h2>
        <BackLink href={returnTo} label="返回对话" />
      </div>
      <p className="text-sm text-muted-foreground">管理你的模型、知识库等配置。</p>

      <nav className="space-y-1">
        {NAV.map((item) => {
          const active = pathname === item.href || pathname.startsWith(item.href + "/");
          const Icon = item.icon;
          const inner = (
            <span
              className={cn(
                "flex items-start gap-3 rounded-md border px-3 py-2.5 text-sm transition-colors",
                active
                  ? "border-border bg-background text-foreground shadow-sm"
                  : "border-transparent text-muted-foreground hover:bg-background/60 hover:text-foreground",
                !item.enabled && "pointer-events-none opacity-50"
              )}
            >
              <Icon className="mt-0.5 h-4 w-4 shrink-0" />
              <span className="flex flex-col">
                <span className="font-medium">{item.label}</span>
                <span className="text-xs text-muted-foreground">{item.description}</span>
              </span>
            </span>
          );
          return item.enabled ? (
            <Link key={item.href} href={withReturnTo(item.href, returnTo)}>
              {inner}
            </Link>
          ) : (
            <div key={item.href} title="即将上线" aria-disabled="true">
              {inner}
            </div>
          );
        })}
      </nav>
    </>
  );
}
