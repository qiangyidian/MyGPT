// composer ``@`` 引用的「落子 + 上限裁决」纯函数测试（条目 26）。
//
// 全部在无 DOM 的 node 环境跑，参照 @/lib/__tests__/kb-retrieval.test.ts 的组织
// 方式。token 的形状只在 @/lib/inline-refs 里定义一次，所以这里也用 encodeRef 造
// 期望值，而不是把 ``@[名字](doc:uuid)`` 再抄一遍。
import { describe, expect, it } from "vitest";

import { encodeRef, findMentionAt } from "@/lib/inline-refs";
import {
  COMPOSER_MAX_CHARS,
  FALLBACK_LIMITS,
  collectMentions,
  droppedNotice,
  insertMention,
  limitsFromMentionList,
  targetToRef,
  type MentionLimits,
} from "@/lib/mention-insert";
import type { MentionTarget } from "@/lib/types";

const KB_ID = "0f1c2b3a-4d5e-4f60-8a9b-aabbccddeeff";
const KB_ID_2 = "11111111-2222-3333-4444-555555555555";
const DOC_ID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee";
const DOC_ID_2 = "abcdef01-2345-6789-abcd-ef0123456789";
const FILE_ID = "00000000-1111-2222-3333-444444444444";

function target(
  kind: MentionTarget["kind"],
  id: string,
  label: string,
  selectable = true,
): MentionTarget {
  return {
    kind,
    id,
    token: `${kind}:${id}`,
    label,
    sublabel: "",
    selectable,
  };
}

function tokenOf(kind: MentionTarget["kind"], id: string, label: string): string {
  return encodeRef({ kind, id, label });
}

const LIMITS: MentionLimits = {
  maxMentions: 8,
  maxKnowledgeBases: 5,
  maxChars: COMPOSER_MAX_CHARS,
};

function limits(over: Partial<MentionLimits>): MentionLimits {
  return { ...LIMITS, ...over };
}

describe("targetToRef", () => {
  it("把候选映射成 token 用的三元组", () => {
    expect(targetToRef(target("doc", DOC_ID, "产品手册.pdf"))).toEqual({
      kind: "doc",
      id: DOC_ID,
      label: "产品手册.pdf",
    });
  });

  it("未知 kind 与空 id 不猜（返回 null）", () => {
    expect(
      targetToRef({
        ...target("doc", DOC_ID, "x"),
        kind: "space" as unknown as MentionTarget["kind"],
      }),
    ).toBeNull();
    expect(targetToRef(target("kb", "", "x"))).toBeNull();
  });
});

describe("limitsFromMentionList", () => {
  it("上限以服务端随响应下发的那一份为准", () => {
    expect(
      limitsFromMentionList({ max_mentions: 12, max_knowledge_bases: 4 }),
    ).toEqual({ maxMentions: 12, maxKnowledgeBases: 4, maxChars: COMPOSER_MAX_CHARS });
  });

  it("缺字段 / 0 / NaN 退回兜底，而不是退回一个写死的数字", () => {
    expect(limitsFromMentionList(null).maxMentions).toBe(FALLBACK_LIMITS.maxMentions);
    expect(
      limitsFromMentionList({ max_mentions: 0, max_knowledge_bases: Number.NaN })
        .maxKnowledgeBases,
    ).toBe(FALLBACK_LIMITS.maxKnowledgeBases);
  });

  it("输入框长度上限由调用方给（这里传 0 也算没给）", () => {
    expect(limitsFromMentionList(null, 1200).maxChars).toBe(1200);
    expect(limitsFromMentionList(null, 0).maxChars).toBe(COMPOSER_MAX_CHARS);
  });
});

describe("insertMention", () => {
  it("``@`` 是最后一个字符：整段换成 token，尾随一个空格", () => {
    const text = "你好 @";
    const t = tokenOf("doc", DOC_ID, "产品手册.pdf");
    const r = insertMention(text, text.length, target("doc", DOC_ID, "产品手册.pdf"), LIMITS);
    expect(r.ok).toBe(true);
    expect(r.text).toBe(`你好 ${t} `);
    expect(r.caret).toBe(3 + t.length + 1);
    // 插入后的光标处不再是一条「正在输入的引用」——弹层因此自然收起。
    expect(findMentionAt(r.text, r.caret)).toBeNull();
  });

  it("``@`` 后面已经有空格：不重复补空格，正文里不会长出空缝", () => {
    const text = "@产品 帮我总结";
    const t = tokenOf("doc", DOC_ID, "产品手册.pdf");
    const r = insertMention(text, 3, target("doc", DOC_ID, "产品手册.pdf"), LIMITS);
    expect(r.ok).toBe(true);
    expect(r.text).toBe(`${t} 帮我总结`);
    expect(r.text).not.toContain("  ");
    expect(r.caret).toBe(t.length);
  });

  it("过滤词整段被替换（用户敲的 ``@项目`` 不该留在正文里）", () => {
    const text = "帮我对比 @项目手册 和 @发布计划";
    const first = insertMention(text, 10, target("doc", DOC_ID, "项目手册"), LIMITS);
    expect(first.ok).toBe(true);
    expect(first.text).toContain(tokenOf("doc", DOC_ID, "项目手册"));
    expect(first.text).not.toContain("@项目手册");
    // 第二个触发词没被这次插入碰到，仍然能弹层。
    expect(findMentionAt(first.text, first.text.length)).not.toBeNull();
  });

  it("全角 ＠ 一样认", () => {
    const text = "＠手册";
    const r = insertMention(text, 3, target("kb", KB_ID, "产品库"), LIMITS);
    expect(r.ok).toBe(true);
    expect(r.text).toContain(tokenOf("kb", KB_ID, "产品库"));
    expect(r.text).not.toContain("＠");
  });

  it("光标不在引用里时就地插入（供「从候选点一下」这种路径用）", () => {
    const text = "看这个：";
    const t = tokenOf("file", FILE_ID, "纪要.txt");
    const r = insertMention(text, text.length, target("file", FILE_ID, "纪要.txt"), LIMITS);
    expect(r.text).toBe(`看这个：${t} `);
    expect(r.caret).toBe(text.length + t.length + 1);
  });

  it("同一目标重复插入被拒，正文原样", () => {
    const once = insertMention("", 0, target("doc", DOC_ID, "手册"), LIMITS);
    const twice = insertMention(once.text, once.caret, target("doc", DOC_ID, "手册"), LIMITS);
    expect(twice.ok).toBe(false);
    expect(twice.reason).toContain("已引用");
    expect(twice.text).toBe(once.text);
  });

  it("目标数上限用服务端下发的数字，并给出中文原因", () => {
    const text = `${tokenOf("doc", DOC_ID, "甲")} ${tokenOf("doc", DOC_ID_2, "乙")}`;
    const r = insertMention(text, text.length, target("doc", FILE_ID, "丙"), limits({ maxMentions: 2 }));
    expect(r.ok).toBe(false);
    expect(r.reason).toContain("2");
    expect(r.reason).toContain("引用");
  });

  it("知识库有自己更小的子上限：文档不受影响", () => {
    const text = tokenOf("kb", KB_ID, "产品库");
    const second = insertMention(`${text} `, text.length + 1, target("kb", KB_ID_2, "发布库"), limits({ maxKnowledgeBases: 1 }));
    expect(second.ok).toBe(false);
    expect(second.reason).toContain("知识库");
    const doc = insertMention(`${text} `, text.length + 1, target("doc", DOC_ID, "手册"), limits({ maxKnowledgeBases: 1 }));
    expect(doc.ok).toBe(true);
  });

  it("会撑破输入框时拒绝，而不是让 textarea 静默截断出半枚 token", () => {
    const text = "正文".repeat(20);
    const r = insertMention(text, text.length, target("doc", DOC_ID, "手册"), limits({ maxChars: text.length + 5 }));
    expect(r.ok).toBe(false);
    expect(r.reason).toContain("上限");
    expect(r.text).toBe(text);
  });

  it("还不可检索的目标即使被点选也插不进去", () => {
    const r = insertMention("@", 1, target("doc", DOC_ID, "还在跑", false), LIMITS);
    expect(r.ok).toBe(false);
    expect(r.reason).toContain("还不可检索");
  });

  it("id 不是 uuid 的目标不写进正文（写了也解不回来）", () => {
    const r = insertMention("", 0, target("doc", "not-a-uuid", "手册"), LIMITS);
    expect(r.ok).toBe(false);
    expect(r.reason).toContain("无法引用");
  });
});

describe("collectMentions", () => {
  it("按正文顺序去重，产出随请求发出的 mentions", () => {
    const text = `先看 ${tokenOf("doc", DOC_ID, "甲")}，再对比 ${tokenOf("doc", DOC_ID_2, "乙")}，回到 ${tokenOf("kb", KB_ID, "库")}，最后 ${tokenOf("doc", DOC_ID, "甲")} 重复`;
    const c = collectMentions(text, LIMITS);
    expect(c.mentions).toEqual([
      { kind: "doc", id: DOC_ID },
      { kind: "doc", id: DOC_ID_2 },
      { kind: "kb", id: KB_ID },
    ]);
    expect(c.dropped).toEqual([]);
  });

  it("普通正文与半个 token 都不会被当成引用", () => {
    expect(collectMentions("帮我总结一下，邮箱 a@x.com", LIMITS).mentions).toEqual([]);
    expect(collectMentions(`@[手册](doc:${DOC_ID.slice(0, 8)}`, LIMITS).mentions).toEqual([]);
  });

  it("超限的部分进 dropped（不静默丢），并给得出中文提醒", () => {
    const text = `${tokenOf("doc", DOC_ID, "甲")} ${tokenOf("doc", DOC_ID_2, "乙")}`;
    const c = collectMentions(text, limits({ maxMentions: 1 }));
    expect(c.mentions).toEqual([{ kind: "doc", id: DOC_ID }]);
    expect(c.dropped.map((r) => r.id)).toEqual([DOC_ID_2]);
    expect(droppedNotice(c.dropped)).toContain("乙");
  });

  it("知识库子上限只裁 kb，不牵连文档", () => {
    const text = `${tokenOf("kb", KB_ID, "库甲")} ${tokenOf("kb", KB_ID_2, "库乙")} ${tokenOf("doc", DOC_ID, "文档")}`;
    const c = collectMentions(text, limits({ maxKnowledgeBases: 1 }));
    expect(c.mentions).toEqual([
      { kind: "kb", id: KB_ID },
      { kind: "doc", id: DOC_ID },
    ]);
    expect(c.dropped.map((r) => r.kind)).toEqual(["kb"]);
  });

  it("没有裁掉任何东西时提醒是 null", () => {
    expect(droppedNotice([])).toBeNull();
  });
});
