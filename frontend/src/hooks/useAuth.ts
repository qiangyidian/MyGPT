"use client";

import { useQuery } from "@tanstack/react-query";
import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { api } from "@/lib/api";
import { getAccessToken } from "@/lib/auth";
import type { User } from "@/lib/types";

export const AUTH_QUERY_KEY = ["auth", "me"] as const;

/**
 * Auth hook backed by react-query. On mount it checks for a stored access
 * token; if present, it calls api.me() to load the user profile. If the
 * request fails (or there is no token) the user is null.
 *
 * Also exposes `logout` which calls the API logout endpoint, clears the
 * cache, and redirects to /login.
 */
export function useAuth() {
  const router = useRouter();
  const hasToken = typeof window !== "undefined" && !!getAccessToken();

  const query = useQuery<User | null>({
    queryKey: AUTH_QUERY_KEY,
    queryFn: async () => {
      if (!getAccessToken()) return null;
      return api.me();
    },
    enabled: hasToken,
    retry: false,
  });

  const logout = useCallback(async () => {
    try {
      await api.logout();
    } catch {
      // Ignore errors — we clear the token locally regardless.
    }
    router.replace("/login");
  }, [router]);

  return {
    user: query.data ?? null,
    isLoading: hasToken && query.isLoading,
    isAuthenticated: !!query.data,
    logout,
  };
}
