/** 每库检索/切分参数的表单逻辑（纯函数，便于单测）。
 *
 * 后端的语义是「留空 = 继承平台默认」，不是「关掉」：`KnowledgeBaseUpdate` 里
 * 省略的字段不动，显式 null 才重置为继承。所以这里把输入框的空串映射成 null，
 * 并且只回传真正改过的字段。
 */
import type { RetrievalSettings } from "./types";

/** 表单态：数字一律留成字符串，空串表示「继承」。 */
export interface RetrievalForm {
  top_k: string;
  score_threshold: string;
  chunk_size: string;
  chunk_overlap: string;
  /** null = 继承；true/false = 本库显式允许/禁止重排。 */
  rerank_enabled: "inherit" | "on" | "off";
}

/** 与服务端 schema 同源的范围（app/schemas/knowledge_base.py）。 */
export const RETRIEVAL_LIMITS = {
  top_k: { min: 1, max: 50 },
  score_threshold: { min: 0, max: 1 },
  chunk_size: { min: 50, max: 8000 },
  chunk_overlap: { min: 0, max: 4000 },
} as const;

export function retrievalFormFromSettings(s: RetrievalSettings): RetrievalForm {
  return {
    top_k: s.top_k == null ? "" : String(s.top_k),
    score_threshold: s.score_threshold == null ? "" : String(s.score_threshold),
    chunk_size: s.chunk_size == null ? "" : String(s.chunk_size),
    chunk_overlap: s.chunk_overlap == null ? "" : String(s.chunk_overlap),
    rerank_enabled:
      s.rerank_enabled == null ? "inherit" : s.rerank_enabled ? "on" : "off",
  };
}

type Errors = Partial<Record<keyof RetrievalForm, string>>;

function checkNumber(
  field: keyof typeof RETRIEVAL_LIMITS,
  raw: string,
  errors: Errors,
  label: string,
  integer: boolean,
): void {
  const trimmed = raw.trim();
  if (!trimmed) return;
  const value = Number(trimmed);
  if (!Number.isFinite(value)) {
    errors[field] = `${label}必须是数字`;
    return;
  }
  if (integer && !Number.isInteger(value)) {
    errors[field] = `${label}必须是整数`;
    return;
  }
  const { min, max } = RETRIEVAL_LIMITS[field];
  if (value < min || value > max) {
    errors[field] = `${label}需在 ${min} 到 ${max} 之间`;
  }
}

/** 返回字段 → 中文错误；有任一错误时调用方应禁用保存。 */
export function validateRetrievalForm(form: RetrievalForm): Errors {
  const errors: Errors = {};
  checkNumber("top_k", form.top_k, errors, "召回条数", true);
  checkNumber(
    "score_threshold",
    form.score_threshold,
    errors,
    "相似度阈值",
    false,
  );
  checkNumber("chunk_size", form.chunk_size, errors, "切块长度", true);
  checkNumber("chunk_overlap", form.chunk_overlap, errors, "切块重叠", true);
  // 只有两端都填了才判得了大小关系（一端继承时由服务端按默认值兜底）。
  if (!errors.chunk_size && !errors.chunk_overlap) {
    const size = Number(form.chunk_size.trim() || NaN);
    const overlap = Number(form.chunk_overlap.trim() || NaN);
    if (Number.isFinite(size) && Number.isFinite(overlap) && overlap >= size) {
      errors.chunk_overlap = "切块重叠必须小于切块长度";
    }
  }
  return errors;
}

function toNumberOrNull(raw: string): number | null {
  const trimmed = raw.trim();
  return trimmed ? Number(trimmed) : null;
}

/**
 * 相对原始设置算出要 PATCH 的字段；没改则返回 null（调用方不给后端发请求）。
 * 清空输入框 = 显式 null = 恢复继承。
 */
export function retrievalPatch(
  form: RetrievalForm,
  original: RetrievalSettings,
): Partial<RetrievalSettings> | null {
  const next: RetrievalSettings = {
    top_k: toNumberOrNull(form.top_k),
    score_threshold: toNumberOrNull(form.score_threshold),
    chunk_size: toNumberOrNull(form.chunk_size),
    chunk_overlap: toNumberOrNull(form.chunk_overlap),
    rerank_enabled:
      form.rerank_enabled === "inherit" ? null : form.rerank_enabled === "on",
  };
  const patch: Partial<RetrievalSettings> = {};
  for (const key of Object.keys(next) as (keyof RetrievalSettings)[]) {
    if (next[key] !== original[key]) {
      (
        patch as Record<
          keyof RetrievalSettings,
          RetrievalSettings[keyof RetrievalSettings]
        >
      )[key] = next[key];
    }
  }
  return Object.keys(patch).length ? patch : null;
}
