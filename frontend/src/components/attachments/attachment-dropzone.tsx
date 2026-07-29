"use client";

import { type ReactNode, useCallback, useEffect, useRef, useState } from "react";

import { cn } from "@/lib/utils";

interface AttachmentDropzoneProps {
  onPick: (files: FileList | File[]) => void;
  /** Disable drop/paste (e.g. while no conversation exists). */
  disabled?: boolean;
  className?: string;
  children: ReactNode;
}

/**
 * Wraps the composer input region to accept drag-drop files and pasted images.
 * Drop and paste are additive affordances — the labelled paperclip button
 * remains the keyboard-accessible path (see AttachmentPicker).
 */
export function AttachmentDropzone({
  onPick,
  disabled,
  className,
  children,
}: AttachmentDropzoneProps) {
  const [dragging, setDragging] = useState(false);
  const depthRef = useRef(0);

  const onDragOver = useCallback((e: React.DragEvent) => {
    if (disabled) return;
    if (Array.from(e.dataTransfer.types).includes("Files")) {
      e.preventDefault();
    }
  }, [disabled]);

  const onDragEnter = useCallback((e: React.DragEvent) => {
    if (disabled) return;
    if (Array.from(e.dataTransfer.types).includes("Files")) {
      depthRef.current += 1;
      setDragging(true);
    }
  }, [disabled]);

  const onDragLeave = useCallback((e: React.DragEvent) => {
    if (disabled) return;
    depthRef.current = Math.max(0, depthRef.current - 1);
    if (depthRef.current === 0) setDragging(false);
  }, [disabled]);

  const onDrop = useCallback(
    (e: React.DragEvent) => {
      if (disabled) return;
      depthRef.current = 0;
      setDragging(false);
      if (e.dataTransfer.files && e.dataTransfer.files.length > 0) {
        e.preventDefault();
        onPick(e.dataTransfer.files);
      }
    },
    [disabled, onPick]
  );

  // Paste handler attached to the wrapper (focusable area).
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
      onDragOver={onDragOver}
      onDragEnter={onDragEnter}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={cn(
        "relative",
        dragging && "ring-2 ring-primary ring-offset-2 ring-offset-background rounded-xl",
        className
      )}
      aria-label="附件拖放区域"
    >
      {children}
    </div>
  );
}
