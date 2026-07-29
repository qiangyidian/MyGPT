"use client";

import { useRef } from "react";
import { Paperclip } from "lucide-react";

import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@/components/ui/tooltip";

interface AttachmentPickerProps {
  onPick: (files: FileList | File[]) => void;
  disabled?: boolean;
  className?: string;
}

/**
 * The paperclip button: opens the native file picker. Drag-drop + paste are
 * handled by the surrounding dropzone; this ensures keyboard-only users still
 * have a reachable upload affordance (a real <input type=file>, labelled).
 */
export function AttachmentPicker({ onPick, disabled, className }: AttachmentPickerProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <TooltipProvider delayDuration={300}>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            type="button"
            variant="ghost"
            size="icon"
            className={cn("h-8 w-8 shrink-0 text-muted-foreground", className)}
            disabled={disabled}
            onClick={() => inputRef.current?.click()}
            aria-label="添加附件"
          >
            <Paperclip className="h-4 w-4" />
          </Button>
        </TooltipTrigger>
        <TooltipContent side="top">添加附件（可拖拽或粘贴）</TooltipContent>
      </Tooltip>
      <input
        ref={inputRef}
        type="file"
        multiple
        className="sr-only"
        tabIndex={-1}
        aria-hidden="true"
        onChange={(e) => {
          if (e.target.files && e.target.files.length > 0) {
            onPick(e.target.files);
          }
          // reset so picking the same file again still fires onChange
          e.target.value = "";
        }}
      />
    </TooltipProvider>
  );
}
