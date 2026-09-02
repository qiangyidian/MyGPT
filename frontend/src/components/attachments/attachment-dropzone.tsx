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
 * dropped anywhere over the conversation — not only on the composer — which is
 * the behaviour users expect from mainstream chat products. While a drag is
 * active a fixed overlay tells the user what will happen on release ("松开以上传").
 * Paste (images/files) and the labelled paperclip button remain the other two
 * upload affordances.
 */
export function AttachmentDropzone({
  onPick,
  disabled,
  className,
  children,
}: AttachmentDropzoneProps) {
  const [dragging, setDragging] = useState(false);
  const depthRef = useRef(0);

  // Document-level drag/drop. The counter (depth) pattern distinguishes a real
  // leave from moving between child elements, which each fire enter/leave.
  useEffect(() => {
    if (disabled) return;

    const hasFiles = (e: DragEvent) =>
      Array.from(e.dataTransfer?.types ?? []).includes("Files");

    const onDragOver = (e: DragEvent) => {
      if (hasFiles(e)) e.preventDefault(); // required to allow a drop
    };
    const onDragEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depthRef.current += 1;
      setDragging(true);
    };
    const onDragLeave = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depthRef.current = Math.max(0, depthRef.current - 1);
      if (depthRef.current === 0) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depthRef.current = 0;
      setDragging(false);
      if (e.dataTransfer?.files && e.dataTransfer.files.length > 0) {
        onPick(e.dataTransfer.files);
      }
    };

    document.addEventListener("dragover", onDragOver);
    document.addEventListener("dragenter", onDragEnter);
    document.addEventListener("dragleave", onDragLeave);
    document.addEventListener("drop", onDrop);
    return () => {
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("dragenter", onDragEnter);
      document.removeEventListener("dragleave", onDragLeave);
      document.removeEventListener("drop", onDrop);
    };
  }, [disabled, onPick]);

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
    <div className={cn("relative", className)} aria-label="附件拖放区域">
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
