"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { ArrowLeft, Home } from "lucide-react";

import { isInAppReferrer } from "@/lib/navigation";
import { Button } from "@/components/ui/button";

/**
 * Global 404. Always offers a deterministic "返回对话" (Link to `/`) so the page
 * is never a dead end; the secondary "返回上一页" falls back to `/` when there is
 * no in-app history (never leaves the site via `router.back()`).
 */
export default function NotFound() {
  const router = useRouter();
  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 px-4">
      <div className="flex max-w-md flex-col items-center gap-4 text-center">
        <p className="text-5xl font-bold text-muted-foreground">404</p>
        <h1 className="text-xl font-semibold">页面不存在</h1>
        <p className="text-sm text-muted-foreground">
          你访问的页面可能已被移除，或者地址有误。
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2">
          <Button asChild aria-label="返回对话">
            <Link href="/">
              <Home />
              返回对话
            </Link>
          </Button>
          <Button
            variant="outline"
            aria-label="返回上一页"
            onClick={() => {
              if (typeof window === "undefined") return;
              // Only go back when the previous entry is provably in-app
              // (same-origin referrer + history); otherwise stay in the app.
              if (
                isInAppReferrer(document.referrer, window.location.origin) &&
                window.history.length > 1
              ) {
                router.back();
              } else {
                router.push("/");
              }
            }}
          >
            <ArrowLeft />
            返回上一页
          </Button>
        </div>
      </div>
    </div>
  );
}
