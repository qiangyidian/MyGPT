"use client";

import Link from "next/link";
import { useEffect, useMemo, useState } from "react";
import {
  Archive,
  ArchiveRestore,
  Boxes,
  FolderInput,
  FolderPlus,
  LogOut,
  MessageSquarePlus,
  Pin,
  Pencil,
  Search,
  Settings,
  Shield,
  Trash2,
} from "lucide-react";

import { cn, relativeTime } from "@/lib/utils";
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
import type { Conversation, Project, User } from "@/lib/types";

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
  /** Projects for sidebar grouping + the "move to project" action. */
  projects?: Project[];
  onAssignToProject?: (conversationId: string, projectId: string) => void;
  onRemoveFromProject?: (conversationId: string) => void;
  onCreateProject?: (name: string) => void;
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
  projects,
  onAssignToProject,
  onRemoveFromProject,
  onCreateProject,
  returnTo,
  className,
}: SidebarProps) {
  const [query, setQuery] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editingValue, setEditingValue] = useState("");
  const [focusedIndex, setFocusedIndex] = useState(0);

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return conversations;
    return conversations.filter(
      (c) =>
        c.title.toLowerCase().includes(q) ||
        (c.last_message_preview ?? "").toLowerCase().includes(q)
    );
  }, [conversations, query]);

  // Conversations filed under a project render in their own sections; the rest
  // flow through the pinned / time-bucket grouping.
  const projectConvs = filtered.filter((c) => c.project_id);
  const unassigned = filtered.filter((c) => !c.project_id);
  const byProject = new Map<string, Conversation[]>();
  for (const c of projectConvs) {
    const key = c.project_id as string;
    const arr = byProject.get(key);
    if (arr) arr.push(c);
    else byProject.set(key, [c]);
  }

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

  const commitRename = (id: string) => {
    const title = editingValue.trim();
    if (title) onRename(id, title);
    setEditingId(null);
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
    const subtitle = conv.last_message_preview?.trim() || relativeTime(conv.updated_at);

    return (
      <li key={conv.id} className="group relative">
        <button
          type="button"
          className={cn(
            "flex w-full flex-col gap-0.5 rounded-md px-3 py-2 text-left text-sm transition-colors",
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
            <span className="flex items-center gap-1 truncate font-medium">
              {conv.is_pinned && <Pin className="h-3 w-3 shrink-0 text-muted-foreground" />}
              <span className="truncate">{conv.title || "新对话"}</span>
            </span>
          )}
          {!isEditing && (
            <span className="truncate text-[11px] text-muted-foreground">{subtitle}</span>
          )}
        </button>

        {/* Per-row menu */}
        <div className="absolute right-1.5 top-1/2 -translate-y-1/2 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6 text-muted-foreground"
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
              <DropdownMenuItem className="gap-2" onClick={() => onTogglePin(conv.id, !conv.is_pinned)}>
                <Pin className="h-4 w-4" /> {conv.is_pinned ? "取消置顶" : "置顶"}
              </DropdownMenuItem>
              <DropdownMenuItem className="gap-2" onClick={() => onToggleArchive(conv.id, !conv.is_archived)}>
                {conv.is_archived ? <ArchiveRestore className="h-4 w-4" /> : <Archive className="h-4 w-4" />}
                {conv.is_archived ? "取消归档" : "归档"}
              </DropdownMenuItem>
              {projects && projects.length > 0 && onAssignToProject && (
                <DropdownMenuSub>
                  <DropdownMenuSubTrigger className="gap-2 text-xs">
                    <FolderInput className="h-4 w-4" /> 移入项目
                  </DropdownMenuSubTrigger>
                  <DropdownMenuSubContent>
                    {projects.map((p) => (
                      <DropdownMenuItem key={p.id} className="gap-2" onClick={() => onAssignToProject(conv.id, p.id)}>
                        <span className="h-2 w-2 rounded-full" style={{ background: p.color }} />
                        {p.name}
                      </DropdownMenuItem>
                    ))}
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
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="搜索对话"
            className="h-9 pl-8 text-sm"
            aria-label="搜索对话"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        {filtered.length === 0 ? (
          <p className="px-3 py-8 text-center text-xs text-muted-foreground">
            {viewMode === "archived" ? "归档为空" : "暂无对话"}
          </p>
        ) : (
          <div
            role="listbox"
            aria-label="对话列表"
            tabIndex={0}
            onKeyDown={onKeyDownList}
          >
            {projects
              ?.filter((p) => byProject.has(p.id))
              .map((p) => (
                <div className="mb-1" key={p.id}>
                  <p className="flex items-center gap-1.5 px-3 py-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                    <span className="h-2 w-2 shrink-0 rounded-full" style={{ background: p.color }} />
                    <span className="truncate">{p.name}</span>
                  </p>
                  <ul className="space-y-0.5">{byProject.get(p.id)!.map((c) => renderItem(c))}</ul>
                </div>
              ))}
            {renderGroup("置顶", pinned)}
            {BUCKET_ORDER.map((b) => renderGroup(BUCKET_LABEL[b], byBucket(b)))}
          </div>
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
            <Link href={withReturnTo("/knowledge-bases", returnTo)}>
              <Boxes className="h-4 w-4" />
              知识库
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
            </DropdownMenuContent>
          </DropdownMenu>
          <ThemeToggle />
        </div>
      </div>
    </aside>
  );
}
