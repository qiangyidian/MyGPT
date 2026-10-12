import { describe, expect, it } from "vitest";

import { groupModelsForMenu } from "../model-groups";
import type { ModelConfig } from "@/lib/types";

function model(overrides: Partial<ModelConfig>): ModelConfig {
  return {
    id: overrides.id ?? Math.random().toString(36).slice(2),
    user_id: null,
    name: overrides.name ?? "",
    provider: overrides.provider ?? "openai-compatible",
    api_base_url: "https://example.com/v1",
    api_key_masked: "",
    has_key: true,
    model_name: overrides.model_name ?? "some-model",
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
    supports_thinking: false,
    output_token_parameter: "max_tokens",
    max_context_tokens: 131072,
    max_tokens: 8192,
    temperature: 0.7,
    top_p: 1,
    is_embedding: false,
    created_at: "2026-01-01T00:00:00Z",
    ...overrides,
  } as ModelConfig;
}

describe("groupModelsForMenu", () => {
  it("每个模型配置自成一个供应商节点，按名称分组", () => {
    const groups = groupModelsForMenu([
      model({ id: "m1", name: "opencode", model_name: "glm-5.3-flash" }),
      model({ id: "m2", name: "Hermes Agent", provider: "hermes", model_name: "hermes-agent" }),
    ]);

    expect(groups).toHaveLength(2);
    expect(groups.map((g) => g.providerName).sort()).toEqual(["Hermes Agent", "opencode"].sort());
    expect(groups.find((g) => g.providerName === "opencode")?.models[0].id).toBe("m1");
  });

  it("同名配置合并为同一供应商节点，模型共用 model_name（或取名兜底）", () => {
    const groups = groupModelsForMenu([
      model({ id: "m1", name: "opencode", model_name: "glm-5.3-flash" }),
      model({ id: "m2", name: "opencode", model_name: "deepseek-v4.1" }),
      model({ id: "m3", name: "Hermes Agent", provider: "hermes", model_name: "hermes-agent" }),
    ]);

    expect(groups).toHaveLength(2);
    const opencode = groups.find((g) => g.providerName === "opencode");
    expect(opencode?.models.map((m) => m.id)).toEqual(["m1", "m2"]);
  });

  it("模型叶子显示名：有 model_name 用 model_name，同名多个时追加 1/2/… 序号", () => {
    const groups = groupModelsForMenu([
      model({ id: "m1", name: "opencode", model_name: "glm-5.3-flash" }),
      model({ id: "m2", name: "opencode", model_name: "glm-5.3-flash" }),
    ]);

    const opencode = groups[0];
    expect(opencode.models[0].displayName).toBe("glm-5.3-flash");
    expect(opencode.models[1].displayName).toBe("glm-5.3-flash 2");
  });

  it("空配置列表返回空数组", () => {
    expect(groupModelsForMenu([])).toEqual([]);
  });

  it("供应商节点按名称排序，且保持稳定", () => {
    const groups = groupModelsForMenu([
      model({ id: "m1", name: "zeta" }),
      model({ id: "m2", name: "alpha" }),
      model({ id: "m3", name: "Hermes Agent" }),
    ]);

    expect(groups.map((g) => g.providerName)).toEqual(["alpha", "Hermes Agent", "zeta"]);
  });
});
