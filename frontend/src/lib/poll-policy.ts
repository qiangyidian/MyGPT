/**
 * 轮询与重连的退避策略（条目 43：SSE/轮询必须有上限）。
 *
 * 之前的写法是「固定 4 秒一次，永远不停」：服务端 5xx、网络断了、甚至标签页在
 * 后台，它都按同一个节奏打。真实后果是——故障时全前端的轮询恰好在大面积重试，
 * 把还没倒的服务按得更死（retry storm），而用户什么都看不到。
 *
 * 三条规则：
 * 1. 失败让下一次变慢（指数退避，封顶），成功立刻回到基准值；
 * 2. 连续失败到达上限就**停下**，而不是永远以 60 秒一次继续敲；
 * 3. 页面在后台时不轮询（可见时立刻补一次），移动端省电也省配额。
 *
 * 随机抖动由调用方注入，所以这里是纯函数、可测。
 */

export interface PollPolicy {
  /** 一次成功后的基准间隔。 */
  baseMs: number;
  /** 退避上限：再糟也不会慢于此。 */
  ceilingMs: number;
  /** 连续失败多少次后放弃轮询（0 = 永不放弃，仅靠 ceiling 限速）。 */
  maxConsecutiveFailures: number;
}

/** agent run 图形的兜底轮询：4 秒基准，最慢 1 分钟，连续 6 次失败即停。 */
export const RUN_POLL: PollPolicy = {
  baseMs: 4_000,
  ceilingMs: 60_000,
  maxConsecutiveFailures: 6,
};

/** 会话/知识库状态的低频轮询：更宽，允许一直退到 5 分钟。 */
export const SLOW_POLL: PollPolicy = {
  baseMs: 5_000,
  ceilingMs: 300_000,
  maxConsecutiveFailures: 10,
};

/**
 * 第 ``failures`` 次连续失败之后的等待时间。
 *
 * 指数退避 base * 2^n，封顶 ceiling。``failures=0`` 就是基准值。
 */
export function backoffDelayMs(failures: number, policy: PollPolicy): number {
  if (!Number.isFinite(policy.baseMs) || policy.baseMs <= 0) return policy.ceilingMs;
  const exp = Math.pow(2, Math.max(0, Math.floor(failures)));
  // 先乘再封顶：2^n 在长时间故障后会溢出成 Infinity，那样 setTimeout 会立刻触发。
  const raw = policy.baseMs * exp;
  return Math.min(policy.ceilingMs, Number.isFinite(raw) ? raw : policy.ceilingMs);
}

/** 抖动：在 [delay, delay*(1+ratio)] 之间取值，避免整排客户端同一毫秒重试。 */
export function jitteredDelayMs(
  delayMs: number,
  rand: () => number,
  ratio = 0.2
): number {
  const r = Math.min(1, Math.max(0, rand()));
  return Math.round(delayMs + r * delayMs * ratio);
}

/** 是否已经放弃。``maxConsecutiveFailures=0`` 表示不限次数。 */
export function shouldStopPolling(failures: number, policy: PollPolicy): boolean {
  return policy.maxConsecutiveFailures > 0 && failures >= policy.maxConsecutiveFailures;
}

/** 放弃时给用户的中文说明——不能只把按钮变灰，得说清为什么不再刷新了。 */
export const POLL_STOPPED_MESSAGE = "连接持续失败，已暂停自动刷新。请点击重试或稍后回来查看。";

/** 这次该不该发请求（后台标签页不发，回到前台补一次）。 */
export function shouldPollNow(
  visible: boolean,
  opts: { wasStopped: boolean }
): boolean {
  return visible && !opts.wasStopped;
}
