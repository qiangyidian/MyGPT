/** 摄取队列的用户可见说明（纯函数，便于单测）。
 *
 * 后端把 documents 行本身当队列条目：`ingest_attempts` 记已试次数，
 * `ingest_next_retry_at` 记下次重试时间。只有 status 的话，「等会儿还会自己重试」
 * 和「已经放弃、要人工处理」在界面上长得一模一样，所以这里把两者分开说清楚。
 */
import type { DocFile } from "./types";

const IN_PROGRESS = new Set(["parsing", "chunking", "embedding"]);

type QueueFields = Pick<
  DocFile,
  "status" | "ingest_attempts" | "ingest_next_retry_at"
>;

function waitLabel(iso: string, now: number): string | null {
  const at = Date.parse(iso);
  if (!Number.isFinite(at)) return null;
  const secs = Math.round((at - now) / 1000);
  if (secs <= 0) return "即将重试";
  if (secs < 60) return `${secs} 秒后重试`;
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins} 分钟后重试`;
  return `${Math.round(mins / 60)} 小时后重试`;
}

/** 一句话说明队列在做什么；不需要说明时返回 null。 */
export function describeIngestQueue(
  doc: QueueFields,
  now = Date.now(),
): string | null {
  const attempts = doc.ingest_attempts ?? 0;
  if (doc.ingest_next_retry_at) {
    const wait = waitLabel(doc.ingest_next_retry_at, now);
    return wait ? `第 ${attempts} 次失败，${wait}` : null;
  }
  if (IN_PROGRESS.has(doc.status)) {
    // 第一次尝试不值得占一行；从第二次起才说明前几次都失败了。
    return attempts > 1 ? `第 ${attempts} 次尝试中` : null;
  }
  if (doc.status === "failed" && attempts > 0) {
    return `已尝试 ${attempts} 次，不再自动重试`;
  }
  return null;
}
