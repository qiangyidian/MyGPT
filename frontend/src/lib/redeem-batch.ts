// 兑换码批次的表单校验、批次状态推导与不可逆操作的确认文案。
//
// 纯函数放这里的原因和 `lib/projects.ts` 一样：vitest 是 `environment: "node"`
// （`frontend/vitest.config.ts`），组件渲染不可测，而「什么算合法的张数」「作废
// 确认框该写哪些后果」正是会写错的地方。
import { buildCsv } from "@/lib/csv";
import { expiryFromDateInput, formatCreditsRaw } from "@/lib/credits";
import type { RedeemBatchProgress } from "@/lib/types";

/**
 * 数字与后端同源，后端才是事实来源；这里只是把它的常量抄成中文提示与钳位：
 *
 * - 单批张数 `1..5000`：`backend/app/credits.py:47` 的
 *   `CreditPolicy.max_codes_per_batch = 5000`（部署可用
 *   `REDEEM_MAX_CODES_PER_BATCH` 覆盖，见 `backend/app/credits.py:71-73`），
 *   超出由 `backend/app/services/redeem_service.py:75-79` 抛
 *   `redeem_batch_too_large`；`count <= 0` 在 `redeem_service.py:73-74` 被拒。
 * - 每码积分 `>= 1`：`redeem_service.py:71-72`，库层还有一条
 *   `credits_per_code > 0` 的 CHECK（`backend/app/models/redeem_code_batch.py:39`）。
 *   **后端没有上限**（列是 BigInteger），所以这里也不发明一个：前端独有的天花板
 *   只会让「发一张大额码」变成要在两边绕的事。
 * - 批次名 `1..128`：`backend/app/schemas/credit.py:48`
 *   （`name: str = Field(min_length=1, max_length=128)`），与
 *   `backend/app/models/redeem_code_batch.py:25` 的 `String(128)` 一致。
 * - 有效期必须晚于当前时间：`redeem_service.py:81-90`（naive 输入按 UTC 处理）。
 * - `note` 后端是 `Text` 且无长度约束（`redeem_code_batch.py:31`），故前端也不限。
 */
export const REDEEM_BATCH_LIMITS = {
  nameMin: 1,
  nameMax: 128,
  countMin: 1,
  countMax: 5000,
  creditsMin: 1,
} as const;

/** 表单态：数字一律留字符串，`expires_at` 是 `<input type="date">` 的 YYYY-MM-DD。 */
export interface RedeemBatchForm {
  name: string;
  credits_per_code: string;
  count: string;
  /** 空串 = 永久有效。 */
  expires_at: string;
  note: string;
}

export const EMPTY_REDEEM_BATCH_FORM: RedeemBatchForm = {
  name: "",
  credits_per_code: "1000",
  count: "10",
  expires_at: "",
  note: "",
};

export type RedeemBatchFormErrors = Partial<Record<keyof RedeemBatchForm, string>>;

export function hasRedeemBatchFormErrors(errors: RedeemBatchFormErrors): boolean {
  return Object.keys(errors).length > 0;
}

function parseInteger(raw: string): number | null {
  const trimmed = raw.trim();
  // 允许前导负号：`-500` 该报「不能小于 1」，而不是「必须是整数」。
  if (!/^-?\d+$/.test(trimmed)) return null;
  const value = Number(trimmed);
  return Number.isSafeInteger(value) ? value : null;
}

/** 单个整数字段：非法就地往 `errors` 里写中文文案（后端仍会再校验一次）。 */
function checkInteger(
  raw: string,
  errors: RedeemBatchFormErrors,
  field: keyof RedeemBatchForm,
  label: string,
  min: number,
  max: number | null
): void {
  if (!raw.trim()) {
    errors[field] = `${label}不能为空`;
    return;
  }
  const value = parseInteger(raw);
  if (value === null) {
    errors[field] = `${label}必须是不带小数点的整数`;
    return;
  }
  if (value < min) {
    errors[field] = `${label}不能小于 ${min}`;
    return;
  }
  if (max !== null && value > max) {
    errors[field] = `${label}不能超过 ${max}`;
    return;
  }
}

/**
 * 返回字段 → 中文错误。空对象表示可以提交。
 *
 * `now` 可注入是为了单测能钉住「有效期必须晚于现在」这条时间相关的规则。
 */
export function validateRedeemBatchForm(
  form: RedeemBatchForm,
  now: Date = new Date()
): RedeemBatchFormErrors {
  const errors: RedeemBatchFormErrors = {};

  const name = form.name.trim();
  if (!name) errors.name = "批次名称不能为空";
  else if (name.length > REDEEM_BATCH_LIMITS.nameMax)
    errors.name = `批次名称不能超过 ${REDEEM_BATCH_LIMITS.nameMax} 个字符`;

  checkInteger(
    form.credits_per_code,
    errors,
    "credits_per_code",
    "每码积分",
    REDEEM_BATCH_LIMITS.creditsMin,
    null
  );
  checkInteger(
    form.count,
    errors,
    "count",
    "生成数量",
    REDEEM_BATCH_LIMITS.countMin,
    REDEEM_BATCH_LIMITS.countMax
  );

  if (form.expires_at.trim()) {
    // `expiryFromDateInput` 把日期转成本地时区当天 23:59:59.999 的 ISO 即时，
    // 与提交给后端的值同源（不能把日期串直接喂给 `new Date(...)`）。
    const instant = expiryFromDateInput(form.expires_at.trim());
    if (!instant) errors.expires_at = "有效期格式不正确";
    else if (new Date(instant).getTime() <= now.getTime())
      errors.expires_at = "有效期必须晚于当前时间";
  }

  return errors;
}

/** 提交给 `POST /api/admin/redeem-batches` 的请求体（假定已通过校验）。 */
export function redeemBatchRequest(form: RedeemBatchForm): {
  name: string;
  credits_per_code: number;
  count: number;
  expires_at: string | null;
  note: string | null;
} {
  return {
    name: form.name.trim(),
    credits_per_code: Number(form.credits_per_code.trim()),
    count: Number(form.count.trim()),
    expires_at: expiryFromDateInput(form.expires_at.trim()),
    note: form.note.trim() || null,
  };
}

/** 表单实时预览：这批一共发出去多少分。字段非法时返回 null（不显示假数字）。 */
export function redeemBatchPreviewText(
  form: RedeemBatchForm
): { count: number; credits: number; total: number } | null {
  const credits = parseInteger(form.credits_per_code);
  const count = parseInteger(form.count);
  if (credits === null || count === null) return null;
  if (credits < REDEEM_BATCH_LIMITS.creditsMin) return null;
  if (count < REDEEM_BATCH_LIMITS.countMin || count > REDEEM_BATCH_LIMITS.countMax)
    return null;
  return { count, credits, total: credits * count };
}

export function formatRedeemBatchPreview(form: RedeemBatchForm): string | null {
  const preview = redeemBatchPreviewText(form);
  if (!preview) return null;
  return `本批合计发放 ${formatCreditsRaw(preview.total)} 积分（${preview.count} 张 × 每张 ${formatCreditsRaw(preview.credits)}）`;
}

// --------------------------------------------------------------------------- //
// 后端时间戳解析
// --------------------------------------------------------------------------- //

/**
 * 把后端返回的时间串解析成即时。
 *
 * 没有时区后缀时按 **UTC** 处理，与 `redeem_service.redeem` 对 naive
 * `expires_at` 的规则一致（`backend/app/services/redeem_service.py:152-157`）：
 * SQLite 取回来的就是 naive 值，若按本地时间解析，Postgres 与 SQLite 两套环境
 * 会给出不同的「是否过期」。
 */
export function parseBackendInstant(value: string | null | undefined): number | null {
  if (!value) return null;
  const trimmed = value.trim();
  if (!trimmed) return null;
  const hasZone = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(trimmed);
  const normalized = hasZone ? trimmed : `${trimmed}Z`;
  const time = new Date(normalized).getTime();
  return Number.isFinite(time) ? time : null;
}

export function isBatchExpired(
  expiresAt: string | null | undefined,
  now: Date = new Date()
): boolean {
  const time = parseBackendInstant(expiresAt);
  if (time === null) return false;
  return time <= now.getTime();
}

/** 展示用日期（解析失败时退回原串，绝不显示 `Invalid Date`）。 */
export function formatDateText(
  value: string | null | undefined,
  fallback = "永久"
): string {
  if (!value) return fallback;
  const time = parseBackendInstant(value);
  if (time === null) return value;
  return new Date(time).toLocaleDateString();
}

export function formatDateTimeText(value: string | null | undefined): string {
  if (!value) return "—";
  const time = parseBackendInstant(value);
  if (time === null) return value;
  return new Date(time).toLocaleString();
}

// --------------------------------------------------------------------------- //
// 批次状态（后端不存批次状态字段，全部由计数推导）
// --------------------------------------------------------------------------- //

export type RedeemBatchTone = "default" | "secondary" | "outline" | "destructive";

export type RedeemBatchStatusKey =
  | "active"
  | "expired"
  | "used_up"
  | "all_void"
  | "no_active"
  | "empty";

export interface RedeemBatchStatus {
  key: RedeemBatchStatusKey;
  label: string;
  tone: RedeemBatchTone;
  /** 「作废剩余」还有没有意义：没有未兑换码时不给这个入口。 */
  voidable: boolean;
}

export function redeemBatchStatus(
  row: RedeemBatchProgress,
  now: Date = new Date()
): RedeemBatchStatus {
  const total = Number(row.total) || 0;
  const active = Number(row.active) || 0;
  const redeemed = Number(row.redeemed) || 0;
  const voided = Number(row.void) || 0;

  if (total <= 0)
    return { key: "empty", label: "已清空", tone: "outline", voidable: false };
  if (active > 0) {
    return isBatchExpired(row.batch.expires_at, now)
      ? { key: "expired", label: "已过期", tone: "destructive", voidable: true }
      : { key: "active", label: "兑换中", tone: "default", voidable: true };
  }
  if (redeemed > 0 && voided === 0)
    return { key: "used_up", label: "已兑完", tone: "secondary", voidable: false };
  if (redeemed === 0)
    return { key: "all_void", label: "已全部作废", tone: "secondary", voidable: false };
  return { key: "no_active", label: "已无可用码", tone: "secondary", voidable: false };
}

export type RedeemBatchFilter = "all" | "operable" | "expired" | "settled";

export const REDEEM_BATCH_FILTERS: { value: RedeemBatchFilter; label: string }[] = [
  { value: "all", label: "全部批次" },
  { value: "operable", label: "还有未兑换码" },
  { value: "expired", label: "已过期" },
  { value: "settled", label: "已了结" },
];

/** `GET /api/admin/redeem-batches` 的一页查询参数。 */
export interface RedeemBatchQuery {
  limit: number;
  offset: number;
  search?: string;
  /** 与后端 `BatchStatusFilter`（`app/schemas/credit.py`）同源，`all` 不发送。 */
  status?: Exclude<RedeemBatchFilter, "all">;
}

/**
 * 列表的筛选/搜索条件全部编成服务端参数 —— 不是风格问题：一旦分页，前端手上只有
 * 当前这几十行，在浏览器里 filter 等于「只在这一页里找」，翻到第 3 页搜老批次会
 * 假装它不存在。
 *
 * 空搜索和 `all` 都不发送：它们是后端的默认值，写进 URL 只会多出噪音。
 */
export function redeemBatchQuery(
  filter: RedeemBatchFilter,
  search: string,
  page: number,
  pageSize: number
): RedeemBatchQuery {
  const query: RedeemBatchQuery = {
    limit: pageSize,
    offset: Math.max(0, Math.trunc(page)) * pageSize,
  };
  const term = search.trim();
  if (term) query.search = term;
  if (filter !== "all") query.status = filter;
  return query;
}

// --------------------------------------------------------------------------- //
// 作废整批：确认框文案
// --------------------------------------------------------------------------- //

export interface VoidBatchConsequences {
  /** 需要在确认框里逐字输入才能解锁按钮的确认词（批次名）。 */
  confirmation: string;
  lines: string[];
}

/**
 * 作废整批的后果清单。每个数字都来自 `GET /api/admin/redeem-batches` 的计数，
 * 不在前端估 —— 后端没有「反作废」端点（`credits.py` 里的 `void_redeem_batch` →
 * `redeem_service.void_batch` 只会把 active 改成 void），所以这句话必须写清楚。
 */
export function voidBatchConsequences(
  row: RedeemBatchProgress,
  now: Date = new Date()
): VoidBatchConsequences {
  const active = Number(row.active) || 0;
  const redeemed = Number(row.redeemed) || 0;
  const voided = Number(row.void) || 0;
  const credits = Number(row.batch.credits_per_code) || 0;
  const lines: string[] = [
    `将把「${row.batch.name}」剩余的 ${active} 张未兑换码改为作废：它们立刻永久失效，用户拿去兑换只会得到「该兑换码已作废」，没有任何办法恢复。`,
    `已兑换的 ${redeemed} 张不受影响 —— 分已经进过用户账本（共 ${formatCreditsRaw(redeemed * credits)} 积分），作废不会回收。`,
    voided > 0
      ? `该批已作废 ${voided} 张，保持原样；本批共 ${row.total} 张。`
      : `本批共 ${row.total} 张，作废记录会保留在库里，不会连带删除任何兑换记录。`,
    "后端不提供反作废接口：这一步不可撤销，只能重新生成一批新码。",
  ];
  if (isBatchExpired(row.batch.expires_at, now)) {
    lines.splice(
      1,
      0,
      `该批有效期已过（${formatDateText(row.batch.expires_at)}），未兑换的码其实已经兑不了；作废只是让它们的状态永久确定下来。`
    );
  }
  return { confirmation: row.batch.name, lines };
}

export function voidBatchConfirmed(batchName: string, typed: string): boolean {
  const expected = batchName.trim();
  if (!expected) return false;
  return typed.trim() === expected;
}

// --------------------------------------------------------------------------- //
// 明文码导出
// --------------------------------------------------------------------------- //

// BOM 与引号/逗号转义收口在 `@/lib/csv`（全项目一份），这里只保留兑换码自己的
// 列形状。导出名 `CSV_BOM` 原样转出去：`redeem-batch-form.tsx` 与既有测试还在用。
export { CSV_BOM } from "@/lib/csv";

export interface RedeemCodesCsvMeta {
  batchName: string;
  creditsPerCode: number;
  /** 展示用的有效期文本。 */
  expiresText: string;
}

/**
 * 明文码的 CSV。这些字符串只在创建响应里出现一次，导出是它们唯一的存档机会，
 * 所以批次名与有效期也写进每一行，免得运营日后对不上是哪批。
 */
export function buildRedeemCodesCsv(codes: string[], meta: RedeemCodesCsvMeta): string {
  const rows = codes.map((code) => [code, meta.batchName, meta.creditsPerCode, meta.expiresText]);
  return buildCsv(["兑换码", "批次", "每码积分", "有效期"], rows);
}
