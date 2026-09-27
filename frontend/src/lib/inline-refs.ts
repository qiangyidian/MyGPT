/** @文件 / @知识库 内联引用的纯函数层（无 DOM，可在 node 环境下单测）。
 *
 * 消息正文里一个引用就是一枚「可读的 token」：
 *
 *     @[产品手册.pdf](doc:3f1a…)
 *
 * 这样一份文本同时满足三件事：模型读得懂（@+文件名+库名，不必额外注入 id 语义）、
 * 前端解析得回来（正则一遍扫完，不依赖任何编辑器数据结构）、复制/导出/重新生成
 * 都不会丢——引用就长在文本里，没有第二份状态需要跟它同步。
 *
 * token 里的 id 是服务端的稳定主键（见 app/api/mentions.py 的 ``token`` 字段），
 * 改文件名不会让一条引用失效；label 只是给人看的那一份。
 */
import type { ChatMention } from "./types";

export type InlineRefKind = "kb" | "doc" | "file";

/** 一条引用：kind + 稳定 id + 展示用名字。 */
export interface InlineRef {
  kind: InlineRefKind;
  id: string;
  label: string;
}

/** 文本里一条引用的位置（``end`` 是 token 末位之后）。 */
export interface RefRange {
  ref: InlineRef;
  start: number;
  end: number;
}

/** ``@`` 后面正在输入的那段查询（picker 的过滤词）。 */
export interface MentionQuery {
  query: string;
  /** 触发符 ``@`` 的下标。 */
  start: number;
  /** 光标下标（== 查询末位之后）。 */
  end: number;
}

export const REF_KINDS: InlineRefKind[] = ["kb", "doc", "file"];

export const REF_KIND_LABEL: Record<InlineRefKind, string> = {
  kb: "知识库",
  doc: "文档",
  file: "文件",
};

/** 触发符：半角 @ 与全角 ＠（中文输入法下两者都会打出来）。 */
export const MENTION_TRIGGERS = ["@", "＠"];

/** 单条消息的引用上限，与后端 ChatRequest.MAX_MENTIONS_PER_REQUEST 同源。 */
export const MAX_INLINE_REFS = 8;
/** 单轮最多读几个知识库，与后端 ChatRequest.MAX_KB_PER_REQUEST 同源。
 *  端点也会把这两个数随响应下发（见 MentionListOut），服务端改了这里只是兜底。 */
export const MAX_KB_PER_REQUEST = 5;
/** ``@`` 之后最多回看多少个字符还算是「正在输入的一条引用」。 */
export const MAX_MENTION_QUERY = 40;
/** token 里 label 的长度上限（文件名可能很长，展示会被截断）。 */
export const MAX_REF_LABEL = 80;

const UUID_SOURCE =
  "[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}";
const TOKEN_SOURCE = String.raw`[@＠]\[([^\[\]\n]{1,${MAX_REF_LABEL}})\]\((kb|doc|file):(${UUID_SOURCE})\)`;
const WHOLE_TOKEN = new RegExp(`^(?:${TOKEN_SOURCE})$`);

/** 每次都要新正则：``/g`` 的 lastIndex 是跨调用状态，共享一个实例会互相打断。 */
function tokenRe(): RegExp {
  return new RegExp(TOKEN_SOURCE, "g");
}

/** 引用集合的键：同一目标的两次 @ 只算一条。 */
export function refKey(ref: InlineRef): string {
  return `${ref.kind}:${ref.id}`;
}

/** 展示用的名字：文件名可能被用户改过，所以一律以 token 里的为准。 */
export function refDisplayName(ref: InlineRef): string {
  return `${REF_KIND_LABEL[ref.kind]}：${ref.label}`;
}

/** 把 label 洗成能安全塞进 token 的形式（去掉方括号/换行/超长）。 */
export function sanitizeLabel(label: string): string {
  const cleaned = (label || "")
    .replace(/[\[\]\n\r]/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, MAX_REF_LABEL)
    .trim();
  return cleaned || "未命名";
}

/** 编码成一枚可读 token（插入文本 / 与服务端比对的唯一形式）。 */
export function encodeRef(ref: InlineRef): string {
  return `@[${sanitizeLabel(ref.label)}](${ref.kind}:${ref.id})`;
}

/** 解析一整枚 token；不是合法 token 就返回 null（不猜）。 */
export function decodeToken(token: string): InlineRef | null {
  const m = WHOLE_TOKEN.exec(token);
  if (!m) return null;
  return { kind: m[2] as InlineRefKind, id: m[3], label: m[1] };
}

/** 文本里所有 token 的出现位置（按出现顺序，含重复）。 */
export function findRefs(text: string): RefRange[] {
  if (!text) return [];
  const out: RefRange[] = [];
  for (const m of text.matchAll(tokenRe())) {
    const ref = decodeToken(m[0]);
    if (ref && typeof m.index === "number") {
      out.push({ ref, start: m.index, end: m.index + m[0].length });
    }
  }
  return out;
}

/** 文本引用的目标集合：按 key 去重，保留首次出现顺序。 */
export function parseRefs(text: string): InlineRef[] {
  const byKey = new Map<string, InlineRef>();
  for (const { ref } of findRefs(text)) {
    const key = refKey(ref);
    if (!byKey.has(key)) byKey.set(key, ref);
  }
  return [...byKey.values()];
}

export function hasRef(text: string, ref: InlineRef): boolean {
  return parseRefs(text).some((r) => refKey(r) === refKey(ref));
}

/** 把文本里的 token 换成可读名字（侧栏摘要、通知预览等场景用）。 */
export function renderRefLabels(text: string): string {
  return text.replace(
    tokenRe(),
    (_full, label: string, kind: string) =>
      `${REF_KIND_LABEL[kind as InlineRefKind] ?? ""}${label}`,
  );
}

/** 删掉某个目标的全部 token，并顺手带走紧跟其后的那个空格。 */
export function removeRef(text: string, ref: InlineRef): string {
  const key = refKey(ref);
  const ranges = findRefs(text).filter(
    ({ ref: r }) => refKey(r) === key,
  );
  if (!ranges.length) return text;
  let out = "";
  let cursor = 0;
  for (const { start, end } of ranges) {
    out += text.slice(cursor, start);
    cursor = end < text.length && text[end] === " " ? end + 1 : end;
  }
  out += text.slice(cursor);
  return out.replace(/ {2,}/g, " ");
}

/** 判断一个字符能否出现在查询里（空白/括号/冒号一律结束本次引用）。 */
function isQueryChar(ch: string): boolean {
  return !/\s/.test(ch) && !"[]():".includes(ch);
}

/**
 * 光标处是否正在输入一条 ``@引用``。
 *
 * IME 说明：合成期间不要拿这个函数去判定（组件在 compositionend 之前不喂文本），
 * 因为候选串会先落进 textarea。这里的规则本身对输入法友好——查询串里出现任何
 * 空白或括号就判定「不在引用中」，所以用户拿拼音打 ``@`` 之前的中文音节永远不会
 * 意外弹开 picker。
 */
export function findMentionAt(
  text: string,
  caret: number,
): MentionQuery | null {
  const end = Math.max(0, Math.min(caret, text.length));
  const floor = Math.max(0, end - MAX_MENTION_QUERY);
  for (let i = end - 1; i >= floor; i -= 1) {
    const ch = text[i];
    if (!isQueryChar(ch)) return null;
    if (MENTION_TRIGGERS.includes(ch)) {
      const prev = i > 0 ? text[i - 1] : "";
      // 邮箱/URL 里的 @，以及 token 内部（``(kb:`` 之前）都不算触发。
      if (prev && /[A-Za-z0-9_@＠]/.test(prev)) return null;
      return { query: text.slice(i + 1, end), start: i, end };
    }
  }
  return null;
}

/** 把解析结果映射成 chat 请求体里的 ``mentions`` 字段（顺序即文本顺序）。 */
export function toChatMentions(refs: InlineRef[]): ChatMention[] {
  return refs.map((ref) => ({ kind: ref.kind, id: ref.id }));
}

/**
 * 整体删掉一枚引用：光标紧贴 token 末位（退格）或首位（删除键）时才生效。
 *
 * 没有这一步的话，用户按一次退格只会抹掉 token 的一个括号，剩下半截文本既不是
 * 引用也不是正文——这是一枚原子记号，就得按原子来删。
 */
export function deleteRefAt(
  text: string,
  caret: number,
  direction: "backward" | "forward" = "backward",
): { value: string; caret: number } | null {
  for (const range of findRefs(text)) {
    const hit =
      direction === "backward" ? range.end === caret : range.start === caret;
    if (!hit) continue;
    // 顺手带走插入时补的那个空格，不留空白缝。
    const tail = text[range.end] === " " ? range.end + 1 : range.end;
    const value = `${text.slice(0, range.start)}${text.slice(tail)}`;
    return { value, caret: range.start };
  }
  return null;
}

export interface KbToggleResult {
  ids: string[];
  ok: boolean;
  reason?: string;
}

/**
 * 知识库多选的勾选/取消：多选就是多选。
 *
 * 超过服务端每轮上限时不静默丢弃，也不挤掉最早那一个——两者都会让用户以为
 * 自己的选择生效了。返回 ``ok:false`` 加原列表，由界面把中文原因讲出来。
 */
export function toggleKnowledgeBase(
  ids: string[],
  id: string,
  max: number = MAX_KB_PER_REQUEST,
): KbToggleResult {
  const current = Array.from(new Set(ids));
  if (current.includes(id)) {
    return { ids: current.filter((x) => x !== id), ok: true };
  }
  if (current.length >= max) {
    return {
      ids: current,
      ok: false,
      reason: `一轮最多选择 ${max} 个知识库，请先取消其他选择`,
    };
  }
  return { ids: [...current, id], ok: true };
}
