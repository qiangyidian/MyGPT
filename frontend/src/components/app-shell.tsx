"use client";

import { useEffect, useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { Menu, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Sidebar } from "@/components/sidebar";
import { useAuth } from "@/hooks/useAuth";
import { useConversations } from "@/hooks/useConversations";
import { useProjects } from "@/hooks/useProjects";
import { useModels } from "@/hooks/useModels";

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
 * Manages conversation list state, the active/archived view, and the mobile
 * sidebar toggle.
 */
export function AppShell({ children }: AppShellProps) {
  const router = useRouter();
  const { user, isLoading: authLoading, logout } = useAuth();

  const [viewMode, setViewMode] = useState<"active" | "archived">("active");
  const {
    conversations,
    create,
    delete: deleteConversation,
    updateAsync,
  } = useConversations({ archived: viewMode === "archived" });
  const { projects, create: createProject, assign, unassign } = useProjects();
  const { chatModels, isLoading: modelsLoading } = useModels();

  const handleAssignToProject = (conversationId: string, projectId: string) => {
    void assign({ projectId, conversationId });
  };
  const handleRemoveFromProject = (conversationId: string) => {
    const conv = conversations.find((c) => c.id === conversationId);
    if (conv?.project_id) void unassign({ projectId: conv.project_id, conversationId });
  };
  const handleCreateProject = async (name: string) => {
    await createProject({ name });
  };

  const [activeId, setActiveId] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  // Render a stable placeholder until mounted so the SSR HTML and the first
  // client render agree. Without this, useAuth's client-only token check makes
  // the server render null while the client renders the loading state → a React
  // hydration mismatch on every logged-in reload.
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  useEffect(() => {
    if (!authLoading && !user) router.replace("/login");
  }, [authLoading, user, router]);

  // First-run: an admin with no real (non-mock, keyed) chat model is sent to
  // the setup wizard instead of an unusable chat.
  useEffect(() => {
    if (!mounted || authLoading || modelsLoading || !user) return;
    if (user.role !== "admin") return;
    const needsOnboarding =
      chatModels.length === 0 ||
      chatModels.every((m) => m.provider === "mock" || !m.has_key || m.model_name === "my-model");
    if (needsOnboarding) router.replace("/onboarding");
  }, [mounted, authLoading, modelsLoading, user, chatModels, router]);

  const handleNewChat = async () => {
    try {
      const conv = await create({});
      setActiveId(conv.id);
      setViewMode("active");
      setSidebarOpen(false);
    } catch (err) {
      toast.error("创建对话失败", { description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleSelect = (id: string) => {
    setActiveId(id);
    setSidebarOpen(false);
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("确定删除这个对话吗？此操作不可撤销。")) return;
    try {
      await deleteConversation(id);
      if (activeId === id) setActiveId(null);
      toast.success("对话已删除");
    } catch (err) {
      toast.error("删除失败", { description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleRename = async (id: string, title: string) => {
    try {
      await updateAsync({ id, body: { title } });
    } catch (err) {
      toast.error("重命名失败", { description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleTogglePin = async (id: string, pinned: boolean) => {
    try {
      await updateAsync({ id, body: { pinned } });
    } catch (err) {
      toast.error("操作失败", { description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleToggleArchive = async (id: string, archived: boolean) => {
    try {
      await updateAsync({ id, body: { archived } });
      toast.success(archived ? "已归档" : "已取消归档");
    } catch (err) {
      toast.error("操作失败", { description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleLogout = async () => {
    await logout();
  };

  if (!mounted || authLoading) {
    return (
      <div className="flex min-h-screen items-center justify-center">
        <p className="text-sm text-muted-foreground">加载中…</p>
      </div>
    );
  }
  if (!user) return null;

  const sidebar = (
    <Sidebar
      conversations={conversations}
      activeConversationId={activeId}
      onSelectConversation={handleSelect}
      onNewChat={handleNewChat}
      onRename={handleRename}
      onTogglePin={handleTogglePin}
      onToggleArchive={handleToggleArchive}
      onDeleteConversation={handleDelete}
      user={user}
      onLogout={handleLogout}
      viewMode={viewMode}
      onViewModeChange={setViewMode}
      projects={projects}
      onAssignToProject={handleAssignToProject}
      onRemoveFromProject={handleRemoveFromProject}
      onCreateProject={handleCreateProject}
    />
  );

  return (
    <div className="app-shell-height flex overflow-hidden bg-background">
      <div className="hidden w-72 shrink-0 border-r border-border md:block">{sidebar}</div>

      {sidebarOpen && (
        <div className="fixed inset-0 z-50 md:hidden">
          <div className="absolute inset-0 bg-black/40" onClick={() => setSidebarOpen(false)} />
          <div className="absolute left-0 top-0 h-full w-72 border-r border-border bg-background">
            {sidebar}
            <Button variant="ghost" size="icon" className="absolute right-2 top-2" onClick={() => setSidebarOpen(false)}>
              <X className="h-4 w-4" />
            </Button>
          </div>
        </div>
      )}

      <div className="flex min-w-0 flex-1 flex-col">
        <div className="flex items-center gap-2 border-b border-border px-3 py-2 md:hidden">
          <Button variant="ghost" size="icon" onClick={() => setSidebarOpen(true)} aria-label="打开侧边栏">
            <Menu className="h-4 w-4" />
          </Button>
          <span className="text-sm font-medium">AI 对话</span>
        </div>

        {children({ activeConversationId: activeId, setActiveConversationId: setActiveId })}
      </div>
    </div>
  );
}
