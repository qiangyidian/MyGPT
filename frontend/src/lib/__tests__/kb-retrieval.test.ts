// 每库检索参数的表单逻辑（条目 23）。后端是 PATCH + model_fields_set，
// 所以「发什么」本身就是语义的一部分：发错字段会把用户没动的配置抹掉。
import { describe, expect, it } from "vitest";

import {
  RETRIEVAL_LIMITS,
  retrievalFormFromSettings,
  retrievalPatch,
  validateRetrievalForm,
  type RetrievalForm,
} from "@/lib/kb-retrieval";
import type { RetrievalSettings } from "@/lib/types";

const INHERIT: RetrievalSettings = {
  top_k: null,
  score_threshold: null,
  rerank_enabled: null,
  chunk_size: null,
  chunk_overlap: null,
};

function form(overrides: Partial<RetrievalForm> = {}): RetrievalForm {
  return {
    top_k: "",
    score_threshold: "",
    chunk_size: "",
    chunk_overlap: "",
    rerank_enabled: "inherit",
    ...overrides,
  };
}

describe("retrievalFormFromSettings", () => {
  it("renders null as an empty input, i.e. inherit", () => {
    expect(retrievalFormFromSettings(INHERIT)).toEqual(form());
  });

  it("renders explicit overrides verbatim", () => {
    expect(
      retrievalFormFromSettings({
        ...INHERIT,
        top_k: 6,
        score_threshold: 0.25,
        rerank_enabled: false,
        chunk_size: 800,
        chunk_overlap: 120,
      }),
    ).toEqual({
      top_k: "6",
      score_threshold: "0.25",
      chunk_size: "800",
      chunk_overlap: "120",
      rerank_enabled: "off",
    });
  });
});

describe("validateRetrievalForm", () => {
  it("accepts blanks and in-range values", () => {
    expect(validateRetrievalForm(form())).toEqual({});
    expect(
      validateRetrievalForm(form({ top_k: "6", chunk_size: "800" })),
    ).toEqual({});
  });

  it("rejects non-numbers and non-integers", () => {
    expect(validateRetrievalForm(form({ top_k: "abc" })).top_k).toContain(
      "数字",
    );
    expect(validateRetrievalForm(form({ top_k: "6.5" })).top_k).toContain(
      "整数",
    );
    // 阈值是小数，不该被整数规则拦下
    expect(validateRetrievalForm(form({ score_threshold: "0.25" }))).toEqual(
      {},
    );
  });

  it("states the server-side ranges", () => {
    const { top_k } = RETRIEVAL_LIMITS;
    const msg = validateRetrievalForm(
      form({ top_k: String(top_k.max + 1) }),
    ).top_k;
    expect(msg).toContain(String(top_k.min));
    expect(msg).toContain(String(top_k.max));
  });

  it("only compares overlap against size when both are set", () => {
    expect(
      validateRetrievalForm(form({ chunk_size: "800", chunk_overlap: "800" }))
        .chunk_overlap,
    ).toContain("小于");
    // 一端继承时服务端拿默认值兜底，客户端不猜
    expect(validateRetrievalForm(form({ chunk_overlap: "800" }))).toEqual({});
  });

  it("skips the cross-check once a field is already wrong", () => {
    const errors = validateRetrievalForm(
      form({ chunk_size: "0", chunk_overlap: "9" }),
    );
    expect(errors.chunk_size).toBeDefined();
    expect(errors.chunk_overlap).toBeUndefined();
  });
});

describe("retrievalPatch", () => {
  it("sends nothing when the form still matches the server", () => {
    expect(retrievalPatch(form(), INHERIT)).toBeNull();
    expect(
      retrievalPatch(form({ top_k: "6", rerank_enabled: "on" }), {
        ...INHERIT,
        top_k: 6,
        rerank_enabled: true,
      }),
    ).toBeNull();
  });

  it("sends only the touched fields", () => {
    expect(retrievalPatch(form({ top_k: "6" }), INHERIT)).toEqual({ top_k: 6 });
  });

  it("treats a cleared input as an explicit reset to inherit", () => {
    expect(retrievalPatch(form(), { ...INHERIT, top_k: 6 })).toEqual({
      top_k: null,
    });
  });

  it("keeps rerank's three states distinct", () => {
    expect(retrievalPatch(form({ rerank_enabled: "off" }), INHERIT)).toEqual({
      rerank_enabled: false,
    });
    expect(retrievalPatch(form({ rerank_enabled: "on" }), INHERIT)).toEqual({
      rerank_enabled: true,
    });
    expect(
      retrievalPatch(form({ rerank_enabled: "inherit" }), {
        ...INHERIT,
        rerank_enabled: true,
      }),
    ).toEqual({ rerank_enabled: null });
  });

  it("ignores surrounding whitespace", () => {
    expect(retrievalPatch(form({ top_k: "  6 " }), INHERIT)).toEqual({
      top_k: 6,
    });
  });
});
