"use client";

import { useEffect, useState } from "react";

import { api } from "@/lib/api";
import { ApiError } from "@/lib/api-client";
import type { Locator } from "@/lib/locator";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetTitle,
} from "@/components/ui/sheet";
import { useHighlightDrawerStore } from "@/stores/highlight-drawer-store";
import { PageImageLocatorView } from "@/components/context/locators/page-image-locator";
import { SheetRangeLocatorView } from "@/components/context/locators/sheet-range-locator";
import { SlideLocatorView } from "@/components/context/locators/slide-locator";
import { TextRangeLocatorView } from "@/components/context/locators/text-range-locator";

/** 按 kind 分派渲染器。加新格式只需要加一个 case，不需要改协议。 */
function renderLocator(loc: Locator, documentId: string) {
  switch (loc.kind) {
    case "page_image":
      return <PageImageLocatorView loc={loc} documentId={documentId} />;
    case "sheet_range":
      return <SheetRangeLocatorView loc={loc} />;
    case "slide":
      return <SlideLocatorView loc={loc} />;
    case "text_range":
      return <TextRangeLocatorView loc={loc} />;
  }
}

/**
 * 引用原文定位抽屉（右侧侧边栏）。
 *
 * locator **每次打开都重新取**，不在 store 里缓存 —— 位置依赖当前解析器，
 * 缓存下来就会在文档被重新索引后指向旧位置。
 */
export function HighlightDrawer() {
  const open = useHighlightDrawerStore((s) => s.open);
  const documentId = useHighlightDrawerStore((s) => s.documentId);
  const chunkId = useHighlightDrawerStore((s) => s.chunkId);
  const title = useHighlightDrawerStore((s) => s.title);
  const close = useHighlightDrawerStore((s) => s.close);

  const [loc, setLoc] = useState<Locator | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  useEffect(() => {
    if (!open || !documentId || !chunkId) return;
    let cancelled = false;
    setLoc(null);
    setMessage(null);
    api
      .locateChunk(documentId, chunkId)
      .then((next) => {
        if (!cancelled) setLoc(next);
      })
      .catch((err: unknown) => {
        if (cancelled) return;
        // no_locator 是"这篇文档就是没有位置可给"，与"加载失败"是两件事，
        // 文案不能混：前者是事实，后者是故障。
        setMessage(
          err instanceof ApiError && err.code === "no_locator"
            ? "该文档无法定位到原文位置：原件格式不含版面坐标，或这个片段入库时没有记录位置。"
            : "原文位置加载失败，请稍后重试。"
        );
      });
    return () => {
      cancelled = true;
    };
  }, [open, documentId, chunkId]);

  return (
    <Sheet
      open={open}
      onOpenChange={(o) => {
        if (!o) close();
      }}
    >
      <SheetContent side="right" className="w-[92vw] gap-0 p-0 sm:max-w-[720px]">
        <SheetTitle className="border-b border-border px-4 py-3 pr-10 text-sm font-medium">
          <span className="line-clamp-1">{title || "原文位置"}</span>
        </SheetTitle>
        <SheetDescription className="sr-only">
          该引用片段在原始文档中的位置。
        </SheetDescription>
        <div className="min-h-0 flex-1 overflow-auto p-4">
          {message && <p className="text-sm text-muted-foreground">{message}</p>}
          {!message && !loc && (
            <p className="text-sm text-muted-foreground">正在定位原文…</p>
          )}
          {loc && documentId && renderLocator(loc, documentId)}
        </div>
      </SheetContent>
    </Sheet>
  );
}
