"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { projectsApi } from "@/lib/api";
import type { Project, ProjectInput } from "@/lib/types";
import { CONVERSATIONS_QUERY_KEY } from "@/hooks/useConversations";

export const PROJECTS_QUERY_KEY = ["projects"] as const;

/**
 * Lists the user's projects (sidebar grouping) + create/delete + assign/unassign
 * a conversation. Mutations invalidate both the projects cache and the
 * conversations list (so project_id / grouping refresh).
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

  const deleteMutation = useMutation({
    mutationFn: (id: string) => projectsApi.delete(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: PROJECTS_QUERY_KEY });
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
    deleteProject: deleteMutation.mutateAsync,
    assign: assignMutation.mutateAsync,
    unassign: unassignMutation.mutateAsync,
  };
}
