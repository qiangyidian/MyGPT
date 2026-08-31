"use client";

/**
 * Tiny external store for the artifact preview panel.
 *
 * Inline artifact handles render deep inside message bubbles; threading an
 * `onPreview` callback up through every level would touch the whole message
 * tree. Instead, any handle calls `openArtifactPreview(id)` and the single
 * `<ArtifactPreviewPanel />` mounted on the chat page subscribes here.
 */
import { useSyncExternalStore } from "react";

let currentArtifactId: string | null = null;
const listeners = new Set<() => void>();

function subscribe(listener: () => void) {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function getSnapshot(): string | null {
  return currentArtifactId;
}

export function openArtifactPreview(artifactId: string) {
  currentArtifactId = artifactId;
  listeners.forEach((l) => l());
}

export function closeArtifactPreview() {
  currentArtifactId = null;
  listeners.forEach((l) => l());
}

export function useArtifactPreviewId(): string | null {
  return useSyncExternalStore(subscribe, getSnapshot, getSnapshot);
}
