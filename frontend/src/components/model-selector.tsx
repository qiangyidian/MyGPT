"use client";

import { Loader2 } from "lucide-react";

import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { useModels } from "@/hooks/useModels";
import { filterModelsByModality } from "@/lib/multimodal";
import { cn } from "@/lib/utils";

interface ModelSelectorProps {
  value: string | null;
  onChange: (modelId: string | null) => void;
  className?: string;
  /**
   * Attachment mime types currently in the composer. When provided, the dropdown
   * filters to models that support every requested modality (image → vision,
   * audio → audio_input). Undefined / empty → all chat models (default path).
   */
  mimes?: string[];
}

/**
 * A dropdown that lists chat-capable (non-embedding) models. When `mimes` is
 * provided, only models whose capability flags support every requested
 * modality are listed (Task 12 multimodal composer).
 */
export function ModelSelector({
  value,
  onChange,
  className,
  mimes,
}: ModelSelectorProps) {
  const { chatModels, isLoading } = useModels();

  const filtered =
    mimes && mimes.length > 0 ? filterModelsByModality(chatModels, mimes) : chatModels;

  const handleSelect = (val: string) => {
    onChange(val === "__none__" ? null : val);
  };

  if (isLoading) {
    return (
      <div
        className={cn(
          "flex h-9 items-center gap-2 rounded-md border border-input bg-background px-3 text-sm text-muted-foreground",
          className
        )}
      >
        <Loader2 className="h-3.5 w-3.5 animate-spin" />
        加载模型...
      </div>
    );
  }

  return (
    <Select
      value={value ?? "__none__"}
      onValueChange={handleSelect}
    >
      <SelectTrigger className={cn("w-[200px] font-medium", className)}>
        <SelectValue placeholder="选择模型" />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value="__none__">默认模型</SelectItem>
        {filtered.map((m) => (
          <SelectItem key={m.id} value={m.id}>
            {m.name}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
