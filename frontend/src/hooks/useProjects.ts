"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectsApi } from "@/lib/api";
import type { Project, ProjectImpact, ProjectInput, ProjectPatch } from "@/lib/types";
import { CONVERSATIONS_QUERY_KEY } from "@/hooks/useConversations";

export const PROJECTS_QUERY_KEY = ["projects"] as const;
export const PROJECT_IMPACT_QUERY_KEY = (id: string) =>
  ["project-impact", id] as const;

/**
 * Lists the user's projects (sidebar grouping) + create / rename / delete +
 * assign/unassign a conversation. Mutations invalidate both the projects cache
 * and the conversations list (so project_id / grouping refresh).
 */
export function useProjects() {
  const queryClient = useQueryClient();

  const list = useQuery<Project[]>({
    queryKey: PROJECTS_QUERY_KEY,
    queryFn: () => projectsApi.list(),
  });

  const createMutation = useMutation({
    mutationFn: (body: ProjectInput) => projectsApi.create(body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY }),
  });

  // 改名只动项目自身：会话行里的 project_id 没变，所以不用重取会话列表。
  const updateMutation = useMutation({
    mutationFn: ({ id, body }: { id: string; body: ProjectPatch }) =>
      projectsApi.update(id, body),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY }),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: string) => projectsApi.delete(id),
    onSuccess: (_data, id) => {
      queryClient.removeQueries({ queryKey: PROJECT_IMPACT_QUERY_KEY(id) });
      queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY });
      // 后端删除会把会话改为未分组（project_id 是软引用，没有 FK 级联），
      // 所以会话列表必须一起失效，否则侧边栏还按已删除的项目分组。
      queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY });
    },
  });

  const assignMutation = useMutation({
    mutationFn: ({ projectId, conversationId }: { projectId: string; conversationId: string }) =>
      projectsApi.assignConversation(projectId, conversationId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY }),
  });

  const unassignMutation = useMutation({
    mutationFn: ({ projectId, conversationId }: { projectId: string; conversationId: string }) =>
      projectsApi.unassignConversation(projectId, conversationId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: CONVERSATIONS_QUERY_KEY }),
  });

  return {
    projects: list.data ?? [],
    isLoading: list.isLoading,
    create: createMutation.mutateAsync,
    rename: updateMutation.mutateAsync,
    isRenaming: updateMutation.isPending,
    deleteProject: deleteMutation.mutateAsync,
    assign: assignMutation.mutateAsync,
    unassign: unassignMutation.mutateAsync,
  };
}

/**
 * 删除一个项目会碰到什么 —— 由服务端算，不在前端猜。
 * 只在确认框打开时取（`enabled`），一次对话最多一次请求。
 */
export function useProjectImpact(projectId: string | null) {
  return useQuery<ProjectImpact | null>({
    queryKey: projectId ? PROJECT_IMPACT_QUERY_KEY(projectId) : ["project-impact", "none"],
    queryFn: () => {
      if (!projectId) return null;
      return projectsApi.impact(projectId);
    },
    enabled: !!projectId,
    // 每次打开确认框都要重新统计：会话在这中间可能又多了几条。
    staleTime: 0,
  });
}
