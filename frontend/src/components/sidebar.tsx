"use client";

import Link from "next/link";
import {
  Boxes,
  LogOut,
  MessageSquarePlus,
  Settings,
  Shield,
  Trash2,
} from "lucide-react";

import { cn, relativeTime } from "@/lib/utils";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { ThemeToggle } from "@/components/theme-toggle";
import type { Conversation, User } from "@/lib/types";

interface SidebarProps {
  conversations: Conversation[];
  activeConversationId: string | null;
  onSelectConversation: (id: string) => void;
  onNewChat: () => void;
  onDeleteConversation: (id: string) => void;
  user: User | null;
  onLogout: () => void;
  className?: string;
}

export function Sidebar({
  conversations,
  activeConversationId,
  onSelectConversation,
  onNewChat,
  onDeleteConversation,
  user,
  onLogout,
  className,
}: SidebarProps) {
  const initials = user?.username
    ? user.username.slice(0, 2).toUpperCase()
    : "?";

  return (
    <aside
      className={cn(
        "flex h-full w-full flex-col bg-secondary/40",
        className
      )}
    >
      {/* New chat button */}
      <div className="p-3">
        <Button onClick={onNewChat} className="w-full justify-start gap-2">
          <MessageSquarePlus className="h-4 w-4" />
          新建对话
        </Button>
      </div>

      {/* Conversation list */}
      <div className="flex-1 overflow-y-auto px-2 pb-2">
        {conversations.length === 0 ? (
          <p className="px-3 py-8 text-center text-xs text-muted-foreground">
            暂无对话
          </p>
        ) : (
          <ul className="space-y-0.5">
            {conversations.map((conv) => (
              <li key={conv.id} className="group relative">
                <button
                  type="button"
                  className={cn(
                    "flex w-full flex-col gap-0.5 rounded-md px-3 py-2 text-left text-sm transition-colors",
                    activeConversationId === conv.id
                      ? "bg-background text-foreground shadow-sm"
                      : "text-muted-foreground hover:bg-background/60 hover:text-foreground"
                  )}
                  onClick={() => onSelectConversation(conv.id)}
                >
                  <span className="truncate font-medium">
                    {conv.title || "新对话"}
                  </span>
                  <span className="text-[11px] text-muted-foreground">
                    {relativeTime(conv.updated_at)}
                  </span>
                </button>
                {/* Delete button on hover */}
                <button
                  type="button"
                  className="absolute right-2 top-1/2 -translate-y-1/2 rounded p-1 text-muted-foreground opacity-0 transition-all hover:bg-destructive hover:text-destructive-foreground group-hover:opacity-100"
                  title="删除对话"
                  onClick={(e) => {
                    e.stopPropagation();
                    onDeleteConversation(conv.id);
                  }}
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </li>
            ))}
          </ul>
        )}
      </div>

      {/* Footer */}
      <div className="border-t border-border p-2">
        <nav className="mb-1 flex flex-col gap-0.5">
          <Button
            asChild
            variant="ghost"
            size="sm"
            className="w-full justify-start gap-2 text-muted-foreground"
          >
            <Link href="/knowledge-bases">
              <Boxes className="h-4 w-4" />
              知识库
            </Link>
          </Button>
          <Button
            asChild
            variant="ghost"
            size="sm"
            className="w-full justify-start gap-2 text-muted-foreground"
          >
            <Link href="/settings">
              <Settings className="h-4 w-4" />
              设置
            </Link>
          </Button>
          {user?.role === "admin" && (
            <Button
              asChild
              variant="ghost"
              size="sm"
              className="w-full justify-start gap-2 text-muted-foreground"
            >
              <Link href="/admin">
                <Shield className="h-4 w-4" />
                管理
              </Link>
            </Button>
          )}
        </nav>

        <div className="flex items-center justify-between gap-2 rounded-md px-1 py-1">
          {/* User menu */}
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
              <DropdownMenuLabel className="truncate">
                {user?.email ?? "未登录"}
              </DropdownMenuLabel>
              <DropdownMenuSeparator />
              <DropdownMenuItem
                onClick={onLogout}
                className="gap-2 text-destructive focus:text-destructive"
              >
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
