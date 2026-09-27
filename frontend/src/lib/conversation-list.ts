// 侧边栏会话列表的分页与分组逻辑（条目 26）。
//
// 全部是纯函数：仓库里的 vitest 跑在 `environment: "node"`（没有 jsdom），
// 组件渲染不可测，所以分页/去重/分组这类会丢数据或重复数据的判断必须离开
// 组件才能被锁住。
//
// 偏移漂移（offset drift）的处理：服务端只提供 `offset/limit`，没有游标。
// 在「第 3 页已加载」时新建一个会话，会让所有行整体下移一位 —— 按
// `上次请求的 offset + 上页长度` 续页会跳过一行，按「缓存里已有多少条」
// 续页则只会拿到重复行（可以按 id 去掉）。所以这里统一用后者：
// 下一页的 offset = 缓存中去重后的行数，永不超过真实覆盖范围，只可能重叠。
import type { Conversation, Project } from "@/lib/types";

/** 每页行数。服务端把单页上限收在 200，侧边栏用 20 保证首屏够快。 */
export const CONVERSATION_PAGE_SIZE = 20;

/** 一页：行 + 它是以哪个 offset 请求来的。 */
export interface ConversationPage {
  offset: number;
  limit: number;
  /**
   * 服务端这一页真正返回的行数。缓存写入（新建置顶、删除）会改 `items`，
   * 但永远不改 `fetched` —— 「还有下一页」必须由服务端返回的条数决定，
   * 否则本地删掉一行就会让列表提前判定到底。
   */
  fetched: number;
  items: Conversation[];
}

export function conversationPage(
  rows: Conversation[],
  offset: number,
  limit: number
): ConversationPage {
  return { offset, limit, fetched: rows.length, items: rows };
}

/** 去重后的扁平列表；同 id 保留第一次出现的那份（更早的页更新更快）。 */
export function flattenConversationPages(
  pages: readonly ConversationPage[] | undefined
): Conversation[] {
  if (!pages || pages.length === 0) return [];
  const seen = new Map<string, Conversation>();
  for (const page of pages) {
    for (const conv of page.items) {
      if (!seen.has(conv.id)) seen.set(conv.id, conv);
    }
  }
  return [...seen.values()];
}

/** 缓存里已有的去重行数 —— 也就是下一页该用的 offset。 */
export function loadedConversationCount(
  pages: readonly ConversationPage[] | undefined
): number {
  if (!pages || pages.length === 0) return 0;
  const ids = new Set<string>();
  for (const page of pages) for (const conv of page.items) ids.add(conv.id);
  return ids.size;
}

/**
 * 下一页的 offset；`null` 表示到底了。
 *
 * 终止条件只看 `lastPage.fetched`（服务端给的条数）：不足一页即真的没有更多。
 */
export function nextConversationOffset(
  lastPage: ConversationPage,
  pages: readonly ConversationPage[]
): number | null {
  if (lastPage.fetched < lastPage.limit) return null;
  return loadedConversationCount(pages);
}

/** 新建的会话插到第一页最前（与服务端「最新在前」一致），已存在则就地替换。 */
export function withConversationPrepended(
  pages: readonly ConversationPage[],
  conv: Conversation
): ConversationPage[] {
  if (pages.length === 0) {
    return [{ offset: 0, limit: CONVERSATION_PAGE_SIZE, fetched: 0, items: [conv] }];
  }
  const removed = withConversationRemoved(pages, conv.id);
  const [first, ...rest] = removed;
  return [{ ...first, items: [conv, ...first.items] }, ...rest];
}

/** 就地更新一行（改名 / 置顶），不改变顺序，也不动 `fetched`。 */
export function withConversationReplaced(
  pages: readonly ConversationPage[],
  conv: Conversation
): ConversationPage[] {
  return pages.map((page) =>
    page.items.some((c) => c.id === conv.id)
      ? { ...page, items: page.items.map((c) => (c.id === conv.id ? conv : c)) }
      : page
  );
}

/** 删掉一行；某页因此变短不会让分页提前结束（见 `ConversationPage.fetched`）。 */
export function withConversationRemoved(
  pages: readonly ConversationPage[],
  id: string
): ConversationPage[] {
  return pages.map((page) =>
    page.items.some((c) => c.id === id)
      ? { ...page, items: page.items.filter((c) => c.id !== id) }
      : page
  );
}

export interface ProjectSection {
  project: Project;
  conversations: Conversation[];
}

export interface SidebarGroups {
  sections: ProjectSection[];
  unassigned: Conversation[];
}

/**
 * 按项目分组。两种「不知道归属」的情况都必须落到未分组，不能整行消失：
 *  - `projects === null`：项目列表还在加载；
 *  - `project_id` 指向一个已经不存在的项目：删除项目留下的悬空引用。
 *
 * `includeEmptyProjects` 在没有搜索词时打开：没有会话的项目也要有分组标题，
 * 否则它的改名 / 删除入口就再也点不到了。
 */
export function groupConversationsByProject(
  conversations: readonly Conversation[],
  projects: readonly Project[] | null,
  includeEmptyProjects = false
): SidebarGroups {
  const known = new Map<string, Project>();
  if (projects) for (const p of projects) known.set(p.id, p);

  const buckets = new Map<string, Conversation[]>();
  const unassigned: Conversation[] = [];
  for (const conv of conversations) {
    const projectId = projects ? conv.project_id : null;
    const project = projectId ? known.get(projectId) : undefined;
    if (!project) {
      unassigned.push(conv);
      continue;
    }
    const bucket = buckets.get(project.id);
    if (bucket) bucket.push(conv);
    else buckets.set(project.id, [conv]);
  }

  const sections: ProjectSection[] = [];
  for (const project of projects ?? []) {
    const rows = buckets.get(project.id);
    if (rows) sections.push({ project, conversations: rows });
    else if (includeEmptyProjects) sections.push({ project, conversations: [] });
  }
  return { sections, unassigned };
}

/**
 * 客户端复刻服务端 `q` 的匹配范围（标题 + 末条预览，大小写不敏感）。
 * 新建会话成功时要靠它判断这条该不该写进当前视图的缓存 —— 判错一次，
 * 用户就会看到一个搜索词根本不匹配的行的凭空出现。
 */
export function conversationMatchesQuery(
  conv: Pick<Conversation, "title" | "last_message_preview">,
  q: string
): boolean {
  const needle = q.trim().toLowerCase();
  if (!needle) return true;
  return (
    (conv.title || "").toLowerCase().includes(needle) ||
    (conv.last_message_preview ?? "").toLowerCase().includes(needle)
  );
}

/** 四态空列表文案：加载失败 / 搜索无结果 / 归档为空 / 真的一个会话都没有。 */
export function conversationEmptyState(args: {
  loading: boolean;
  /** 列表请求失败：说「还没有会话」是把故障说成事实。 */
  error: boolean;
  hasRows: boolean;
  query: string;
  archived: boolean;
}): "none" | "error" | "loading" | "no-results" | "empty-archived" | "empty-active" {
  if (args.hasRows) return "none";
  if (args.error) return "error";
  if (args.loading) return "loading";
  if (args.query.trim()) return "no-results";
  return args.archived ? "empty-archived" : "empty-active";
}
