// Pure-helper unit tests for Task 12 surfaces: artifact type guards + handles,
// modality-aware model selection, and user-memory opt-in defaults.

import { describe, expect, it } from "vitest";
import {
  findArtifactHandles,
  formatArtifactSize,
  isArtifactMeta,
  parseArtifactHandle,
  type ArtifactMeta,
} from "@/lib/artifacts";
import {
  filterModelsByModality,
  modalityFromMime,
  modelSupportsModalities,
  requiredModalitiesFor,
} from "@/lib/multimodal";
import {
  DEFAULT_USER_MEMORY_PROPOSE,
  userMemoryIsActive,
} from "@/lib/memories";
import type { ModelConfig, UserMemory } from "@/lib/types";

const artifact = (over: Partial<ArtifactMeta> = {}): ArtifactMeta => ({
  id: "11111111-1111-1111-1111-111111111111",
  media_type: "application/pdf",
  size: 1024,
  checksum: "sha256:abc",
  filename: "report.pdf",
  source: "tool_output",
  created_at: "2026-01-01T00:00:00Z",
  ...over,
});

const model = (over: Partial<ModelConfig>): ModelConfig => ({
  id: "m1",
  user_id: null,
  name: "M",
  provider: "openai-compatible",
  api_base_url: "",
  api_key_masked: "",
  has_key: true,
  model_name: "m",
  embedding_model_name: null,
  supports_stream: true,
  supports_tools: false,
  supports_parallel_tools: false,
  supports_vision: false,
  supports_audio_input: false,
  supports_audio_output: false,
  supports_image_generation: false,
  supports_structured_output: false,
  supports_reasoning_effort: false,
  output_token_parameter: "max_tokens",
  max_context_tokens: 8000,
  max_tokens: 4000,
  temperature: 0.7,
  top_p: 1,
  is_embedding: false,
  created_at: "t",
  ...over,
});

describe("isArtifactMeta — type guard", () => {
  it("accepts a well-formed artifact", () => {
    expect(isArtifactMeta(artifact())).toBe(true);
  });
  it("rejects missing required fields", () => {
    expect(isArtifactMeta({})).toBe(false);
    expect(isArtifactMeta({ id: "x" })).toBe(false);
    expect(isArtifactMeta(null)).toBe(false);
    expect(isArtifactMeta("artifact:123")).toBe(false);
  });
  it("rejects a non-numeric size", () => {
    expect(isArtifactMeta(artifact({ size: "big" as unknown as number }))).toBe(false);
  });
});

describe("parseArtifactHandle", () => {
  it("parses an artifact:<id> handle", () => {
    const id = "11111111-1111-1111-1111-111111111111";
    expect(parseArtifactHandle(`see artifact:${id} for details`)).toBe(id);
  });
  it("returns null when no handle is present", () => {
    expect(parseArtifactHandle("no references here")).toBeNull();
  });
});

describe("findArtifactHandles", () => {
  it("extracts every artifact handle in the text, in order", () => {
    const a = "11111111-1111-1111-1111-111111111111";
    const b = "22222222-2222-2222-2222-222222222222";
    expect(findArtifactHandles(`first artifact:${a} then artifact:${b}`)).toEqual([
      a,
      b,
    ]);
  });
  it("dedupes repeated handles", () => {
    const a = "11111111-1111-1111-1111-111111111111";
    expect(findArtifactHandles(`artifact:${a} artifact:${a}`)).toEqual([a]);
  });
});

describe("formatArtifactSize", () => {
  it("formats bytes / KB / MB / GB boundaries", () => {
    expect(formatArtifactSize(0)).toBe("0 B");
    expect(formatArtifactSize(512)).toBe("512 B");
    expect(formatArtifactSize(2048)).toBe("2.0 KB");
    expect(formatArtifactSize(5 * 1024 * 1024)).toBe("5.0 MB");
  });
});

describe("modality-aware model selection", () => {
  it("classifies image / audio / file modalities by mime", () => {
    expect(modalityFromMime("image/png")).toBe("image");
    expect(modalityFromMime("audio/wav")).toBe("audio");
    expect(modalityFromMime("application/pdf")).toBe("file");
    expect(modalityFromMime("text/plain")).toBe("file");
  });

  it("derives the unique set of required modalities for a set of attachments", () => {
    expect(
      requiredModalitiesFor(["image/png", "image/jpeg", "audio/mpeg", "application/pdf"]),
    ).toEqual(["image", "audio", "file"]);
  });

  it("modelSupportsModalities requires vision for image", () => {
    expect(modelSupportsModalities(model({ supports_vision: true }), ["image"])).toBe(true);
    expect(modelSupportsModalities(model({ supports_vision: false }), ["image"])).toBe(false);
  });

  it("modelSupportsModalities requires audio_input for audio", () => {
    expect(
      modelSupportsModalities(model({ supports_audio_input: true }), ["audio"]),
    ).toBe(true);
    expect(
      modelSupportsModalities(model({ supports_audio_input: false }), ["audio"]),
    ).toBe(false);
  });

  it("filterModelsByModality keeps only models that support every required modality", () => {
    const vision = model({ id: "vision", supports_vision: true });
    const audio = model({ id: "audio", supports_audio_input: true });
    const both = model({ id: "both", supports_vision: true, supports_audio_input: true });
    const neither = model({ id: "neither" });
    const out = filterModelsByModality(
      [vision, audio, both, neither],
      ["image/png", "audio/wav"],
    );
    expect(out.map((m) => m.id)).toEqual(["both"]);
  });

  it("filterModelsByModality returns all non-embedding models when nothing is attached", () => {
    const a = model({ id: "a" });
    const b = model({ id: "b", is_embedding: true });
    expect(filterModelsByModality([a, b], []).map((m) => m.id)).toEqual(["a"]);
  });
});

describe("user memory opt-in defaults", () => {
  const mkMemory = (over: Partial<UserMemory>): UserMemory => ({
    id: "mem-1",
    user_id: "u1",
    memory_type: "fact",
    content: "likes tea",
    structured_value: null,
    confidence: 0.5,
    active: false,
    confirmed_by_user: false,
    source_message_id: null,
    source_conversation_id: null,
    expires_at: null,
    embedding_id: null,
    created_at: "t",
    updated_at: "t",
    ...over,
  });

  it("a newly proposed user memory defaults to INACTIVE (opt-in)", () => {
    const propose = DEFAULT_USER_MEMORY_PROPOSE("likes coffee");
    expect(propose.active).toBe(false);
    expect(propose.memory_type).toBe("fact");
    expect(propose.confidence).toBe(0.5);
  });

  it("userMemoryIsActive is false for a fresh proposal and true after activation", () => {
    expect(userMemoryIsActive(mkMemory({ active: false }))).toBe(false);
    expect(userMemoryIsActive(mkMemory({ active: true }))).toBe(true);
  });

  it("an expired-but-active memory is treated as inactive", () => {
    const expired = mkMemory({
      active: true,
      expires_at: "2000-01-01T00:00:00Z", // in the past
    });
    expect(userMemoryIsActive(expired)).toBe(false);
  });
});
