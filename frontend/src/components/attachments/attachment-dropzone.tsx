"use client";

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";
import { FileUp } from "lucide-react";

import { cn } from "@/lib/utils";

interface AttachmentDropzoneProps {
  onPick: (files: FileList | File[]) => void;
  /** Disable drop/paste (e.g. while no conversation exists). */
  disabled?: boolean;
  className?: string;
  children: ReactNode;
}

/**
 * Full-screen drag-drop target for the chat page.
 *
 * The drop listeners live on ``document`` (not a wrapper div), so a file can be
 * dropped anywhere over the conversation — not only on the composer. While a
 * drag is active a fixed overlay tells the user what will happen on release.
 * Paste (images/files) and the labelled paperclip button remain the other two
 * upload affordances.
 *
 * Overlay lifecycle: dragover events fire continuously (~every 100-350ms)
 * while a real drag is in flight. A heartbeat timer watches for them — if
 * none arrive within the window the drag is over (dropped outside, cancelled
 * with Esc, or the browser cleared dataTransfer on the final dragleave, which
 * breaks naive enter/leave counters) and the overlay is torn down. This is
 * deliberately more robust than the depth-counter approach: a missed leave
 * event can no longer leave the overlay stuck on screen.
 */
export function AttachmentDropzone({
  onPick,
  disabled,
  className,
  children,
}: AttachmentDropzoneProps) {
  const [dragging, setDragging] = useState(false);
  const lastDragOverRef = useRef(0);

  const stopDragging = useCallback(() => {
    lastDragOverRef.current = 0;
    setDragging(false);
  }, []);

  // Document-level drag/drop + the heartbeat watchdog.
  useEffect(() => {
    if (disabled) {
      stopDragging();
      return;
    }

    const hasFiles = (e: DragEvent) =>
      Array.from(e.dataTransfer?.types ?? []).includes("Files");

    const onDragOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault(); // required to allow a drop
      lastDragOverRef.current = Date.now();
      setDragging(true);
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      stopDragging();
      if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
        onPick(e.dataTransfer.files);
      }
    };
    // dragend fires on the SOURCE element when a drag is cancelled (Esc) or
    // completes — belt-and-braces reset alongside the heartbeat.
    const onDragEnd = () => stopDragging();
    // Leaving the window entirely (relatedTarget null) ends the drag visually.
    const onDragLeave = (e: DragEvent) => {
      if (e.relatedTarget === null) stopDragging();
    };

    document.addEventListener("dragover", onDragOver);
    document.addEventListener("drop", onDrop);
    document.addEventListener("dragend", onDragEnd);
    document.addEventListener("dragleave", onDragLeave);

    // Watchdog: while `dragging` is on, require a recent dragover heartbeat.
    // Browsers throttle dragover to a few hundred ms max gap; 700ms covers the
    // slowest observed cadence with margin.
    const watchdog = window.setInterval(() => {
      if (lastDragOverRef.current === 0) return;
      if (Date.now() - lastDragOverRef.current > 700) stopDragging();
    }, 250);
    // Tab hidden (alt-tab mid-drag) — the drag is dead; clean up.
    const onVisibility = () => {
      if (document.hidden) stopDragging();
    };
    document.addEventListener("visibilitychange", onVisibility);

    return () => {
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("drop", onDrop);
      document.removeEventListener("dragend", onDragEnd);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("visibilitychange", onVisibility);
      window.clearInterval(watchdog);
    };
  }, [disabled, onPick, stopDragging]);

  // Paste handler (document level): files/images copied to the clipboard.
  useEffect(() => {
    if (disabled) return;
    const handler = (e: ClipboardEvent) => {
      const items = e.clipboardData?.items;
      if (!items) return;
      const files: File[] = [];
      for (const item of items) {
        if (item.kind === "file") {
          const f = item.getAsFile();
          if (f) files.push(f);
        }
      }
      if (files.length > 0) {
        e.preventDefault();
        onPick(files);
      }
    };
    document.addEventListener("paste", handler);
    return () => document.removeEventListener("paste", handler);
  }, [disabled, onPick]);

  return (
    <div
      className={cn(
        // Layout-transparent wrapper: this component's job is document-level
        // listeners + a fixed overlay, NOT layout. contents keeps the parent's
        // flex/grid treating children as direct items, so wrapping <main> can
        // never distort the page layout.
        "contents",
        className
      )}
      aria-label="附件拖放区域"
    >
      {children}
      {/* Full-screen drop hint overlay. pointer-events-none so the drag
          sequence is never interrupted; purely visual feedback. */}
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-[60] flex items-center justify-center bg-background/70 backdrop-blur-[2px]">
          <div className="mx-4 flex max-w-md flex-col items-center gap-3 rounded-2xl border-2 border-dashed border-primary/60 bg-card/95 px-10 py-8 shadow-xl">
            <span className="flex h-12 w-12 items-center justify-center rounded-full bg-primary/10 text-primary">
              <FileUp className="h-6 w-6" />
            </span>
            <p className="text-base font-semibold">松开以上传附件</p>
            <p className="text-center text-xs leading-relaxed text-muted-foreground">
              支持文档（PDF / Word / Excel / PPT / Markdown…）、图片、音频
              <br />
              上传后自动解析内容，发送时作为上下文
            </p>
          </div>
        </div>
      )}
    </div>
  );
}
