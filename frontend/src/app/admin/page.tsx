"use client";

import { useEffect } from "react";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldCheck, ShieldOff } from "lucide-react";

import { api } from "@/lib/api";
import type { User } from "@/lib/types";
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
}
interface UsageRow {
  date: string;
  conversations?: number;
  messages?: number;
  user_messages?: number;
  assistant_messages?: number;
  tool_calls?: number;
}

export default function AdminPage() {
  const router = useRouter();
  const qc = useQueryClient();

  const meQ = useQuery({ queryKey: ["me"], queryFn: api.me });
  useEffect(() => {
    if (meQ.data && meQ.data.role !== "admin") router.replace("/");
  }, [meQ.data, router]);

  const usersQ = useQuery({
    queryKey: ["admin-users"],
    queryFn: api.adminListUsers,
    enabled: !!meQ.data && meQ.data.role === "admin",
  });
  const statsQ = useQuery({
    queryKey: ["admin-stats"],
    queryFn: api.adminStats,
    enabled: !!meQ.data && meQ.data.role === "admin",
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

  if (!meQ.data) return <p className="p-6 text-sm text-muted-foreground">加载中…</p>;
  if (meQ.data.role !== "admin") return null;

  const status = (statsQ.data?.status ?? {}) as SystemStatus;
  const usage = (statsQ.data?.usage ?? []) as UsageRow[];

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">管理后台</h1>
        <p className="text-sm text-muted-foreground">用户、系统状态与用量。</p>
      </div>

      <Tabs defaultValue="users">
        <TabsList>
          <TabsTrigger value="users">用户</TabsTrigger>
          <TabsTrigger value="status">系统状态</TabsTrigger>
          <TabsTrigger value="usage">用量</TabsTrigger>
        </TabsList>

        {/* Users */}
        <TabsContent value="users" className="space-y-3">
          <div className="rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="p-3">用户</th>
                  <th className="p-3">邮箱</th>
                  <th className="p-3">角色</th>
                  <th className="p-3">启用</th>
                </tr>
              </thead>
              <tbody>
                {(usersQ.data ?? []).map((u: User) => (
                  <tr key={u.id} className="border-t border-border">
                    <td className="p-3 font-medium">{u.username}</td>
                    <td className="p-3 text-muted-foreground">{u.email}</td>
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
                        onCheckedChange={(v) =>
                          updateMut.mutate({ id: u.id, body: { is_active: v } })
                        }
                      />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </TabsContent>

        {/* System status */}
        <TabsContent value="status" className="space-y-3">
          <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
            <Stat label="数据库" value={status.db} />
            <Stat label="Redis" value={status.redis} />
            <Stat label="Qdrant" value={status.qdrant} />
            <Stat label="用户数" value={String(status.users ?? "-")} raw />
            <Stat label="会话数" value={String(status.conversations ?? "-")} raw />
            <Stat label="文档数" value={String(status.documents ?? "-")} raw />
          </div>
        </TabsContent>

        {/* Usage */}
        <TabsContent value="usage" className="space-y-3">
          <div className="rounded-lg border border-border">
            <table className="w-full text-sm">
              <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
                <tr>
                  <th className="p-3">日期</th>
                  <th className="p-3">消息</th>
                  <th className="p-3">用户消息</th>
                  <th className="p-3">AI 消息</th>
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
                        <td className="p-3">{u.user_messages ?? 0}</td>
                        <td className="p-3">{u.assistant_messages ?? 0}</td>
                      </tr>
                    ))
                )}
              </tbody>
            </table>
          </div>
        </TabsContent>
      </Tabs>
    </div>
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
