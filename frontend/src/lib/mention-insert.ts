/** composer 里 ``@`` 引用的「落子 + 上限裁决」纯函数层（无 DOM、无 React）。
 *
 * token 的编解码只有一份：`@/lib/inline-refs`（形如 ``@[产品手册.pdf](doc:3f1a…)``）。
 * 本模块不定义第二套 token 形状，只补 inline-refs 不管的两件事：
 *
 * 1. 上限从哪来 —— 由 mentions 端点随候选下发（``max_mentions`` /
 *    ``max_knowledge_bases``），客户端不再抄一份数字。服务端改了上限这里跟着变，
 *    不会出现「界面让你选、发出去 422」。
 * 2. 拒绝时的中文原因 —— 超限 / 重复 / 还不可检索 / 会撑破输入框，这四种都是
 *    「静默失败最难查」的那类，所以判定留在纯函数里，UI 只负责把 reason 念出来。
 */
import {
  MAX_INLINE_REFS,
  MAX_KB_PER_REQUEST,
  decodeToken,
  encodeRef,
  findMentionAt,
  hasRef,
  parseRefs,
  refDisplayName,
  toChatMentions,
  type InlineRef,
  type InlineRefKind,
  type MentionQuery,
} from "./inline-refs";
import type { ChatMention, MentionList, MentionTarget } from "./types";

/** 输入框的字符上限：一枚 token 约 53 字，卡太紧会在两三枚引用后吞掉正文。 */
export const COMPOSER_MAX_CHARS = 8000;
/** 剩余不足这么多字时才开始提示（常驻一个计数器只是噪音）。 */
export const COMPOSER_NEAR_LIMIT = 200;

/** 一次插入 / 一次收敛要用的三个上限。 */
export interface MentionLimits {
  maxMentions: number;
  maxKnowledgeBases: number;
  maxChars: number;
}

/** 候选还没拿到（或端点没下发）时的兜底，取自 inline-refs 的同源常量。 */
export const FALLBACK_LIMITS: MentionLimits = {
  maxMentions: MAX_INLINE_REFS,
  maxKnowledgeBases: MAX_KB_PER_REQUEST,
  maxChars: COMPOSER_MAX_CHARS,
};

function positiveInt(value: unknown, fallback: number): number {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? Math.trunc(value)
    : fallback;
}

/** 把候选响应里的上限读成裁决用的形状；缺字段/坏值退回兜底，不抛错。 */
export function limitsFromMentionList(
  list:
    | Pick<MentionList, "max_mentions" | "max_knowledge_bases">
    | null
    | undefined,
  maxChars: number = COMPOSER_MAX_CHARS,
): MentionLimits {
  return {
    maxMentions: positiveInt(list?.max_mentions, FALLBACK_LIMITS.maxMentions),
    maxKnowledgeBases: positiveInt(
      list?.max_knowledge_bases,
      FALLBACK_LIMITS.maxKnowledgeBases,
    ),
    maxChars: positiveInt(maxChars, FALLBACK_LIMITS.maxChars),
  };
}

const KINDS: InlineRefKind[] = ["kb", "doc", "file"];

/** 候选 → 引用（label 怎么洗进 token 由 inline-refs 负责）。未知 kind 回 null。 */
export function targetToRef(target: MentionTarget): InlineRef | null {
  if (!KINDS.includes(target.kind) || !target.id) return null;
  return { kind: target.kind, id: target.id, label: target.label };
}

export interface InsertMentionResult {
  text: string;
  caret: number;
  ok: boolean;
  /** 失败时给用户看的中文原因；成功时为 null。 */
  reason: string | null;
}

function rejected(
  text: string,
  caret: number,
  reason: string,
): InsertMentionResult {
  return { text, caret, ok: false, reason };
}

/**
 * 把选中目标的 token 插进光标处：光标正落在 ``@查询`` 里就整段替换（连带用户已经
 * 敲的那段过滤词），否则就地插入。
 *
 * ``anchor`` 让调用方把弹层用的那一份判定结果传进来，免得两处各自理解「正在输入
 * 哪条引用」；没给就自己 findMentionAt。
 */
export function insertMention(
  text: string,
  caret: number,
  target: MentionTarget,
  limits: MentionLimits = FALLBACK_LIMITS,
  anchor?: MentionQuery | null,
): InsertMentionResult {
  const at = Math.max(0, Math.min(caret, text.length));
  const ref = targetToRef(target);
  if (!ref) return rejected(text, at, "这个目标无法引用");
  const token = encodeRef(ref);
  // 编出来的 token 必须能被同一份解析器读回（id 不是 uuid、label 洗完为空都会破坏
  // 这一点）：写进正文却解不回来的引用，等于发送时静默丢失。
  if (!decodeToken(token)) return rejected(text, at, "这个目标无法引用");
  if (target.selectable === false) {
    return rejected(text, at, `${refDisplayName(ref)} 还不可检索，请稍后再试`);
  }

  const existing = parseRefs(text);
  if (hasRef(text, ref)) {
    return rejected(text, at, `已引用${refDisplayName(ref)}`);
  }
  if (existing.length >= limits.maxMentions) {
    return rejected(
      text,
      at,
      `一条消息最多引用 ${limits.maxMentions} 个目标，请先删掉不需要的引用`,
    );
  }
  if (ref.kind === "kb") {
    const kbCount = existing.filter((r) => r.kind === "kb").length;
    if (kbCount >= limits.maxKnowledgeBases) {
      return rejected(
        text,
        at,
        `一条消息最多引用 ${limits.maxKnowledgeBases} 个知识库`,
      );
    }
  }

  const active = anchor === undefined ? findMentionAt(text, at) : anchor;
  const from = active ? active.start : at;
  const to = active ? Math.max(from, Math.min(active.end, text.length)) : at;
  const tail = text.slice(to);
  // 补一个尾随空格让下一次 findMentionAt 立刻判为「不在引用中」，弹层自然收起；
  // 后面本来就是空白时不再补，免得正文长出一串空格。
  const inserted = /^\s/.test(tail) ? token : `${token} `;
  const next = `${text.slice(0, from)}${inserted}${tail}`;
  if (next.length > limits.maxChars) {
    // textarea 的 maxLength 只会静默截断，那会留下半枚 token——所以在这里挡掉。
    return rejected(
      text,
      at,
      `正文与引用合计上限 ${limits.maxChars} 字，本次需要 ${next.length} 字，请先精简`,
    );
  }
  return { text: next, caret: from + inserted.length, ok: true, reason: null };
}

export interface CollectedMentions {
  refs: InlineRef[];
  /** 随请求发出的 ``ChatRequest.mentions``：顺序即正文顺序，已按上限裁过。 */
  mentions: ChatMention[];
  /** 越过上限、没有发出去的引用（粘贴进来的长文本可能有）。 */
  dropped: InlineRef[];
}

/**
 * 正文 → 请求体：解析只走 inline-refs 那一份正则，这里只补裁决。
 *
 * 超限不静默丢弃——把没发出去的那几枚回给调用方去说明，否则用户会以为模型读过
 * 了自己的文件。
 */
export function collectMentions(
  text: string,
  limits: MentionLimits = FALLBACK_LIMITS,
): CollectedMentions {
  const refs: InlineRef[] = [];
  const dropped: InlineRef[] = [];
  let kbCount = 0;
  for (const ref of parseRefs(text)) {
    if (refs.length >= limits.maxMentions) {
      dropped.push(ref);
      continue;
    }
    if (ref.kind === "kb") {
      if (kbCount >= limits.maxKnowledgeBases) {
        dropped.push(ref);
        continue;
      }
      kbCount += 1;
    }
    refs.push(ref);
  }
  return { refs, mentions: toChatMentions(refs), dropped };
}

/** 被裁掉的引用 → 中文提醒；什么都没裁掉时回 null。 */
export function droppedNotice(dropped: readonly InlineRef[]): string | null {
  if (dropped.length === 0) return null;
  const names = dropped.map((r) => refDisplayName(r)).join("、");
  return `引用已达上限，这些不会随本轮发出：${names}`;
}
