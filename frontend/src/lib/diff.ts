// 行级 diff 的纯逻辑。
//
// 放在 lib 而不是组件里，理由和 `lib/redeem-batch.ts` 一样：vitest 是
// `environment: "node"`（`frontend/vitest.config.ts`），组件的着色画不出来，而
// 「第 37 行到底是不是第 37 行」正是这个功能唯一会骗人的地方。
//
// 规模上限：任一侧超过 `DIFF_MAX_LINES_PER_SIDE` 行就不做逐行比对，直接退化成
// 「整段删除 + 整段新增」（O(n+m)，永不卡住）；分段比对时单段的 LCS 动态规划
// 预算 `MAX_LCS_CELLS` 格，超预算先用唯一行锚点二分，找不到锚点同样整段替换。
// 两种降级都只是「看起来像全改了」，不会给出错误的行号。

export type DiffRowKind = "equal" | "add" | "del";

export interface DiffRow {
  kind: DiffRowKind;
  /** 1-based；纯新增行在旧文里没有对应行，为 null。 */
  oldNumber: number | null;
  /** 1-based；纯删除行在新文里没有对应行，为 null。 */
  newNumber: number | null;
  text: string;
}

/** 单侧行数上限：超过即整段替换，不进入任何 O(n·m) 计算。 */
export const DIFF_MAX_LINES_PER_SIDE = 20_000;

/** 单次 LCS 动态规划的格子预算（约 1MB 方向矩阵 + 两行长度数组）。 */
const MAX_LCS_CELLS = 1_000_000;

/** 一次 diff 允许展开的分段数，超出后剩余段整段替换。 */
const MAX_SEGMENTS = 2_000;

/** `hunks` 默认保留的上下文行数。 */
export const DIFF_DEFAULT_CONTEXT = 3;

interface Pair {
  i: number;
  j: number;
}

/**
 * 拆行。行尾换行符本身不算一行（所以 `"a\n"` 与 `"a"` 的 diff 为空——本模块不
 * 表达「文件末尾缺少换行」这一差异）；`""` 是 0 行，`"\n"` 是 1 行空行。
 */
function splitLines(text: string): string[] {
  if (!text) return [];
  const parts = text.replace(/\r\n?/g, "\n").split("\n");
  if (parts[parts.length - 1] === "") parts.pop();
  return parts;
}

/** 任一侧超出规模上限：调用方据此提示「差异过大，未逐行比对」。 */
export function diffTooLarge(oldText: string, newText: string): boolean {
  return (
    splitLines(oldText ?? "").length > DIFF_MAX_LINES_PER_SIDE ||
    splitLines(newText ?? "").length > DIFF_MAX_LINES_PER_SIDE
  );
}

export function diffLines(oldText: string, newText: string): DiffRow[] {
  const a = splitLines(oldText ?? "");
  const b = splitLines(newText ?? "");
  const rows: DiffRow[] = [];

  let ai = 0;
  let bi = 0;
  for (const pair of collectPairs(a, b)) {
    while (ai < pair.i) rows.push(delRow(a, ai++));
    while (bi < pair.j) rows.push(addRow(b, bi++));
    rows.push({ kind: "equal", oldNumber: ai + 1, newNumber: bi + 1, text: a[ai] });
    ai = pair.i + 1;
    bi = pair.j + 1;
  }
  while (ai < a.length) rows.push(delRow(a, ai++));
  while (bi < b.length) rows.push(addRow(b, bi++));
  return rows;
}

function delRow(a: string[], i: number): DiffRow {
  return { kind: "del", oldNumber: i + 1, newNumber: null, text: a[i] };
}

function addRow(b: string[], j: number): DiffRow {
  return { kind: "add", oldNumber: null, newNumber: j + 1, text: b[j] };
}

/**
 * 收集「旧文第 i 行 ↔ 新文第 j 行」的对应关系（严格递增）。空数组 = 两侧毫无
 * 对应，`diffLines` 自然退化成整段替换。
 */
function collectPairs(a: string[], b: string[]): Pair[] {
  const pairs: Pair[] = [];
  if (a.length > DIFF_MAX_LINES_PER_SIDE || b.length > DIFF_MAX_LINES_PER_SIDE) return pairs;

  interface Segment {
    a0: number;
    a1: number;
    b0: number;
    b1: number;
  }
  const stack: Segment[] = [{ a0: 0, a1: a.length, b0: 0, b1: b.length }];
  let expanded = 0;

  while (stack.length > 0 && expanded < MAX_SEGMENTS) {
    expanded += 1;
    const seg = stack.pop() as Segment;
    let a0 = seg.a0;
    let a1 = seg.a1;
    let b0 = seg.b0;
    let b1 = seg.b1;

    while (a0 < a1 && b0 < b1 && a[a0] === b[b0]) {
      pairs.push({ i: a0, j: b0 });
      a0 += 1;
      b0 += 1;
    }
    const tail: Pair[] = [];
    while (a0 < a1 && b0 < b1 && a[a1 - 1] === b[b1 - 1]) {
      a1 -= 1;
      b1 -= 1;
      tail.push({ i: a1, j: b1 });
    }
    for (let k = tail.length - 1; k >= 0; k -= 1) pairs.push(tail[k]);

    const n = a1 - a0;
    const m = b1 - b0;
    if (n === 0 || m === 0) continue;

    if (n * m <= MAX_LCS_CELLS) {
      lcsPairs(a, b, a0, a1, b0, b1, pairs);
      continue;
    }

    const anchor = findAnchor(a, b, a0, a1, b0, b1);
    if (!anchor) continue;
    const ai = a0 + anchor.ia;
    const bj = b0 + anchor.jb;
    pairs.push({ i: ai, j: bj });
    stack.push({ a0: ai + 1, a1, b0: bj + 1, b1 });
    stack.push({ a0, a1: ai, b0, b1: bj });
  }

  pairs.sort((p, q) => p.i - q.i || p.j - q.j);
  const ordered: Pair[] = [];
  let lastI = -1;
  let lastJ = -1;
  for (const p of pairs) {
    if (p.i > lastI && p.j > lastJ) {
      ordered.push(p);
      lastI = p.i;
      lastJ = p.j;
    }
  }
  return ordered;
}

/** 经典 LCS 动态规划：方向矩阵 + 两行滚动长度数组，然后回溯出对应关系。 */
function lcsPairs(
  a: string[],
  b: string[],
  a0: number,
  a1: number,
  b0: number,
  b1: number,
  pairs: Pair[]
): void {
  const n = a1 - a0;
  const m = b1 - b0;
  const width = m + 1;
  const dir = new Uint8Array((n + 1) * width);
  let cur = new Int32Array(width);
  let next = new Int32Array(width);

  for (let i = n - 1; i >= 0; i -= 1) {
    const row = i * width;
    const line = a[a0 + i];
    cur[m] = 0;
    for (let j = m - 1; j >= 0; j -= 1) {
      if (line === b[b0 + j]) {
        cur[j] = next[j + 1] + 1;
        dir[row + j] = 1;
      } else if (next[j] >= cur[j + 1]) {
        cur[j] = next[j];
        dir[row + j] = 2;
      } else {
        cur[j] = cur[j + 1];
        dir[row + j] = 3;
      }
    }
    const swap = cur;
    cur = next;
    next = swap;
  }

  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    const d = dir[i * width + j];
    if (d === 1) {
      pairs.push({ i: a0 + i, j: b0 + j });
      i += 1;
      j += 1;
    } else if (d === 2) {
      i += 1;
    } else {
      j += 1;
    }
  }
}

/**
 * 找一段两侧都只出现一次的公共行，取最靠近段中点的那个当锚点。锚点把大段一
 * 分为二，让动态规划的规模保持在对数级深度上；找不到就返回 null（该段整段替
 * 换）。
 */
function findAnchor(
  a: string[],
  b: string[],
  a0: number,
  a1: number,
  b0: number,
  b1: number
): { ia: number; jb: number } | null {
  interface Hit {
    ia: number;
    jb: number;
    dup: boolean;
  }
  const seen = new Map<string, Hit>();
  for (let i = a0; i < a1; i += 1) {
    const hit = seen.get(a[i]);
    if (hit) hit.dup = true;
    else seen.set(a[i], { ia: i - a0, jb: -1, dup: false });
  }
  for (let j = b0; j < b1; j += 1) {
    const hit = seen.get(b[j]);
    if (!hit) continue;
    if (hit.jb >= 0 || hit.dup) {
      hit.dup = true;
      continue;
    }
    hit.jb = j - b0;
  }

  const n = a1 - a0;
  const m = b1 - b0;
  let best: Hit | null = null;
  let bestDistance = Number.POSITIVE_INFINITY;
  for (const hit of seen.values()) {
    if (hit.dup || hit.jb < 0) continue;
    const distance =
      Math.abs(hit.ia - n / 2) / Math.max(n, 1) + Math.abs(hit.jb - m / 2) / Math.max(m, 1);
    if (distance < bestDistance) {
      bestDistance = distance;
      best = hit;
    }
  }
  return best ? { ia: best.ia, jb: best.jb } : null;
}

/** 折叠后的绘制块：一段要画出来的行，或一段「省略了 N 行未变更」。 */
export type DiffBlock =
  | { kind: "rows"; rows: DiffRow[] }
  | { kind: "fold"; count: number; oldStart: number; newStart: number };

/**
 * 把连续的 equal 段按上下文行数折叠。只改 equal 段的呈现，绝不合并或丢弃
 * add/del，所以画出来的行号与 `diffLines` 输出一一对应。
 */
export function hunks(
  rows: readonly DiffRow[],
  contextLines = DIFF_DEFAULT_CONTEXT
): DiffBlock[] {
  const ctx =
    Number.isFinite(contextLines) && contextLines > 0 ? Math.floor(contextLines) : 0;
  const blocks: DiffBlock[] = [];
  let buffer: DiffRow[] = [];
  const flush = () => {
    if (buffer.length > 0) {
      blocks.push({ kind: "rows", rows: buffer });
      buffer = [];
    }
  };

  let index = 0;
  while (index < rows.length) {
    if (rows[index].kind !== "equal") {
      buffer.push(rows[index]);
      index += 1;
      continue;
    }
    let end = index;
    while (end < rows.length && rows[end].kind === "equal") end += 1;
    const length = end - index;
    const head = Math.min(ctx, length);
    const tail = Math.min(ctx, length - head);
    const folded = length - head - tail;
    for (let k = 0; k < head; k += 1) buffer.push(rows[index + k]);
    if (folded > 0) {
      flush();
      const first = rows[index + head];
      blocks.push({
        kind: "fold",
        count: folded,
        oldStart: first.oldNumber ?? 0,
        newStart: first.newNumber ?? 0,
      });
    }
    for (let k = end - tail; k < end; k += 1) buffer.push(rows[k]);
    index = end;
  }
  flush();
  return blocks;
}

/** 统一补丁（`--- `/`+++ `/`@@` 头 + `-`/`+`/上下文行）解析出来的结果。 */
export interface UnifiedDiff {
  /** 补丁覆盖几个文件（按 `--- ` 头计数，没有头则算 1 个）。 */
  fileCount: number;
  oldPath: string | null;
  newPath: string | null;
  /** 行号按文件内计数，所以多文件补丁里会看到它重新从 1 开始。 */
  rows: DiffRow[];
}

const HUNK_HEADER = /^@@+ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@/;
const FILE_META = /^(diff --git |index |new file mode|deleted file mode|old mode|new mode|similarity index|rename from|rename to|copy from|copy to|===|RCS file:)/;

/** `--- a/path<TAB>时间戳` / `+++ b/path` → 去掉制表符后的 git 前缀路径；`/dev/null` → null。 */
function headerPath(line: string): string | null {
  const value = line.slice(4).split("\t")[0].trim();
  if (!value || value === "/dev/null") return null;
  return value.replace(/^[ab]\//, "");
}

/**
 * 解析统一格式补丁，行号直接取 `@@` 头里的起点（重新 diff 一遍反而会算错）。
 * 没有 `@@` 头时按「单段、从第 1 行起」处理；认不出来（一个 `-`/`+` 都没有）
 * 时返回 null，调用方回落到普通代码块。
 */
export function parseUnifiedDiff(text: string): UnifiedDiff | null {
  const lines = splitLines(text ?? "");
  const rows: DiffRow[] = [];
  let oldPath: string | null = null;
  let newPath: string | null = null;
  let fileCount = 0;
  let oldNumber = 1;
  let newNumber = 1;
  let sawHunk = false;
  let sawFileHeader = false;
  let inHunk = false;
  let oldLeft = 0;
  let newLeft = 0;
  let changes = 0;

  for (const line of lines) {
    // 还在 @@ 段里时 `--- x` 是一行被删掉的内容，不是文件头。
    if (!inHunk && line.startsWith("--- ")) {
      oldPath = headerPath(line);
      fileCount += 1;
      sawFileHeader = true;
      continue;
    }
    if (!inHunk && line.startsWith("+++ ")) {
      newPath = headerPath(line);
      if (fileCount === 0) fileCount = 1;
      sawFileHeader = true;
      continue;
    }
    const hunk = HUNK_HEADER.exec(line);
    if (hunk) {
      oldNumber = Number(hunk[1]);
      newNumber = Number(hunk[3]);
      // 省略计数 = 1 行（POSIX），`0` 是「这一侧没有行」而不是「没写」。
      oldLeft = hunk[2] ? Number(hunk[2]) : 1;
      newLeft = hunk[4] ? Number(hunk[4]) : 1;
      sawHunk = true;
      inHunk = true;
      if (fileCount === 0) fileCount = 1;
      continue;
    }
    if (line.startsWith("\\")) continue;

    if (line.startsWith("-")) {
      rows.push({ kind: "del", oldNumber, newNumber: null, text: line.slice(1) });
      oldNumber += 1;
      oldLeft -= 1;
      changes += 1;
    } else if (line.startsWith("+")) {
      rows.push({ kind: "add", oldNumber: null, newNumber, text: line.slice(1) });
      newNumber += 1;
      newLeft -= 1;
      changes += 1;
    } else if (inHunk || (!sawHunk && !sawFileHeader && !FILE_META.test(line))) {
      // 正规上下文行带一个前导空格；模型常把空格省掉，两种都得当成未变更行，
      // 否则正文会在界面上凭空消失。
      rows.push({
        kind: "equal",
        oldNumber,
        newNumber,
        text: line.startsWith(" ") ? line.slice(1) : line,
      });
      oldNumber += 1;
      newNumber += 1;
      oldLeft -= 1;
      newLeft -= 1;
    }
    if (inHunk && oldLeft <= 0 && newLeft <= 0) inHunk = false;
  }

  if (changes === 0) return null;
  return { fileCount: Math.max(fileCount, 1), oldPath, newPath, rows };
}
