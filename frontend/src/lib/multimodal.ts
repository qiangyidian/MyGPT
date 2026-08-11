/**
 * Modality-aware model selection for the multimodal composer (Task 12).
 *
 * The composer attaches typed parts (image / audio / file). The model dropdown
 * must filter to models whose capability flags support EVERY attached
 * modality, so a request can never be sent to a model that cannot accept it.
 */

import type { ModelConfig } from "./types";

export type Modality = "text" | "image" | "audio" | "file";

/** Classify a single mime type into a high-level modality. */
export function modalityFromMime(mime: string): Modality {
  const lower = (mime || "").toLowerCase();
  if (lower.startsWith("image/")) return "image";
  if (lower.startsWith("audio/")) return "audio";
  // Video carries both an image and an audio track; treat as image for the
  // purposes of model capability (vision models accept video frames).
  if (lower.startsWith("video/")) return "image";
  return "file";
}

/**
 * The unique, ordered set of modalities required for a set of attachment mime
 * types. `text` is never required (always supported) so it's omitted.
 */
export function requiredModalitiesFor(mimes: string[]): Modality[] {
  const order: Modality[] = ["image", "audio", "file"];
  const present = new Set(mimes.map(modalityFromMime));
  return order.filter((m) => present.has(m));
}

/** True when a single model can serve every required modality. */
export function modelSupportsModalities(
  model: ModelConfig,
  required: Modality[],
): boolean {
  for (const r of required) {
    if (r === "image" && !model.supports_vision) return false;
    if (r === "audio" && !model.supports_audio_input) return false;
    // text + file are always supported (delivered as text/attachment).
  }
  return true;
}

/**
 * Filter a list of models to the chat-capable (non-embedding) models that
 * support every required modality. An empty `mimes` list returns all
 * non-embedding models (the default text-only path).
 */
export function filterModelsByModality(
  models: ModelConfig[],
  mimes: string[],
): ModelConfig[] {
  const required = requiredModalitiesFor(mimes);
  return models
    .filter((m) => !m.is_embedding)
    .filter((m) => modelSupportsModalities(m, required));
}
