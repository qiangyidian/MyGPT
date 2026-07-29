"use client";

import { ModelSelector } from "@/components/model-selector";

/**
 * The model selector shown in the composer toolbar. Always visible so the user
 * can pick a chat-capable model; selecting "默认模型" (null) lets the backend
 * choose.
 */
export function AdvancedModelSelector({
  value,
  onChange,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
}) {
  return <ModelSelector value={value} onChange={onChange} />;
}
