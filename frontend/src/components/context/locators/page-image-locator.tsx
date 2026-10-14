"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import type { PageImageLocator } from "@/lib/locator";
import { HighlightOverlay } from "@/components/context/locators/highlight-overlay";

/**
 * 原页底图 + 归一化高亮层。
 *
 * 底图是干净的整页图（后端渲染时**不画框**），框是独立的一层。这样同一页被
 * 多个引用命中时图只取一次，缩放时框自动跟随。
 *
 * 图必须走 blob 而不是 `<img src="…/image">`：端点要 Bearer token，而 img 标签
 * 带不上 Authorization 头，直接写 src 会 401。
 */
export function PageImageLocatorView({
  loc,
  documentId,
}: {
  loc: PageImageLocator;
  documentId: string;
}) {
  const [url, setUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    let created: string | null = null;
    setUrl(null);
    setError(null);
    api
      .documentPageImage(documentId, loc.page)
      .then((blob) => {
        if (cancelled) return;
        created = URL.createObjectURL(blob);
        setUrl(created);
      })
      .catch(() => {
        if (!cancelled) setError("原页渲染失败，无法显示底图");
      });
    return () => {
      cancelled = true;
      // objectURL 不释放就是一次内存泄漏，而抽屉会被反复开关。
      if (created) URL.revokeObjectURL(created);
    };
  }, [documentId, loc.page]);

  return (
    <div className="space-y-2">
      <p className="text-[11px] text-muted-foreground">
        第 {loc.page} 页 · 共 {loc.total_pages} 页
      </p>
      <div className="relative w-full overflow-hidden rounded-lg border border-border bg-white">
        {error ? (
          <p className="p-6 text-center text-xs text-muted-foreground">{error}</p>
        ) : (
          <>
            {url ? (
              <img src={url} alt={`第 ${loc.page} 页`} className="block w-full" />
            ) : (
              <div className="aspect-[1/1.414] w-full animate-pulse bg-muted" />
            )}
            {url && <HighlightOverlay boxes={loc.boxes} />}
          </>
        )}
      </div>
    </div>
  );
}
