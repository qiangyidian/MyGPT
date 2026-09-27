import { describe, expect, it } from "vitest";

import {
  DIFF_MAX_LINES_PER_SIDE,
  diffLines,
  diffTooLarge,
  hunks,
  parseUnifiedDiff,
  type DiffRow,
} from "@/lib/diff";

/** 行号是这个功能唯一会骗人的地方：每个用例都顺手把不变量钉一遍。 */
function expectValidNumbering(rows: DiffRow[]) {
  let lastOld = 0;
  let lastNew = 0;
  for (const row of rows) {
    if (row.kind === "equal") {
      expect(row.oldNumber).not.toBeNull();
      expect(row.newNumber).not.toBeNull();
    } else if (row.kind === "del") {
      expect(row.oldNumber).not.toBeNull();
      expect(row.newNumber).toBeNull();
    } else {
      expect(row.oldNumber).toBeNull();
      expect(row.newNumber).not.toBeNull();
    }
    if (row.oldNumber !== null) {
      expect(row.oldNumber).toBeGreaterThan(lastOld);
      lastOld = row.oldNumber;
    }
    if (row.newNumber !== null) {
      expect(row.newNumber).toBeGreaterThan(lastNew);
      lastNew = row.newNumber;
    }
  }
}

const kinds = (rows: DiffRow[]) => rows.map((r) => r.kind).join(",");

describe("diffLines — 基本形态", () => {
  it("两侧相同 → 全 equal", () => {
    const rows = diffLines("a\nb\nc", "a\nb\nc");
    expect(kinds(rows)).toBe("equal,equal,equal");
    expectValidNumbering(rows);
  });

  it("空串 / 单侧为空", () => {
    expect(diffLines("", "")).toEqual([]);
    expect(diffLines("", "x\ny").map((r) => [r.kind, r.newNumber, r.text])).toEqual([
      ["add", 1, "x"],
      ["add", 2, "y"],
    ]);
    expect(diffLines("x\ny", "").map((r) => [r.kind, r.oldNumber, r.text])).toEqual([
      ["del", 1, "x"],
      ["del", 2, "y"],
    ]);
    // "\n" 是一行空行，"" 是零行。
    expect(diffLines("", "\n").map((r) => [r.kind, r.text])).toEqual([["add", ""]]);
  });

  it("行尾换行符本身不算一行", () => {
    expect(diffLines("a\nb\n", "a\nb")).toEqual(diffLines("a\nb", "a\nb"));
    expect(kinds(diffLines("a\nb\n", "a\nb"))).toBe("equal,equal");
  });

  it("CRLF 与 LF 等价", () => {
    expect(kinds(diffLines("a\r\nb", "a\nb"))).toBe("equal,equal");
  });

  it("全增：开头插入一行，后面的行号各自对齐", () => {
    const rows = diffLines("b\nc", "a\nb\nc");
    expect(kinds(rows)).toBe("add,equal,equal");
    expect(rows.map((r) => [r.oldNumber, r.newNumber])).toEqual([
      [null, 1],
      [1, 2],
      [2, 3],
    ]);
    expectValidNumbering(rows);
  });

  it("中间改一处：删除行在前、新增行在后，行号都指回自己那一侧", () => {
    const rows = diffLines("a\nb\nc", "a\nx\nc");
    expect(kinds(rows)).toBe("equal,del,add,equal");
    expect(rows.map((r) => [r.kind, r.oldNumber, r.newNumber, r.text])).toEqual([
      ["equal", 1, 1, "a"],
      ["del", 2, null, "b"],
      ["add", null, 2, "x"],
      ["equal", 3, 3, "c"],
    ]);
    expectValidNumbering(rows);
  });

  it("重复行的对应关系取最近的一处", () => {
    const rows = diffLines("x\nx\ny", "x\nz\ny");
    expect(kinds(rows)).toBe("equal,del,add,equal");
    expect(rows.map((r) => r.oldNumber)).toEqual([1, 2, null, 3]);
    expect(rows.map((r) => r.newNumber)).toEqual([1, null, 2, 3]);
    expectValidNumbering(rows);
  });
});

function lines(n: number, map?: (i: number) => string) {
  return Array.from({ length: n }, (_, i) => (map ? map(i) : `l${i}`)).join("\n");
}

describe("diffLines — 规模与降级", () => {
  it("1 万行只改中间一行：逐行比对，不炸开", () => {
    const rows = diffLines(
      lines(10_000),
      lines(10_000, (i) => (i === 5_000 ? "CHANGED" : `l${i}`))
    );
    const equal = rows.filter((r) => r.kind === "equal").length;
    expect(equal).toBe(9_999);
    expect(rows.filter((r) => r.kind === "del")).toEqual([
      { kind: "del", oldNumber: 5_001, newNumber: null, text: "l5000" },
    ]);
    expect(rows.filter((r) => r.kind === "add")).toEqual([
      { kind: "add", oldNumber: null, newNumber: 5_001, text: "CHANGED" },
    ]);
    expectValidNumbering(rows);
  });

  it("上限之内不降级：两侧 1000×1000 真的走进一次动态规划", () => {
    const h1 = Array.from({ length: 500 }, (_, i) => `a${i}`);
    const h2 = Array.from({ length: 500 }, (_, i) => `b${i}`);
    // 整段前后互换：首尾公共行剪不掉，剩下的正是 1000×1000。
    const rows = diffLines([...h1, ...h2].join("\n"), [...h2, ...h1].join("\n"));
    expect(rows.filter((r) => r.kind === "equal")).toHaveLength(500);
    expect(rows.filter((r) => r.kind === "del")).toHaveLength(500);
    expect(rows.filter((r) => r.kind === "add")).toHaveLength(500);
    expectValidNumbering(rows);
  });

  it("超过上限 → 整段替换，且 diffTooLarge 事先说得出", () => {
    const over = lines(DIFF_MAX_LINES_PER_SIDE + 1);
    const other = lines(DIFF_MAX_LINES_PER_SIDE + 1, (i) => (i === 0 ? "top" : `l${i}`));
    expect(diffTooLarge(over, other)).toBe(true);
    expect(diffTooLarge("a", "b")).toBe(false);

    const rows = diffLines(over, other);
    expect(rows.filter((r) => r.kind === "equal")).toHaveLength(0);
    expect(rows.filter((r) => r.kind === "del")).toHaveLength(DIFF_MAX_LINES_PER_SIDE + 1);
    expect(rows.filter((r) => r.kind === "add")).toHaveLength(DIFF_MAX_LINES_PER_SIDE + 1);
    // 降级也绝不给错行号。
    expectValidNumbering(rows);
  });

  it("大段零散改动：靠唯一行锚点分段，仍然逐行对上", () => {
    const total = 2_500;
    const changed = new Set(Array.from({ length: 20 }, (_, k) => 500 + k * 75));
    const rows = diffLines(
      lines(total),
      lines(total, (i) => (changed.has(i) ? `x${i}` : `l${i}`))
    );
    expect(rows.filter((r) => r.kind === "equal")).toHaveLength(total - changed.size);
    expect(rows.filter((r) => r.kind === "del")).toHaveLength(changed.size);
    expect(rows.filter((r) => r.kind === "add")).toHaveLength(changed.size);
    expectValidNumbering(rows);
  });
});

describe("hunks — 折叠", () => {
  const run = (kindsSpec: string): DiffRow[] => {
    const rows: DiffRow[] = [];
    let old = 1;
    let next = 1;
    for (const kind of kindsSpec.split(",")) {
      if (kind === "e") rows.push({ kind: "equal", oldNumber: old++, newNumber: next++, text: "c" });
      else if (kind === "-") rows.push({ kind: "del", oldNumber: old++, newNumber: null, text: "d" });
      else rows.push({ kind: "add", oldNumber: null, newNumber: next++, text: "a" });
    }
    return rows;
  };
  const foldCount = (rows: DiffRow[], ctx: number) =>
    hunks(rows, ctx).filter((b) => b.kind === "fold");

  it("空输入 → 空输出；没有改动也不留空块", () => {
    expect(hunks([], 3)).toEqual([]);
    const only = run("e,e,e");
    expect(hunks(only, 3).map((b) => b.kind)).toEqual(["rows"]);
  });

  it("上下文行数 = 2×ctx 的 equal 段不折叠，多一行才折", () => {
    expect(foldCount(run("e,e,e,e,e,e,-,e"), 3)).toHaveLength(0);
    const blocks = hunks(run("e,e,e,e,e,e,e,-,e"), 3);
    expect(blocks.map((b) => b.kind)).toEqual(["rows", "fold", "rows"]);
    const fold = blocks[1];
    expect(fold.kind === "fold" && fold.count).toBe(1);
  });

  it("折叠段的行号指向被省略的第一行", () => {
    const blocks = hunks(run("e,e,e,e,e,e,e,e,-,e"), 3);
    const fold = blocks.find((b) => b.kind === "fold");
    expect(fold).toEqual({ kind: "fold", count: 2, oldStart: 4, newStart: 4 });
  });

  it("ctx=0 时 equal 段全部折叠，且不会留下空 rows 块", () => {
    const blocks = hunks(run("e,e,-,e"), 0);
    expect(blocks.map((b) => b.kind)).toEqual(["fold", "rows", "fold"]);
    for (const b of blocks) if (b.kind === "rows") expect(b.rows.length).toBeGreaterThan(0);
  });

  it("首尾的 equal 段也参与折叠，改动行一个不丢", () => {
    const rows = run("e,e,e,e,e,e,e,e,e,e,-,+,e,e,e,e,e,e,e,e,e,e,e");
    const blocks = hunks(rows, 3);
    const drawn = blocks.flatMap((b) => (b.kind === "rows" ? b.rows : []));
    expect(drawn.filter((r) => r.kind !== "equal")).toHaveLength(2);
    const folded = blocks.filter((b) => b.kind === "fold");
    expect(folded).toHaveLength(2);
    let total = 0;
    for (const b of blocks) total += b.kind === "fold" ? b.count : b.rows.length;
    expect(total).toBe(rows.length);
  });

  it("非法 ctx 当作 0 处理，不抛异常", () => {
    expect(hunks(run("e,e,-"), Number.NaN).map((b) => b.kind)).toEqual(["fold", "rows"]);
    expect(hunks(run("e,e,-"), -5).map((b) => b.kind)).toEqual(["fold", "rows"]);
  });
});

describe("parseUnifiedDiff", () => {
  it("git 风格补丁：行号取 @@ 头，文件头不混进正文", () => {
    const patch = parseUnifiedDiff(
      [
        "diff --git a/src/a.ts b/src/a.ts",
        "index 1234567..89abcde 100644",
        "--- a/src/a.ts",
        "+++ b/src/a.ts",
        "@@ -10,3 +10,4 @@",
        " keep",
        "-old",
        "+new",
        "+extra",
        " tail",
      ].join("\n")
    );
    expect(patch).not.toBeNull();
    expect(patch?.oldPath).toBe("src/a.ts");
    expect(patch?.newPath).toBe("src/a.ts");
    expect(patch?.fileCount).toBe(1);
    expect(patch?.rows.map((r) => [r.kind, r.oldNumber, r.newNumber])).toEqual([
      ["equal", 10, 10],
      ["del", 11, null],
      ["add", null, 11],
      ["add", null, 12],
      ["equal", 12, 13],
    ]);
    expectValidNumbering(patch?.rows ?? []);
  });

  it("多 hunk / 多文件：每个 @@ 头各自重数行号", () => {
    const patch = parseUnifiedDiff(
      [
        "--- a/x",
        "+++ b/x",
        "@@ -1 +1 @@",
        "-one",
        "+ONE",
        "@@ -20 +20 @@",
        "-twenty",
        "+TWENTY",
        "--- a/y",
        "+++ /dev/null",
        "@@ -1,2 +0,0 @@",
        "-gone",
        "-also",
      ].join("\n")
    );
    expect(patch?.fileCount).toBe(2);
    expect(patch?.newPath).toBeNull();
    expect(patch?.rows.map((r) => `${r.kind}:${r.oldNumber ?? ""}${r.newNumber ?? ""}`)).toEqual([
      "del:1",
      "add:1",
      "del:20",
      "add:20",
      "del:1",
      "del:2",
    ]);
  });

  it("省略前导空格的上下文行不会凭空消失", () => {
    const patch = parseUnifiedDiff("@@ -5,3 +5,3 @@\n a\n-b\n+B\n c");
    expect(patch?.rows.map((r) => [r.kind, r.text])).toEqual([
      ["equal", "a"],
      ["del", "b"],
      ["add", "B"],
      ["equal", "c"],
    ]);
  });

  it("没有 @@ 头也能读：按出现顺序从第 1 行数起", () => {
    const patch = parseUnifiedDiff("-old\n+new");
    expect(patch?.rows).toEqual([
      { kind: "del", oldNumber: 1, newNumber: null, text: "old" },
      { kind: "add", oldNumber: null, newNumber: 1, text: "new" },
    ]);
  });

  it("\\ No newline at end of file 不参与行号", () => {
    const patch = parseUnifiedDiff("@@ -1,2 +1,2 @@\n a\n-old\n+new\n\\ No newline at end of file");
    expect(patch?.rows.map((r) => `${r.kind}:${r.oldNumber ?? r.newNumber}`)).toEqual([
      "equal:1",
      "del:2",
      "add:2",
    ]);
  });

  it("认不出来就返回 null，调用方回落到普通代码块", () => {
    expect(parseUnifiedDiff("just some prose\nnothing prefixed")).toBeNull();
    expect(parseUnifiedDiff("")).toBeNull();
    expect(parseUnifiedDiff(" ctx\n ctx2")).toBeNull();
  });

  it("补丁被当成一整个 diff 时不吞行：与 hunks 一起用仍守恒", () => {
    const body = Array.from({ length: 40 }, (_, i) => ` line${i}`).join("\n");
    const patch = parseUnifiedDiff(`@@ -1,40 +1,41 @@\n${body}\n+added`);
    const blocks = hunks(patch?.rows ?? [], 3);
    let total = 0;
    for (const b of blocks) total += b.kind === "fold" ? b.count : b.rows.length;
    expect(total).toBe(41);
    expectValidNumbering(patch?.rows ?? []);
  });
});
