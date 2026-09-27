// 侧边栏会话列表的分页 / 分组逻辑（条目 26）。
//
// 重点测偏移漂移：中途新建一行会让服务端整体下移，续页的 offset 只能
// 「重复」不能「漏行」—— 漏掉的那一行用户永远翻不到。
import { describe, expect, it } from "vitest";

import {
  CONVERSATION_PAGE_SIZE,
  conversationEmptyState,
  conversationMatchesQuery,
  conversationPage,
  flattenConversationPages,
  groupConversationsByProject,
  loadedConversationCount,
  nextConversationOffset,
  withConversationPrepended,
  withConversationRemoved,
  withConversationReplaced,
  type ConversationPage,
} from "@/lib/conversation-list";
import type { Conversation, Project } from "@/lib/types";

const LIMIT = 10;

function conv(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    user_id: "u1",
    title: `会话 ${id}`,
    model_id: null,
    knowledge_base_id: null,
    system_prompt: null,
    is_pinned: false,
    is_archived: false,
    last_message_preview: null,
    parent_conversation_id: null,
    branch_from_message_id: null,
    project_id: null,
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
    ...overrides,
  };
}

function project(id: string, name = `项目 ${id}`): Project {
  return {
    id,
    name,
    description: null,
    color: "#6366f1",
    created_at: "2026-01-01T00:00:00Z",
    updated_at: "2026-01-01T00:00:00Z",
  };
}

/** 模拟服务端：按 offset/limit 切一片「最新的在前」的行。 */
function pageOf(server: readonly Conversation[], offset: number, limit = LIMIT): ConversationPage {
  return conversationPage(server.slice(offset, offset + limit), offset, limit);
}

describe("flattenConversationPages / loadedConversationCount", () => {
  it("treats missing data as an empty list", () => {
    expect(flattenConversationPages(undefined)).toEqual([]);
    expect(loadedConversationCount(undefined)).toBe(0);
    expect(loadedConversationCount([])).toBe(0);
  });

  it("collapses the overlap a drifted page produces, keeping the first copy", () => {
    const a = conv("a", { title: "页面一里的 a" });
    const aAgain = conv("a", { title: "页面二里的 a（更旧的那次取数）" });
    const pages = [conversationPage([a], 0, LIMIT), conversationPage([aAgain, conv("b")], 1, LIMIT)];

    expect(flattenConversationPages(pages).map((c) => c.id)).toEqual(["a", "b"]);
    expect(flattenConversationPages(pages)[0].title).toBe("页面一里的 a");
    expect(loadedConversationCount(pages)).toBe(2);
  });
});

describe("nextConversationOffset", () => {
  it("stops once the server returned less than a page", () => {
    const pages = [pageOf(Array.from({ length: 3 }, (_, i) => conv(`s${i}`)), 0)];
    expect(nextConversationOffset(pages[0], pages)).toBeNull();
  });

  it("keeps going on a full page, anchored to the distinct cached rows", () => {
    const server = Array.from({ length: 25 }, (_, i) => conv(`s${i}`));
    const pages = [pageOf(server, 0)];
    expect(nextConversationOffset(pages[0], pages)).toBe(LIMIT);
  });

  it("uses the distinct cached rows, not the sum of page lengths", () => {
    const server = Array.from({ length: 25 }, (_, i) => conv(`s${i}`));
    const pages = [pageOf(server, 0), pageOf(server, 5)]; // 故意重叠 5 行
    expect(nextConversationOffset(pages[1], pages)).toBe(15);
  });

  it("ignores how many rows a page still holds after a local delete", () => {
    // 删掉一行不能把列表判成「到底」：fetched 是服务端给的条数。
    const server = Array.from({ length: 25 }, (_, i) => conv(`s${i}`));
    const page = pageOf(server, 0);
    const edited = withConversationRemoved([page], "s0");
    expect(edited[0].items).toHaveLength(LIMIT - 1);
    expect(edited[0].fetched).toBe(LIMIT);
    expect(nextConversationOffset(edited[0], edited)).toBe(LIMIT - 1);
  });
});

describe("offset drift", () => {
  it("walks every row exactly once when a new conversation lands mid-walk", () => {
    const server = Array.from({ length: 25 }, (_, i) => conv(`s${i}`));
    // 只有「开始翻页时就在那儿」的行是被承诺的：中途新建的那一行排在已覆盖
    // 范围之上，任何 offset 分页都不会回头去补它（它靠下一次列表刷新出现）。
    const originalIds = server.map((c) => c.id);
    const pages: ConversationPage[] = [];
    const collected = new Set<string>();
    let offset = 0;

    for (let guard = 0; guard < 20; guard += 1) {
      if (guard === 2) {
        // 用户翻到第 3 页时别处新建了一个会话：服务端整表下移一位。
        server.unshift(conv("new"));
      }
      const page = pageOf(server, offset);
      for (const c of page.items) collected.add(c.id);
      pages.push(page);
      const next = nextConversationOffset(page, pages);
      if (next === null) break;
      offset = next;
    }

    // 不漏行：漂移只会把已看过的行再推一遍，不会跳过任何一行。
    const seenOriginal = [...collected].filter((id) => id !== "new");
    expect(seenOriginal.sort()).toEqual([...originalIds].sort());
    expect(collected.has("new")).toBe(false);
    // 重叠确实发生过：s19 既是第 2 页的最后一行，也是第 3 页的第一行。
    const lastPage = pages[pages.length - 1];
    expect(lastPage.items[0].id).toBe("s19");
    expect(lastPage.items[1].id).toBe("s20");
    // 不重复行：展平时按 id 去重。
    const flat = flattenConversationPages(pages).map((c) => c.id);
    expect(new Set(flat).size).toBe(flat.length);
    expect(flat).toHaveLength(25);
    // 兜底：测试本身不能死循环。
    expect(pages.length).toBeLessThan(20);
  });

  it("anchors the next offset on the cache after writing a create locally", () => {
    const server = Array.from({ length: 25 }, (_, i) => conv(`s${i}`));
    const pages = [pageOf(server, 0)];
    const afterCreate = withConversationPrepended(pages, conv("new"));

    // 新建被直接写进第一页（不重取），所以缓存里有 11 行、下一页从 11 起。
    // 服务端此时是 [new, s0 … s24]：offset 11 正是 s10，也就是缓存之后紧接着
    // 的第一行 —— 这一条路径上既不重叠也不漏行。
    expect(afterCreate[0].items.map((c) => c.id)[0]).toBe("new");
    expect(loadedConversationCount(afterCreate)).toBe(11);
    expect(nextConversationOffset(afterCreate[0], afterCreate)).toBe(11);

    const shiftedServer = [conv("new"), ...server];
    expect(pageOf(shiftedServer, 11).items.map((c) => c.id)[0]).toBe("s10");
  });

  it("creates a first page when the list was empty", () => {
    const pages = withConversationPrepended([], conv("only"));
    expect(pages).toHaveLength(1);
    expect(pages[0]).toMatchObject({ offset: 0, fetched: 0, items: [expect.objectContaining({ id: "only" })] });
    // fetched=0 < limit：空列表上凭空写入一行不会假装还有下一页。
    expect(nextConversationOffset(pages[0], pages)).toBeNull();
  });
});

describe("cache writes", () => {
  it("replaces a row in place without touching order or fetched", () => {
    const pages = [pageOf(Array.from({ length: 3 }, (_, i) => conv(`s${i}`)), 0, LIMIT)];
    const replaced = withConversationReplaced(pages, conv("s1", { title: "改名后" }));
    expect(replaced[0].items.map((c) => c.id)).toEqual(["s0", "s1", "s2"]);
    expect(replaced[0].items[1].title).toBe("改名后");
    expect(replaced[0].fetched).toBe(3);
  });

  it("leaves other pages alone and drops a row from every page", () => {
    const server = Array.from({ length: 4 }, (_, i) => conv(`s${i}`));
    const pages = [pageOf(server, 0, 2), pageOf(server, 2, 2)];
    const removed = withConversationRemoved(pages, "s1");
    expect(removed[0].items.map((c) => c.id)).toEqual(["s0"]);
    expect(removed[0].fetched).toBe(2);
    expect(removed[1].items.map((c) => c.id)).toEqual(["s2", "s3"]);
  });

  it("moves an archived row out of the active page instead of editing it", () => {
    const pages = [pageOf([conv("s0"), conv("s1")], 0, 2)];
    const archived = withConversationReplaced(pages, conv("s0", { is_archived: true }));
    const removed = withConversationRemoved(pages, "s0");
    expect(archived[0].items[0].is_archived).toBe(true);
    expect(removed[0].items.map((c) => c.id)).toEqual(["s1"]);
  });
});

describe("groupConversationsByProject", () => {
  const p1 = project("p1");
  const p2 = project("p2");

  it("falls back to unassigned while the project list is still loading", () => {
    // projects === null 时把行按 project_id 藏起来，就等于「我的会话不见了」。
    const rows = [conv("a", { project_id: "p1" }), conv("b")];
    const groups = groupConversationsByProject(rows, null);
    expect(groups.sections).toEqual([]);
    expect(groups.unassigned.map((c) => c.id)).toEqual(["a", "b"]);
  });

  it("keeps rows with a dangling project_id visible", () => {
    const groups = groupConversationsByProject([conv("a", { project_id: "gone" })], [p1]);
    expect(groups.unassigned.map((c) => c.id)).toEqual(["a"]);
  });

  it("buckets rows per project in project order", () => {
    const groups = groupConversationsByProject(
      [conv("a", { project_id: "p2" }), conv("b", { project_id: "p1" }), conv("c")],
      [p1, p2]
    );
    expect(groups.sections.map((s) => [s.project.id, s.conversations.map((c) => c.id)])).toEqual([
      ["p1", ["b"]],
      ["p2", ["a"]],
    ]);
    expect(groups.unassigned.map((c) => c.id)).toEqual(["c"]);
  });

  it("omits empty projects unless asked to keep their header reachable", () => {
    const withEmpty = groupConversationsByProject([conv("a", { project_id: "p1" })], [p1, p2], true);
    expect(withEmpty.sections.map((s) => s.project.id)).toEqual(["p1", "p2"]);
    expect(withEmpty.sections[1].conversations).toEqual([]);

    const withoutEmpty = groupConversationsByProject([conv("a", { project_id: "p1" })], [p1, p2], false);
    expect(withoutEmpty.sections.map((s) => s.project.id)).toEqual(["p1"]);
  });
});

describe("conversationMatchesQuery", () => {
  it("matches everything while the search box is blank", () => {
    expect(conversationMatchesQuery(conv("a"), "")).toBe(true);
    expect(conversationMatchesQuery(conv("a"), "   ")).toBe(true);
  });

  it("is case-insensitive over the title, mirroring the server's ilike", () => {
    expect(conversationMatchesQuery(conv("a", { title: "Quarterly Report" }), "QUARTERLY")).toBe(true);
    expect(conversationMatchesQuery(conv("a", { title: "Quarterly Report" }), "monthly")).toBe(false);
  });

  it("also matches the last-message preview", () => {
    const row = conv("a", { title: "新对话", last_message_preview: "帮我看下这份预算表" });
    expect(conversationMatchesQuery(row, "预算")).toBe(true);
    expect(conversationMatchesQuery(row, "预算表 ")).toBe(true);
  });

  it("tolerates a missing preview", () => {
    expect(() => conversationMatchesQuery(conv("a"), "x")).not.toThrow();
    expect(conversationMatchesQuery({ title: "x", last_message_preview: null }, "y")).toBe(false);
  });
});

describe("conversationEmptyState", () => {
  it("never shows a loading or empty message while rows are on screen", () => {
    expect(
      conversationEmptyState({
        loading: true,
        error: true,
        hasRows: true,
        query: "x",
        archived: false,
      })
    ).toBe("none");
  });

  it("separates loading from truly empty", () => {
    expect(
      conversationEmptyState({
        loading: true,
        error: false,
        hasRows: false,
        query: "",
        archived: false,
      })
    ).toBe("loading");
  });

  it("calls a failed load 加载失败 instead of 还没有会话", () => {
    expect(
      conversationEmptyState({
        loading: false,
        error: true,
        hasRows: false,
        query: "",
        archived: false,
      })
    ).toBe("error");
    // 归档视图里失败也是失败，不是「归档为空」。
    expect(
      conversationEmptyState({ loading: false, error: true, hasRows: false, query: "", archived: true })
    ).toBe("error");
  });

  it("says 没有匹配 instead of 还没有会话 when a search is in effect", () => {
    expect(
      conversationEmptyState({
        loading: false,
        error: false,
        hasRows: false,
        query: "季度",
        archived: false,
      })
    ).toBe("no-results");
    expect(
      conversationEmptyState({ loading: false, error: false, hasRows: false, query: "   ", archived: true })
    ).toBe("empty-archived");
  });

  it("distinguishes the archived view from the active one", () => {
    expect(
      conversationEmptyState({ loading: false, error: false, hasRows: false, query: "", archived: true })
    ).toBe("empty-archived");
    expect(
      conversationEmptyState({
        loading: false,
        error: false,
        hasRows: false,
        query: "",
        archived: false,
      })
    ).toBe("empty-active");
  });
});

describe("page size", () => {
  it("stays well under the server's per-request cap", () => {
    expect(CONVERSATION_PAGE_SIZE).toBeGreaterThan(0);
    expect(CONVERSATION_PAGE_SIZE).toBeLessThanOrEqual(200);
  });
});
