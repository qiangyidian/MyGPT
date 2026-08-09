import type { ModelConfigInput } from "./types";


export function parseOptionalNumber(raw: string): number | undefined {
  if (!raw.trim()) return undefined;
  const value = Number(raw);
  return Number.isFinite(value) ? value : undefined;
}


export function parseOptionalPositiveInteger(raw: string): number | undefined {
  const value = parseOptionalNumber(raw);
  return value !== undefined && Number.isInteger(value) && value > 0
    ? value
    : undefined;
}


export function validateModelConfigNumbers(
  input: Pick<
    ModelConfigInput,
    "max_context_tokens" | "max_tokens" | "temperature" | "top_p"
  >,
): string | null {
  if (
    !Number.isInteger(input.max_context_tokens) ||
    (input.max_context_tokens ?? 0) <= 0 ||
    !Number.isInteger(input.max_tokens) ||
    (input.max_tokens ?? 0) <= 0
  ) {
    return "Token 上限必须是大于 0 的整数";
  }
  if (
    !Number.isFinite(input.temperature) ||
    !Number.isFinite(input.top_p)
  ) {
    return "temperature 和 top_p 必须是有效数字";
  }
  return null;
}
