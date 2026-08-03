"use client";

import { useEffect } from "react";
import Link from "next/link";
import { Home, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";

/**
 * Global error boundary (must be a Client Component). Shows a friendly message
 * — the full stack is logged to the console, never shown to end users — with a
 * "重试" (reset) and a deterministic "返回对话" (Link to `/`).
 */
export default function Error({
  error,
  reset,
}: {
  error: Error & { digest?: string };
  reset: () => void;
}) {
  useEffect(() => {
    // Full detail for developers only.
    // eslint-disable-next-line no-console
    console.error(error);
  }, [error]);

  return (
    <div className="flex min-h-screen items-center justify-center bg-muted/30 px-4">
      <div className="flex max-w-md flex-col items-center gap-4 text-center">
        <h1 className="text-xl font-semibold">出错了</h1>
        <p className="text-sm text-muted-foreground">
          页面加载时发生了错误。你可以重试，或返回对话继续。
          {error.digest ? `（错误编号 ${error.digest}）` : ""}
        </p>
        <div className="flex flex-wrap items-center justify-center gap-2">
          <Button onClick={reset} aria-label="重试">
            <RotateCcw />
            重试
          </Button>
          <Button asChild variant="outline" aria-label="返回对话">
            <Link href="/">
              <Home />
              返回对话
            </Link>
          </Button>
        </div>
      </div>
    </div>
  );
}
