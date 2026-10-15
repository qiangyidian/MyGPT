/**
 * 输入框上方工具栏：一行两个元素 —— 模式（实心主按钮）+ 请求设置组
 * （知识库 / 模型 / 推理力度 共用一个容器，竖线分隔）。
 *
 * 本仓库的测试栈没有 DOM（无 jsdom / testing-library），组件只能走
 * react-dom/server 再对字符串断言，所以这里钉的是「结构」与「文案」：少了
 * 什么入口、三项是否真在同一个容器里、顺序如何、标签写的是中文还是枚举。
 * 测不到的：纯 CSS 的宽度/居中/悬停、以及弹层内容（Radix 关闭时不渲染）。
 *
 * 另：zustand 走 useSyncExternalStore，服务端只读 getInitialState，所以
 * mode 恒为初始值 speed —— Hermes 分支（整组隐藏）在这里覆盖不到。
 */
import { beforeAll, describe, expect, it, vi } from "vitest";
import { renderToString } from "react-dom/server";
import * as React from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

import { ComposerToolbar } from "@/components/chat/composer-toolbar";
import { MODELS_QUERY_KEY } from "@/hooks/useModels";
import type { KnowledgeBase, ModelConfig } from "@/lib/types";

// 模型选择器的「管理模型」要跳设置页，所以它读 useRouter；这里脱离 Next 的
// app router 渲染，不 mock 的话会在渲染期抛 invariant。
vi.mock("next/navigation", () => ({
  useRouter: () => ({ push: () => {}, replace: () => {}, prefetch: () => {} }),
}));

beforeAll(() => {
  (globalThis as { React?: unknown }).React = React;
});

function model(patch: Partial<ModelConfig> = {}): ModelConfig {
  return {
    id: "m1",
    user_id: "u1",
    name: "commandcode",
    provider: "openai",
    api_base_url: "https://example.invalid/v1",
    api_key_masked: "sk-…",
    has_key: true,
    model_name: "gpt-x",
    embedding_model_name: null,
    supports_stream: true,
    supports_tools: true,
    supports_parallel_tools: true,
    supports_vision: false,
    supports_audio_input: false,
    supports_audio_output: false,
    supports_image_generation: false,
    supports_structured_output: false,
    supports_reasoning_effort: true,
    output_token_parameter: "max_tokens",
    max_context_tokens: 128000,
    max_tokens: 4096,
    temperature: 0.7,
    top_p: 1,
    is_embedding: false,
    created_at: "2026-01-01T00:00:00Z",
    ...patch,
  };
}

function kb(id: string, name: string): KnowledgeBase {
  return {
    id,
    user_id: "u1",
    name,
    description: null,
    embedding_model_id: null,
    document_count: 3,
    chunk_count: 12,
    created_at: "2026-01-01T00:00:00Z",
    top_k: null,
    score_threshold: null,
    rerank_enabled: null,
    chunk_size: null,
    chunk_overlap: null,
  };
}

/**
 * renderToString 不跑 effect，数据必须预先塞进 QueryClient，否则 useQuery
 * 永远停在 pending，模型那一项只会渲染加载态、断言不到任何东西。
 */
function render(
  patch: {
    modelId?: string | null;
    knowledgeBaseIds?: string[];
    knowledgeBases?: KnowledgeBase[];
    models?: ModelConfig[];
  } = {},
) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  client.setQueryData(MODELS_QUERY_KEY, patch.models ?? [model()]);

  return renderToString(
    <QueryClientProvider client={client}>
      <ComposerToolbar
        modelId={patch.modelId === undefined ? "m1" : patch.modelId}
        onModelChange={() => {}}
        knowledgeBaseIds={patch.knowledgeBaseIds ?? []}
        onKnowledgeBaseIdsChange={() => {}}
        knowledgeBases={
          patch.knowledgeBases ?? [kb("kb1", "测试知识库"), kb("kb2", "产品手册")]
        }
      />
    </QueryClientProvider>,
  );
}

describe("ComposerToolbar 结构", () => {
  it("提示词库入口已从工具栏移除", () => {
    const html = render();
    expect(html).not.toContain("提示词库");
    expect(html).not.toContain("插入模板");
    expect(html).not.toContain("存为模板");
  });

  it("提示词库相关的 props 已从接口上删除", () => {
    // 上面的字符串断言是弱守卫：不传 onOpenPromptLibrary，那个块本来就不渲染，
    // 「没渲染」不等于「删掉了」。真正的证据在类型上——若有人把 prop 加回来，
    // Extract 会从 never 变成那个字面量，下面赋 true 直接编译不过。
    type Removed =
      | "onOpenPromptLibrary"
      | "onSaveSelectionAsTemplate"
      | "canSaveSelectionAsTemplate"
      | "savingSelectionAsTemplate";
    type Props = React.ComponentProps<typeof ComposerToolbar>;
    const guard: Extract<Removed, keyof Props> extends never ? true : never = true;
    expect(guard).toBe(true);
  });

  it("三项请求设置同处一个容器，模式在容器之外", () => {
    const html = render();
    const groupAt = html.indexOf('role="group"');
    expect(groupAt).toBeGreaterThan(-1);
    expect(html).toContain("本轮请求设置");

    // 模式是独立的主按钮：它必须排在设置组之前，而不是组内第四项。
    // 认 aria-label 而不是模式名：store 初始值是「极速」，写死某个模式名会在
    // 默认值一变时假失败。
    const modeAt = html.indexOf("当前模式：");
    expect(modeAt).toBeGreaterThan(-1);
    expect(modeAt).toBeLessThan(groupAt);
  });

  it("组内顺序是 知识库 → 模型 → 推理力度", () => {
    const html = render({ knowledgeBaseIds: ["kb1"] });
    const group = html.slice(html.indexOf('role="group"'));
    const kbAt = group.indexOf("测试知识库");
    const modelAt = group.indexOf("commandcode");
    const effortAt = group.indexOf("均衡");
    expect(kbAt).toBeGreaterThan(-1);
    expect(modelAt).toBeGreaterThan(kbAt);
    expect(effortAt).toBeGreaterThan(modelAt);
  });
});

describe("知识库触发器文案", () => {
  it("未选时显示提示用的「知识库」，而不是某个库名", () => {
    const html = render({ knowledgeBaseIds: [] });
    expect(html).toContain("知识库");
    expect(html).not.toContain("测试知识库");
  });

  it("只选一个时显示库名", () => {
    const html = render({ knowledgeBaseIds: ["kb1"] });
    expect(html).toContain("测试知识库");
  });

  it("多选时显示已选数量", () => {
    const html = render({ knowledgeBaseIds: ["kb1", "kb2"] });
    expect(html).toContain("已选 2 个");
  });
});

describe("推理力度触发器文案", () => {
  it("只写中文，不再把 wire 枚举摆到触发器上", () => {
    const html = render();
    // 触发器的可见文案必须正好是「均衡」：旧写法「均衡（medium）」会在 span
    // 里多出一截。这里不能裸搜 "medium"——CSS 类 font-medium 就含它（假阳性）。
    expect(html).toContain(">均衡</span>");
    expect(html).toContain('aria-label="推理力度：均衡"');
    expect(html).not.toContain("（medium）");
    expect(html).not.toContain("（low）");
    expect(html).not.toContain("（high）");
  });

  it("模型不支持推理力度时该项不渲染，其余两项仍在", () => {
    const html = render({ models: [model({ supports_reasoning_effort: false })] });
    expect(html).not.toContain('aria-label="推理力度');
    expect(html).toContain("知识库");
    expect(html).toContain("commandcode");
  });
});

describe("模型触发器文案", () => {
  it("选了具体模型时显示配置名", () => {
    expect(render({ modelId: "m1" })).toContain("commandcode");
  });

  it("默认模型时显示「默认模型」", () => {
    const html = render({ modelId: null });
    expect(html).toContain("默认模型");
    expect(html).not.toContain("commandcode");
  });
});
