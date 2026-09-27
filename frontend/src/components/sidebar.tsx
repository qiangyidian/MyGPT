"use client";

import Link from "next/link";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  Archive,
  ArchiveRestore,
  Boxes,
  Coins,
  FolderInput,
  FolderPlus,
  Loader2,
  LogOut,
  MessageSquareMore,
  MessageSquarePlus,
  MoreHorizontal,
  Pencil,
  Pin,
  Search,
  Settings,
  Shield,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";

import { cn } from "@/lib/utils";
import { withReturnTo } from "@/lib/navigation";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuSub,
  DropdownMenuSubContent,
  DropdownMenuSubTrigger,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ThemeToggle } from "@/components/theme-toggle";
import { api } from "@/lib/api";
import { setAccessToken } from "@/lib/auth";
import { ConversationSystemPromptDialog } from "@/components/conversation-system-prompt-dialog";
import { DeleteAccountDialog } from "@/components/delete-account-dialog";
import { ProjectDeleteDialog } from "@/components/project-delete-dialog";
import type { Conversation, Project, User } from "@/lib/types";
import { useCredits } from "@/hooks/useCredits";
import { formatCredits } from "@/lib/credits";
import {
  conversationEmptyState,
  groupConversationsByProject,
} from "@/lib/conversation-list";
import { projectRenamePatch, validateProjectName } from "@/lib/projects";

interface SidebarProps {
  conversations: Conversation[];
  activeConversationId: string | null;
  onSelectConversation: (id: string) => void;
  onNewChat: () => void;
  onRename: (id: string, title: string) => void;
  onTogglePin: (id: string, pinned: boolean) => void;
  onToggleArchive: (id: string, archived: boolean) => void;
  onDeleteConversation: (id: string) => void;
  user: User | null;
  onLogout: () => void;
  viewMode: "active" | "archived";
  onViewModeChange: (mode: "active" | "archived") => void;
  /**
   * Server-side search (条目 26): the box is controlled from the app shell and
   * debounced there, so what reaches the API is `q` — not every keystroke.
   */
  searchQuery: string;
  onSearchQueryChange: (query: string) => void;
  /** True while the debounced value hasn't caught up with the input. */
  isSearchPending: boolean;
  /** Paging: what the infinite list currently knows. */
  hasNextPage: boolean;
  isLoadingMore: boolean;
  /** 已加载条数（服务端第一页起算，去重后）。 */
  loadedCount: number;
  /** 列表正在取数（含防抖后的新搜索），用来和「真的没有会话」区分开。 */
  isListFetching: boolean;
  /** 列表请求失败：空状态必须说「加载失败 + 重试」，不能说「还没有会话」。 */
  isListError: boolean;
  onRetryList: () => void;
  onLoadMore: () => void;
  /**
   * Projects for sidebar grouping + the "move to project" action.
   * `null` = 还在加载 / 未知：此时一律按未分组渲染，绝不因为分组信息缺失
   * 就把会话行藏掉（`project_id` 悬空时行会整个消失，就是这个坑）。
   */
  projects?: Project[] | null;
  onAssignToProject?: (conversationId: string, projectId: string) => void;
  onRemoveFromProject?: (conversationId: string) => void;
  onCreateProject?: (name: string) => void;
  onRenameProject?: (projectId: string, name: string) => Promise<void>;
  onDeleteProject?: (projectId: string) => Promise<void>;
  onUpdateSystemPrompt?: (conversationId: string, systemPrompt: string | null) => Promise<void>;
  /**
   * Sanitised "return to chat" target (e.g. `/?conversation=<id>`) forwarded as
   * `returnTo` on the 知识库 / 设置 / 管理 links so leaving and coming back
   * restores the current conversation.
   */
  returnTo?: string;
  className?: string;
}

type Bucket = "today" | "yesterday" | "week" | "older";
const BUCKET_LABEL: Record<Bucket, string> = {
  today: "今天",
  yesterday: "昨天",
  week: "最近 7 天",
  older: "更早",
};
const BUCKET_ORDER: Bucket[] = ["today", "yesterday", "week", "older"];

function bucketOf(updatedAt: string): Bucket {
  const d = new Date(updatedAt);
  const now = new Date();
  const startToday = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime();
  const day = 86_400_000;
  const t = d.getTime();
  if (t >= startToday) return "today";
  if (t >= startToday - day) return "yesterday";
  if (t >= startToday - 7 * day) return "week";
  return "older";
}

// Doubao-style per-conversation pastel bubble icon: hash the stable id into a
// small palette so each conversation keeps one color across renders/sessions.
const BUBBLE_COLORS = [
  "bg-amber-100 text-amber-600",
  "bg-emerald-100 text-emerald-600",
  "bg-sky-100 text-sky-600",
  "bg-rose-100 text-rose-600",
  "bg-violet-100 text-violet-600",
  "bg-teal-100 text-teal-600",
  "bg-orange-100 text-orange-600",
  "bg-fuchsia-100 text-fuchsia-600",
];

function bubbleColorOf(id: string): string {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return BUBBLE_COLORS[Math.abs(h) % BUBBLE_COLORS.length];
}

export function Sidebar({
  conversations,
  activeConversationId,
  onSelectConversation,
  onNewChat,
  onRename,
  onTogglePin,
  onToggleArchive,
  onDeleteConversation,
  user,
  onLogout,
  viewMode,
  onViewModeChange,
  searchQuery,
  onSearchQueryChange,
  isSearchPending,
  hasNextPage,
  isLoadingMore,
  loadedCount,
  isListFetching,
  isListError,
  onRetryList,
  onLoadMore,
  projects,
  onAssignToProject,
  onRemoveFromProject,
  onCreateProject,
  onRenameProject,
  onDeleteProject,
  onUpdateSystemPrompt,
  returnTo,
  className,
}: SidebarProps) {
  const [deleteAccountOpen, setDeleteAccountOpen] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [focusedIndex, setFocusedIndex] = useState(0);
  // 项目改名：一次只有一个标题在编辑，错误就地显示（不弹 toast，
  // 输入框还在用户手上进来的路径上）。
  const [editingProjectId, setEditingProjectId] = useState<string | null>(null);
  const [projectDraft, setProjectDraft] = useState("");
  const [projectError, setProjectError] = useState<string | null>(null);
  const [promptForId, setPromptForId] = useState<string | null>(null);
  const [projectToDelete, setProjectToDelete] = useState<string | null>(null);
  const { credits } = useCredits();

  // 搜索已经交给服务端（`q`），所以这里不再对已加载的行做二次过滤：
  // 那样只能搜到「已经翻到的那几页」，用户以为搜过了全部，其实没有。
  const hasQuery = searchQuery.trim().length > 0;
  const { sections, unassigned } = useMemo(
    () => groupConversationsByProject(conversations, projects ?? null, !hasQuery),
    [conversations, projects, hasQuery]
  );

  const pinned = unassigned.filter((c) => c.is_pinned);
  const nonPinned = unassigned.filter((c) => !c.is_pinned);
  const byBucket = (Bucket: Bucket) => nonPinned.filter((c) => bucketOf(c.updated_at) === Bucket);

  // Flat ordered id list for keyboard navigation.
  const flatIds = useMemo(() => {
    const order: string[] = [];
    pinned.forEach((c) => order.push(c.id));
    BUCKET_ORDER.forEach((b) => byBucket(b).forEach((c) => order.push(c.id)));
    return order;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [unassigned]);

  useEffect(() => {
    if (focusedIndex > flatIds.length - 1) setFocusedIndex(Math.max(0, flatIds.length - 1));
  }, [flatIds.length, focusedIndex]);

  // 滚到底自动补下一页。哨兵用 state callback ref 拿：它在「空列表 → 有列表」
  // 时才挂载，只依赖 hasNextPage 的话第一次挂载会被漏掉。
  const [sentinel, setSentinel] = useState<HTMLDivElement | null>(null);
  const loadMoreRef = useRef(onLoadMore);
  useEffect(() => {
    loadMoreRef.current = onLoadMore;
  }, [onLoadMore]);
  useEffect(() => {
    if (!sentinel || !hasNextPage || isLoadingMore) return;
    if (typeof IntersectionObserver === "undefined") return;
    const observer = new IntersectionObserver(
      (entries) => {
        if (entries.some((entry) => entry.isIntersecting)) loadMoreRef.current();
      },
      { rootMargin: "160px" }
    );
    observer.observe(sentinel);
    return () => observer.disconnect();
  }, [sentinel, hasNextPage, isLoadingMore]);

  const commitRename = (id: string) => {
    const title = editingValue.trim();
    if (title) onRename(id, title);
    setEditingId(null);
  };

  const startProjectRename = (project: Project) => {
    setEditingProjectId(project.id);
    setProjectDraft(project.name);
    setProjectError(null);
  };

  /** `enter` 提交时留下错误让用户改；`blur` 时放弃这次改名（不卡住输入框）。 */
  const commitProjectRename = (project: Project, via: "enter" | "blur") => {
    const error = validateProjectName(projectDraft);
    if (error) {
      if (via === "enter") setProjectError(error);
      else {
        setEditingProjectId(null);
        setProjectError(null);
      }
      return;
    }
    const patch = projectRenamePatch(project, projectDraft);
    setEditingProjectId(null);
    setProjectError(null);
    if (patch) void onRenameProject?.(project.id, patch.name);
  };

  const onKeyDownList = (e: React.KeyboardEvent) => {
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setFocusedIndex((i) => Math.min(i + 1, flatIds.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setFocusedIndex((i) => Math.max(i - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      const id = flatIds[focusedIndex];
      if (id) onSelectConversation(id);
    }
  };

  const initials = user?.username ? user.username.slice(0, 2).toUpperCase() : "?";

  const renderItem = (conv: Conversation) => {
    const flatIndex = flatIds.indexOf(conv.id);
    const isActive = activeConversationId === conv.id;
    const isEditing = editingId === conv.id;

    return (
      <li key={conv.id} className="group relative">
        <button
          type="button"
          className={cn(
            "flex w-full items-center gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors",
            isActive
              ? "bg-background text-foreground shadow-sm"
              : "text-muted-foreground hover:bg-background/60 hover:text-foreground"
          )}
          tabIndex={flatIndex === focusedIndex ? 0 : -1}
          onClick={() => onSelectConversation(conv.id)}
          onDoubleClick={() => {
            setEditingId(conv.id);
            setEditingValue(conv.title);
          }}
        >
          {/* Doubao-style pastel bubble avatar, color derived from the id. */}
          <span
            className={cn(
              "flex h-8 w-8 max-sm:h-9 max-sm:w-9 shrink-0 items-center justify-center rounded-full",
              bubbleColorOf(conv.id)
            )}
          >
            <MessageSquareMore className="h-3.5 w-3.5" />
          </span>
          {isEditing ? (
            <Input
              autoFocus
              value={editingValue}
              onChange={(e) => setEditingValue(e.target.value)}
              onBlur={() => commitRename(conv.id)}
              onKeyDown={(e) => {
                if (e.key === "Enter") {
                  e.preventDefault();
                  commitRename(conv.id);
                } else if (e.key === "Escape") {
                  setEditingId(null);
                }
              }}
              onClick={(e) => e.stopPropagation()}
              className="h-7 text-sm"
            />
          ) : (
            <span className="flex min-w-0 flex-1 items-center gap-1 truncate">
              {conv.is_pinned && <Pin className="h-3 w-3 shrink-0 text-muted-foreground" />}
              <span className="truncate">{conv.title || "新对话"}</span>
              {/* 这个会话设过自定义系统提示词 —— 没有标记的话，行为差异
                  只能靠用户自己记住哪几段对话被改过设定。 */}
              {!!conv.system_prompt?.trim() && (
                <Sparkles
                  className="h-3 w-3 shrink-0 text-muted-foreground"
                  aria-label="已设定系统提示词"
                />
              )}
            </span>
          )}
        </button>

        {/* Per-row menu */}
        <div className="absolute right-1.5 top-1/2 -translate-y-1/2 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-8 w-8 max-sm:h-9 max-sm:w-9 text-muted-foreground"
                aria-label={`对话 ${conv.title} 操作`}
              >
                <Settings className="h-3.5 w-3.5" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-44">
              <DropdownMenuLabel className="truncate text-xs text-muted-foreground">
                {conv.title || "新对话"}
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="gap-2"
                onClick={() => {
                  setEditingId(conv.id);
                  setEditingValue(conv.title);
                }}
              >
                <Pencil className="h-4 w-4" /> 重命名
              </DropdownMenuItem>
              {onUpdateSystemPrompt && (
                <DropdownMenuItem className="gap-2" onClick={() => setPromptForId(conv.id)}>
                  <Sparkles className="h-4 w-4" /> 系统提示词
                </DropdownMenuItem>
              )}
              <DropdownMenuItem className="gap-2" onClick={() => onTogglePin(conv.id, !conv.is_pinned)}>
                <Pin className="h-4 w-4" /> {conv.is_pinned ? "取消置顶" : "置顶"}
              </DropdownMenuItem>
              <DropdownMenuItem className="gap-2" onClick={() => onToggleArchive(conv.id, !conv.is_archived)}>
                {conv.is_archived ? <ArchiveRestore className="h-4 w-4" /> : <Archive className="h-4 w-4" />}
                {conv.is_archived ? "取消归档" : "归档"}
              </DropdownMenuItem>
              {onAssignToProject && (
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger className="gap-2 text-xs">
                    <FolderInput className="h-4 w-4" /> 移入项目
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent>
                    {projects?.map((p) => (
                      <DropdownMenuItem key={p.id} className="gap-2" onClick={() => onAssignToProject(conv.id, p.id)}>
                        <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
                        {p.name}
                      </DropdownMenuItem>
                    ))}
                    {(!projects || projects.length === 0) && (
                      <p className="px-2 py-1.5 text-[11px] text-muted-foreground">
                        暂无项目，先创建一个：
                      </p>
                    )}
                    {onCreateProject && (
                      <DropdownMenuItem
                        className="gap-2"
                        onClick={() => {
                          const name = window.prompt("新建项目名称");
                          if (name && name.trim()) onCreateProject(name.trim());
                        }}
                      >
                        <FolderPlus className="h-4 w-4" /> 新建项目
                      </DropdownMenuItem>
                    )}
                  </DropdownMenuSubContent>
                </DropdownMenuSub>
              )}
              {conv.project_id && onRemoveFromProject && (
                <DropdownMenuItem className="gap-2" onClick={() => onRemoveFromProject(conv.id)}>
                  <FolderInput className="h-4 w-4" /> 移出项目
                </DropdownMenuItem>
              )}
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="gap-2 text-destructive focus:text-destructive"
                onClick={() => onDeleteConversation(conv.id)}
              >
                <Trash2 className="h-4 w-4" /> 删除
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </li>
    );
  };

  const renderGroup = (label: string, items: Conversation[]) =>
    items.length ? (
      <div key={label} className="mb-1">
        <p className="px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
          {label}
        </p>
        <ul className="space-y-0.5">{items.map((c) => renderItem(c))}</ul>
      </div>
    ) : null;

  // 项目分组标题：和会话行同一套交互（双击改名、右侧「…」菜单），
  // 改名内联、删除走带影响范围的确认框。
  const renderProjectSection = (project: Project, items: Conversation[]) => {
    const isEditing = editingProjectId === project.id;
    return (
      <div className="mb-1" key={project.id}>
        <div className="group relative flex items-center gap-1.5 pr-8">
          {isEditing ? (
            <div className="min-w-0 flex-1">
              <Input
                autoFocus
                value={projectDraft}
                aria-label={`重命名项目 ${project.name}`}
                onChange={(e) => {
                  setProjectDraft(e.target.value);
                  if (projectError) setProjectError(null);
                }}
                onBlur={() => commitProjectRename(project, "blur")}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    commitProjectRename(project, "enter");
                  } else if (e.key === "Escape") {
                    setEditingProjectId(null);
                    setProjectError(null);
                  }
                }}
                className="h-7 text-sm"
              />
              {projectError && <p className="px-1 pt-1 text-[11px] text-destructive">{projectError}</p>}
            </div>
          ) : (
            <p
              className="flex min-w-0 flex-1 items-center gap-1.5 px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground"
              onDoubleClick={() => startProjectRename(project)}
              title="双击重命名"
            >
              <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: project.color }} />
              <span className="truncate">{project.name}</span>
              <span className="shrink-0 tabular-nums normal-case">{items.length}</span>
            </p>
          )}
          {!isEditing && (onRenameProject || onDeleteProject) && (
            <div className="absolute right-1 top-1/2 -translate-y-1/2 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
              <DropdownMenu>
                <DropdownMenuTrigger asChild>
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6 text-muted-foreground"
                    aria-label={`项目 ${project.name} 操作`}
                  >
                    <MoreHorizontal className="h-3.5 w-3.5" />
                  </Button>
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-40">
                  <DropdownMenuLabel className="truncate text-xs text-muted-foreground">
                    {project.name}
                  </DropdownMenuLabel>
                  <DropdownMenuSeparator />
                  {onRenameProject && (
                    <DropdownMenuItem className="gap-2" onClick={() => startProjectRename(project)}>
                      <Pencil className="h-4 w-4" /> 重命名
                    </DropdownMenuItem>
                  )}
                  {onDeleteProject && (
                    <DropdownMenuItem
                      className="gap-2 text-destructive focus:text-destructive"
                      onClick={() => setProjectToDelete(project.id)}
                    >
                      <Trash2 className="h-4 w-4" /> 删除项目
                    </DropdownMenuItem>
                  )}
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
          )}
        </div>
        {items.length > 0 ? (
          <ul className="space-y-0.5">{items.map((c) => renderItem(c))}</ul>
        ) : (
          <p className="px-3 py-1 text-[11px] text-muted-foreground">还没有会话归到这个项目</p>
        )}
      </div>
    );
  };

  const emptyState = conversationEmptyState({
    loading: isListFetching && conversations.length === 0,
    error: isListError,
    hasRows: conversations.length > 0,
    query: searchQuery,
    archived: viewMode === "archived",
  });

  return (
    <aside className={cn("flex h-full w-full flex-col bg-secondary/40", className)}>
      <div className="p-3">
        <Button onClick={onNewChat} className="w-full justify-start gap-2">
          <MessageSquarePlus className="h-4 w-4" />
          新建对话
        </Button>
        <div className="relative mt-2">
          <Search className="absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <Input
            id="conversation-search"
            // type="text" 而不是 "search"：浏览器自带的清除按钮会和下面这个
            // 自定义的 X 叠在同一个位置，两个都做同一件事。
            type="text"
            enterKeyHint="search"
            value={searchQuery}
            onChange={(e) => onSearchQueryChange(e.target.value)}
            placeholder="搜索对话（标题与最近一句）"
            className="h-9 pl-8 pr-8 text-sm"
            aria-label="搜索对话"
            aria-describedby="conversation-search-hint"
          />
          {(isSearchPending || hasQuery) && (
            <button
              type="button"
              onClick={() => onSearchQueryChange("")}
              aria-label="清除搜索"
              className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-0.5 text-muted-foreground hover:text-foreground"
            >
              {isSearchPending ? (
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
              ) : (
                <X className="h-3.5 w-3.5" />
              )}
            </button>
          )}
          <p id="conversation-search-hint" className="sr-only">
            输入后按服务端搜索全部会话，不只是已经加载出来的那几页。
          </p>
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {emptyState !== "none" ? (
          <div className="px-3 py-8 text-center text-xs text-muted-foreground">
            {emptyState === "loading" && (
              <p className="flex items-center justify-center gap-2">
                <Loader2 className="h-3.5 w-3.5 animate-spin" />
                正在加载会话……
              </p>
            )}
            {emptyState === "error" && (
              <>
                <p>会话列表加载失败</p>
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto p-0 text-xs"
                  onClick={onRetryList}
                >
                  重试
                </Button>
              </>
            )}
            {emptyState === "no-results" && (
              <>
                <p>没有匹配的会话</p>
                <Button
                  variant="link"
                  size="sm"
                  className="h-auto p-0 text-xs"
                  onClick={() => onSearchQueryChange("")}
                >
                  清除搜索条件
                </Button>
              </>
            )}
            {emptyState === "empty-archived" && <p>还没有归档的会话</p>}
            {emptyState === "empty-active" && (
              <>
                <p>还没有会话</p>
                <p className="mt-1">点上方「新建对话」开始，历史超过一屏会自动分页。</p>
              </>
            )}
          </div>
        ) : (
          <>
            <div
              role="listbox"
              aria-label="对话列表"
              tabIndex={0}
              onKeyDown={onKeyDownList}
            >
              {sections.map(({ project, conversations: rows }) =>
                renderProjectSection(project, rows)
              )}
              {renderGroup("置顶", pinned)}
              {BUCKET_ORDER.map((b) => renderGroup(BUCKET_LABEL[b], byBucket(b)))}
            </div>

            <div className="mt-1 space-y-1 pb-1">
              {hasNextPage ? (
                <Button
                  variant="ghost"
                  size="sm"
                  className="w-full gap-2 text-muted-foreground"
                  onClick={onLoadMore}
                  disabled={isLoadingMore}
                >
                  {isLoadingMore ? (
                    <>
                      <Loader2 className="h-3.5 w-3.5 animate-spin" />
                      正在加载……
                    </>
                  ) : (
                    <>加载更多（已显示 {loadedCount} 条）</>
                  )}
                </Button>
              ) : (
                <p className="py-1 text-center text-[11px] text-muted-foreground">
                  已显示全部 {loadedCount} 条会话
                </p>
              )}
              {/* IntersectionObserver 哨兵：滚到列表底部自动补下一页。 */}
              <div ref={setSentinel} aria-hidden className="h-px" />
            </div>
          </>
        )}
      </div>

      <div className="border-t border-border p-2">
        <nav className="mb-1 flex flex-col gap-0.5">
          <Button
            variant={viewMode === "archived" ? "secondary" : "ghost"}
            size="sm"
            className="w-full justify-start gap-2 text-muted-foreground"
            onClick={() => onViewModeChange(viewMode === "archived" ? "active" : "archived")}
          >
            {viewMode === "archived" ? <ArchiveRestore className="h-4 w-4" /> : <Archive className="h-4 w-4" />}
            {viewMode === "archived" ? "返回对话" : "归档"}
          </Button>
          <Button asChild variant="ghost" size="sm" className="w-full justify-start gap-2 text-muted-foreground">
            <Link href={withReturnTo("/settings/knowledge-bases", returnTo)}>
              <Boxes className="h-4 w-4" />
              知识库
            </Link>
          </Button>
          <Button asChild variant="ghost" size="sm" className="w-full justify-start gap-2 text-muted-foreground">
            <Link href={withReturnTo("/settings/credits", returnTo)} className="w-full">
              <span className="flex w-full items-center justify-between">
                <span className="flex items-center gap-2">
                  <Coins className="h-4 w-4" />
                  积分
                </span>
                <span className="font-mono tabular-nums">
                  {credits ? formatCredits(credits.balance) : "—"}
                </span>
              </span>
            </Link>
          </Button>
          <Button asChild variant="ghost" size="sm" className="w-full justify-start gap-2 text-muted-foreground">
            <Link href={withReturnTo("/settings", returnTo)}>
              <Settings className="h-4 w-4" />
              设置
            </Link>
          </Button>
          {user?.role === "admin" && (
            <Button asChild variant="ghost" size="sm" className="w-full justify-start gap-2 text-muted-foreground">
              <Link href={withReturnTo("/admin", returnTo)}>
                <Shield className="h-4 w-4" />
                管理
              </Link>
            </Button>
          )}
        </nav>

        <div className="flex items-center justify-between gap-2 rounded-md px-1 py-1">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <button
                type="button"
                className="flex min-w-0 flex-1 items-center gap-2 rounded-md px-2 py-1.5 text-left hover:bg-accent"
              >
                <Avatar className="h-7 w-7">
                  <AvatarFallback className="bg-primary text-[11px] text-primary-foreground">
                    {initials}
                  </AvatarFallback>
                </Avatar>
                <span className="min-w-0 flex-1 truncate text-sm font-medium">
                  {user?.username ?? "用户"}
                </span>
              </button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="start" className="w-48">
              <DropdownMenuLabel className="truncate">{user?.email ?? "未登录"}</DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem onClick={onLogout} className="gap-2 text-destructive focus:text-destructive">
                <LogOut className="h-4 w-4" />
                退出登录
              </DropdownMenuItem>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                className="gap-2 text-destructive focus:text-destructive"
                onSelect={() => setDeleteAccountOpen(true)}
              >
                <Trash2 className="h-4 w-4" />
                注销账号
              </DropdownMenuItem>
            </DropdownMenuContent>
          </DropdownMenu>
          <ThemeToggle />
        </div>
        <DeleteAccountDialog
          open={deleteAccountOpen}
          onOpenChange={setDeleteAccountOpen}
          onDeleted={() => {
            // Session cleared server-side; hard navigation re-runs the auth gate.
            window.location.href = "/login";
          }}
          deleteAccount={async (password: string) => {
            await api.deleteMyAccount(password);
            setAccessToken(null);
          }}
        />

        {/* 按 id 现找：列表分页/搜索后行可能已经不在缓存里，此时会话为 null，
            对话框自己会收起，不会出现「对着一条已经看不见的会话改设定」。 */}
        <ConversationSystemPromptDialog
          open={!!promptForId}
          conversation={conversations.find((c) => c.id === promptForId) ?? null}
          onOpenChange={(open) => {
            if (!open) setPromptForId(null);
          }}
          onSave={async (conversationId, systemPrompt) => {
            if (!onUpdateSystemPrompt) return;
            await onUpdateSystemPrompt(conversationId, systemPrompt);
          }}
        />
        <ProjectDeleteDialog
          open={!!projectToDelete}
          project={projects?.find((p) => p.id === projectToDelete) ?? null}
          onOpenChange={(open) => {
            if (!open) setProjectToDelete(null);
          }}
          onDelete={async (projectId) => {
            if (!onDeleteProject) return;
            await onDeleteProject(projectId);
          }}
        />
      </div>
    </aside>
  );
}
