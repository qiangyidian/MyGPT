"use client";

// 管理端 · 兑换码运营。
//
// 批次是运营单位，单码才是客服现场遇到的东西（"这批里有一张贴错了群，只废那一
// 张"）。所以这一页同时提供两层：批次列表（进度 / 有效期 / 状态）与单码看板
// （复用 `components/redeem-code-browser.tsx`，含单码作废与删除）。
//
// 三条后端硬事实会直接决定界面上有没有按钮，别在这儿含糊：
//   * 明文取不回来 —— 库里只有 peppered HMAC（`app/models/redeem_code.py:1-15`），
//     所以页面没有任何「显示完整码」的入口，能给的只有 6 位前缀 + 掩码
//     （`app/api/admin_redeem.py:76-81`）。
//   * 已兑换的码不能作废也不能删除 —— 分已经进过用户账本，删行会断掉对账
//     （`app/api/admin_redeem.py:169-170`、`:205-206`，两处都是 409）。
//   * 批次没有反作废端点 —— `credits.py` 的 `void_redeem_batch` 只把 active→void，
//     所以整批作废必须逐字确认。

import { useDeferredValue, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  EllipsisVertical,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldAlert,
} from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { userErrorMessage } from "@/lib/api-error";
import { formatCreditsRaw } from "@/lib/credits";
import {
  REDEEM_BATCH_FILTERS,
  formatDateText,
  formatDateTimeText,
  isBatchExpired,
  redeemBatchQuery,
  redeemBatchStatus,
  voidBatchConfirmed,
  voidBatchConsequences,
  type RedeemBatchFilter,
} from "@/lib/redeem-batch";
import type { RedeemBatchCreateResult, RedeemBatchProgress } from "@/lib/types";
import { cn } from "@/lib/utils";
import { NavSuspense } from "@/components/navigation/page-loading";
import { AppPageShell } from "@/components/navigation/app-page-shell";
import { RedeemBatchForm } from "@/components/admin/redeem-batch-form";
import { RedeemCodeBrowser } from "@/components/redeem-code-browser";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";

/** 一页取多少个批次（服务端 `limit` 上限 500）。 */
const PAGE_SIZE = 50;

export default function AdminRedeemPage() {
  return (
    <NavSuspense>
      <AppPageShell
        title="兑换码运营"
        description="批次生成与核销进度、按批次查看单码、作废单码或整批。"
        requireAdmin
        secondaryBack={{ href: "/admin", label: "返回管理后台" }}
        breadcrumbs={[{ label: "管理后台", href: "/admin" }, { label: "兑换码运营" }]}
      >
        <RedeemContent />
      </AppPageShell>
    </NavSuspense>
  );
}

function RedeemContent() {
  const qc = useQueryClient();
  const [filter, setFilter] = useState<RedeemBatchFilter>("all");
  const [search, setSearch] = useState("");
  const [page, setPage] = useState(0);
  const [codesBatch, setCodesBatch] = useState<{ id: string; name: string } | null>(null);
  const [voidTarget, setVoidTarget] = useState<RedeemBatchProgress | null>(null);
  const [latest, setLatest] = useState<{ id: string; name: string; count: number } | null>(null);

  // 分页与筛选都在服务端：批次可能上千，全量拉到前端只是把浏览器变成缓冲区，
  // 而且「只在当前这一页里搜」会假装老批次不存在。后端不返回总数，所以
  // 「这一页装满了」是还有下一页的唯一信号。
  const deferredSearch = useDeferredValue(search);
  const query = redeemBatchQuery(filter, deferredSearch, page, PAGE_SIZE);
  const batchesQ = useQuery({
    queryKey: ["admin-redeem-batches", query],
    queryFn: () => api.adminListRedeemBatches(query),
  });

  const rows = batchesQ.data ?? [];
  // 空列表有两种含义（库里真没有 vs 筛没了），文案必须分得清。
  const filtering = filter !== "all" || deferredSearch.trim().length > 0;

  const onCreated = (result: RedeemBatchCreateResult) => {
    // 新批次排在第 0 页：留在第 3 页会以为「没生成成功」。
    setPage(0);
    setLatest({
      id: result.batch.id,
      name: result.batch.name,
      count: result.codes.length,
    });
  };

  return (
    <div className="space-y-5">
      <RedeemBatchForm onCreated={onCreated} />

      {latest ? (
        <div className="flex flex-wrap items-center gap-2 rounded-lg border border-border bg-card p-3 text-sm">
          <span className="text-muted-foreground">最新批次：</span>
          <span className="font-medium">{latest.name}</span>
          <span className="text-xs text-muted-foreground">
            已生成 {latest.count} 张，明文只在生成时显示过
          </span>
          <Button
            variant="outline"
            size="sm"
            className="ml-auto gap-1.5"
            onClick={() => setCodesBatch({ id: latest.id, name: latest.name })}
          >
            <KeyRound className="h-3.5 w-3.5" />
            运营这批单码
          </Button>
        </div>
      ) : null}

      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="space-y-1">
          <h2 className="text-sm font-semibold">批次列表</h2>
          <p className="text-xs text-muted-foreground">
            批次状态由「已兑换 / 已作废 / 未兑换」计数与有效期推导，后端不保存批次状态字段。
          </p>
        </div>
        <div className="flex items-center gap-2">
          <Input
            value={search}
            onChange={(e) => {
              setSearch(e.target.value);
              // 搜索打在服务端，翻页偏移量是按旧条件算出来的：改条件必须回第 0 页。
              setPage(0);
            }}
            placeholder="按批次名或备注筛选"
            className="max-w-56"
            aria-label="筛选批次"
          />
          <Button
            variant="outline"
            size="sm"
            className="gap-1.5"
            disabled={batchesQ.isFetching}
            onClick={() => void batchesQ.refetch()}
          >
            <RefreshCw className={cn("h-3.5 w-3.5", batchesQ.isFetching && "animate-spin")} />
            刷新
          </Button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-1.5">
        {REDEEM_BATCH_FILTERS.map((f) => (
          <Button
            key={f.value}
            size="sm"
            variant={filter === f.value ? "default" : "outline"}
            onClick={() => {
              setFilter(f.value);
              // 换条件等于换一次查询：留在第 3 页会看见「本页 0 条」，而那不是
              // 真的没有结果。
              setPage(0);
            }}
          >
            {f.label}
          </Button>
        ))}
        <span className="ml-auto text-xs text-muted-foreground">
          {`第 ${page + 1} 页 · 本页 ${rows.length} 个批次`}
        </span>
      </div>

      {batchesQ.isError ? (
        <div className="flex flex-col items-start gap-3 rounded-lg border border-dashed p-6">
          <p className="flex items-center gap-2 text-sm text-destructive">
            <AlertTriangle className="h-4 w-4 shrink-0" />
            批次列表加载失败：{userErrorMessage(batchesQ.error)}
          </p>
          <Button variant="outline" size="sm" onClick={() => void batchesQ.refetch()}>
            重试
          </Button>
        </div>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-border">
          <table className="w-full text-sm">
            <thead className="bg-secondary/50 text-left text-xs text-muted-foreground">
              <tr>
                <th className="p-3">批次</th>
                <th className="p-3 text-right">面额</th>
                <th className="p-3">核销进度</th>
                <th className="hidden p-3 lg:table-cell">有效期</th>
                <th className="p-3">状态</th>
                <th className="p-3" />
              </tr>
            </thead>
            <tbody>
              {batchesQ.isLoading ? (
                Array.from({ length: 4 }).map((_, i) => (
                  <tr key={i} className="border-t border-border">
                    <td className="p-3" colSpan={6}>
                      <Skeleton className="h-5 w-full" />
                    </td>
                  </tr>
                ))
              ) : !rows.length ? (
                <tr>
                  <td colSpan={6} className="p-6 text-center text-muted-foreground">
                    {filtering
                      ? "没有匹配当前筛选条件的批次。"
                      : "还没有兑换码批次，用上面的表单生成第一批。"}
                  </td>
                </tr>
              ) : (
                rows.map((row: RedeemBatchProgress) => (
                  <BatchRow
                    key={row.batch.id}
                    row={row}
                    onOpenCodes={() =>
                      setCodesBatch({ id: row.batch.id, name: row.batch.name })
                    }
                    onVoidBatch={() => setVoidTarget(row)}
                  />
                ))
              )}
            </tbody>
          </table>
        </div>
      )}

      <div className="flex items-center justify-end gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={page === 0 || batchesQ.isFetching}
          onClick={() => setPage((p) => Math.max(0, p - 1))}
        >
          上一页
        </Button>
        <Button
          variant="outline"
          size="sm"
          // 后端不给总数：装满一页才可能有下一页，最后一页必然短于 PAGE_SIZE。
          disabled={rows.length < PAGE_SIZE || batchesQ.isFetching}
          onClick={() => setPage((p) => p + 1)}
        >
          下一页
        </Button>
      </div>

      <p className="text-xs text-muted-foreground">
        已兑换的码不会出现在「作废 / 删除」的可操作行里：分已经进过用户账本，
        后端对这类请求一律返回冲突（409），留档才能对账。
      </p>

      <RedeemCodeBrowser batch={codesBatch} onClose={() => setCodesBatch(null)} />

      {voidTarget ? (
        <VoidBatchDialog
          row={voidTarget}
          onClose={() => setVoidTarget(null)}
          onVoided={() => {
            qc.invalidateQueries({ queryKey: ["admin-redeem-batches"] });
            qc.invalidateQueries({ queryKey: ["admin-redeem-codes"] });
          }}
        />
      ) : null}
    </div>
  );
}

function BatchRow({
  row,
  onOpenCodes,
  onVoidBatch,
}: {
  row: RedeemBatchProgress;
  onOpenCodes: () => void;
  onVoidBatch: () => void;
}) {
  const status = redeemBatchStatus(row);
  const expired = isBatchExpired(row.batch.expires_at);
  const percent = row.total > 0 ? Math.min(100, Math.round((row.redeemed / row.total) * 100)) : 0;

  return (
    <tr className={cn("border-t border-border", expired && status.voidable && "bg-amber-500/5")}>
      <td className="p-3">
        <div className="font-medium">{row.batch.name}</div>
        <div className="text-xs text-muted-foreground">
          {row.batch.note ? `${row.batch.note} · ` : ""}
          创建于 {formatDateTimeText(row.batch.created_at)}
        </div>
      </td>
      <td className="p-3 text-right tabular-nums">
        {formatCreditsRaw(row.batch.credits_per_code)}
        <div className="text-xs text-muted-foreground">
          共 {formatCreditsRaw(row.batch.credits_per_code * row.total)}
        </div>
      </td>
      <td className="p-3">
        <div className="tabular-nums">
          {row.redeemed}/{row.total} 已兑换
        </div>
        <div className="mt-1 h-1.5 w-32 rounded-full bg-secondary">
          <div
            className="h-full rounded-full bg-emerald-500"
            style={{ width: `${percent}%` }}
          />
        </div>
        <div className="mt-1 text-xs text-muted-foreground tabular-nums">
          未兑换 {row.active}
          {row.void > 0 ? ` · 已作废 ${row.void}` : ""}
        </div>
      </td>
      <td className="hidden p-3 lg:table-cell">
        <span className={cn(expired && "text-destructive")}>
          {formatDateText(row.batch.expires_at)}
        </span>
        {expired ? <div className="text-xs text-destructive">已过期</div> : null}
      </td>
      <td className="p-3">
        <Badge variant={status.tone} className="text-[10px]">
          {status.label}
        </Badge>
      </td>
      <td className="whitespace-nowrap p-3 text-right">
        <Button variant="ghost" size="sm" onClick={onOpenCodes}>
          单码运营
        </Button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button variant="ghost" size="icon" aria-label={`${row.batch.name} 的更多操作`}>
              <EllipsisVertical className="h-4 w-4" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="end">
            <DropdownMenuItem onSelect={onOpenCodes}>
              <KeyRound className="mr-2 h-4 w-4" />
              查看 / 处理这批的单码
            </DropdownMenuItem>
            <DropdownMenuSeparator />
            <DropdownMenuItem
              disabled={!status.voidable}
              className="text-destructive focus:text-destructive"
              onSelect={onVoidBatch}
            >
              <ShieldAlert className="mr-2 h-4 w-4" />
              作废剩余 {row.active} 张未兑换码
            </DropdownMenuItem>
          </DropdownMenuContent>
        </DropdownMenu>
      </td>
    </tr>
  );
}

/**
 * 整批作废的确认框。
 *
 * 与 `components/project-delete-dialog.tsx` 同一套做法：后果逐条列明、数字全部
 * 来自服务端计数，并且要逐字输入批次名才解锁按钮 —— 这一步不可撤销，后端没有
 * 反作废接口，「你确定吗」这种确认框对它是不够的。
 */
function VoidBatchDialog({
  row,
  onClose,
  onVoided,
}: {
  row: RedeemBatchProgress;
  onClose: () => void;
  onVoided: () => void;
}) {
  const [typed, setTyped] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const consequences = voidBatchConsequences(row);
  const confirmed = voidBatchConfirmed(consequences.confirmation, typed);

  const submit = async () => {
    if (!confirmed || pending) return;
    setPending(true);
    setError(null);
    try {
      const result = await api.adminVoidRedeemBatch(row.batch.id);
      // 后端对「没有剩余可作废」也回 200 voided=0，这里如实转达。
      if (result.voided === 0) {
        toast.warning("该批次已无可作废的兑换码，可能已被其他人处理");
      } else {
        toast.success(`已作废 ${result.voided} 张未兑换的码`);
      }
      onVoided();
      onClose();
    } catch (err) {
      setError(userErrorMessage(err));
    } finally {
      setPending(false);
    }
  };

  return (
    <Dialog
      open
      onOpenChange={(next) => {
        if (!next) onClose();
      }}
    >
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle className="flex items-center gap-2 text-destructive">
            <ShieldAlert className="h-4 w-4" />
            作废批次「{row.batch.name}」的未兑换码
          </DialogTitle>
          <DialogDescription>此操作不可撤销，请先看清下面每一条后果。</DialogDescription>
        </DialogHeader>

        <ul className="space-y-1.5 text-sm">
          {consequences.lines.map((line) => (
            <li key={line} className="flex gap-2">
              <span className="mt-[7px] h-1 w-1 shrink-0 rounded-full bg-current opacity-60" />
              <span>{line}</span>
            </li>
          ))}
        </ul>

        <div className="space-y-1.5">
          <Label htmlFor="void-batch-confirm" className="text-xs">
            请输入批次名称{" "}
            <span className="font-medium text-foreground">{row.batch.name}</span> 以确认
          </Label>
          <Input
            id="void-batch-confirm"
            value={typed}
            onChange={(e) => setTyped(e.target.value)}
            autoComplete="off"
            placeholder={row.batch.name}
            disabled={pending}
            onKeyDown={(e) => {
              if (e.key === "Enter") void submit();
            }}
          />
        </div>

        {error ? (
          <p className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/10 p-2.5 text-sm text-destructive">
            <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
            <span>作废失败：{error}</span>
          </p>
        ) : null}

        <DialogFooter>
          <Button variant="ghost" disabled={pending} onClick={onClose}>
            取消
          </Button>
          <Button
            variant="destructive"
            disabled={!confirmed || pending}
            onClick={() => void submit()}
          >
            {pending ? <Loader2 className="h-4 w-4 animate-spin" /> : null}
            确认作废 {row.active} 张
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
