"use client";

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { toast } from "sonner";
import { ShieldCheck, ShieldOff } from "lucide-react";
import { useState } from "react";

import { api, ApiError } from "@/lib/api";
import { redeemErrorMessage } from "@/lib/credits";
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
        <TabsTrigger value="redeem">兑换码</TabsTrigger>
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
      {/* Redeem codes — 批次生成、导出、作废 */}
      <TabsContent value="redeem" className="space-y-3">
        <RedeemCodesPanel />
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

interface RedeemBatchRow {
  batch: {
    id: string;
    name: string;
    credits_per_code: number;
    expires_at: string | null;
    created_at: string;
  };
  total: number;
  redeemed: number;
  void: number;
  active: number;
}

/**
 * 兑换码面板。
 *
 * 明文码只在创建响应里出现一次 —— 库中只存 SHA-256 哈希，之后再也取不回。
 * 所以创建成功后必须立刻弹出明文供复制 / 下载，并在关掉弹窗后明确提示
 * "明文不会再次显示"。
 */
function RedeemCodesPanel() {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [credits, setCredits] = useState("1000");
  const [count, setCount] = useState("10");
  const [expiresAt, setExpiresAt] = useState("");
  const [note, setNote] = useState("");
  const [issued, setIssued] = useState<string[] | null>(null);

  const batchesQ = useQuery({
    queryKey: ["admin-redeem-batches"],
    queryFn: api.adminListRedeemBatches,
  });

  const createMut = useMutation({
    mutationFn: () =>
      api.adminCreateRedeemBatch({
        name: name.trim(),
        credits_per_code: Number(credits),
        count: Number(count),
        expires_at: expiresAt ? new Date(expiresAt).toISOString() : null,
        note: note.trim() || null,
      }),
    onSuccess: (result) => {
      setIssued(result.codes);
      setOpen(false);
      qc.invalidateQueries({ queryKey: ["admin-redeem-batches"] });
      toast.success(`已生成 ${result.codes.length} 个兑换码`);
    },
    onError: (err) => {
      const apiErr = err as ApiError;
      toast.error(redeemErrorMessage(apiErr.code, apiErr.message));
    },
  });

  const voidMut = useMutation({
    mutationFn: (batchId: string) => api.adminVoidRedeemBatch(batchId),
    onSuccess: (result) => {
      toast.success(`已作废 ${result.voided} 个未使用的兑换码`);
      qc.invalidateQueries({ queryKey: ["admin-redeem-batches"] });
    },
    onError: () => toast.error("作废失败"),
  });

  const downloadCsv = (codes: string[]) => {
    const rows = ["兑换码", ...codes].join("\n");
    // 加 BOM，否则 Excel 打开中文表头会乱码。
    const blob = new Blob([`﻿${rows}`], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `兑换码-${Date.now()}.csv`;
    a.click();
    URL.revokeObjectURL(url);
  };

  return (
    <>
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          生成兑换码发给用户，用户在「设置 → 积分」兑换。
        </p>
        <Button size="sm" onClick={() => setOpen(true)}>
          生成兑换码
        </Button>
      </div>

      {batchesQ.isError ? (
        <ErrorState onRetry={() => batchesQ.refetch()} />
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-3">批次</th>
                <th className="p-3">面额</th>
                <th className="p-3">核销</th>
                <th className="hidden p-3 sm:table-cell">有效期</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {batchesQ.isLoading ? (
                <tr>
                  <td colSpan={5} className="p-6 text-center text-muted-foreground">
                    加载中…
                  </td>
                </tr>
              ) : !batchesQ.data?.length ? (
                <tr>
                  <td colSpan={5} className="p-6 text-center text-muted-foreground">
                    还没有兑换码批次。
                  </td>
                </tr>
              ) : (
                batchesQ.data.map((row: RedeemBatchRow) => (
                  <tr key={row.batch.id} className="border-t border-border">
                    <td className="p-3 font-medium">{row.batch.name}</td>
                    <td className="p-3 tabular-nums">{row.batch.credits_per_code}</td>
                    <td className="p-3 tabular-nums">
                      {row.redeemed}/{row.total}
                      {row.active > 0 ? (
                        <span className="ml-2 text-xs text-muted-foreground">
                          剩 {row.active}
                        </span>
                      ) : null}
                      {row.void > 0 ? (
                        <span className="ml-2 text-xs text-muted-foreground">
                          作废 {row.void}
                        </span>
                      ) : null}
                    </td>
                    <td className="hidden p-3 text-muted-foreground sm:table-cell">
                      {row.batch.expires_at
                        ? new Date(row.batch.expires_at).toLocaleDateString()
                        : "永久"}
                    </td>
                    <td className="p-3 text-right">
                      {row.active > 0 ? (
                        <Button
                          variant="ghost"
                          size="sm"
                          className="text-destructive"
                          disabled={voidMut.isPending}
                          onClick={() => {
                            if (
                              confirm(
                                `确定作废「${row.batch.name}」剩余的 ${row.active} 个兑换码？已兑换的不受影响。`
                              )
                            ) {
                              voidMut.mutate(row.batch.id);
                            }
                          }}
                        >
                          作废剩余
                        </Button>
                      ) : null}
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      {/* 新建批次 */}
      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="sm:max-w-md">
          <DialogHeader>
            <DialogTitle>生成兑换码</DialogTitle>
            <DialogDescription>生成后明文只显示一次，请当场导出。</DialogDescription>
          </DialogHeader>
          <div className="space-y-3">
            <div className="space-y-1">
              <Label htmlFor="batch-name">批次名称</Label>
              <Input
                id="batch-name"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="2026 中秋活动"
              />
            </div>
            <div className="grid grid-cols-2 gap-3">
              <div className="space-y-1">
                <Label htmlFor="batch-credits">每码积分</Label>
                <Input
                  id="batch-credits"
                  type="number"
                  min={1}
                  value={credits}
                  onChange={(e) => setCredits(e.target.value)}
                />
              </div>
              <div className="space-y-1">
                <Label htmlFor="batch-count">生成数量</Label>
                <Input
                  id="batch-count"
                  type="number"
                  min={1}
                  max={5000}
                  value={count}
                  onChange={(e) => setCount(e.target.value)}
                />
              </div>
            </div>
            <div className="space-y-1">
              <Label htmlFor="batch-expires">有效期（留空为永久）</Label>
              <Input
                id="batch-expires"
                type="date"
                value={expiresAt}
                onChange={(e) => setExpiresAt(e.target.value)}
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="batch-note">备注</Label>
              <Input
                id="batch-note"
                value={note}
                onChange={(e) => setNote(e.target.value)}
                placeholder="选填"
              />
            </div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setOpen(false)}>
              取消
            </Button>
            <Button
              disabled={!name.trim() || createMut.isPending}
              onClick={() => createMut.mutate()}
            >
              {createMut.isPending ? "生成中…" : "生成"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      {/* 明文码：唯一一次可见 */}
      <Dialog open={!!issued} onOpenChange={(next) => !next && setIssued(null)}>
        <DialogContent className="sm:max-w-lg">
          <DialogHeader>
            <DialogTitle>兑换码已生成</DialogTitle>
            <DialogDescription className="rounded-md border border-destructive/40 bg-destructive/10 p-2 text-destructive">
              系统只保存兑换码的哈希值，
              <strong>关闭本窗口后将无法再次查看明文</strong>
              ，请立即复制或下载 CSV。
            </DialogDescription>
          </DialogHeader>
          <pre className="max-h-64 overflow-auto rounded-md border border-border bg-secondary/40 p-3 font-mono text-xs">
            {(issued ?? []).join("\n")}
          </pre>
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                navigator.clipboard.writeText((issued ?? []).join("\n"));
                toast.success("已复制");
              }}
            >
              复制全部
            </Button>
            <Button variant="outline" onClick={() => downloadCsv(issued ?? [])}>
              下载 CSV
            </Button>
            <Button onClick={() => setIssued(null)}>我已保存</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
