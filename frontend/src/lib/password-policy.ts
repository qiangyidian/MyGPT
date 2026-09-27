// 密码策略的单一来源：常量 + 校验 + 中文提示。
//
// 三处（登录注册、改密卡片、找回密码）各自抄过一遍规则，抄本会漂；后端才是事实
// 来源，这里只把它的常量翻成中文。放成纯函数的理由同 `lib/redeem-batch.ts`：
// vitest 是 `environment: "node"`（`frontend/vitest.config.ts`），组件渲染不可测。
//
// 数字与后端的对应关系：
//
// - 最短 8 位：`backend/app/core/security.py:61`（`len(pwd) < PASSWORD_MIN_LENGTH`
//   即抛 `password_too_short`），默认值在 `backend/app/core/config.py:621`；
//   同时被 schema 再钉一遍：`backend/app/schemas/auth.py:17`（注册）、`:42`（改密）、
//   `:56`（重置）都是 `min_length=8`。提示文案逐字取自 `security.py:62`。
// - 最长 128 位：`backend/app/schemas/auth.py:17,41,42,56` 的 `max_length=128`。
//   前端不发明这个天花板，只是提前说 —— 超了会撞 422（`@/lib/api-error` 把
//   pydantic 的英文规则翻成「长度最多 128 个字符」）。
// - 大写 + 小写 + 数字：`backend/app/core/security.py:63-65`（`PASSWORD_REQUIRE_COMPLEXITY`
//   为真时要求 `islower() and isupper() and isdigit()`），文案取自 `:65`。
//   开关默认值是 `config.py:622` 的 `True`；后端没有端点把「这一部署到底开没开」
//   告诉前端，所以这里按默认值校验（关掉时前端只会比后端更严，不会更松）。
//   另一个已知的更严之处：后端用的是 Python `str.islower/isupper/isdigit`（认
//   全字母），这里的 `[a-z]`/`[A-Z]` 只认 ASCII —— 纯非拉丁字母的密码后端收、
//   前端拦，属于现状（放宽它等于改判定规则，不在本次单源化里做）。
// - **登录不套这套规则**：`backend/app/schemas/auth.py:26-31` 的 `LoginRequest.password`
//   刻意没有 `min_length`（老账号与输错长度不该被误报成「凭证不对」），所以
//   `app/login/page.tsx` 的登录分支不做校验，只有注册分支用。
//
// 计数口径：后端 `len()` 数的是码点，所以这里也用 `Array.from` 而不是 `.length`
// —— 一个 emoji 在 JS 的 UTF-16 长度里算 2，会让「8 位」变 9。
export const PASSWORD_POLICY = {
  minLength: 8,
  maxLength: 128,
  requireComplexity: true,
} as const;

/** 一行中文规则，直接喂给 placeholder 与说明文字。 */
export function passwordPolicySummary(): string {
  return `至少 ${PASSWORD_POLICY.minLength} 位，需含大写字母、小写字母和数字`;
}

function codePointLength(password: string): number {
  return Array.from(password).length;
}

/**
 * 第一条不满足的规则，中文；`null` = 后端会接受这个密码。
 *
 * 顺序与后端一致（先长度后复杂度），文案与 `security.py:62,65` 同源。
 */
export function passwordProblem(password: string): string | null {
  const length = codePointLength(password);
  if (length < PASSWORD_POLICY.minLength) {
    return `密码至少需要 ${PASSWORD_POLICY.minLength} 个字符`;
  }
  if (length > PASSWORD_POLICY.maxLength) {
    return `密码最多 ${PASSWORD_POLICY.maxLength} 个字符`;
  }
  if (
    PASSWORD_POLICY.requireComplexity &&
    !(
      /[a-z]/.test(password) &&
      /[A-Z]/.test(password) &&
      /\d/.test(password)
    )
  ) {
    return "密码需包含大写字母、小写字母和数字";
  }
  return null;
}
