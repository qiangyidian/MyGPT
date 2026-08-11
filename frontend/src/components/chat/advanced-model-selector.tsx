"use client";

import { ModelSelector } from "@/components/model-selector";

/**
 * The model selector shown in the composer toolbar. Always visible so the user
 * can pick a chat-capable model; selecting "默认模型" (null) lets the backend
 * choose. Forwards `mimes` so the dropdown filters to models that support the
 * requested modalities (Task 12 multimodal composer).
 */
export function AdvancedModelSelector({
  value,
  onChange,
  mimes,
}: {
  value: string | null;
  onChange: (id: string | null) => void;
  mimes?: string[];
}) {
  return <ModelSelector value={value} onChange={onChange} mimes={mimes} />;
}
