"use client";

import { BrainCircuit } from "lucide-react";

import { useModels } from "@/hooks/useModels";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

const EFFORTS = [
  { value: "low", label: "快速（low）" },
  { value: "medium", label: "均衡（medium）" },
  { value: "high", label: "深入（high）" },
] as const;

/**
 * Reasoning-effort picker (B6): shown when the selected model (or, when
 * "默认模型" is chosen, ANY chat model) declares supports_reasoning_effort.
 * The value rides the chat request as `reasoning_effort`; the backend passes
 * it to the provider only for capable models and ignores it otherwise.
 */
export function ReasoningEffortSelector({
  modelId,
  value,
  onChange,
  className,
}: {
  modelId: string | null;
  value: "low" | "medium" | "high" | undefined;
  onChange: (v: "low" | "medium" | "high") => void;
  className?: string;
}) {
  const { chatModels } = useModels();

  const capable = modelId
    ? chatModels.some((m) => m.id === modelId && m.supports_reasoning_effort)
    : chatModels.some((m) => m.supports_reasoning_effort);
  if (!capable) return null;

  return (
    <Select
      value={value ?? "medium"}
      onValueChange={(v) => onChange(v as "low" | "medium" | "high")}
    >
      <SelectTrigger className={className} aria-label="推理力度">
        <BrainCircuit className="h-4 w-4 shrink-0" />
        <SelectValue />
      </SelectTrigger>
      {/* Composer sits at the bottom of the chat column — always open upward. */}
      <SelectContent side="top">
        {EFFORTS.map((e) => (
          <SelectItem key={e.value} value={e.value}>
            {e.label}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}
