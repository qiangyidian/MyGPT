"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Menu, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Sidebar } from "@/components/sidebar";
import { useAuth } from "@/hooks/useAuth";
import { useConversations } from "@/hooks/useConversations";

interface AppShellProps {
  /** Render-prop for the main chat area, receiving the active conversation. */
  children: (context: {
    activeConversationId: string | null;
    setActiveConversationId: (id: string | null) => void;
  }) => ReactNode;
}

/**
 * Client shell composing the sidebar + main content area.
 * Redirects to /login if no token / user is found after auth check.
 * Manages conversation list state and the mobile sidebar toggle.
 */
export function AppShell({ children }: AppShellProps) {
  const router = useRouter();
  const { user, isLoading: authLoading, logout } = useAuth();
  const {
    conversations,
    create,
    delete: deleteConversation,
  } = useConversations();

  const [activeId, setActiveId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);

  // Redirect to login if auth resolves to no user.
  useEffect(() => {
    if (!authLoading && !user) {
      router.replace("/login");
    }
  }, [authLoading, user, router]);

  const handleNewChat = async () => {
    try {
      const conv = await create({});
      setActiveId(conv.id);
      setSidebarOpen(false);
    } catch (err) {
      toast.error("创建对话失败", {
        description: err instanceof Error ? err.message : undefined,
      });
    }
  };

  const handleSelect = (id: string) => {
    setActiveId(id);
    setSidebarOpen(false);
  };

  const handleDelete = async (id: string) => {
    try {
      await deleteConversation(id);
      if (activeId === id) setActiveId(null);
      toast.success("对话已删除");
    } catch (err) {
      toast.error("删除失败", {
        description: err instanceof Error ? err.message : undefined,
      });
    }
  };

  const handleLogout = async () => {
    await logout();
  };

  // Loading state while auth resolves.
  if (authLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-muted-foreground">加载中...</p>
      </div>
    );
  }

  // If still no user after loading, render nothing (redirect is in flight).
  if (!user) return null;

  return (
    <div className="flex h-screen overflow-hidden bg-background">
      {/* Desktop sidebar */}
      <div className="hidden w-72 shrink-0 border-r border-border md:block">
        <Sidebar
          conversations={conversations}
          activeConversationId={activeId}
          onSelectConversation={handleSelect}
          onNewChat={handleNewChat}
          onDeleteConversation={handleDelete}
          user={user}
          onLogout={handleLogout}
        />
      </div>

      {/* Mobile sidebar overlay */}
      {sidebarOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div
            className="absolute inset-0 bg-black/40"
            onClick={() => setSidebarOpen(false)}
          />
          <div className="absolute left-0 top-0 h-full w-72 border-r border-border bg-background">
            <Sidebar
              conversations={conversations}
              activeConversationId={activeId}
              onSelectConversation={handleSelect}
              onNewChat={handleNewChat}
              onDeleteConversation={handleDelete}
              user={user}
              onLogout={handleLogout}
            />
            <Button
              variant="ghost"
              size="icon"
              className="absolute right-2 top-2"
              onClick={() => setSidebarOpen(false)}
            >
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      {/* Main area */}
      <div className="flex min-w-0 flex-1 flex-col">
        {/* Mobile top bar */}
        <div className="flex items-center gap-2 border-b border-border px-3 py-2 md:hidden">
          <Button
            variant="ghost"
            size="icon"
            onClick={() => setSidebarOpen(true)}
          >
            <Menu className="h-4 w-4" />
          </Button>
          <span className="text-sm font-medium">AI 对话</span>
        </div>

        {children({
          activeConversationId: activeId,
          setActiveConversationId: setActiveId,
        })}
      </div>
    </div>
  );
}
