import { describe, expect, it } from "vitest";

import {
  parseOptionalNumber,
  parseOptionalPositiveInteger,
  validateModelConfigNumbers,
} from "../model-config-validation";


describe("model config numeric validation", () => {
  it("omits cleared and non-finite numeric input instead of producing NaN", () => {
    expect(parseOptionalNumber("")).toBeUndefined();
    expect(parseOptionalNumber("not-a-number")).toBeUndefined();
    expect(parseOptionalNumber("Infinity")).toBeUndefined();
  });

  it("accepts only positive integer token limits", () => {
    expect(parseOptionalPositiveInteger("8192")).toBe(8192);
    expect(parseOptionalPositiveInteger("0")).toBeUndefined();
    expect(parseOptionalPositiveInteger("1.5")).toBeUndefined();
  });

  it("returns a user-facing error before invalid numeric values are submitted", () => {
    expect(
      validateModelConfigNumbers({
        max_context_tokens: undefined,
        max_tokens: 1024,
        temperature: 0.7,
        top_p: 1,
      }),
    ).toMatch(/Token/);
    expect(
      validateModelConfigNumbers({
        max_context_tokens: 8192,
        max_tokens: 1024,
        temperature: Number.NaN,
        top_p: 1,
      }),
    ).not.toBeNull();
  });
});
