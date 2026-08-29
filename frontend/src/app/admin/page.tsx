"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldCheck, ShieldOff } from "lucide-react";

import { api } from "@/lib/api";
import type { User } from "@/lib/types";
import { NavSuspense } from "@/components/navigation/page-loading";
import { AppPageShell } from "@/components/navigation/app-page-shell";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";

interface SystemStatus {
  db?: string;
  redis?: string;
  qdrant?: string;
  users?: number;
  conversations?: number;
  documents?: number;
  uptime_s?: number;
}
interface UsageRow {
  date: string;
  conversations?: number;
  messages?: number;
  user_messages?: number;
  assistant_messages?: number;
  tool_calls?: number;
}
interface AuditRow {
  id: string;
  actor_id: string | null;
  action: string;
  target: string | null;
  detail: Record<string, unknown> | null;
  created_at: string | null;
}
interface ToolInfoRow {
  name: string;
  description: string;
  dangerous?: boolean;
}

/**
 * Admin console. Auth + role gating (login redirect, non-admin redirect, loading
 * state) all live in `AppPageShell` via the shared `useAuth` (`["auth","me"]`)
 * cache — this replaces the page's old divergent `["me"]` query. Children only
 * mount once the user is confirmed to be an admin.
 */
export default function AdminPage() {
  return (
    <NavSuspense>
      <AppPageShell title="管理后台" description="用户、系统状态与用量。" requireAdmin>
        <AdminContent />
      </AppPageShell>
    </NavSuspense>
  );
}

function AdminContent() {
  const qc = useQueryClient();

  const usersQ = useQuery({
    queryKey: ["admin-users"],
    queryFn: api.adminListUsers,
  });
  const statsQ = useQuery({
    queryKey: ["admin-stats"],
    queryFn: api.adminStats,
  });
  // Audit log (real backend: /api/admin/audit → AuditEvent rows).
  const auditQ = useQuery({
    queryKey: ["admin-audit"],
    queryFn: () => api.adminAuditLog(200),
  });
  // Registered tool catalog (real backend: /api/tools).
  const toolsQ = useQuery({
    queryKey: ["admin-tools"],
    queryFn: () => api.listTools(),
  });

  const updateMut = useMutation({
    mutationFn: ({ id, body }: { id: string; body: { role?: string; is_active?: boolean } }) =>
      api.adminUpdateUser(id, body),
    onSuccess: () => {
      toast.success("已更新");
      qc.invalidateQueries({ queryKey: ["admin-users"] });
    },
    onError: () => toast.error("更新失败（可能是最后一个管理员）"),
  });

  const status = (statsQ.data?.status ?? {}) as SystemStatus;
  const usage = (statsQ.data?.usage ?? []) as UsageRow[];

  return (
    <Tabs defaultValue="users">
      <TabsList>
        <TabsTrigger value="users">用户</TabsTrigger>
        <TabsTrigger value="status">系统状态</TabsTrigger>
        <TabsTrigger value="usage">用量</TabsTrigger>
        <TabsTrigger value="audit">审计日志</TabsTrigger>
        <TabsTrigger value="tools">工具</TabsTrigger>
      </TabsList>

      {/* Users */}
      <TabsContent value="users" className="space-y-3">
        {usersQ.isError ? (
          <ErrorState onRetry={() => usersQ.refetch()} />
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="p-3">用户</th>
                  <th className="hidden p-3 sm:table-cell">邮箱</th>
                  <th className="p-3">角色</th>
                  <th className="p-3">启用</th>
                </tr>
              </thead>
              <tbody>
                {usersQ.isLoading ? (
                  <tr>
                    <td colSpan={4} className="p-6 text-center text-muted-foreground">
                      加载中…
                    </td>
                  </tr>
                ) : (
                  (usersQ.data ?? []).map((u: User) => (
                    <tr key={u.id} className="border-t border-border">
                      <td className="p-3 font-medium">
                        {u.username}
                        <div className="text-xs text-muted-foreground sm:hidden">{u.email}</div>
                      </td>
                      <td className="hidden p-3 text-muted-foreground sm:table-cell">{u.email}</td>
                      <td className="p-3">
                        <Button
                          variant="ghost"
                          size="sm"
                          className="gap-1"
                          onClick={() =>
                            updateMut.mutate({
                              id: u.id,
                              body: { role: u.role === "admin" ? "user" : "admin" },
                            })
                          }
                        >
                          {u.role === "admin" ? (
                            <>
                              <ShieldCheck className="h-3.5 w-3.5" /> 管理员
                            </>
                          ) : (
                            <>
                              <ShieldOff className="h-3.5 w-3.5" /> 用户
                            </>
                          )}
                        </Button>
                      </td>
                      <td className="p-3">
                        <Switch
                          checked={u.is_active}
                          aria-label={`启用 ${u.username}`}
                          onCheckedChange={(v) =>
                            updateMut.mutate({ id: u.id, body: { is_active: v } })
                          }
                        />
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </TabsContent>

      {/* System status */}
      <TabsContent value="status" className="space-y-3">
        {statsQ.isError ? (
          <ErrorState onRetry={() => statsQ.refetch()} />
        ) : statsQ.isLoading ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : (
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Stat label="数据库" value={status.db} />
            <Stat label="Redis" value={status.redis} />
            <Stat label="Qdrant" value={status.qdrant} />
            <Stat label="用户数" value={String(status.users ?? "-")} raw />
            <Stat label="会话数" value={String(status.conversations ?? "-")} raw />
            <Stat label="文档数" value={String(status.documents ?? "-")} raw />
            <Stat
              label="运行时长"
              value={
                status.uptime_s != null
                  ? status.uptime_s >= 3600
                    ? `${Math.floor(status.uptime_s / 3600)} 时 ${Math.floor((status.uptime_s % 3600) / 60)} 分`
                    : status.uptime_s >= 60
                      ? `${Math.floor(status.uptime_s / 60)} 分`
                      : `${status.uptime_s} 秒`
                  : "—"
              }
              raw
            />
          </div>
        )}
      </TabsContent>

      {/* Usage */}
      <TabsContent value="usage" className="space-y-3">
        {statsQ.isError ? (
          <ErrorState onRetry={() => statsQ.refetch()} />
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="p-3">日期</th>
                  <th className="p-3">消息</th>
                  <th className="hidden p-3 sm:table-cell">用户消息</th>
                  <th className="hidden p-3 sm:table-cell">AI 消息</th>
                </tr>
              </thead>
              <tbody>
                {usage.length === 0 ? (
                  <tr>
                    <td colSpan={4} className="p-6 text-center text-muted-foreground">
                      近 14 天暂无数据
                    </td>
                  </tr>
                ) : (
                  usage
                    .slice()
                    .reverse()
                    .map((u) => (
                      <tr key={u.date} className="border-t border-border">
                        <td className="p-3">{u.date}</td>
                        <td className="p-3">{u.messages ?? 0}</td>
                        <td className="hidden p-3 sm:table-cell">{u.user_messages ?? 0}</td>
                        <td className="hidden p-3 sm:table-cell">{u.assistant_messages ?? 0}</td>
                      </tr>
                    ))
                )}
              </tbody>
            </table>
          </div>
        )}
      </TabsContent>

      {/* Audit log — real AuditEvent rows (tool calls, approvals, auth). */}
      <TabsContent value="audit" className="space-y-3">
        {auditQ.isError ? (
          <ErrorState onRetry={() => auditQ.refetch()} />
        ) : auditQ.isLoading ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : !auditQ.data?.length ? (
          <p className="text-sm text-muted-foreground">暂无审计事件。</p>
        ) : (
          <div className="overflow-x-auto rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="p-3">时间</th>
                  <th className="p-3">操作</th>
                  <th className="hidden p-3 sm:table-cell">目标</th>
                  <th className="hidden p-3 md:table-cell">详情</th>
                </tr>
              </thead>
              <tbody>
                {auditQ.data.map((a: AuditRow) => (
                  <tr key={a.id} className="border-t border-border">
                    <td className="whitespace-nowrap p-3 text-muted-foreground">
                      {a.created_at ? new Date(a.created_at).toLocaleString() : "—"}
                    </td>
                    <td className="p-3 font-medium">{a.action}</td>
                    <td className="hidden max-w-[160px] truncate p-3 text-muted-foreground sm:table-cell">
                      {a.target ?? "—"}
                    </td>
                    <td className="hidden max-w-[280px] truncate p-3 text-muted-foreground md:table-cell">
                      {a.detail ? JSON.stringify(a.detail) : "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </TabsContent>

      {/* Tool catalog — the registry the agent runtimes actually use. */}
      <TabsContent value="tools" className="space-y-3">
        {toolsQ.isError ? (
          <ErrorState onRetry={() => toolsQ.refetch()} />
        ) : toolsQ.isLoading ? (
          <p className="text-sm text-muted-foreground">加载中…</p>
        ) : (
          <div className="grid gap-2 sm:grid-cols-2">
            {(toolsQ.data ?? []).map((t: ToolInfoRow) => (
              <div key={t.name} className="rounded-lg border border-border bg-card p-3">
                <div className="flex items-center gap-2">
                  <span className="font-mono text-sm font-medium">{t.name}</span>
                  {t.dangerous && (
                    <Badge variant="destructive" className="text-[10px]">危险</Badge>
                  )}
                </div>
                <p className="mt-1 line-clamp-2 text-xs text-muted-foreground">
                  {t.description || "（无描述）"}
                </p>
              </div>
            ))}
          </div>
        )}
      </TabsContent>
    </Tabs>
  );
}

function Stat({ label, value, raw }: { label: string; value?: string; raw?: boolean }) {
  const ok = raw ? true : value === "ok";
  return (
    <div className="rounded-lg border border-border p-4">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 flex items-center gap-2">
        <span className="text-lg font-semibold">{value ?? "—"}</span>
        {!raw && (
          <Badge variant={ok ? "default" : "destructive"}>{ok ? "正常" : "异常"}</Badge>
        )}
      </div>
    </div>
  );
}

function ErrorState({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="flex flex-col items-center gap-3 rounded-lg border border-dashed py-12 text-center">
      <p className="text-sm text-muted-foreground">数据加载失败，请重试。</p>
      <Button variant="outline" size="sm" onClick={onRetry}>
        重试
      </Button>
    </div>
  );
}
