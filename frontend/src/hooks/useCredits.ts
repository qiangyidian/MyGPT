"use client";

import { useQuery } from "@tanstack/react-query";

import { api } from "@/lib/api";
import { useAuth } from "@/hooks/useAuth";

/**
 * 当前用户的积分余额。
 *
 * 侧边栏与积分页共用这一份 React Query 缓存（key `["credits","me"]`），
 * 兑换或调分后失效该 key 即可让两处同时刷新。
 *
 * `enabled` 跟着登录态走：未登录时不发请求（侧边栏在登录页也会渲染）。
 * 不引入 zustand —— 代码库里已有 selector 返回 `?? []` 导致无限重渲染的
 * 前车之鉴，余额这种服务端状态本来就该归 React Query。
 */
export function useCredits() {
  const { user, isLoading: authLoading } = useAuth();

  const query = useQuery({
    queryKey: ["credits", "me"],
    queryFn: api.fetchCredits,
    enabled: !!user && !authLoading,
    // 余额变化不需要秒级同步，避免每次窗口聚焦都打一次接口。
    staleTime: 30_000,
  });

  return {
    credits: query.data,
    isLoading: query.isLoading,
    isError: query.isError,
  };
}
