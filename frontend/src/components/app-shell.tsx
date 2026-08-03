"use client";

import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Menu, X } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { Sidebar } from "@/components/sidebar";
import { useAuth } from "@/hooks/useAuth";
import { useConversationDetail, useConversations } from "@/hooks/useConversations";
import { useProjects } from "@/hooks/useProjects";
import { useModels } from "@/hooks/useModels";
import { ApiError } from "@/lib/api";
import {
  buildLoginUrl,
  buildReturnTo,
  getConversationIdFromSearch,
  stripConversationParam,
  withConversationParam,
} from "@/lib/navigation";
import { isOnboardingSkipped } from "@/lib/onboarding-preference";

interface AppShellProps {
  /** Render-prop for the main chat area, receiving the active conversation. */
  children: (context: {
    activeConversationId: string | null;
    setActiveConversationId: (id: string | null) => void;
  }) => ReactNode;
}

/**
 * Client shell composing the sidebar + main content area.
 *
 * Responsibilities:
 *  - Auth gate: redirect to /login (carrying `next`) when there is no user.
 *  - Onboarding gate: send an admin with no real chat model to /onboarding —
 *    unless they have dismissed it (per-user skip flag), which breaks the old
 *    /  ↔ /onboarding bounce loop.
 *  - Conversation ↔ URL sync: the active conversation id is mirrored to
 *    `?conversation=<id>` so reload / back / forward / shareable links all work.
 *
 * The `mounted` gate below is load-bearing: `useAuth`'s token check is
 * client-only, so without it the server renders `null` while the client renders
 * the loading state → a hydration mismatch on every logged-in reload.
 */
export function AppShell({ children }: AppShellProps) {
  const router = useRouter();
  const searchParams = useSearchParams();
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

  // activeId is the single source of truth for the displayed conversation.
  // The raw setter is used by the URL→state sync (never pushes back); the
  // wrapped `setActiveConversationId` (below) also updates the URL and is what
  // every user action + child component uses.
  const [activeId, setActiveIdRaw] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [mounted, setMounted] = useState(false);

  useEffect(() => {
    setMounted(true);
  }, []);

  // URL → state (initial restore + browser back/forward). Functional update so
  // we don't need `activeId` in deps (which would race with the wrapped setter).
  useEffect(() => {
    if (!mounted) return;
    const urlConv = getConversationIdFromSearch(searchParams);
    setActiveIdRaw((prev) => (prev === urlConv ? prev : urlConv));
  }, [mounted, searchParams]);

  // Validate the URL conversation: if it 404s (deleted / foreign), clear it once
  // with a friendly toast. We wait for the detail query to settle so we never
  // misjudge while data is still loading. A valid-but-archived conversation is
  // NOT treated as invalid (the detail query still resolves for it).
  const urlConv = getConversationIdFromSearch(searchParams);
  const invalidCheck = useConversationDetail(urlConv);
  const invalidNotifiedRef = useRef<string | null>(null);
  useEffect(() => {
    if (!mounted || !urlConv || invalidCheck.isLoading) return;
    const err = invalidCheck.error;
    if (err instanceof ApiError && (err.status === 404 || err.status === 403)) {
      if (invalidNotifiedRef.current !== urlConv) {
        invalidNotifiedRef.current = urlConv;
        toast.error("该对话不存在或已被删除");
      }
      setActiveIdRaw(null);
      router.replace(stripConversationParam(searchParams));
    }
  }, [mounted, urlConv, invalidCheck.isLoading, invalidCheck.error, searchParams, router]);

  // Not logged in → /login, carrying the current location as `next`.
  useEffect(() => {
    if (!authLoading && !user) {
      router.replace(buildLoginUrl(buildReturnTo("/", searchParams)));
    }
  }, [authLoading, user, router, searchParams]);

  // First-run: an admin with no real (non-mock, keyed) chat model is sent to
  // the setup wizard — unless they have explicitly dismissed it.
  useEffect(() => {
    if (!mounted || authLoading || modelsLoading || !user) return;
    if (user.role !== "admin") return;
    const needsOnboarding =
      chatModels.length === 0 ||
      chatModels.every(
        (m) => m.provider === "mock" || !m.has_key || m.model_name === "my-model"
      );
    if (needsOnboarding && !isOnboardingSkipped(user.id)) {
      router.replace("/onboarding");
    }
  }, [mounted, authLoading, modelsLoading, user, chatModels, router]);

  // Wrapped setter: updates activeId AND mirrors it to the URL. Non-null ids use
  // router.push (so back returns to the previous conversation); null uses
  // router.replace (clears a deleted conversation without leaving a history entry).
  const setActiveConversationId = useCallback(
    (id: string | null) => {
      setActiveIdRaw(id);
      if (id) {
        router.push(withConversationParam(searchParams, id));
      } else {
        router.replace(stripConversationParam(searchParams));
      }
    },
    [searchParams, router]
  );

  const handleNewChat = async () => {
    try {
      const conv = await create({});
      setActiveConversationId(conv.id);
      setViewMode("active");
      setSidebarOpen(false);
    } catch (err) {
      toast.error("创建对话失败", { description: err instanceof Error ? err.message : undefined });
    }
  };

  const handleSelect = (id: string) => {
    setActiveConversationId(id);
    setSidebarOpen(false);
  };

  const handleDelete = async (id: string) => {
    if (!window.confirm("确定删除这个对话吗？此操作不可撤销。")) return;
    try {
      await deleteConversation(id);
      if (activeId === id) setActiveConversationId(null);
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

  // The "return to here" target carried into sidebar links (知识库 / 设置 / 管理)
  // so coming back restores the current conversation.
  const homeReturnTo = buildReturnTo("/", searchParams);

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
      returnTo={homeReturnTo}
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

        {children({ activeConversationId: activeId, setActiveConversationId })}
      </div>
    </div>
  );
}
