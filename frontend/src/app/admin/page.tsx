"use client";

import Link from "next/link";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldCheck, ShieldOff } from "lucide-react";
import { useState } from "react";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
// 管理侧不钳位：负余额是超扣信号，必须原样显示给运营。
import { formatCreditsRaw as formatCredits } from "@/lib/credits";
import type { User } from "@/lib/types";
import { NavSuspense } from "@/components/navigation/page-loading";
import { AppPageShell } from "@/components/navigation/app-page-shell";
import { AuditLogPanel } from "@/components/admin/audit-log-panel";
import { FeatureFlagsPanel } from "@/components/admin/feature-flags-panel";
import { ToolCatalogPanel } from "@/components/admin/tool-catalog-panel";
import { UsageReportPanel } from "@/components/admin/usage-report-panel";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { Badge } from "@/components/ui/badge";
import {
  Tabs,
  TabsContent,
  TabsList,
  TabsTrigger,
} from "@/components/ui/tabs";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Label } from "@/components/ui/label";
import { Input } from "@/components/ui/input";

interface SystemStatus {
  db?: string;
  redis?: string;
  qdrant?: string;
  users?: number;
  conversations?: number;
  documents?: number;
  uptime_s?: number;
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
      <AppPageShell
        title="管理后台"
        description="用户、系统状态与用量。"
        requireAdmin
        actions={
          // 运行时观测与兑换码运营都是独立路由（列表 + 详情太重，不适合塞进这里的 Tabs）。
          <>
            <Button variant="outline" size="sm" asChild>
              <Link href="/admin/redeem">兑换码运营</Link>
            </Button>
            <Button variant="outline" size="sm" asChild>
              <Link href="/admin/runtime">运行时观测</Link>
            </Button>
          </>
        }
      >
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

  return (
    <Tabs defaultValue="users">
      <TabsList>
        <TabsTrigger value="users">用户</TabsTrigger>
        <TabsTrigger value="status">系统状态</TabsTrigger>
        <TabsTrigger value="usage">用量</TabsTrigger>
        <TabsTrigger value="credits">积分</TabsTrigger>
        <TabsTrigger value="audit">审计日志</TabsTrigger>
        <TabsTrigger value="tools">工具</TabsTrigger>
        <TabsTrigger value="flags">运行开关</TabsTrigger>
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

      {/* Usage — 日期区间 + 维度切换 + 分页 + CSV 导出（面板自管查询） */}
      <TabsContent value="usage" className="space-y-3">
        <UsageReportPanel />
      </TabsContent>

      {/* Audit log — 服务端筛选 + 分页 + CSV 导出（面板自管查询） */}
      <TabsContent value="audit" className="space-y-3">
        <AuditLogPanel />
      </TabsContent>

      {/* Tool catalog — 目录 + 启停（面板自管查询与写请求，见 components/admin）。 */}
      <TabsContent value="tools" className="space-y-3">
        <ToolCatalogPanel />
      </TabsContent>
      {/* 运行开关 —— 只读：这里回的是"算过的生效结论"，不是 .env 原文。 */}
      <TabsContent value="flags" className="space-y-3">
        <FeatureFlagsPanel />
      </TabsContent>
      {/* Redeem codes 不在这里：批次生成 + 单码运营整面在 /admin/redeem（见页头按钮）。 */}
      {/* Credits — 用户余额与手动调分 */}
      <TabsContent value="credits" className="space-y-3">
        <CreditsPanel />
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

interface CreditRow {
  user_id: string;
  email: string;
  username: string;
  balance: number;
  lifetime_granted: number;
  lifetime_consumed: number;
}

/**
 * 用户积分面板。
 *
 * 手动调分是客服必备：支付成功但码没发出去、需要赔偿用户、测试账号充值 ——
 * 这些都不该逼管理员去生成一个一次性兑换码。调分与兑换码走同一套账本，
 * 并写一条 `credits:adjust` 审计事件。
 */
function CreditsPanel() {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [target, setTarget] = useState<CreditRow | null>(null);
  const [delta, setDelta] = useState("");
  const [note, setNote] = useState("");

  const accountsQ = useQuery({
    queryKey: ["admin-credit-accounts", search],
    queryFn: () => api.adminListCreditAccounts(search.trim() || undefined),
  });

  const adjustMut = useMutation({
    mutationFn: (row: CreditRow) =>
      api.adminAdjustCredits({
        user_id: row.user_id,
        delta: Number(delta),
        note: note.trim() || null,
      }),
    onSuccess: (updated) => {
      toast.success(
        `${updated.username} 当前余额 ${formatCredits(updated.balance)}`
      );
      setTarget(null);
      setDelta("");
      setNote("");
      qc.invalidateQueries({ queryKey: ["admin-credit-accounts"] });
    },
    onError: (err) => {
      toast.error("调分失败", { description: userErrorMessage(err) });
    },
  });

  return (
    <>
      <div className="flex items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          查看用户余额，或手动加 / 扣积分（会留审计记录）。
        </p>
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="搜索邮箱或用户名"
          className="max-w-56"
        />
      </div>

      {accountsQ.isError ? (
        <ErrorState onRetry={() => accountsQ.refetch()} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-3">用户</th>
                <th className="hidden p-3 sm:table-cell">邮箱</th>
                <th className="p-3 text-right">余额</th>
                <th className="hidden p-3 text-right md:table-cell">累计获得</th>
                <th className="hidden p-3 text-right md:table-cell">累计消耗</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {accountsQ.isLoading ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    加载中…
                  </td>
                </tr>
              ) : !accountsQ.data?.length ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    没有匹配的用户。
                  </td>
                </tr>
              ) : (
                accountsQ.data.map((row: CreditRow) => (
                  <tr key={row.user_id} className="border-t border-border">
                    <td className="p-3 font-medium">{row.username}</td>
                    <td className="hidden p-3 text-muted-foreground sm:table-cell">
                      {row.email}
                    </td>
                    <td className="p-3 text-right tabular-nums">
                      {formatCredits(row.balance)}
                    </td>
                    <td className="hidden p-3 text-right tabular-nums text-muted-foreground md:table-cell">
                      {row.lifetime_granted}
                    </td>
                    <td className="hidden p-3 text-right tabular-nums text-muted-foreground md:table-cell">
                      {row.lifetime_consumed}
                    </td>
                    <td className="p-3 text-right">
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => {
                          setTarget(row);
                          setDelta("");
                          setNote("");
                        }}
                      >
                        调整
                      </Button>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      <Dialog open={!!target} onOpenChange={(next) => !next && setTarget(null)}>
        <DialogContent className="sm:max-w-sm">
          <DialogHeader>
            <DialogTitle>调整积分</DialogTitle>
            <DialogDescription>
              {target?.username}（当前 {formatCredits(target?.balance)}）
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="adjust-delta">变动数量（正数增加，负数扣减）</Label>
              <Input
                id="adjust-delta"
                type="number"
                value={delta}
                onChange={(e) => setDelta(e.target.value)}
                placeholder="例如 1000 或 -500"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="adjust-note">备注</Label>
              <Input
                id="adjust-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="例如 客服补偿"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setTarget(null)}>
              取消
            </Button>
            <Button
              disabled={!delta || Number(delta) === 0 || adjustMut.isPending}
              onClick={() => target && adjustMut.mutate(target)}
            >
              {adjustMut.isPending ? "提交中…" : "确认"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
