/**
 * 积分相关的纯函数与文案映射。
 *
 * 与后端 `app/credits.py` 的规范化规则必须保持一致：去掉非字母数字、
 * 转大写、修正手抄歧义字符（I/L→1、O→0）。两边不一致会导致用户明明输对了
 * 却兑换失败。
 */

const CODE_LENGTH = 16;
const CODE_GROUP = 4;

// 与后端 _CHAR_FIXES 一致。
const CHAR_FIXES: Record<string, string> = { I: "1", L: "1", O: "0" };

/**
 * 把输入框里的内容整理成展示格式（XXXX-XXXX-XXXX-XXXX）。
 *
 * 在 onChange 里直接调用，用户粘贴带空格 / 小写 / 别的分隔符的码时会被
 * 悄悄修正，减少"我明明输对了"的客服成本。
 */
export function normalizeRedeemCodeInput(raw: string): string {
  const chars: string[] = [];
  // NFKC 必须放在最前，且必须与后端 app/credits.py 的 normalize_code 一致。
  // 少了它，全角字母/数字会被下面的 [A-Z0-9] 过滤直接**丢弃** —— 用户粘贴一个
  // 全角码会得到被截断的短串，卡在"兑换码应为 16 位字符"上，永远提交不出去，
  // 后端那侧的全角兼容就形同虚设。
  for (const ch of (raw || "").normalize("NFKC").toUpperCase()) {
    if (!/[A-Z0-9]/.test(ch)) continue;
    chars.push(CHAR_FIXES[ch] ?? ch);
    if (chars.length >= CODE_LENGTH) break;
  }
  const groups: string[] = [];
  for (let i = 0; i < chars.length; i += CODE_GROUP) {
    groups.push(chars.slice(i, i + CODE_GROUP).join(""));
  }
  return groups.join("-");
}

/**
 * 余额展示。
 *
 * 负数显示为 0：准入在轮前、扣费在轮后，最后一轮会把余额扣成负数，那是
 * 平台的负债而不是用户的欠款，在用户侧显示成负数只会造成困惑。
 */
export function formatCredits(value: number | null | undefined): string {
  const safe = Math.max(0, Math.floor(Number(value) || 0));
  return safe.toLocaleString("zh-CN");
}

/**
 * 余额展示（管理后台 / 运营视角专用）。
 *
 * 与上面的 `formatCredits` 相对：那边做负数钳位，是因为在用户侧余额是
 * 平台负债、显示为负只会造成困惑；而管理侧的负数余额是一个真实信号 ——
 * 它意味着账户超扣（消耗多于获得）或调分过量，运营需要看到它来诊断和
 * 补偿。这里不做钳位，只做同样的取整与千分位格式化。
 */
export function formatCreditsRaw(value: number | null | undefined): string {
  return Math.floor(Number(value) || 0).toLocaleString("zh-CN");
}

/** 后端稳定错误码 → 用户可读文案。 */
export const REDEEM_ERROR_MESSAGES: Record<string, string> = {
  redeem_code_not_found: "兑换码不存在，请检查是否输入有误",
  redeem_code_used: "该兑换码已被使用",
  redeem_code_expired: "该兑换码已过期",
  redeem_code_void: "该兑换码已作废",
  rate_limited: "操作过于频繁，请稍后再试",
};

/** 已知码用映射文案；未知码回落到后端 message，再兜底通用文案。 */
export function redeemErrorMessage(code: string, fallback?: string): string {
  return REDEEM_ERROR_MESSAGES[code] || fallback || "兑换失败，请稍后重试";
}
